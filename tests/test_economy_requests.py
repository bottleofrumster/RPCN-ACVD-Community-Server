"""Economy handler contracts independent of sockets and the monolithic dispatcher."""

from copy import deepcopy

import pytest

from acvd_server.handlers import economy, profiles
from acvd_server.protocol import decode_proto, first_field, proto_bytes, proto_varint
from acvd_server.teams import TeamStore
from acvd_server.udp import (
    GET_MY_USER_INFO,
    REGISTER_BATTLE_RESULT,
    SYNC_MONEY,
    TRADE_MONEY,
    GameMessage,
    GameSession,
)


class Events:
    def __init__(self):
        self.rows = []

    def emit(self, name, **fields):
        self.rows.append((name, fields))


@pytest.fixture
def setup(tmp_path):
    store = TeamStore(tmp_path / "teams")
    store.remember_player("Sender", 11)
    store.remember_player("Recipient", 22)
    session = GameSession(b"token123", bytes(16), "Sender", 11)
    replies, events = [], Events()
    return store, session, replies, events


def message(opcode, payload):
    return GameMessage(opcode, b"\x00\x00\x00\x01", payload, 1)


def cash_payload(offline, online, timestamp=0):
    return proto_varint(2, timestamp) + proto_varint(3, offline) + proto_varint(4, online)


def test_first_balance_uses_offline_cash_and_retry_keeps_server_spending(setup):
    store, session, replies, events = setup
    payload = bytes.fromhex("100018e6c1d6b40320a08d06")
    assert payload == cash_payload(915775718, 100000)
    economy.sync_money(
        session,
        message(SYNC_MONEY, payload),
        teams=store,
        telemetry=events,
        reply=replies.append,
    )
    assert store.balance("Sender") == 915775718
    assert first_field(decode_proto(replies[-1]), 1).value == 915775718
    enrollment = deepcopy(store.player_profile("Sender")["cash_enrollment"])
    assert enrollment["offline_balance"] == 915775718
    assert enrollment["previous_online_balance"] == 100000
    # Simulate an already committed server purchase, then a stale reconnect.
    store.synchronize_balance("Sender", 915755718)
    store = TeamStore(store.root)
    before = store.state_path.read_bytes()
    for payload in (payload, cash_payload(999999999, 100000, 100)):
        economy.sync_money(
            session,
            message(SYNC_MONEY, payload),
            teams=store,
            telemetry=events,
            reply=replies.append,
        )
        assert store.balance("Sender") == 915755718
        assert first_field(decode_proto(replies[-1]), 1).value == 915755718
    assert store.state_path.read_bytes() == before
    assert store.player_profile("Sender")["cash_enrollment"] == enrollment
    assert events.rows[0][1]["wallet_enrolled"] is True
    assert events.rows[-1][1]["wallet_enrolled"] is False


def test_same_login_sync_replaces_only_its_new_save_provisional(setup):
    store, session, replies, events = setup
    assert store.provision_new_balance("Sender") == (100_000, True)
    session.new_wallet_provisional = True
    economy.sync_money(
        session,
        message(SYNC_MONEY, cash_payload(915_775_718, 100_000, 77)),
        teams=store,
        telemetry=events,
        reply=replies.append,
    )
    assert store.balance("Sender") == 915_775_718
    assert session.new_wallet_provisional is False
    assert store.player_profile("Sender")["cash_enrollment"] == {
        "policy": "offline-first-v1",
        "offline_balance": 915_775_718,
        "previous_online_balance": 100_000,
        "client_time": 77,
        "replaced_new_save_provisional": 100_000,
    }


def test_later_login_cannot_replace_new_save_wallet_with_stale_sync(setup):
    store, session, replies, events = setup
    store.provision_new_balance("Sender")
    economy.sync_money(
        session,
        message(SYNC_MONEY, cash_payload(915_775_718, 100_000, 77)),
        teams=store,
        telemetry=events,
        reply=replies.append,
    )
    assert store.balance("Sender") == 100_000
    assert store.player_profile("Sender")["cash_enrollment"] == {
        "policy": "native-new-save-v1",
        "starting_balance": 100_000,
    }


@pytest.mark.parametrize("offline,online", [(0, 100000), (50, 100000), (999999999, 0)])
def test_enrollment_is_neither_sum_nor_max(setup, offline, online):
    store, session, replies, events = setup
    economy.sync_money(
        session,
        message(SYNC_MONEY, cash_payload(offline, online)),
        teams=store,
        telemetry=events,
        reply=replies.append,
    )
    assert store.balance("Sender") == offline
    assert first_field(decode_proto(replies[-1]), 1).value == offline


@pytest.mark.parametrize("existing", [0, 100000, 200000])
def test_existing_wallet_is_not_reenrolled_or_overwritten(setup, existing):
    store, session, replies, events = setup
    store.synchronize_balance("Sender", existing)
    before = store.state_path.read_bytes()
    economy.sync_money(
        session,
        message(SYNC_MONEY, cash_payload(915775718, 100000)),
        teams=store,
        telemetry=events,
        reply=replies.append,
    )
    assert store.balance("Sender") == existing
    assert store.state_path.read_bytes() == before
    assert "cash_enrollment" not in store.player_profile("Sender")


def test_first_enrollment_commits_before_reply_and_keeps_other_profiles(setup):
    store, session, replies, events = setup
    before = deepcopy(store.state)

    def reply(payload):
        reloaded = TeamStore(store.root)
        assert reloaded.balance("Sender") == 915775718
        assert reloaded.player_profile("Sender")["cash_enrollment"]["policy"] == "offline-first-v1"
        replies.append(payload)

    economy.sync_money(
        session,
        message(SYNC_MONEY, cash_payload(915775718, 100000)),
        teams=store,
        telemetry=events,
        reply=reply,
    )
    assert replies
    for key in before:
        if key != "player_profiles":
            assert store.state[key] == before[key]
    assert store.player_profile("Recipient") == before["player_profiles"]["Recipient"]


def test_enrollment_write_failure_keeps_memory_and_disk(setup, monkeypatch):
    store, session, replies, events = setup
    before = deepcopy(store.state)
    disk = store.state_path.read_bytes()

    def fail():
        raise OSError("test disk failure")

    monkeypatch.setattr(store, "_save", fail)
    with pytest.raises(OSError, match="test disk failure"):
        economy.sync_money(
            session,
            message(SYNC_MONEY, cash_payload(915775718, 100000)),
            teams=store,
            telemetry=events,
            reply=replies.append,
        )
    assert store.state == before and store.state_path.read_bytes() == disk and not replies


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        proto_bytes(4, b"not-an-integer"),
        proto_varint(4, 100000),
        cash_payload(1, 2) + proto_varint(3, 1000),
        cash_payload(1, 2) + proto_varint(5, 1000),
        proto_varint(2, 0) + proto_bytes(3, b"1") + proto_varint(4, 2),
        cash_payload(1000000000, 1),
        cash_payload(1, 1000000000),
        cash_payload((1 << 64) - 1_000_000_000, 1),
        b"\x80",
    ],
)
def test_bad_balance_rejected_without_reply_or_state_change(setup, payload):
    store, session, replies, events = setup
    before = deepcopy(store.state)
    economy.sync_money(
        session, message(SYNC_MONEY, payload), teams=store, telemetry=events, reply=replies.append
    )
    assert store.state == before and not replies
    assert events.rows[-1][0] == "game_message_rejected"


def test_transfer_commits_before_acknowledgement(setup):
    store, session, replies, events = setup
    store.synchronize_balance("Sender", 1000)
    store.synchronize_balance("Recipient", 50)

    def reply(payload):
        assert store.balance("Sender") == 750 and store.balance("Recipient") == 300
        replies.append(payload)

    economy.trade_money(
        session,
        message(TRADE_MONEY, proto_varint(1, 22) + proto_varint(3, 250)),
        teams=store,
        telemetry=events,
        reply=reply,
        initial_balance=1000000,
    )
    assert replies == [b""]
    assert len(store.state["money_transfers"]) == 1


@pytest.mark.parametrize("recipient,amount", [(11, 50), (99, 50), (22, 0), (22, 1001)])
def test_invalid_transfer_does_not_change_balances(setup, recipient, amount):
    store, session, replies, events = setup
    store.synchronize_balance("Sender", 1000)
    before = deepcopy(store.state)
    economy.trade_money(
        session,
        message(TRADE_MONEY, proto_varint(1, recipient) + proto_varint(3, amount)),
        teams=store,
        telemetry=events,
        reply=replies.append,
        initial_balance=1000000,
    )
    assert store.state == before and not replies
    assert events.rows[-1][0] == "game_message_rejected"


def result(player=11, repair=500, ammo=100):
    player_result = b"".join(
        proto_varint(n, v)
        for n, v in ((1, player), (2, 0), (3, 1), (4, repair), (5, ammo), (6, 0), (7, 0))
    )
    return message(REGISTER_BATTLE_RESULT, proto_varint(2, 801) + proto_bytes(3, player_result))


def test_settlement_retry_and_store_reload_do_not_double_pay(setup):
    store, session, replies, events = setup
    store.synchronize_balance("Sender", 1000)
    request = result()
    economy.register_battle_result(
        session,
        request,
        teams=store,
        telemetry=events,
        reply=replies.append,
        initial_balance=1000000,
    )
    # Mission 801 base pay is 170000. Both submitted amounts are expenses.
    assert store.balance("Sender") == 170400
    reloaded = TeamStore(store.root)
    economy.register_battle_result(
        session,
        request,
        teams=reloaded,
        telemetry=events,
        reply=replies.append,
        initial_balance=1000000,
    )
    assert reloaded.balance("Sender") == 170400
    assert replies[0] == replies[1]
    assert events.rows[-1][1]["money_settlement"]["applied"] is False


def test_result_for_other_identity_does_not_settle(setup):
    store, session, replies, events = setup
    before = deepcopy(store.state)
    economy.register_battle_result(
        session,
        result(player=22),
        teams=store,
        telemetry=events,
        reply=replies.append,
        initial_balance=1000000,
    )
    assert store.state == before and replies == [b""]
    assert events.rows[-1][0] == "game_story_reward_rejected"


def test_expenses_above_base_pay_create_retail_debt(setup):
    store, session, replies, events = setup
    store.synchronize_balance("Sender", 50)
    economy.register_battle_result(
        session,
        result(repair=170000, ammo=2200),
        teams=store,
        telemetry=events,
        reply=replies.append,
        initial_balance=1000000,
    )
    assert store.balance("Sender") == -2150
    assert len(replies) == 1


def test_negative_native_sync_cash_is_sign_extended_and_retained(setup):
    store, session, replies, events = setup
    economy.sync_money(
        session,
        message(SYNC_MONEY, cash_payload(-50_000, -10_000)),
        teams=store,
        telemetry=events,
        reply=replies.append,
    )
    assert store.balance("Sender") == -50_000
    assert store.player_profile("Sender")["cash_enrollment"]["offline_balance"] == -50_000
    assert first_field(decode_proto(replies[-1]), 1).value == (1 << 64) - 50_000
    assert TeamStore(store.root).balance("Sender") == -50_000


def test_negative_wallet_readback_uses_native_signed_varint(setup):
    store, session, replies, events = setup
    store.synchronize_balance("Sender", -50_000)
    profiles.get_my_user_info(
        session,
        message(GET_MY_USER_INFO, b""),
        teams=store,
        telemetry=events,
        reply=replies.append,
    )
    assert first_field(decode_proto(replies[-1]), 11).value == (1 << 64) - 50_000
