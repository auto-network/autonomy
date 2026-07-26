"""Storage-state descriptor: signing, commitment, freshness, tamper paths."""

from __future__ import annotations

import dataclasses
import hashlib

import pytest

from tools.network.idkit import KeyPair, canonical_json
from tools.network.storagekit import (
    CommitmentError,
    MalformedRecordError,
    RecordSignatureError,
    SuiteError,
)
from tools.network.storagekit.state import (
    STATE_SECRET_LEN,
    STATE_VERSION,
    StorageStateDescriptor,
    compute_secret_commitment,
    generate,
    verify_secret_commitment,
    verify_structure,
)

GENESIS = "1a" * 32
DOMAIN_ID = "2b" * 32
PARENTS = ["3c" * 32, "4d" * 32]
HEADS = ["5e" * 32, "6f" * 32]
LOSS = ["7a" * 32]
DIGEST = "8b" * 32


@pytest.fixture
def creator() -> KeyPair:
    return KeyPair.generate()


@pytest.fixture
def minted(creator):
    return generate(creator, GENESIS, DOMAIN_ID, PARENTS, HEADS, LOSS, DIGEST)


def test_generate_signs_and_returns_secret(creator, minted):
    descriptor, secret = minted
    assert verify_structure(descriptor) is None
    assert isinstance(secret, bytes) and len(secret) == STATE_SECRET_LEN
    assert descriptor.version == STATE_VERSION
    assert descriptor.creator_persona == creator.public_hex
    assert descriptor.parent_state_ids == tuple(sorted(PARENTS))


def test_generation_is_fresh_per_call(creator, minted):
    d1, s1 = minted
    d2, s2 = generate(creator, GENESIS, DOMAIN_ID, PARENTS, HEADS, LOSS, DIGEST)
    assert s1 != s2
    assert d1.state_nonce != d2.state_nonce
    assert d1.secret_commitment != d2.secret_commitment
    assert d1.state_id != d2.state_id


def test_secret_commitment_verifies_and_rejects_wrong_secret(minted):
    descriptor, secret = minted
    assert verify_secret_commitment(descriptor, secret) is None
    wrong = bytes(32) if secret != bytes(32) else b"\x01" * 32
    with pytest.raises(CommitmentError):
        verify_secret_commitment(descriptor, wrong)
    flipped = secret[:-1] + bytes([secret[-1] ^ 1])
    with pytest.raises(CommitmentError):
        verify_secret_commitment(descriptor, flipped)
    with pytest.raises(MalformedRecordError):
        verify_secret_commitment(descriptor, secret[:31])
    with pytest.raises(MalformedRecordError):
        verify_secret_commitment(descriptor, secret.hex())


def test_state_id_commits_to_wire_and_roundtrips(minted):
    descriptor, _ = minted
    wire = descriptor.to_json()
    assert descriptor.state_id == hashlib.sha256(wire).hexdigest()
    restored = StorageStateDescriptor.from_json(wire)
    assert restored == descriptor
    assert restored.state_id == descriptor.state_id


def test_altered_suite_fails_closed(minted):
    descriptor, _ = minted
    downgraded = dataclasses.replace(descriptor, suite_id="aes-128-gcm")
    with pytest.raises(SuiteError):
        verify_structure(downgraded)
    wire = canonical_json({**downgraded.signed_dict(), "signature": downgraded.signature})
    with pytest.raises(SuiteError):
        StorageStateDescriptor.from_json(wire)


@pytest.mark.parametrize(
    "field,value",
    [
        ("genesis_id", "9c" * 32),
        ("domain_id", "9c" * 32),
        ("state_nonce", "9c" * 32),
        ("parent_state_ids", ("9c" * 32,)),
        ("authority_heads", ()),
        ("covered_loss_heads", ()),
        ("loss_projection_digest", "9c" * 32),
        ("secret_commitment", "9c" * 32),
        # Deterministic (xdist collects per-worker): a fixed unrelated key.
        ("creator_persona", KeyPair.from_private_hex("aa" * 32).public_hex),
    ],
)
def test_mutating_any_signed_field_breaks_the_signature(minted, field, value):
    descriptor, _ = minted
    tampered = dataclasses.replace(descriptor, **{field: value})
    with pytest.raises(RecordSignatureError):
        verify_structure(tampered)
    assert tampered.state_id != descriptor.state_id


def test_forged_or_absent_signature_rejected(minted):
    descriptor, _ = minted
    forged = dataclasses.replace(
        descriptor, signature=KeyPair.generate().sign_hex(descriptor.signing_input())
    )
    with pytest.raises(RecordSignatureError):
        verify_structure(forged)
    blank = dataclasses.replace(descriptor, signature="0" * 128)
    with pytest.raises(RecordSignatureError):
        verify_structure(blank)
    with pytest.raises(MalformedRecordError):
        verify_structure(dataclasses.replace(descriptor, signature="nope"))


def test_non_canonical_wires_rejected(minted):
    descriptor, _ = minted
    wire = descriptor.to_json()
    text = wire.decode("ascii")
    with pytest.raises(MalformedRecordError):
        StorageStateDescriptor.from_json(text.replace(":", ": ", 1).encode("ascii"))
    key = '"domain_id"'
    reordered = text.replace(key, '"zz_moved"').replace(
        '"genesis_id"', key
    )  # crude but guaranteed non-canonical key material
    with pytest.raises(MalformedRecordError):
        StorageStateDescriptor.from_json(reordered.encode("ascii"))

    unsorted = canonical_json(
        {
            **descriptor.signed_dict(),
            "parent_state_ids": list(reversed(sorted(PARENTS))),
            "signature": descriptor.signature,
        }
    )
    with pytest.raises(MalformedRecordError):
        StorageStateDescriptor.from_json(unsorted)

    duplicated = canonical_json(
        {
            **descriptor.signed_dict(),
            "authority_heads": sorted(HEADS) + [sorted(HEADS)[-1]],
            "signature": descriptor.signature,
        }
    )
    with pytest.raises(MalformedRecordError):
        StorageStateDescriptor.from_json(duplicated)


def test_raw_secret_never_in_wire(minted):
    descriptor, secret = minted
    wire = descriptor.to_json()
    assert secret not in wire
    assert secret.hex().encode("ascii") not in wire
    assert descriptor.secret_commitment != secret.hex()


def test_generate_rejects_malformed_inputs(creator):
    with pytest.raises(MalformedRecordError):
        generate(creator, "short", DOMAIN_ID, PARENTS, HEADS, LOSS, DIGEST)
    with pytest.raises(MalformedRecordError):
        generate(creator, GENESIS, DOMAIN_ID, ["not-hex!"], HEADS, LOSS, DIGEST)
    with pytest.raises(MalformedRecordError):
        generate(creator, GENESIS, DOMAIN_ID, PARENTS, HEADS, LOSS, "AB" * 32)


def test_generate_sorts_and_dedups_identifier_lists(creator):
    descriptor, _ = generate(
        creator,
        GENESIS,
        DOMAIN_ID,
        list(reversed(PARENTS)) + PARENTS,
        list(reversed(HEADS)),
        LOSS + LOSS,
        DIGEST,
    )
    assert descriptor.parent_state_ids == tuple(sorted(PARENTS))
    assert descriptor.authority_heads == tuple(sorted(HEADS))
    assert descriptor.covered_loss_heads == tuple(sorted(LOSS))
    assert verify_structure(descriptor) is None
