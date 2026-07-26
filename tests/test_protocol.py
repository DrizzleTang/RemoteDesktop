import pytest

from common import protocol


def test_encode_decode_control_roundtrip():
    plaintext = protocol.encode_control({"t": protocol.T_PING, "ts": 123, "n": 1})
    kind, msg = protocol.decode_plaintext(plaintext)
    assert kind == protocol.MSG_CONTROL
    assert msg == {"t": protocol.T_PING, "ts": 123, "n": 1}


def test_encode_decode_video_frame_roundtrip():
    image_bytes = b"\xff\xd8\xff\xd9fake-jpeg-bytes"
    plaintext = protocol.encode_video_frame(
        seq=42, ts_ms=99999, width=1280, height=720, quality=60,
        fmt=protocol.FMT_JPEG, keyframe=True, image_bytes=image_bytes,
    )
    kind, frame = protocol.decode_plaintext(plaintext)
    assert kind == protocol.MSG_VIDEO_FRAME
    assert isinstance(frame, protocol.VideoFrame)
    assert frame.seq == 42
    assert frame.width == 1280 and frame.height == 720
    assert frame.quality == 60
    assert frame.keyframe is True
    assert frame.image_bytes == image_bytes


def test_control_message_missing_t_field_rejected():
    body = b'{"foo": 1}'
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_plaintext(bytes([protocol.MSG_CONTROL]) + body)


def test_unknown_msg_kind_rejected():
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_plaintext(bytes([0x99]) + b"whatever")


def test_empty_plaintext_rejected():
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_plaintext(b"")


def test_oversized_control_message_rejected_on_encode():
    huge = {"t": protocol.T_CLIPBOARD, "text": "x" * (protocol.MAX_CONTROL_MESSAGE_BYTES + 10)}
    with pytest.raises(protocol.ProtocolError):
        protocol.encode_control(huge)


def test_video_frame_header_truncated_rejected():
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_plaintext(bytes([protocol.MSG_VIDEO_FRAME]) + b"\x00\x01")


def test_video_frame_bad_magic_rejected():
    bad_header = protocol.FRAME_HEADER_STRUCT.pack(0x00, 1, 1, 10, 10, 50, 0, 0)
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_plaintext(bytes([protocol.MSG_VIDEO_FRAME]) + bad_header)


def test_kex_init_roundtrip():
    text = protocol.encode_kex_init("saltsalt==", 200_000)
    msg = protocol.parse_kex_text(text)
    assert msg["t"] == protocol.KEX_INIT
    assert msg["salt"] == "saltsalt=="
    assert msg["iterations"] == 200_000


def test_parse_kex_text_rejects_garbage():
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_kex_text("not json")


def test_parse_kex_text_rejects_oversized():
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_kex_text("x" * 9000)
