"""Capability grant and receipt: delivery, fail-closed paths, possession."""

from __future__ import annotations

import dataclasses
import os

import pytest

from tools.network.idkit import KeyPair, canonical_json
from tools.network.idkit.errors import SealingError
from tools.network.idkit.sealing import derive_encapsulation_keypair
from tools.network.idkit.sealing import open as seal_open
from tools.network.storagekit import (
    CommitmentError,
    MalformedRecordError,
    RecordSignatureError,
    SuiteError,
)
from tools.network.storagekit import capability, credentials, state
from tools.network.storagekit.capability import (
    CapabilityGrant,
    CapabilityReceipt,
    PossessionTagError,
    accept,
    grant_purpose,
    issue,
    issue_receipt,
    receipt_possession_tag,
    verify_grant,
    verify_possession_tag,
    verify_receipt,
)

GENESIS = "1a" * 32
DOMAIN_ID = "2b" * 32
HEADS = ["6f" * 32]
DIGEST = "8b" * 32
HLC0 = (1_800_000_000_000, 0)
KEM_SEED = bytes(range(32))


class Setup:
    def __init__(self):
        self.grantor = KeyPair.generate()
        self.receiver = KeyPair.generate()
        self.credential, self.kem_private = credentials.build(
            self.receiver, GENESIS, KEM_SEED, HEADS, HLC0
        )
        self.descriptor, self.secret = state.generate(
            self.grantor, GENESIS, DOMAIN_ID, [], HEADS, [], DIGEST
        )
        self.grant = issue(
            self.grantor,
            genesis_id=GENESIS,
            domain_id=DOMAIN_ID,
            storage_state_id=self.descriptor.state_id,
            recipient_credential=self.credential,
            state_secret=self.secret,
            state_secret_commitment=self.descriptor.secret_commitment,
            authority_heads=HEADS,
        )


@pytest.fixture(scope="module")
def s() -> Setup:
    return Setup()


def _resign(grant: CapabilityGrant, signer: KeyPair) -> CapabilityGrant:
    return dataclasses.replace(grant, signature=signer.sign_hex(grant.signing_input()))


# -- grant delivery -------------------------------------------------------------------


def test_issue_accept_roundtrip(s):
    assert accept(s.grant, s.kem_private, s.descriptor) == s.secret
    assert accept(s.grant.to_json(), s.kem_private, s.descriptor) == s.secret


def test_wrong_recipient_key_fails_closed(s):
    other_private, _ = derive_encapsulation_keypair(
        bytes(range(1, 33)), credentials.kem_purpose(GENESIS)
    )
    with pytest.raises(SealingError):
        accept(s.grant, other_private, s.descriptor)


def test_tampered_ciphertext_fails_closed(s):
    sealed = bytearray(bytes.fromhex(s.grant.encapsulated_state_secret))
    sealed[-1] ^= 0x01
    tampered = _resign(
        dataclasses.replace(s.grant, encapsulated_state_secret=bytes(sealed).hex()),
        s.grantor,
    )
    with pytest.raises(SealingError):
        accept(tampered, s.kem_private, s.descriptor)


def test_substituted_secret_raises_commitment_error(s):
    # A grant that claims to deliver descriptor2's secret but seals a
    # different one: opens fine, then the double commitment check refuses.
    descriptor2, _secret2 = state.generate(
        s.grantor, GENESIS, DOMAIN_ID, [], HEADS, [], DIGEST
    )
    lying = issue(
        s.grantor,
        genesis_id=GENESIS,
        domain_id=DOMAIN_ID,
        storage_state_id=descriptor2.state_id,
        recipient_credential=s.credential,
        state_secret=s.secret,  # not descriptor2's secret
        state_secret_commitment=descriptor2.secret_commitment,
        authority_heads=HEADS,
    )
    with pytest.raises(CommitmentError):
        accept(lying, s.kem_private, descriptor2)


def test_altered_grant_commitment_raises_commitment_error(s):
    altered = _resign(
        dataclasses.replace(s.grant, state_secret_commitment="9c" * 32), s.grantor
    )
    with pytest.raises(CommitmentError):
        accept(altered, s.kem_private, s.descriptor)


def test_unknown_suite_raises_before_decapsulation(s, monkeypatch):
    downgraded = _resign(dataclasses.replace(s.grant, hpke_suite_id=2), s.grantor)

    def bomb(*args, **kwargs):  # decapsulation must never be reached
        raise AssertionError("decapsulated before the suite check")

    monkeypatch.setattr(capability, "seal_open", bomb)
    with pytest.raises(SuiteError):
        accept(downgraded, s.kem_private, s.descriptor)


def test_identifier_binding_at_the_sealing_layer(s):
    sealed = bytes.fromhex(s.grant.encapsulated_state_secret)
    base = dict(
        genesis_id=GENESIS,
        domain_id=DOMAIN_ID,
        storage_state_id=s.descriptor.state_id,
        recipient_kem_key_id=s.credential.kem_key_id,
    )
    assert seal_open(sealed, s.kem_private, grant_purpose(**base)) == s.secret
    for field in base:
        with pytest.raises(SealingError):
            seal_open(
                sealed, s.kem_private, grant_purpose(**{**base, field: "9c" * 32})
            )


def test_accept_refuses_foreign_context(s):
    # Recipient-credential swap reaches the sealing layer and fails there;
    # org/domain/state swaps fail the descriptor context check first.
    swapped_kem = _resign(
        dataclasses.replace(s.grant, recipient_kem_key_id="9c" * 32), s.grantor
    )
    with pytest.raises(SealingError):
        accept(swapped_kem, s.kem_private, s.descriptor)
    for field in ("genesis_id", "domain_id", "storage_state_id"):
        foreign = _resign(dataclasses.replace(s.grant, **{field: "9c" * 32}), s.grantor)
        with pytest.raises(MalformedRecordError):
            accept(foreign, s.kem_private, s.descriptor)


# -- grant record ---------------------------------------------------------------------


def test_verify_grant_rejects_tampering_and_non_canonical(s):
    assert verify_grant(s.grant) == s.grant
    assert verify_grant(s.grant.to_json()) == s.grant

    for field, value in (
        ("domain_id", "9c" * 32),
        ("recipient_kem_key_id", "9c" * 32),
        ("state_secret_commitment", "9c" * 32),
        ("authority_heads", ()),
        ("grantor_persona", KeyPair.from_private_hex("aa" * 32).public_hex),
    ):
        with pytest.raises(RecordSignatureError):
            verify_grant(dataclasses.replace(s.grant, **{field: value}))

    text = s.grant.to_json().decode("ascii")
    with pytest.raises(MalformedRecordError):
        verify_grant(text.replace(":", ": ", 1).encode("ascii"))
    with pytest.raises(MalformedRecordError):
        verify_grant(
            canonical_json(
                {**s.grant.signed_dict(), "signature": s.grant.signature, "extra": 1}
            )
        )
    with pytest.raises(MalformedRecordError):
        verify_grant(12345)


def test_grant_structural_rejections(s):
    with pytest.raises(MalformedRecordError):
        verify_grant(_resign(dataclasses.replace(s.grant, version=2), s.grantor))
    with pytest.raises(SuiteError):
        verify_grant(_resign(dataclasses.replace(s.grant, hpke_suite_id=True), s.grantor))
    with pytest.raises(MalformedRecordError):
        verify_grant(
            _resign(
                dataclasses.replace(s.grant, encapsulated_state_secret="ab" * 10),
                s.grantor,
            )
        )
    with pytest.raises(MalformedRecordError):
        verify_grant(
            _resign(
                dataclasses.replace(s.grant, authority_heads=("7f" * 32, "6f" * 32)),
                s.grantor,
            )
        )


# -- receipt --------------------------------------------------------------------------


def _receipt(s, receiver=None, secret=None) -> CapabilityReceipt:
    return issue_receipt(
        receiver or s.receiver,
        genesis_id=GENESIS,
        domain_id=DOMAIN_ID,
        storage_state_id=s.descriptor.state_id,
        state_secret=secret if secret is not None else s.secret,
    )


def test_receipt_roundtrip_and_possession(s):
    receipt = _receipt(s)
    assert verify_receipt(receipt) == receipt
    assert verify_receipt(receipt.to_json()) == receipt
    assert verify_possession_tag(receipt, s.secret) is None
    # The creator's self-receipt is the same record shape, one per §10.
    self_receipt = _receipt(s, receiver=s.grantor)
    assert verify_possession_tag(self_receipt, s.secret) is None
    assert self_receipt.possession_tag != receipt.possession_tag  # persona-bound


def test_receipt_rejects_forged_signature_and_non_canonical(s):
    receipt = _receipt(s)
    forged = dataclasses.replace(
        receipt, signature=KeyPair.generate().sign_hex(receipt.signing_input())
    )
    with pytest.raises(RecordSignatureError):
        verify_receipt(forged)
    text = receipt.to_json().decode("ascii")
    with pytest.raises(MalformedRecordError):
        verify_receipt(text.replace(":", ": ", 1).encode("ascii"))


def test_possession_tag_requires_the_secret(s):
    # A receipt asserting possession of a secret its issuer never held.
    pretender = _receipt(s, secret=os.urandom(32))
    assert verify_receipt(pretender) == pretender  # signature alone passes
    with pytest.raises(PossessionTagError):
        verify_possession_tag(pretender, s.secret)  # ...the tag does not
    with pytest.raises(PossessionTagError):
        verify_possession_tag(_receipt(s), os.urandom(32))


def test_possession_tag_binds_all_four_identifiers(s):
    receipt = _receipt(s)
    for field in ("genesis_id", "domain_id", "storage_state_id"):
        moved = dataclasses.replace(receipt, **{field: "9c" * 32})
        with pytest.raises(PossessionTagError):
            verify_possession_tag(moved, s.secret)
    stolen = dataclasses.replace(
        receipt, receiver_persona=KeyPair.from_private_hex("aa" * 32).public_hex
    )
    with pytest.raises(PossessionTagError):
        verify_possession_tag(stolen, s.secret)


def test_tag_formula_matches_contract(s):
    receipt = _receipt(s)
    assert receipt.possession_tag == receipt_possession_tag(
        s.secret, GENESIS, DOMAIN_ID, s.descriptor.state_id, s.receiver.public_hex
    )
    assert len(bytes.fromhex(receipt.possession_tag)) == 32
