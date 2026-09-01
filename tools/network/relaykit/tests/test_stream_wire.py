"""tls-stream/1 wire layer (auto-9z1xh): frame type, ctrl codec, OPEN
payload, SNI peek, and credit accounting — the pure pieces both the relay
ingress and the connector adapter share via ``stream_wire.py``.
"""

from __future__ import annotations

import json
import struct

import pytest

from tools.network.relaykit import frames as frames_mod
from tools.network.relaykit import stream_wire as sw


def test_stream_ctrl_frame_type_is_registered():
    assert frames_mod.FRAME_STREAM_CTRL == 0x05
    encoded = frames_mod.encode_frame(
        frames_mod.FRAME_STREAM_CTRL, b"c" * 16, b"{}"
    )
    frame = frames_mod.decode_frame(encoded)
    assert frame.type == frames_mod.FRAME_STREAM_CTRL


def test_constants_match_the_frozen_seam():
    assert sw.STREAM_INITIAL_CREDIT == 256 * 1024
    assert sw.STREAM_MAX_BUFFER == 512 * 1024
    assert sw.STREAM_MAX_DATA == 64 * 1024
    assert sw.STREAM_MAX_PER_TUNNEL == 128
    assert sw.CAP_TLS_STREAM == "tls-stream/1"
    assert sw.RESET_ORDERLY == 1
    assert sw.RESET_TIMEOUT == 2
    assert sw.RESET_OVERFLOW == 3
    assert sw.RESET_BYTE_BUDGET == 4
    assert sw.RESET_ROUTE_RELEASED == 5
    assert sw.RESET_TUNNEL_LOSS == 6
    assert sw.RESET_PROTOCOL == 7


def test_open_payload_round_trip():
    payload = sw.build_stream_open(
        host="docs.worker-aa.serve.auto.network", reservation="r" * 8,
        credit=sw.STREAM_INITIAL_CREDIT,
    )
    meta = json.loads(payload)
    assert meta == {
        "kind": "tls-stream", "v": 1,
        "host": "docs.worker-aa.serve.auto.network",
        "reservation": "r" * 8,
        "credit": sw.STREAM_INITIAL_CREDIT,
    }


@pytest.mark.parametrize("build,expect", [
    (lambda: sw.build_ctrl_open_ok(credit=262144),
     {"op": "open-ok", "v": 1, "credit": 262144}),
    (lambda: sw.build_ctrl_credit(65536), {"op": "credit", "add": 65536}),
    (lambda: sw.build_ctrl_eof(), {"op": "eof"}),
    (lambda: sw.build_ctrl_reset(sw.RESET_ROUTE_RELEASED),
     {"op": "reset", "code": 5}),
])
def test_ctrl_payloads_are_exact(build, expect):
    assert json.loads(build()) == expect


@pytest.mark.parametrize("raw", [
    b"not json",
    b"[]",
    b'{"op":"nope"}',
    b'{"op":"credit"}',
    b'{"op":"credit","add":0}',
    b'{"op":"credit","add":-5}',
    b'{"op":"credit","add":"x"}',
    b'{"op":"reset"}',
    b'{"op":"reset","code":0}',
    b'{"op":"reset","code":99}',
    b'{"op":"open-ok","v":1}',
    b'{"op":"open-ok","v":2,"credit":1}',
    b'{"op":"eof","extra":1}',
])
def test_malformed_ctrl_payloads_are_refused(raw):
    with pytest.raises(sw.StreamProtocolError):
        sw.parse_ctrl(raw)


def test_ctrl_parse_round_trips():
    assert sw.parse_ctrl(sw.build_ctrl_eof()) == {"op": "eof"}
    parsed = sw.parse_ctrl(sw.build_ctrl_open_ok(credit=1))
    assert parsed["op"] == "open-ok" and parsed["credit"] == 1


# -- SNI peek ---------------------------------------------------------------


def _client_hello(server_name: str | None) -> bytes:
    """Craft a minimal but structurally valid TLS 1.2+ ClientHello."""
    body = b"\x03\x03" + b"\x00" * 32          # client_version + random
    body += b"\x00"                            # session id
    body += struct.pack(">H", 2) + b"\x13\x01"  # one cipher suite
    body += b"\x01\x00"                        # compression: null
    exts = b""
    if server_name is not None:
        name = server_name.encode("ascii")
        entry = b"\x00" + struct.pack(">H", len(name)) + name
        lst = struct.pack(">H", len(entry)) + entry
        exts += struct.pack(">HH", 0x0000, len(lst)) + lst
    exts += struct.pack(">HH", 0x002B, 2) + b"\x02\x03"  # random other ext
    body += struct.pack(">H", len(exts)) + exts
    handshake = b"\x01" + struct.pack(">I", len(body))[1:] + body
    record = b"\x16\x03\x01" + struct.pack(">H", len(handshake)) + handshake
    return record


def test_sni_is_extracted_from_a_client_hello():
    raw = _client_hello("docs.worker-aa.serve.auto.network")
    assert sw.extract_sni(raw) == "docs.worker-aa.serve.auto.network"


def test_sni_peek_reports_incomplete_and_absent():
    raw = _client_hello("x.example")
    with pytest.raises(sw.NeedMoreData):
        sw.extract_sni(raw[:20])              # truncated record
    assert sw.extract_sni(_client_hello(None)) is None


@pytest.mark.parametrize("raw", [
    b"GET / HTTP/1.1\r\n\r\n" + b"\x00" * 16,   # not TLS
    b"\x17\x03\x03\x00\x05hello",               # not a handshake record
    b"\x16\x03\x01\x00\x04\x02\x00\x00\x00",    # not a ClientHello
])
def test_non_client_hello_bytes_are_rejected(raw):
    with pytest.raises(sw.StreamProtocolError):
        sw.extract_sni(raw)


# -- credit accounting ------------------------------------------------------


def test_credit_window_bounds_sender_and_batches_replenish():
    window = sw.CreditWindow(sw.STREAM_INITIAL_CREDIT)
    assert window.sendable == sw.STREAM_INITIAL_CREDIT
    window.consume(sw.STREAM_MAX_DATA)
    assert window.sendable == sw.STREAM_INITIAL_CREDIT - sw.STREAM_MAX_DATA
    window.grant(sw.STREAM_MAX_DATA)
    assert window.sendable == sw.STREAM_INITIAL_CREDIT
    with pytest.raises(sw.StreamProtocolError):
        window.consume(window.sendable + 1)   # credit violation

    receiver = sw.ReplenishTracker(batch=64 * 1024)
    assert receiver.consumed(10) is None       # below batch: no ctrl yet
    assert receiver.consumed(64 * 1024) == 64 * 1024 + 10
    assert receiver.consumed(1) is None
