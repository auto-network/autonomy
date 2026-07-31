"""Validate the frozen cross-language channel fixture (auto-25pky).

The checked-in tools/.../fixtures/channel_v1.json is the single source of truth
that Python, browser JavaScript, and Go all implement against. This suite
proves, against the REAL channel.py primitives:

* the file is schema-valid, lowercase-hex, integer-typed, no dup case names;
* regenerating it is byte-identical (deterministic, git-clean);
* every positive handshake verifies and its keys re-derive exactly, every
  record opens to its chunk, every stream reassembles to its message;
* every negative case is refused at its named stage.

No socket, no clock.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tools.network.relaykit import channel as ch
from tools.network.relaykit.channel import (
    DIR_C2S,
    DIR_S2C,
    ChannelCrypto,
    HandshakeError,
    RecordError,
)
from tools.network.relaykit.tests import generate_channel_vectors as gen

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "channel_v1.json"
_HEX = re.compile(r"[0-9a-f]*")

ERROR_CATEGORIES = {
    "invalid_encoding", "invalid_fields", "unsupported_version", "invalid_ephemeral",
    "noncanonical_cert", "wrong_root", "wrong_org", "invalid_scope", "invalid_time",
    "invalid_narrowing", "invalid_signature", "authentication_failed",
    "sequence_mismatch", "reserved_flags", "message_too_large", "sequence_exhausted",
}
HANDSHAKE_FIELDS = {
    "name", "now", "org", "token", "root", "delegates", "client_private_hex",
    "client_public_hex", "server_private_hex", "server_public_hex",
    "client_hello_utf8_hex", "signed_payload_hex", "hello_signature_hex",
    "server_hello_utf8_hex", "transcript_hash_hex", "shared_secret_hex",
    "hkdf_okm_hex", "key_c2s_hex", "key_s2c_hex",
}
RECORD_FIELDS = {"name", "handshake", "direction", "sequence", "flags", "chunk_hex",
                 "nonce_hex", "aad_hex", "ciphertext_hex", "wire_record_hex"}
STEP_FIELDS = {"sender", "message_hex", "start_sequence", "records_hex"}
NEG_FIELDS = {"name", "stage", "input", "expected_error"}


@pytest.fixture(scope="module")
def vectors():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _is_hex(s) -> bool:
    return isinstance(s, str) and len(s) % 2 == 0 and _HEX.fullmatch(s) is not None


# ── schema + determinism ────────────────────────────────────────────


def test_regeneration_is_byte_identical():
    # Deterministic generator -> checked-in file is exactly its output.
    assert gen.render(gen.build_vectors()) == FIXTURE.read_text(encoding="utf-8")


def test_top_level_schema(vectors):
    assert set(vectors) == {"schema", "encoding", "constants", "handshakes",
                            "records", "streams", "negative_cases"}
    assert vectors["schema"] == "autonomy.network.channel.vectors.v1"
    assert vectors["encoding"] == "lowercase-hex"
    c = vectors["constants"]
    assert c["handshake_version"] == 1
    assert c["chunk_size"] == ch.CHUNK_SIZE
    assert c["max_message_size"] == ch.MAX_MESSAGE_SIZE
    assert c["known_flags_mask"] == 0x03


def test_field_sets_hex_and_unique_names(vectors):
    for hs in vectors["handshakes"]:
        assert set(hs) == HANDSHAKE_FIELDS
        assert set(hs["root"]) == {"private_hex", "public_hex"}
        for dg in hs["delegates"]:
            assert set(dg) == {"private_hex", "public_hex", "cert_wire_utf8_hex"}
    for rec in vectors["records"]:
        assert set(rec) == RECORD_FIELDS
        assert rec["direction"] in ("c2s", "s2c")
        assert isinstance(rec["sequence"], int) and isinstance(rec["flags"], int)
        assert rec["flags"] & ~0x03 == 0                       # valid flag mask only
        for k in ("chunk_hex", "nonce_hex", "aad_hex", "ciphertext_hex", "wire_record_hex"):
            assert _is_hex(rec[k]), (rec["name"], k)
    for st in vectors["streams"]:
        assert set(st) == {"name", "handshake", "steps"}
        for step in st["steps"]:
            assert set(step) == STEP_FIELDS
            assert step["sender"] in ("client", "server")
            assert all(_is_hex(r) for r in step["records_hex"])
    names = [n["name"] for n in vectors["negative_cases"]]
    assert len(names) == len(set(names)), "duplicate negative-case names"
    for nc in vectors["negative_cases"]:
        assert set(nc) == NEG_FIELDS
        assert nc["stage"] in ("hello", "record_open", "record_seal")
        assert nc["expected_error"] in ERROR_CATEGORIES


def test_required_negative_catalogue_present(vectors):
    # The security-critical rejection catalogue must be complete.
    by_err = {n["expected_error"] for n in vectors["negative_cases"]}
    for required in ("wrong_root", "wrong_org", "invalid_signature", "unsupported_version",
                     "invalid_fields", "invalid_ephemeral", "invalid_encoding",
                     "noncanonical_cert", "invalid_time", "authentication_failed",
                     "sequence_mismatch", "reserved_flags", "message_too_large",
                     "sequence_exhausted"):
        assert required in by_err, f"missing negative category: {required}"


# ── positives reproduce against real channel.py ─────────────────────


def _keys(hs):
    return (bytes.fromhex(hs["transcript_hash_hex"]),
            bytes.fromhex(hs["key_c2s_hex"]), bytes.fromhex(hs["key_s2c_hex"]))


def test_handshakes_verify_and_keys_rederive(vectors):
    for hs in vectors["handshakes"]:
        sp, tr = ch.verify_server_hello(
            bytes.fromhex(hs["server_hello_utf8_hex"]), root_pub=hs["root"]["public_hex"],
            org=hs["org"], token=hs["token"], client_eph=hs["client_public_hex"], now=hs["now"])
        assert sp == hs["server_public_hex"]
        assert tr.hex() == hs["transcript_hash_hex"]
        # Independently re-derive the shared secret + keys from raw fixture privs.
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
        from cryptography.hazmat.primitives import hashes
        cpriv = X25519PrivateKey.from_private_bytes(bytes.fromhex(hs["client_private_hex"]))
        spriv = X25519PrivateKey.from_private_bytes(bytes.fromhex(hs["server_private_hex"]))
        shared = cpriv.exchange(spriv.public_key())
        assert shared.hex() == hs["shared_secret_hex"]
        okm = HKDF(algorithm=hashes.SHA256(), length=64, salt=tr, info=ch.KEYS_INFO).derive(shared)
        assert okm.hex() == hs["hkdf_okm_hex"]
        assert okm[:32].hex() == hs["key_c2s_hex"] and okm[32:].hex() == hs["key_s2c_hex"]


def test_records_open_to_their_chunk(vectors):
    hs = {h["name"]: h for h in vectors["handshakes"]}
    for rec in vectors["records"]:
        tr, kc, ks = _keys(hs[rec["handshake"]])
        recv_dir = DIR_C2S if rec["direction"] == "c2s" else DIR_S2C
        recv_key = kc if rec["direction"] == "c2s" else ks
        other_dir = DIR_S2C if rec["direction"] == "c2s" else DIR_C2S
        other_key = ks if rec["direction"] == "c2s" else kc
        c = ChannelCrypto(other_key, recv_key, tr, other_dir, recv_dir)
        c._recv_seq = rec["sequence"]
        opened = c.open_stream_record(bytes.fromhex(rec["wire_record_hex"]))
        assert opened.chunk.hex() == rec["chunk_hex"], rec["name"]


def test_streams_reassemble_to_their_message(vectors):
    hs = {h["name"]: h for h in vectors["handshakes"]}
    for st in vectors["streams"]:
        tr, kc, ks = _keys(hs[st["handshake"]])
        for step in st["steps"]:
            recv_dir = DIR_C2S if step["sender"] == "client" else DIR_S2C
            recv_key = kc if step["sender"] == "client" else ks
            other_dir = DIR_S2C if step["sender"] == "client" else DIR_C2S
            other_key = ks if step["sender"] == "client" else kc
            c = ChannelCrypto(other_key, recv_key, tr, other_dir, recv_dir)
            c._recv_seq = step["start_sequence"]
            message = None
            for rh in step["records_hex"]:
                m = c.open_record(bytes.fromhex(rh))
                if m is not None:
                    message = m
            assert message is not None and message.hex() == step["message_hex"], st["name"]


# ── negatives are refused at their stage ────────────────────────────


def test_every_negative_is_refused(vectors):
    for nc in vectors["negative_cases"]:
        inp = nc["input"]
        with pytest.raises((HandshakeError, RecordError)):
            if nc["stage"] == "hello":
                ch.verify_server_hello(
                    bytes.fromhex(inp["wire_utf8_hex"]), root_pub=inp["root_public_hex"],
                    org=inp["org"], token=inp["token"], client_eph=inp["client_public_hex"],
                    now=inp["now"])
            elif nc["stage"] == "record_open":
                rd = DIR_C2S if inp["recv_direction"] == "c2s" else DIR_S2C
                od = DIR_S2C if inp["recv_direction"] == "c2s" else DIR_C2S
                c = ChannelCrypto(b"\x00" * 32, bytes.fromhex(inp["recv_key_hex"]),
                                  bytes.fromhex(inp["transcript_hash_hex"]), od, rd,
                                  max_message_size=inp["max_message_size"])
                c._recv_seq = inp["start_sequence"]
                for rh in inp["records_hex"]:
                    c.open_record(bytes.fromhex(rh))
            else:  # record_seal
                sd = DIR_C2S if inp["send_direction"] == "c2s" else DIR_S2C
                od = DIR_S2C if inp["send_direction"] == "c2s" else DIR_C2S
                c = ChannelCrypto(bytes.fromhex(inp["send_key_hex"]), b"\x00" * 32,
                                  bytes.fromhex(inp["transcript_hash_hex"]), sd, od,
                                  max_message_size=inp["max_message_size"])
                c._send_seq = inp["start_sequence"]
                for mh in inp["messages_hex"]:
                    list(c.iter_seal_message(bytes.fromhex(mh)))
