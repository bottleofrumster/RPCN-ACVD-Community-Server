from acvd_server.protocol import (
    Message,
    Packet,
    decode_message,
    decode_packet,
    decode_proto,
    encode_message,
    encode_packet,
    proto_bytes,
    proto_varint,
)


def test_packet_and_request_message_round_trip() -> None:
    message = Message(msg_type=5, msg_index=0x12345678, payload=b"request")
    encoded_message = encode_message(message)
    frame = encode_packet(Packet(send_counter=7, payload=encoded_message))
    body_length = int.from_bytes(frame[:2], "big")
    assert body_length == len(frame) - 2
    packet = decode_packet(frame[2:])
    decoded = decode_message(packet.payload)
    assert packet.send_counter == 7
    assert decoded == message


def test_response_message_has_response_header() -> None:
    message = Message(msg_type=0, msg_index=9, payload=b"response")
    decoded = decode_message(encode_message(message))
    assert decoded.msg_index == 9
    assert decoded.response_header == (0, 1, 0, 0)
    assert decoded.payload == b"response"


def test_minimal_protobuf_codec() -> None:
    payload = proto_bytes(1, "pilot") + proto_varint(3, 101)
    fields = decode_proto(payload)
    assert [(field.number, field.wire_type, field.value) for field in fields] == [
        (1, 2, b"pilot"),
        (3, 0, 101),
    ]
