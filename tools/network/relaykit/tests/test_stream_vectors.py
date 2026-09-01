"""tls-stream/1 golden vectors — cross-implementation byte stability.

A drift in the frame type byte, JSON field ordering, or ctrl encodings
fails here before it can strand a deployed peer.
"""

from __future__ import annotations

import json
from pathlib import Path

from tools.network.relaykit.frames import decode_frame
from tools.network.relaykit.stream_wire import parse_ctrl, parse_stream_open

from .generate_stream_vectors import build_fixture

FIXTURE = Path(__file__).parent / "fixtures" / "stream_v1.json"


def test_fixture_matches_regeneration_exactly():
    assert json.loads(FIXTURE.read_text()) == build_fixture()


def test_fixture_frames_decode_and_parse():
    fixture = json.loads(FIXTURE.read_text())
    for name, frame_hex in fixture["ctrl_frames_hex"].items():
        frame = decode_frame(bytes.fromhex(frame_hex))
        assert frame.type == fixture["frame_type_stream_ctrl"]
        assert frame.channel_id.hex() == fixture["channel_id_hex"]
        parsed = parse_ctrl(frame.payload)
        assert parsed == json.loads(
            bytes.fromhex(fixture["ctrl_payloads_hex"][name])
        )
    host, reservation, credit = parse_stream_open(fixture["open_payload"])
    assert host == fixture["open_payload"]["host"]
    assert credit == fixture["open_payload"]["credit"]
