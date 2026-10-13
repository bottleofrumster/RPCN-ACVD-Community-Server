"""Exact transfer retries must not become a second payment; other debts remain."""

from copy import deepcopy
from pathlib import Path

import pytest

from acvd_server.handlers import economy
from acvd_server.protocol import proto_varint
from acvd_server.server import GameDatagramProtocol
from acvd_server.teams import TeamStore
from acvd_server.telemetry import Telemetry
from acvd_server.udp import (
    ACK,
    DAT,
    DAT_ACK,
    TRADE_MONEY,
    GameMessage,
    GameSession,
    ReliablePacket,
    decode_reliable_packet,
    decrypt_server_datagram,
    encode_game_request,
    encode_reliable_packet,
    encrypt_client_datagram,
)


def setup_store(root):
    store = TeamStore(root)
    for name, number in (("Sender", 11), ("Recipient", 22), ("Other", 33)):
        store.remember_player(name, number)
        store.synchronize_balance(name, 1000, initial=0)
    return store


def actor(name="Sender", number=11, token=b"token123"):
    return GameSession(token, bytes(range(16)), name, number, established=True)


def request(index=1, counter=7, amount=100):
    return GameMessage(
        TRADE_MONEY,
        index.to_bytes(4, "big"),
        proto_varint(1, 22) + proto_varint(3, amount),
        counter,
    )


def invoke(store, session, message, replies, events):
    economy.trade_money(
        session,
        message,
        teams=store,
        telemetry=events,
        reply=replies.append,
        initial_balance=1000000,
    )


@pytest.mark.parametrize("amount", [100, 1000])
def test_replay_survives_store_reload_without_another_debit(tmp_path, amount):
    store = setup_store(tmp_path / "teams")
    session, message, replies = actor(), request(amount=amount), []
    events = Telemetry(tmp_path / "events.jsonl")
    invoke(store, session, message, replies, events)
    committed = deepcopy(store.state)
    reloaded = TeamStore(store.root)
    invoke(reloaded, session, message, replies, events)
    assert reloaded.state == committed
    assert reloaded.balance("Sender") == 1000 - amount
    assert reloaded.balance("Recipient") == 1000 + amount
    assert len(reloaded.state["money_transfers"]) == 1
    assert replies == [b"", b""]
    assert '"event":"game_money_trade_replayed"' in (tmp_path / "events.jsonl").read_text()


@pytest.mark.parametrize(
    "index,counter,token", [(2, 7, b"token123"), (1, 8, b"token123"), (1, 7, b"token456")]
)
def test_distinct_request_or_auth_context_allows_an_equal_payment(tmp_path, index, counter, token):
    store = setup_store(tmp_path / "teams")
    replies, events = [], Telemetry(tmp_path / "events.jsonl")
    invoke(store, actor(), request(), replies, events)
    invoke(store, actor(token=token), request(index, counter), replies, events)
    assert store.balance("Sender") == 800 and store.balance("Recipient") == 1200
    assert len(store.state["money_transfers"]) == 2 and replies == [b"", b""]


def test_authenticated_owner_is_part_of_request_identity(tmp_path):
    store = setup_store(tmp_path / "teams")
    replies, events = [], Telemetry(tmp_path / "events.jsonl")
    invoke(store, actor(), request(), replies, events)
    invoke(store, actor("Other", 33), request(), replies, events)
    assert store.balance("Sender") == store.balance("Other") == 900
    assert store.balance("Recipient") == 1200
    assert len(store.state["money_transfers"]) == 2


def test_store_rejects_conflicting_reuse_without_mutation(tmp_path):
    store = setup_store(tmp_path / "teams")
    assert store.transfer_money("Sender", 22, 100, request_digest="request-one")["applied"]
    before = deepcopy(store.state)
    assert store.transfer_money("Sender", 22, 200, request_digest="request-one") is None
    assert store.transfer_money("Sender", 33, 100, request_digest="request-one") is None
    assert store.state == before


def test_identical_encrypted_datagram_is_acknowledged_without_repayment(tmp_path):
    store = setup_store(tmp_path / "teams")
    session = actor()
    library = Path(__file__).resolve().parents[1] / "native/libacvd_cwc.so"
    packet = ReliablePacket(101, 0, DAT, payload=encode_game_request(request()))
    encrypted = encrypt_client_datagram(
        encode_reliable_packet(packet), session.game_key, session.auth_token, 0, library
    )

    class Transport:
        def __init__(self):
            self.packets = []

        def sendto(self, data, peer):
            self.packets.append(data)

    # Recreate the decoder/coordinators and reload persistent state between deliveries.
    # Authentication is pre-seeded; no real network or RPCN service is contacted.
    for delivery in range(2):
        current = TeamStore(store.root)
        protocol = GameDatagramProtocol(
            Telemetry(tmp_path / "events.jsonl"),
            {session.auth_token: actor()},
            library,
            tmp_path / "players",
            current,
        )
        transport = Transport()
        protocol.connection_made(transport)
        try:
            protocol.datagram_received(encrypted, ("127.0.0.1", 40001))
            opcodes = [
                decode_reliable_packet(
                    decrypt_server_datagram(data, session.game_key, library)
                ).opcode
                for data in transport.packets
            ]
            assert opcodes == [ACK, DAT_ACK]
            assert current.balance("Sender") == 900 and current.balance("Recipient") == 1100
            assert len(current.state["money_transfers"]) == 1
        finally:
            protocol.connection_lost(None)


@pytest.mark.parametrize("missing", [("Sender",), ("Recipient",), ("Sender", "Recipient")])
@pytest.mark.parametrize("digest", [None, "new-request"])
def test_transfer_cannot_bootstrap_a_missing_wallet(tmp_path, missing, digest):
    store = setup_store(tmp_path / "teams")
    for name in missing:
        del store.state["player_profiles"][name]["balance"]
    store._save()
    before = deepcopy(store.state)
    before_bytes = store.state_path.read_bytes()
    assert store.transfer_money("Sender", 22, 100, initial=1000000, request_digest=digest) is None
    assert store.state == before and store.state_path.read_bytes() == before_bytes


def test_zero_recipient_balance_is_known_not_missing(tmp_path):
    store = setup_store(tmp_path / "teams")
    store.synchronize_balance("Recipient", 0, initial=0)
    transfer = store.transfer_money("Sender", 22, 100)
    assert transfer is not None
    assert store.balance("Sender") == 900 and store.balance("Recipient") == 100


def test_transfer_rejects_recipient_wallet_overflow_without_debit(tmp_path):
    store = setup_store(tmp_path / "teams")
    store.synchronize_balance("Recipient", 999_999_950)
    before, disk = deepcopy(store.state), store.state_path.read_bytes()
    assert store.transfer_money("Sender", 22, 100, request_digest="over-cap") is None
    assert store.state == before and store.state_path.read_bytes() == disk


def test_transfer_into_debt_reduces_the_recipient_obligation(tmp_path):
    store = setup_store(tmp_path / "teams")
    store.synchronize_balance("Recipient", -500)
    transfer = store.transfer_money("Sender", 22, 100, request_digest="debt-credit")
    assert transfer["applied"] and store.balance("Sender") == 900
    assert store.balance("Recipient") == -400
    assert TeamStore(store.root).balance("Recipient") == -400


def test_transfer_commit_failure_keeps_both_wallets_and_retry_available(tmp_path, monkeypatch):
    store = setup_store(tmp_path / "teams")
    before, disk = deepcopy(store.state), store.state_path.read_bytes()
    temporary = store.state_path.with_suffix(".json.tmp")
    original = Path.replace

    def fail(path, *args, **kwargs):
        if path == temporary:
            raise OSError("Injected transfer commit failure")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", fail)
        with pytest.raises(OSError, match="transfer commit failure"):
            store.transfer_money("Sender", 22, 100, request_digest="atomic-transfer")
    assert store.state == before and store.state_path.read_bytes() == disk
    committed = store.transfer_money("Sender", 22, 100, request_digest="atomic-transfer")
    assert committed["applied"] and store.balance("Sender") == 900


def test_missing_wallet_handler_rejects_without_success_ack_or_data_write(tmp_path):
    store = setup_store(tmp_path / "teams")
    del store.state["player_profiles"]["Recipient"]["balance"]
    store._save()
    before = store.state_path.read_bytes()
    replies, events = [], Telemetry(tmp_path / "events.jsonl")
    invoke(store, actor(), request(), replies, events)
    assert not replies and store.state_path.read_bytes() == before
    assert '"recipient_balance_stored":false' in (tmp_path / "events.jsonl").read_text()


def test_committed_retry_does_not_recreate_a_later_missing_wallet(tmp_path):
    store = setup_store(tmp_path / "teams")
    replies, events = [], Telemetry(tmp_path / "events.jsonl")
    invoke(store, actor(), request(), replies, events)
    del store.state["player_profiles"]["Recipient"]["balance"]
    store._save()
    before = deepcopy(store.state)
    invoke(store, actor(), request(), replies, events)
    assert store.state == before and replies == [b"", b""]


@pytest.mark.parametrize("missing", [("Sender",), ("Recipient",), ("Sender", "Recipient")])
def test_missing_wallet_gets_explicit_failure_without_success_or_mutation(tmp_path, missing):
    store = setup_store(tmp_path / "teams")
    for name in missing:
        del store.state["player_profiles"][name]["balance"]
    store._save()
    before = store.state_path.read_bytes()
    replies, errors = [], []
    economy.trade_money(
        actor(),
        request(),
        teams=store,
        telemetry=Telemetry(tmp_path / "events.jsonl"),
        reply=replies.append,
        reply_error=lambda result, detail: errors.append((result, detail)),
        initial_balance=1000000,
    )
    assert replies == [] and errors == [(1, 28)]
    assert store.state_path.read_bytes() == before


def test_committed_retry_keeps_success_even_if_wallet_later_disappears(tmp_path):
    store = setup_store(tmp_path / "teams")
    replies, errors, events = [], [], Telemetry(tmp_path / "events.jsonl")
    invoke(store, actor(), request(), replies, events)
    del store.state["player_profiles"]["Recipient"]["balance"]
    store._save()
    before = store.state_path.read_bytes()
    economy.trade_money(
        actor(),
        request(),
        teams=store,
        telemetry=events,
        reply=replies.append,
        reply_error=lambda result, detail: errors.append((result, detail)),
        initial_balance=1000000,
    )
    assert replies == [b"", b""] and errors == []
    assert store.state_path.read_bytes() == before


@pytest.mark.parametrize("missing", [("Sender",), ("Recipient",), ("Sender", "Recipient")])
def test_encrypted_missing_wallet_failure_is_correlated_and_peer_scoped(tmp_path, missing):
    store = setup_store(tmp_path / "teams")
    for name in missing:
        del store.state["player_profiles"][name]["balance"]
    store._save()
    before = store.state_path.read_bytes()
    sender, recipient = actor(), actor("Recipient", 22, b"other123")
    sender.server_sequence = 500
    recipient.peer = ("127.0.0.1", 40002)
    library = Path(__file__).resolve().parents[1] / "native/libacvd_cwc.so"
    protocol = GameDatagramProtocol(
        Telemetry(tmp_path / "events.jsonl"),
        {s.auth_token: s for s in (sender, recipient)},
        library,
        tmp_path / "players",
        store,
    )

    class Transport:
        def __init__(self):
            self.packets = []

        def sendto(self, data, peer):
            self.packets.append((data, peer))

    transport = Transport()
    protocol.connection_made(transport)
    message = request()
    wire = encrypt_client_datagram(
        encode_reliable_packet(ReliablePacket(101, 0, DAT, payload=encode_game_request(message))),
        sender.game_key,
        sender.auth_token,
        0,
        library,
    )
    try:
        original_reply = None
        for attempt in range(2):
            transport.packets.clear()
            protocol.datagram_received(wire, ("127.0.0.1", 40001))
            if attempt:
                # A duplicate reliable packet is ACKed, not applied again.
                # The native client requests the retained reply with opcode3.
                assert len(transport.packets) == 1
                retry = encrypt_client_datagram(
                    encode_reliable_packet(ReliablePacket(0, 499, 0x03)),
                    sender.game_key, sender.auth_token, 0, library,
                )
                protocol.datagram_received(retry, ("127.0.0.1", 40001))
            decoded = [
                decode_reliable_packet(decrypt_server_datagram(data, sender.game_key, library))
                for data, _ in transport.packets
            ]
            assert [packet.opcode for packet in decoded] == [ACK, DAT_ACK]
            if original_reply is None:
                original_reply = decoded[1]
            else:
                assert decoded[1] == original_reply
            assert all(peer == ("127.0.0.1", 40001) for _, peer in transport.packets)
            body = decoded[1].payload[12:]
            assert body[:8] == bytes.fromhex("0000000c00000000")
            assert body[8:12] == message.message_index
            assert body[12:] == bytes.fromhex("000000010000001c0000000000000000")
            assert store.state_path.read_bytes() == before
            assert not store.state.get("money_transfers")
    finally:
        protocol.connection_lost(None)


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        proto_varint(1, 11) + proto_varint(3, 100),
        proto_varint(1, 99) + proto_varint(3, 100),
        proto_varint(1, 22) + proto_varint(3, 0),
        proto_varint(1, 22) + proto_varint(3, 1001),
    ],
)
def test_other_rejections_are_not_reclassified_as_missing_wallet(tmp_path, payload):
    store = setup_store(tmp_path / "teams")
    before = store.state_path.read_bytes()
    replies, errors = [], []
    economy.trade_money(
        actor(),
        GameMessage(TRADE_MONEY, bytes(4), payload, 7),
        teams=store,
        telemetry=Telemetry(tmp_path / "events.jsonl"),
        reply=replies.append,
        reply_error=lambda result, detail: errors.append((result, detail)),
        initial_balance=1000000,
    )
    assert not replies and not errors and store.state_path.read_bytes() == before


def test_rejected_request_can_succeed_after_a_real_baseline_arrives(tmp_path):
    store = setup_store(tmp_path / "teams")
    del store.state["player_profiles"]["Recipient"]["balance"]
    store._save()
    replies, errors, events = [], [], Telemetry(tmp_path / "events.jsonl")
    for delivery in range(2):
        if delivery:
            store.synchronize_balance("Recipient", 1000, initial=0)
        economy.trade_money(
            actor(),
            request(),
            teams=store,
            telemetry=events,
            reply=replies.append,
            reply_error=lambda result, detail: errors.append((result, detail)),
            initial_balance=1000000,
        )
    assert errors == [(1, 28)] and replies == [b""]
    assert store.balance("Sender") == 900 and store.balance("Recipient") == 1100
    assert len(store.state["money_transfers"]) == 1
