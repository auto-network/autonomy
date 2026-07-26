"""Persona KEM credential: build, validate, fold checks, supersession."""

from __future__ import annotations

import dataclasses
import hashlib
import random

import pytest

from tools.network.idkit import KeyPair, canonical_json
from tools.network.idkit.sealing import derive_encapsulation_keypair
from tools.network.ledger import Ledger, fold
from tools.network.ledger.tests.conftest import Sim
from tools.network.storagekit import (
    MalformedRecordError,
    RecordSignatureError,
    SuiteError,
    domain_member_keys,
    select_current_credential,
)
from tools.network.storagekit.credentials import (
    CredentialError,
    PersonaKemCredential,
    build,
    compute_kem_key_id,
    kem_purpose,
    validate,
    verify_against_fold,
)

GENESIS = "1a" * 32
OTHER_GENESIS = "2b" * 32
HEADS = ["6f" * 32]
HLC0 = (1_800_000_000_000, 0)
SEED = bytes(range(32))


@pytest.fixture
def signer() -> KeyPair:
    return KeyPair.generate()


@pytest.fixture
def minted(signer):
    return build(signer, GENESIS, SEED, HEADS, HLC0)


class TestBuild:
    def test_binding_and_signature(self, signer, minted):
        credential, kem_private = minted
        assert credential.persona == signer.public_hex
        expected_priv, expected_pub = derive_encapsulation_keypair(
            SEED, kem_purpose(GENESIS)
        )
        assert credential.kem_public_key == expected_pub
        assert kem_private == expected_priv
        assert credential.kem_key_id == hashlib.sha256(
            canonical_json(credential.binding_dict())
        ).hexdigest()
        assert validate(credential) == credential

    def test_derivation_is_deterministic_and_org_bound(self, signer, minted):
        credential, _ = minted
        again, _ = build(signer, GENESIS, SEED, HEADS, HLC0)
        assert again.kem_public_key == credential.kem_public_key
        other_org, _ = build(signer, OTHER_GENESIS, SEED, HEADS, HLC0)
        assert other_org.kem_public_key != credential.kem_public_key

    def test_kem_key_is_not_the_signing_key(self, signer, minted):
        credential, kem_private = minted
        assert credential.kem_public_key != credential.persona
        assert kem_private != signer.private_hex


class TestValidate:
    def test_wire_and_dict_roundtrip(self, minted):
        credential, _ = minted
        assert PersonaKemCredential.from_json(credential.to_json()) == credential
        assert validate(credential.to_json()) == credential
        assert validate(credential.to_dict()) == credential

    def test_rejections(self, signer, minted):
        credential, _ = minted
        cases = [
            (dataclasses.replace(credential, version=2), MalformedRecordError),
            (dataclasses.replace(credential, suite_id=0), SuiteError),
            (dataclasses.replace(credential, suite_id=True), SuiteError),
            (dataclasses.replace(credential, persona="zz" * 32), MalformedRecordError),
            # right shape, not an Ed25519 point
            (dataclasses.replace(credential, persona="ff" * 32), MalformedRecordError),
            (dataclasses.replace(credential, kem_public_key="XY" * 32), MalformedRecordError),
            (
                dataclasses.replace(credential, authority_heads=("7f" * 32, "6f" * 32)),
                MalformedRecordError,
            ),
            (
                dataclasses.replace(credential, authority_heads=("6f" * 32, "6f" * 32)),
                MalformedRecordError,
            ),
            (dataclasses.replace(credential, created_hlc=(True, 0)), MalformedRecordError),
            (dataclasses.replace(credential, created_hlc=(-1, 0)), MalformedRecordError),
            (
                dataclasses.replace(
                    credential, kem_key_id="0" + credential.kem_key_id[1:]
                ),
                MalformedRecordError,
            ),
            (
                dataclasses.replace(
                    credential,
                    signature=("%x" % (int(credential.signature, 16) ^ 1)).zfill(128),
                ),
                RecordSignatureError,
            ),
        ]
        for bad, exc in cases:
            with pytest.raises(exc):
                validate(bad)

    def test_foreign_signer_rejected(self, minted):
        credential, _ = minted
        forged = dataclasses.replace(
            credential, signature=KeyPair.generate().sign_hex(credential.signing_input())
        )
        with pytest.raises(RecordSignatureError):
            validate(forged)

    def test_tampered_binding_field_breaks_kem_key_id(self, minted):
        credential, _ = minted
        with pytest.raises(MalformedRecordError):
            validate(dataclasses.replace(credential, genesis_id=OTHER_GENESIS))

    def test_dict_with_wrong_field_set_rejected(self, minted):
        credential, _ = minted
        with pytest.raises(MalformedRecordError):
            validate({**credential.to_dict(), "extra": 1})
        short = credential.to_dict()
        del short["kem_key_id"]
        with pytest.raises(MalformedRecordError):
            validate(short)

    def test_non_canonical_wire_rejected(self, minted):
        credential, _ = minted
        text = credential.to_json().decode("ascii")
        with pytest.raises(MalformedRecordError):
            validate(text.replace(":", ": ", 1).encode("ascii"))


# -- fold-backed scenarios -----------------------------------------------------------


def _member_sim():
    """Genesis + founder-style member + one invited member, all role-holding."""
    sim = Sim()
    sim.role_define(sim.root, "member", scope_set=["link:publish"])
    founder, founder_invite = KeyPair.generate(), KeyPair.generate()
    fid = sim.invite(sim.root, "member", invite_key=founder_invite)
    sim.claim(fid, founder_invite, founder)  # the founding self-claim analog
    member, invite_key = KeyPair.generate(), KeyPair.generate()
    iid = sim.invite(sim.root, "member", invite_key=invite_key)
    claim_id = sim.claim(iid, invite_key, member)
    return sim, founder, member, claim_id


def _credential_for(sim, signer):
    credential, kem_private = build(
        signer, sim.fold().genesis_id, SEED, [sim.genesis_id], HLC0
    )
    return credential, kem_private


class TestRoster:
    def test_role_holding_current_keys_only(self):
        sim, founder, member, _ = _member_sim()
        state = sim.fold()
        assert domain_member_keys(state) == frozenset(
            {founder.public_hex, member.public_hex}
        )
        # Root is never a domain principal.
        assert sim.root.public_hex not in domain_member_keys(state)

    def test_rekey_moves_the_current_key(self):
        sim, founder, member, _ = _member_sim()
        new_key = KeyPair.generate()
        sim.rekey(member, member, member, new_key)
        keys = domain_member_keys(sim.fold())
        assert new_key.public_hex in keys
        assert member.public_hex not in keys

    def test_role_stripped_member_excluded(self):
        sim, founder, member, _ = _member_sim()
        sim.role_revoke(sim.root, member, "member")
        keys = domain_member_keys(sim.fold())
        assert member.public_hex not in keys
        assert founder.public_hex in keys

    def test_order_independent(self):
        sim, founder, member, _ = _member_sim()
        reference = sorted(domain_member_keys(sim.fold()))
        events = sim.ledger.events()
        rng = random.Random(20260726)
        for _ in range(3):
            batch = list(events)
            rng.shuffle(batch)
            replica = Ledger()
            replica.ingest(batch)
            assert sorted(domain_member_keys(fold(replica))) == reference


class TestVerifyAgainstFold:
    def test_accepts_current_role_holding_member(self):
        sim, _, member, _ = _member_sim()
        credential, _ = _credential_for(sim, member)
        assert verify_against_fold(credential, sim.fold()) == credential

    def test_rejects_rekeyed_prior_key_and_accepts_fresh(self):
        sim, _, member, _ = _member_sim()
        old_credential, _ = _credential_for(sim, member)
        new_key = KeyPair.generate()
        sim.rekey(member, member, member, new_key)
        state = sim.fold()
        with pytest.raises(CredentialError):
            verify_against_fold(old_credential, state)
        fresh, _ = _credential_for(sim, new_key)
        assert verify_against_fold(fresh, state) == fresh

    def test_rejects_revoked_persona(self):
        sim, _, member, claim_id = _member_sim()
        credential, _ = _credential_for(sim, member)
        sim.revoke_event(sim.root, claim_id)
        with pytest.raises(CredentialError):
            verify_against_fold(credential, sim.fold())

    def test_rejects_role_stripped_persona(self):
        sim, _, member, _ = _member_sim()
        credential, _ = _credential_for(sim, member)
        sim.role_revoke(sim.root, member, "member")
        with pytest.raises(CredentialError):
            verify_against_fold(credential, sim.fold())

    def test_rejects_non_member_and_root(self):
        sim, _, _, _ = _member_sim()
        state = sim.fold()
        stranger, _ = _credential_for(sim, KeyPair.generate())
        with pytest.raises(CredentialError):
            verify_against_fold(stranger, state)
        root_credential, _ = _credential_for(sim, sim.root)
        with pytest.raises(CredentialError):
            verify_against_fold(root_credential, state)

    def test_rejects_cross_organization(self):
        sim, _, member, _ = _member_sim()
        credential, _ = build(member, OTHER_GENESIS, SEED, [OTHER_GENESIS], HLC0)
        with pytest.raises(CredentialError):
            verify_against_fold(credential, sim.fold())


class TestSupersession:
    def test_causal_descent_supersedes(self):
        sim, _, member, _ = _member_sim()
        early_heads = list(sim.ledger.heads())
        later = sim.delegate(sim.root, KeyPair.generate(), ["link:publish"])
        old, _ = build(member, sim.fold().genesis_id, SEED, early_heads, HLC0)
        new, _ = build(member, sim.fold().genesis_id, bytes(range(1, 33)), [later], HLC0)
        current = select_current_credential([old, new], sim.ledger.ancestry)
        assert current == new
        # Order of candidates does not matter.
        assert select_current_credential([new, old], sim.ledger.ancestry) == new

    def test_concurrent_frontiers_resolve_by_kem_key_id(self):
        sim, _, member, _ = _member_sim()
        base = list(sim.ledger.heads())
        x = sim.delegate(sim.root, KeyPair.generate(), ["link:publish"], parents=base)
        y = sim.delegate(sim.root, KeyPair.generate(), ["link:revoke"], parents=base)
        gid = sim.fold().genesis_id
        a, _ = build(member, gid, SEED, [x], HLC0)
        b, _ = build(member, gid, bytes(range(1, 33)), [y], HLC0)
        expected = max([a, b], key=lambda c: c.kem_key_id)
        assert select_current_credential([a, b], sim.ledger.ancestry) == expected

    def test_equal_frontiers_are_concurrent_not_mutually_superseding(self):
        sim, _, member, _ = _member_sim()
        heads = list(sim.ledger.heads())
        gid = sim.fold().genesis_id
        a, _ = build(member, gid, SEED, heads, HLC0)
        b, _ = build(member, gid, bytes(range(1, 33)), heads, HLC0)
        expected = max([a, b], key=lambda c: c.kem_key_id)
        assert select_current_credential([a, b], sim.ledger.ancestry) == expected

    def test_rekey_retires_and_fresh_is_current(self):
        sim, _, member, _ = _member_sim()
        gid = sim.fold().genesis_id
        old, _ = build(member, gid, SEED, [sim.genesis_id], HLC0)
        new_key = KeyPair.generate()
        sim.rekey(member, member, member, new_key)
        fresh, _ = build(
            new_key, gid, bytes(range(1, 33)), list(sim.ledger.heads()), HLC0
        )
        state = sim.fold()
        survivors = []
        for candidate in (old, fresh):
            try:
                survivors.append(verify_against_fold(candidate, state))
            except CredentialError:
                pass
        assert survivors == [fresh]
        assert select_current_credential(survivors, sim.ledger.ancestry) == fresh

    def test_empty_candidates_raise(self):
        with pytest.raises(CredentialError):
            select_current_credential([], lambda heads: frozenset())
