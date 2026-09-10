"""fleet-directed-stream/1 wire pieces: strict parsing, exact credit
accounting, and message framing that credits only consumed frames."""

from __future__ import annotations

import json

import pytest

from tools.network.relaykit import fleet_stream_wire as fw
from tools.network.relaykit.stream_wire import (
    STREAM_MAX_DATA,
    StreamProtocolError,
    parse_ctrl,
)

PERSONA = "ab" * 32
MACHINE = "11" * 32
OP = "cd" * 16
NONCE = "ef" * 16
PAIR = "01" * 16


# -- open args ------------------------------------------------------------


def test_fleet_open_args_are_validated_and_the_hint_is_optional():
    parsed = fw.parse_fleet_open_args(
        {"dst_persona_pub": PERSONA, "dst_machine": MACHINE, "operation_id": OP})
    assert parsed["claimed_machine_pub"] is None
    parsed = fw.parse_fleet_open_args({
        "dst_persona_pub": PERSONA, "dst_machine": MACHINE, "operation_id": OP,
        "claimed_machine_pub": "22" * 32,
    })
    assert parsed["claimed_machine_pub"] == "22" * 32


@pytest.mark.parametrize("bad", [
    None,
    {},
    {"dst_persona_pub": PERSONA, "dst_machine": MACHINE},
    {"dst_persona_pub": PERSONA, "dst_machine": "", "operation_id": OP},
    {"dst_persona_pub": PERSONA, "dst_machine": MACHINE, "operation_id": "x"},
    {"dst_persona_pub": PERSONA, "dst_machine": MACHINE, "operation_id": OP, "extra": 1},
    {"dst_persona_pub": PERSONA, "dst_machine": MACHINE, "operation_id": OP,
     "claimed_machine_pub": "zz"},
])
def test_malformed_fleet_open_args_are_refused(bad):
    with pytest.raises(StreamProtocolError):
        fw.parse_fleet_open_args(bad)


# -- OPEN payload -------------------------------------------------------------


def test_fleet_open_round_trips_and_carries_no_credit():
    raw = fw.build_fleet_open(
        pair_id=PAIR, leg_nonce=NONCE, role=fw.ROLE_DESTINATION, operation_id=OP,
        peer_persona_pub=PERSONA, peer_machine=MACHINE, claimed_machine_pub="22" * 32,
    )
    meta = json.loads(raw)
    assert "credit" not in meta and "window" not in meta
    parsed = fw.parse_fleet_open(meta)
    assert parsed == {
        "pair_id": PAIR, "leg_nonce": NONCE, "role": fw.ROLE_DESTINATION,
        "operation_id": OP, "peer_persona_pub": PERSONA, "peer_machine": MACHINE,
        "claimed_machine_pub": "22" * 32,
    }


def test_fleet_open_rejects_unknown_roles_and_shapes():
    with pytest.raises(StreamProtocolError):
        fw.build_fleet_open(
            pair_id=PAIR, leg_nonce=NONCE, role="relay", operation_id=OP,
            peer_persona_pub=PERSONA, peer_machine=MACHINE, claimed_machine_pub=None)
    good = json.loads(fw.build_fleet_open(
        pair_id=PAIR, leg_nonce=NONCE, role=fw.ROLE_SOURCE, operation_id=OP,
        peer_persona_pub=PERSONA, peer_machine=MACHINE, claimed_machine_pub=None))
    for mutate in (
        lambda m: m.pop("peer"),
        lambda m: m.update(kind="tls-stream"),
        lambda m: m.update(v=2),
        lambda m: m.update(role="viewer"),
        lambda m: m["peer"].update(machine=""),
        lambda m: m.update(leg_nonce="short"),
    ):
        meta = json.loads(json.dumps(good))
        mutate(meta)
        with pytest.raises(StreamProtocolError):
            fw.parse_fleet_open(meta)


# -- controls -----------------------------------------------------------------


def test_fleet_controls_round_trip_with_byte_and_slot_windows():
    ok = fw.parse_fleet_ctrl(fw.build_fleet_open_ok(nonce=NONCE, bytes_=4096, slots=8))
    assert ok == {"op": "fleet-open-ok", "v": 1, "nonce": NONCE,
                  "window": {"bytes": 4096, "slots": 8}}
    ready = fw.parse_fleet_ctrl(fw.build_fleet_ready(
        pair_id=PAIR, source_nonce=NONCE, destination_nonce="00" * 16,
        bytes_=1024, slots=2))
    assert ready["window"] == {"bytes": 1024, "slots": 2}
    credit = fw.parse_fleet_ctrl(fw.build_fleet_credit(bytes_=300, slots=3))
    assert credit == {"op": "fleet-credit", "bytes": 300, "slots": 3}
    assert fw.parse_fleet_ctrl(b'{"op": "eof"}') == {"op": "eof"}
    assert fw.parse_fleet_ctrl(b'{"op": "reset", "code": 6}') == {"op": "reset", "code": 6}


@pytest.mark.parametrize("raw", [
    b"not json",
    b"[]",
    b'{"op": "credit", "add": 5}',                     # the PUBLIC credit op
    b'{"op": "fleet-credit", "bytes": 0, "slots": 1}',
    b'{"op": "fleet-credit", "bytes": 2, "slots": 3}',  # fewer bytes than frames
    b'{"op": "fleet-credit", "bytes": 2, "slots": 0}',
    b'{"op": "fleet-credit", "bytes": 2.0, "slots": 1}',
    b'{"op": "fleet-open-ok", "v": 1, "nonce": "%s", "window": {"bytes": 0, "slots": 1}}' % NONCE.encode(),
    b'{"op": "fleet-open-ok", "v": 1, "nonce": "%s", "window": {"bytes": 10, "slots": 100000}}' % NONCE.encode(),
    b'{"op": "fleet-open-ok", "v": 2, "nonce": "%s", "window": {"bytes": 10, "slots": 1}}' % NONCE.encode(),
    b'{"op": "fleet-ready", "pair_id": "x", "source_nonce": "%s", "destination_nonce": "%s", "window": {"bytes": 1, "slots": 1}}' % (NONCE.encode(), NONCE.encode()),
    b'{"op": "reset", "code": 99}',
    b'{"op": "eof", "extra": 1}',
])
def test_malformed_fleet_controls_are_refused(raw):
    with pytest.raises(StreamProtocolError):
        fw.parse_fleet_ctrl(raw)


def test_public_tls_stream_parser_still_refuses_fleet_ops():
    """The public tls-stream/1 vectors are untouched: a fleet op on a public
    stream is a protocol error there, exactly as before."""
    with pytest.raises(StreamProtocolError):
        parse_ctrl(fw.build_fleet_credit(bytes_=1, slots=1))
    with pytest.raises(StreamProtocolError):
        parse_ctrl(fw.build_fleet_open_ok(nonce=NONCE, bytes_=1, slots=1))


# -- windows and receipts -----------------------------------------------------


def test_fleet_window_needs_both_bytes_and_a_slot():
    window = fw.FleetWindow(10, 1)
    assert window.can_send(10)
    window.consume(10)
    assert not window.can_send(1)
    window.grant(5, 1)
    assert window.can_send(5) and not window.can_send(6)
    window.consume(5)
    window.grant(100, 0)                 # bytes without a slot: still blocked
    assert not window.can_send(1)
    with pytest.raises(StreamProtocolError):
        window.consume(1)
    assert not fw.FleetWindow(STREAM_MAX_DATA + 1, 1).can_send(STREAM_MAX_DATA + 1)
    assert not fw.FleetWindow(1, 1).can_send(0)


def test_receipts_must_be_credited_as_an_exact_fifo_prefix():
    ledger = fw.ReceiptLedger()
    for n in (1, 63, 65_535):
        ledger.forwarded(n)
    assert ledger.outstanding_bytes == 65_599 and len(ledger) == 3
    # partial byte prefix / wrong count / undershoot / overshoot / duplicate
    for bytes_, slots in ((1, 2), (64, 1), (63, 1), (65, 2), (65_600, 3), (1, 4)):
        with pytest.raises(StreamProtocolError):
            ledger.credited(bytes_, slots)
    assert ledger.outstanding_bytes == 65_599 and len(ledger) == 3
    ledger.credited(64, 2)               # 1 + 63, exactly two frames
    assert ledger.outstanding_bytes == 65_535 and len(ledger) == 1
    ledger.credited(65_535, 1)
    assert ledger.outstanding_bytes == 0 and len(ledger) == 0
    with pytest.raises(StreamProtocolError):
        ledger.credited(1, 1)            # nothing outstanding: no double refund


# -- message framing ----------------------------------------------------------


def test_messages_reassemble_across_frames_and_credit_only_consumed_frames():
    asm = fw.MessageAssembler(max_message=1024)
    a = fw.encode_message(b"a" * 100)
    b = fw.encode_message(b"b" * 50)
    stream = a + b
    # Three frames, boundaries deliberately not on message boundaries.
    frames = [stream[:60], stream[60:110], stream[110:]]
    for frame in frames:
        asm.feed(frame)
    assert asm.retained_bytes == len(stream)

    message, consumed_frames, consumed_bytes = asm.next_message()
    assert message == b"a" * 100
    # Message a spans frames 1 and 2 and ends INSIDE frame 2 (104 bytes with
    # its prefix): only frame 1 is fully consumed.
    assert (consumed_frames, consumed_bytes) == (1, 60)

    message, consumed_frames, consumed_bytes = asm.next_message()
    assert message == b"b" * 50
    assert (consumed_frames, consumed_bytes) == (2, 50 + len(stream[110:]))
    assert asm.next_message() is None
    assert asm.retained_bytes == 0


def test_a_message_above_the_maximum_is_refused_before_it_is_retained():
    asm = fw.MessageAssembler(max_message=16)
    asm.feed(fw.encode_message(b"x" * 16))
    assert asm.next_message()[0] == b"x" * 16
    asm.feed(b"\x00\x00\x00\x11")        # a 17-byte message announced
    with pytest.raises(StreamProtocolError):
        asm.next_message()
    with pytest.raises(StreamProtocolError):
        fw.encode_message(b"y" * (fw.FLEET_STREAM_MAX_MESSAGE + 1))


def test_an_empty_message_is_a_valid_frame_of_four_bytes():
    asm = fw.MessageAssembler()
    asm.feed(fw.encode_message(b""))
    assert asm.next_message() == (b"", 1, 4)
