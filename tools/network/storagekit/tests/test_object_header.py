"""Object-key header: wrap/unwrap, both body suites, context binding."""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import os

import pytest

from tools.network.idkit import KeyPair, canonical_json
from tools.network.storagekit import (
    BODY_SUITE_DEFAULT,
    BODY_SUITE_LARGE,
    MalformedRecordError,
    RecordSignatureError,
    SuiteError,
)
from tools.network.storagekit.object_header import (
    ObjectHeaderError,
    ObjectKeyHeader,
    build,
    open_body,
    seal_body,
    unwrap_cek,
    verify_structure,
    wrap_cek,
)

GENESIS = "1a" * 32
DOMAIN_ID = "2b" * 32
OBJECT_ID = "3c" * 32
REVISION_ID = "4d" * 32
STATE_ID = "5e" * 32
HEADS = ["6f" * 32]
PLAINTEXT = b"the object body plaintext, sealed exactly once under its own key"

CONTEXT = dict(
    genesis_id=GENESIS,
    domain_id=DOMAIN_ID,
    object_id=OBJECT_ID,
    revision_id=REVISION_ID,
    storage_state_id=STATE_ID,
)


class Obj:
    """One sealed object: secret, cek, blob, and signed header."""

    def __init__(self, body_suite_id: str = BODY_SUITE_DEFAULT):
        self.author = KeyPair.generate()
        self.state_secret = os.urandom(32)
        self.cek = os.urandom(32)
        self.body_nonce = os.urandom(12)
        self.wrap_nonce = os.urandom(12)
        self.blob = seal_body(
            self.cek,
            PLAINTEXT,
            body_suite_id=body_suite_id,
            body_nonce=self.body_nonce,
            **CONTEXT,
        )
        self.header = build(
            self.author,
            self.state_secret,
            self.cek,
            writer_authority_heads=HEADS,
            body_suite_id=body_suite_id,
            body_nonce=self.body_nonce,
            wrap_nonce=self.wrap_nonce,
            ciphertext_hash=hashlib.sha256(self.blob).hexdigest(),
            **CONTEXT,
        )


@pytest.fixture(scope="module")
def obj() -> Obj:
    return Obj()


# -- round trip ---------------------------------------------------------------------


def test_build_has_exactly_fifteen_fields_and_cek_roundtrips(obj):
    assert len(dataclasses.fields(ObjectKeyHeader)) == 15
    assert set(obj.header.signed_dict()) | {"signature"} == {
        f.name for f in dataclasses.fields(ObjectKeyHeader)
    }
    assert unwrap_cek(obj.state_secret, obj.header) == obj.cek


@pytest.mark.parametrize("suite", [BODY_SUITE_DEFAULT, BODY_SUITE_LARGE])
def test_both_body_suites_roundtrip(suite):
    o = Obj(body_suite_id=suite)
    assert o.header.body_suite_id == suite
    assert open_body(o.header, o.cek, o.blob) == PLAINTEXT


def test_full_read_path(obj):
    cek = unwrap_cek(obj.state_secret, obj.header)
    assert open_body(obj.header, cek, obj.blob) == PLAINTEXT


# -- wrap fail-closed ---------------------------------------------------------------


def test_wrong_state_secret_fails(obj):
    with pytest.raises(ObjectHeaderError):
        unwrap_cek(os.urandom(32), obj.header)


@pytest.mark.parametrize(
    "field",
    [
        "wrapped_cek",
        "wrap_nonce",
        "object_id",
        "revision_id",
        "storage_state_id",
        "ciphertext_hash",
        "genesis_id",
        "domain_id",
    ],
)
def test_unwrap_fails_when_context_altered(obj, field):
    if field in ("wrapped_cek", "wrap_nonce"):
        raw = bytearray(base64.b64decode(getattr(obj.header, field)))
        raw[0] ^= 0x01
        value = base64.b64encode(bytes(raw)).decode("ascii")
    else:
        value = "9c" * 32
    tampered = dataclasses.replace(obj.header, **{field: value})
    with pytest.raises(ObjectHeaderError):
        unwrap_cek(obj.state_secret, tampered)


def test_cross_object_replay_fails():
    # DoD: a header re-pointed at a second object's blob (or vice versa)
    # fails — the wrap binds ciphertext_hash, the address check binds the
    # blob, and the body AAD binds the object identifiers.
    a, b = Obj(), Obj()
    swapped = dataclasses.replace(
        a.header, ciphertext_hash=hashlib.sha256(b.blob).hexdigest()
    )
    with pytest.raises(ObjectHeaderError):
        unwrap_cek(a.state_secret, swapped)
    with pytest.raises(ObjectHeaderError):
        open_body(a.header, a.cek, b.blob)  # address mismatch


def test_wrap_suite_downgrade_fails_closed(obj):
    downgraded = dataclasses.replace(obj.header, wrap_suite_id="aes-128-gcm")
    with pytest.raises(SuiteError):
        unwrap_cek(obj.state_secret, downgraded)


# -- body fail-closed ---------------------------------------------------------------


def test_open_body_fails_when_bound_identifier_differs(obj):
    for field in (
        "genesis_id",
        "domain_id",
        "object_id",
        "revision_id",
        "storage_state_id",
    ):
        tampered = dataclasses.replace(obj.header, **{field: "9c" * 32})
        with pytest.raises(ObjectHeaderError):
            open_body(tampered, obj.cek, obj.blob)
    other_suite = dataclasses.replace(obj.header, body_suite_id=BODY_SUITE_LARGE)
    with pytest.raises(ObjectHeaderError):
        open_body(other_suite, obj.cek, obj.blob)


def test_content_address_binds_blob(obj):
    assert hashlib.sha256(obj.blob).hexdigest() == obj.header.ciphertext_hash
    corrupted = obj.blob[:-1] + bytes([obj.blob[-1] ^ 1])
    assert hashlib.sha256(corrupted).hexdigest() != obj.header.ciphertext_hash
    with pytest.raises(ObjectHeaderError):
        open_body(obj.header, obj.cek, corrupted)


def test_wrong_cek_fails(obj):
    with pytest.raises(ObjectHeaderError):
        open_body(obj.header, os.urandom(32), obj.blob)


# -- structure ----------------------------------------------------------------------


def test_verify_structure_accepts_wire_and_object(obj):
    assert verify_structure(obj.header) == obj.header
    assert verify_structure(obj.header.to_json()) == obj.header


def test_verify_structure_rejections(obj):
    wire = obj.header.to_json()
    text = wire.decode("ascii")

    flipped_sig = dataclasses.replace(
        obj.header,
        signature=("%x" % (int(obj.header.signature, 16) ^ 1)).zfill(128),
    )
    with pytest.raises(RecordSignatureError):
        verify_structure(flipped_sig)

    with pytest.raises(MalformedRecordError):  # unknown field
        verify_structure(
            canonical_json(
                {**obj.header.signed_dict(), "signature": obj.header.signature, "x": 1}
            )
        )
    missing = {**obj.header.signed_dict(), "signature": obj.header.signature}
    del missing["object_id"]
    with pytest.raises(MalformedRecordError):
        verify_structure(canonical_json(missing))

    with pytest.raises(MalformedRecordError):
        verify_structure(dataclasses.replace(obj.header, version=2))
    with pytest.raises(SuiteError):
        verify_structure(dataclasses.replace(obj.header, body_suite_id="chunked-aead-v1"))
    with pytest.raises(SuiteError):
        verify_structure(dataclasses.replace(obj.header, wrap_suite_id="chacha20-poly1305"))
    with pytest.raises(MalformedRecordError):
        verify_structure(dataclasses.replace(obj.header, object_id="XY" * 32))
    with pytest.raises(MalformedRecordError):
        verify_structure(dataclasses.replace(obj.header, revision_id="ab" * 31))
    with pytest.raises(MalformedRecordError):
        verify_structure(
            dataclasses.replace(obj.header, writer_authority_heads=tuple(HEADS + HEADS))
        )
    with pytest.raises(MalformedRecordError):
        verify_structure(
            dataclasses.replace(
                obj.header, writer_authority_heads=("7f" * 32, "6f" * 32)
            )
        )
    with pytest.raises(MalformedRecordError):  # non-canonical wire
        verify_structure(text.replace(":", ": ", 1).encode("ascii"))
    with pytest.raises(MalformedRecordError):
        verify_structure(12345)


def test_header_id_commits_to_signature(obj):
    resigned = dataclasses.replace(
        obj.header, signature=KeyPair.generate().sign_hex(obj.header.signing_input())
    )
    assert resigned.header_id != obj.header.header_id
