"""Tunnel mux framing (Q1 decision)."""

from __future__ import annotations

import pytest

from tools.network.relaykit.frames import (
    CHANNEL_ID_LEN,
    FRAME_CLOSE,
    FRAME_DATA,
    FRAME_OPEN,
    FrameError,
    decode_frame,
    encode_frame,
    new_channel_id,
)


def test_roundtrip_all_types():
    channel_id = new_channel_id()
    for frame_type in (FRAME_OPEN, FRAME_DATA, FRAME_CLOSE):
        payload = b"payload-bytes" if frame_type != FRAME_CLOSE else b""
        frame = decode_frame(encode_frame(frame_type, channel_id, payload))
        assert (frame.type, frame.channel_id, frame.payload) == (frame_type, channel_id, payload)


def test_channel_ids_are_random_and_sized():
    ids = {new_channel_id() for _ in range(64)}
    assert len(ids) == 64
    assert all(len(i) == CHANNEL_ID_LEN for i in ids)


def test_empty_payload_roundtrip():
    frame = decode_frame(encode_frame(FRAME_DATA, b"\x00" * 16))
    assert frame.payload == b""


@pytest.mark.parametrize("raw", [b"", b"\x02", b"\x02" + b"x" * 15])
def test_short_messages_rejected(raw):
    with pytest.raises(FrameError):
        decode_frame(raw)


def test_unknown_type_rejected():
    with pytest.raises(FrameError):
        decode_frame(b"\x7f" + b"x" * 16)
    with pytest.raises(FrameError):
        encode_frame(0x7F, b"x" * 16)


def test_bad_channel_id_length_rejected():
    with pytest.raises(FrameError):
        encode_frame(FRAME_DATA, b"short")
