"""Content store: round-trip, immutability, dedup, integrity, counters."""

from __future__ import annotations

import dataclasses
import hashlib
import os

import pytest

from tools.network.idkit import KeyPair
from tools.network.storagekit import object_header
from tools.network.storagekit.errors import (
    BodyNotFoundError,
    ContentHashMismatchError,
    ObjectNotFoundError,
    RevisionExistsError,
)
from tools.network.storagekit.store import ContentStore
from tools.network.storagekit.suites import BODY_SUITE_DEFAULT

GENESIS = "1a" * 32
DOMAIN_ID = "2b" * 32
STATE_A = "3c" * 32
STATE_B = "4d" * 32

AUTHOR = KeyPair.from_private_hex("ab" * 32)
STATE_SECRET = bytes(range(32))


def make_pair(
    plaintext: bytes = b"body", state_id: str = STATE_A, object_id=None, revision_id=None
):
    cek, body_nonce, wrap_nonce = os.urandom(32), os.urandom(12), os.urandom(12)
    ids = dict(
        genesis_id=GENESIS,
        domain_id=DOMAIN_ID,
        object_id=object_id or os.urandom(32).hex(),
        revision_id=revision_id or os.urandom(32).hex(),
        storage_state_id=state_id,
    )
    body = object_header.seal_body(
        cek, plaintext, body_suite_id=BODY_SUITE_DEFAULT, body_nonce=body_nonce, **ids
    )
    header = object_header.build(
        AUTHOR, STATE_SECRET, cek,
        writer_authority_heads=["6f" * 32],
        body_suite_id=BODY_SUITE_DEFAULT, body_nonce=body_nonce,
        wrap_nonce=wrap_nonce,
        ciphertext_hash=hashlib.sha256(body).hexdigest(), **ids,
    )
    return header, body


@pytest.fixture
def store(tmp_path) -> ContentStore:
    with ContentStore(tmp_path) as s:
        yield s


def test_put_get_roundtrip(store):
    header, body = make_pair(b"the plaintext body")
    assert store.put_object(header, body) == header.ciphertext_hash
    got_header, got_body = store.get_object(header.object_id, header.revision_id)
    assert got_header == header
    assert got_body == body


def test_hash_mismatch_persists_nothing(store):
    header, body = make_pair()
    with pytest.raises(ContentHashMismatchError):
        store.put_object(header, body + b"x")
    with pytest.raises(ObjectNotFoundError):
        store.get_object(header.object_id, header.revision_id)
    assert store.object_count(STATE_A) == 0
    assert not any(store.bodies_dir.rglob("*"))


def test_revision_immutability_and_idempotence(store):
    header, body = make_pair()
    store.put_object(header, body)
    # Byte-identical replay: idempotent, counter unchanged.
    assert store.put_object(header, body) == header.ciphertext_hash
    assert store.object_count(STATE_A) == 1
    # Same key, different bytes: refused.
    other_header, other_body = make_pair(
        b"different", object_id=header.object_id, revision_id=header.revision_id
    )
    with pytest.raises(RevisionExistsError):
        store.put_object(other_header, other_body)
    # A new revision of the same object is an independent entry.
    rev2_header, rev2_body = make_pair(b"second rev", object_id=header.object_id)
    store.put_object(rev2_header, rev2_body)
    assert store.get_object(header.object_id, rev2_header.revision_id)[1] == rev2_body
    assert store.object_count(STATE_A) == 2


def test_shared_body_is_deduplicated(store):
    header1, body = make_pair(b"shared bytes")
    # A second header referencing the same blob (structurally valid).
    header2, _ = make_pair(b"ignored")
    header2 = dataclasses.replace(
        header2, ciphertext_hash=header1.ciphertext_hash
    )
    header2 = dataclasses.replace(
        header2, signature=AUTHOR.sign_hex(header2.signing_input())
    )
    store.put_object(header1, body)
    store.put_object(header2, body)
    rows = store._db.execute("SELECT COUNT(*) FROM content_bodies").fetchone()[0]
    assert rows == 1
    blobs = [p for p in store.bodies_dir.rglob("*") if p.is_file()]
    assert len(blobs) == 1
    assert store._db.execute("SELECT COUNT(*) FROM content_objects").fetchone()[0] == 2


def test_missing_object_and_missing_body(store):
    with pytest.raises(ObjectNotFoundError):
        store.get_object("aa" * 32, "bb" * 32)
    header, body = make_pair()
    store.put_object(header, body)
    store._blob_path(header.ciphertext_hash).unlink()
    with pytest.raises(BodyNotFoundError):
        store.get_object(header.object_id, header.revision_id)


def test_corrupted_blob_is_refused_on_read(store):
    header, body = make_pair()
    store.put_object(header, body)
    path = store._blob_path(header.ciphertext_hash)
    corrupted = bytearray(path.read_bytes())
    corrupted[0] ^= 0x01
    path.write_bytes(bytes(corrupted))
    with pytest.raises(ContentHashMismatchError):
        store.get_object(header.object_id, header.revision_id)


def test_invalid_header_persists_nothing(store):
    header, body = make_pair()
    forged = dataclasses.replace(
        header, signature=KeyPair.generate().sign_hex(header.signing_input())
    )
    from tools.network.storagekit.errors import RecordSignatureError, SuiteError

    with pytest.raises(RecordSignatureError):
        store.put_object(forged, body)
    downgraded = dataclasses.replace(header, wrap_suite_id="aes-128-gcm")
    with pytest.raises(SuiteError):
        store.put_object(downgraded, body)
    assert store._db.execute("SELECT COUNT(*) FROM content_objects").fetchone()[0] == 0
    assert store.object_count(STATE_A) == 0


def test_per_state_counters_exact_under_interleaving(store):
    pairs_a = [make_pair(b"a%d" % i, state_id=STATE_A) for i in range(3)]
    pairs_b = [make_pair(b"b%d" % i, state_id=STATE_B) for i in range(2)]
    interleaved = [pairs_a[0], pairs_b[0], pairs_a[1], pairs_b[1], pairs_a[2]]
    for header, body in interleaved:
        store.put_object(header, body)
    store.put_object(*pairs_a[0])  # idempotent replay: no change
    bad_header, bad_body = make_pair(b"bad", state_id=STATE_A)
    with pytest.raises(ContentHashMismatchError):  # rejected put: no change
        store.put_object(bad_header, bad_body + b"!")
    assert store.object_count(STATE_A) == 3
    assert store.object_count(STATE_B) == 2
    assert store.object_count("5e" * 32) == 0


def test_store_reopens_with_state(tmp_path):
    header, body = make_pair()
    with ContentStore(tmp_path) as store:
        store.put_object(header, body)
    with ContentStore(tmp_path) as reopened:
        got_header, got_body = reopened.get_object(header.object_id, header.revision_id)
        assert (got_header, got_body) == (header, body)
        assert reopened.object_count(STATE_A) == 1
