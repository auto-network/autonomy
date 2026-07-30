"""The attachment fixture freezes record bytes and application framing."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tools.network.relaykit.channel import (
    DIR_C2S,
    DIR_S2C,
    ChannelCrypto,
)
from tools.network.relaykit.tests.generate_attachment_vectors import build_vectors

FIXTURE = Path(__file__).with_name("fixtures") / "attachment_v1.json"


def _cryptos(data):
    crypto = data["record_crypto"]
    c2s = bytes.fromhex(crypto["c2s_key_hex"])
    s2c = bytes.fromhex(crypto["s2c_key_hex"])
    transcript = bytes.fromhex(crypto["transcript_hash_hex"])
    return (
        ChannelCrypto(c2s, s2c, transcript, DIR_C2S, DIR_S2C),
        ChannelCrypto(s2c, c2s, transcript, DIR_S2C, DIR_C2S),
    )


def _assert_step(step, receiver):
    records = [bytes.fromhex(value) for value in step["records_hex"]]
    assert int.from_bytes(records[0][:8], "big") == step["starting_sequence"]
    message = bytearray()
    final = None
    for record in records:
        opened = receiver.open_stream_record(record)
        message.extend(opened.chunk)
        if opened.message_end:
            final = opened.stream_final
    assert bytes(message) == bytes.fromhex(step["message_hex"])
    assert final is not None
    return bytes(message), final


def _assert_decoded(message, decoded):
    if decoded.get("kind") != "attachment.body":
        assert json.loads(message) == decoded
        return
    assert len(message) >= 9
    offset = int.from_bytes(message[:8], "big")
    flags = message[8]
    chunk = message[9:]
    assert offset == decoded["offset"]
    assert flags == decoded["flags"]
    assert bool(flags & 0x01) == decoded["last_in_window"]
    assert bool(flags & 0x02) == decoded["eof"]
    assert len(chunk) == decoded["chunk_length"]
    assert hashlib.sha256(chunk).hexdigest() == decoded["chunk_sha256"]


def test_fixture_is_canonical_generator_output():
    expected = json.dumps(
        build_vectors(), ensure_ascii=True, indent=2, sort_keys=True
    ) + "\n"
    assert FIXTURE.read_text(encoding="utf-8") == expected


def test_positive_stream_steps_cross_record_application_boundary():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert data["schema"] == "autonomy.network.attachment.vectors.v1"
    client_receiver, server_receiver = _cryptos(data)

    for case in data["positive"]:
        for index, step in enumerate(case["stream_steps"]):
            receiver = server_receiver if step["sender"] == "client" else client_receiver
            message, stream_final = _assert_step(step, receiver)
            _assert_decoded(message, step["decoded"])
            if step["sender"] == "server":
                later_server_message = any(
                    candidate["sender"] == "server"
                    for candidate in case["stream_steps"][index + 1:]
                )
                assert stream_final is not later_server_message


def test_every_error_code_is_canonical_and_final():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    expected = {
        "not_found",
        "not_authorized",
        "oversize",
        "out_of_range",
        "unavailable",
        "internal",
    }
    actual = set()
    for step in data["errors"]:
        client_receiver, _server = _cryptos(data)
        message, stream_final = _assert_step(step, client_receiver)
        _assert_decoded(message, step["decoded"])
        assert stream_final
        actual.add(step["decoded"]["code"])
    assert actual == expected


def test_cancel_message_is_canonical_and_final():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    _client, server_receiver = _cryptos(data)
    message, stream_final = _assert_step(data["cancel"], server_receiver)
    _assert_decoded(message, data["cancel"]["decoded"])
    assert stream_final


def _assembly_error(case, chunk_size):
    if case["kind"] == "request":
        request = json.loads(bytes.fromhex(case["message_hex"]))
        if (
            request["offset"] % chunk_size
            or request.get("length", chunk_size) % chunk_size
        ):
            return "out_of_range"
        return None

    expected_offset = case["request"]["offset"]
    limit = min(
        expected_offset + case["request"]["length"], case["total_size"]
    )
    for value in case["messages_hex"]:
        message = bytes.fromhex(value)
        offset = int.from_bytes(message[:8], "big")
        flags = message[8]
        chunk = message[9:]
        if offset != expected_offset:
            return "non_contiguous"
        expected_offset += len(chunk)
        if flags & 0x02 and expected_offset != case["total_size"]:
            return "wrong_eof"
        if flags & 0x01 and expected_offset != limit:
            return "wrong_window_end"
    return None


@pytest.mark.parametrize("case_index", range(5))
def test_negative_assembly_vectors(case_index):
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    case = data["negative"][case_index]
    assert _assembly_error(
        case, data["constants"]["application_chunk_size"]
    ) == case["expected_error"]
