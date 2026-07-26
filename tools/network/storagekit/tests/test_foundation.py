"""storagekit foundation: suite recognition fail-closed, record helpers."""

from __future__ import annotations

import hashlib

import pytest

from tools.network.idkit import KeyPair, canonical_json, verify_signature
from tools.network.idkit.errors import SignatureError
from tools.network.storagekit import (
    BODY_SUITE_CHUNKED_RESERVED,
    BODY_SUITE_DEFAULT,
    BODY_SUITE_LARGE,
    BODY_SUITES,
    HASH_SUITE,
    HASH_SUITES,
    MalformedRecordError,
    SEAL_SUITE,
    SEAL_SUITES,
    SIGNATURE_SUITE,
    SIGNATURE_SUITES,
    SuiteError,
    WRAP_SUITE,
    WRAP_SUITES,
    parse_canonical,
    record_id,
    require_suite,
    signing_input,
)

ALL_POSITIONS = (HASH_SUITES, SIGNATURE_SUITES, SEAL_SUITES, WRAP_SUITES, BODY_SUITES)


# -- suites ----------------------------------------------------------------------


def test_recognized_identifiers_pass():
    assert require_suite(HASH_SUITE, HASH_SUITES) is None
    assert require_suite(SIGNATURE_SUITE, SIGNATURE_SUITES) is None
    assert require_suite(SEAL_SUITE, SEAL_SUITES) is None
    assert require_suite(WRAP_SUITE, WRAP_SUITES) is None
    assert require_suite(BODY_SUITE_DEFAULT, BODY_SUITES) is None
    assert require_suite(BODY_SUITE_LARGE, BODY_SUITES) is None


@pytest.mark.parametrize("recognized", ALL_POSITIONS)
def test_unrecognized_identifier_fails_closed(recognized):
    for bad in ("aes-128-gcm", "", None, 999):
        with pytest.raises(SuiteError):
            require_suite(bad, recognized)


@pytest.mark.parametrize("recognized", ALL_POSITIONS)
def test_reserved_chunked_identifier_fails_closed_everywhere(recognized):
    with pytest.raises(SuiteError):
        require_suite(BODY_SUITE_CHUNKED_RESERVED, recognized)


def test_seal_suite_matches_idkit_wire_tag():
    # The record-level identifier is the same value the sealed wire record
    # is tagged with — and its type (int, a wire byte) is deliberately
    # distinct from the string identifiers of the other positions, so a
    # seal suite id can never pass another position's recognized set.
    from tools.network.idkit.sealing import SUITE_X25519_HKDF_SHA256_CHACHA20POLY1305

    assert SEAL_SUITE == SUITE_X25519_HKDF_SHA256_CHACHA20POLY1305
    assert isinstance(SEAL_SUITE, int)
    for other in (HASH_SUITES, SIGNATURE_SUITES, WRAP_SUITES, BODY_SUITES):
        with pytest.raises(SuiteError):
            require_suite(SEAL_SUITE, other)


def test_wrap_and_body_defaults_are_gcm_siv():
    assert WRAP_SUITE == "aes-256-gcm-siv"
    assert BODY_SUITE_DEFAULT == "aes-256-gcm-siv"
    assert BODY_SUITE_LARGE == "chacha20-poly1305"


# -- signed-record helpers ---------------------------------------------------------

DOMAIN = b"autonomy.storage.test-record.v1\n"
FIELDS = ("kind", "payload", "signature")


def _make_record(signer: KeyPair, payload: str = "value") -> tuple:
    body = {"kind": "test-record", "payload": payload}
    sig = signer.sign_hex(signing_input(DOMAIN, body))
    wire = canonical_json({**body, "signature": sig})
    return body, sig, wire


def test_signed_record_roundtrip():
    signer = KeyPair.generate()
    body, sig, wire = _make_record(signer)

    parsed = parse_canonical(FIELDS, wire)
    resigned_input = signing_input(DOMAIN, {k: parsed[k] for k in ("kind", "payload")})
    assert verify_signature(signer.public_hex, parsed["signature"], resigned_input) is None

    with pytest.raises(SignatureError):
        verify_signature(KeyPair.generate().public_hex, parsed["signature"], resigned_input)


def test_domain_prefix_separates_signatures():
    signer = KeyPair.generate()
    body, sig, _ = _make_record(signer)
    other_input = signing_input(b"autonomy.storage.other.v1\n", body)
    with pytest.raises(SignatureError):
        verify_signature(signer.public_hex, sig, other_input)


def test_record_id_is_sha256_and_commits_to_signature():
    signer = KeyPair.generate()
    _, _, wire = _make_record(signer)
    assert record_id(wire) == hashlib.sha256(wire).hexdigest()

    # Same payload, different signer -> different signature -> different id.
    _, _, other_wire = _make_record(KeyPair.generate())
    assert record_id(wire) != record_id(other_wire)


def test_parse_canonical_accepts_only_the_canonical_bytes():
    signer = KeyPair.generate()
    body, sig, wire = _make_record(signer)
    assert parse_canonical(FIELDS, wire) == {**body, "signature": sig}

    text = wire.decode("ascii")
    reordered = (
        '{"signature":"%s","kind":"test-record","payload":"value"}' % sig
    ).encode("ascii")
    non_canonical = (
        reordered,  # reordered keys
        text.replace(":", ": ", 1).encode("ascii"),  # added whitespace
        text.replace('"kind"', '"kin\\u0064"').encode("ascii"),  # escape form
        wire + b"\n",  # trailing bytes
    )
    for bad in non_canonical:
        with pytest.raises(MalformedRecordError):
            parse_canonical(FIELDS, bad)


def test_parse_canonical_enforces_the_exact_field_set():
    signer = KeyPair.generate()
    body, sig, _ = _make_record(signer)
    unknown = canonical_json({**body, "signature": sig, "extra": 1})
    missing = canonical_json({"kind": body["kind"], "signature": sig})
    duplicate = b'{"kind":"a","kind":"b","payload":"v","signature":"s"}'
    for bad in (unknown, missing, duplicate, b"[]", b"not json", b""):
        with pytest.raises(MalformedRecordError):
            parse_canonical(FIELDS, bad)
    with pytest.raises(MalformedRecordError):
        parse_canonical(FIELDS, "not bytes")


def test_signing_input_rejects_bad_arguments():
    with pytest.raises(MalformedRecordError):
        signing_input(b"", {"a": 1})
    with pytest.raises(MalformedRecordError):
        signing_input("not bytes", {"a": 1})
    with pytest.raises(MalformedRecordError):
        signing_input(DOMAIN, "not a dict")
    with pytest.raises(MalformedRecordError):
        signing_input(DOMAIN, {"a": 1.5})  # floats are outside canonical JSON
