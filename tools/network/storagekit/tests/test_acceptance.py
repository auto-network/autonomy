"""Acceptance procedures: membership-only authorization, coverage, recency."""

from __future__ import annotations

import dataclasses
import hashlib
import os
import random

import pytest

from tools.network.idkit import KeyPair
from tools.network.ledger import Ledger, fold
from tools.network.ledger.projections import organization_content_domain_id
from tools.network.ledger.scopes import validate_scope
from tools.network.ledger.tests.conftest import Sim
from tools.network.storagekit import bridge as bridge_mod
from tools.network.storagekit import capability, credentials, object_header, state
from tools.network.storagekit.acceptance import (
    AuthorityError,
    DomainError,
    FrontierRecencyError,
    LossCoverageError,
    ScopeError,
    accept_grant,
    accept_object,
    accept_state,
    loss_projection_digest,
    resolve_member_key,
    scope_storage_advance,
    scope_storage_grant,
)
from tools.network.storagekit.errors import RecordSignatureError

HLC0 = (1_800_000_000_000, 0)
KEM_SEED = bytes(range(32))


def _bomb(*args, **kwargs):
    raise AssertionError("authority seam touched before record verification")


class Org:
    """Founded org: three role-holding members, a delegated agent key,
    and a delegation chain terminating outside the roster."""

    def __init__(self):
        self.sim = Sim()
        s = self.sim
        s.role_define(s.root, "member", scope_set=["link:publish"])
        self.founder, self.founder_claim = self._admit()
        self.member, self.member_claim = self._admit()
        self.recipient, self.recipient_claim = self._admit()
        s.delegate(s.root, self.member, ["link:publish"], redelegate=True)
        self.agent = KeyPair.generate()
        s.delegate(self.member, self.agent, ["link:publish"])
        self.outside = KeyPair.generate()
        s.delegate(s.root, self.outside, ["link:publish"], redelegate=True)
        self.outside_leaf = KeyPair.generate()
        s.delegate(self.outside, self.outside_leaf, ["link:publish"])
        self.gen = s.genesis_id
        self.dom = organization_content_domain_id(self.gen)

    def _admit(self):
        persona, invite_key = KeyPair.generate(), KeyPair.generate()
        iid = self.sim.invite(self.sim.root, "member", invite_key=invite_key)
        return persona, self.sim.claim(iid, invite_key, persona)

    def seams(self):
        s = self.sim
        return (
            lambda heads: s.fold(heads=list(heads)),
            lambda ids: s.ledger.ancestry(ids),
        )

    def mint(self, creator, parents=(), covered=None, digest=None, heads=None):
        heads = sorted(self.sim.ledger.heads()) if heads is None else sorted(heads)
        f = self.sim.fold(heads=heads)
        covered = sorted(f.loss_heads) if covered is None else sorted(covered)
        digest = loss_projection_digest(f) if digest is None else digest
        return state.generate(creator, self.gen, self.dom, parents, heads, covered, digest)


@pytest.fixture
def org() -> Org:
    return Org()


# -- scopes ---------------------------------------------------------------------------


def test_scope_constructors():
    dom = "ab" * 32
    assert scope_storage_advance(dom) == f"storage:state:advance:{dom}"
    assert scope_storage_grant(dom) == f"storage:capability:grant:{dom}"
    for scope in (scope_storage_advance(dom), scope_storage_grant(dom)):
        assert validate_scope(scope) == scope


# -- accept_state ----------------------------------------------------------------------


class TestAcceptState:
    def test_member_creator_accepted(self, org):
        descriptor, _ = org.mint(org.member)
        fold_at, ancestry = org.seams()
        result = accept_state(descriptor, fold_at, ancestry)
        assert result.creator_member == org.member.public_hex
        assert result.history_complete is True  # no parents

    def test_superset_coverage_accepted(self, org):
        d1 = org.sim.delegate(org.sim.root, KeyPair.generate(), ["link:publish"])
        base = sorted(org.sim.ledger.heads())
        r1 = org.sim.revoke_event(org.sim.root, d1, parents=base)
        d2 = org.sim.delegate(org.sim.root, KeyPair.generate(), ["link:revoke"], parents=base)
        r2 = org.sim.revoke_key(org.sim.root, KeyPair.generate(), parents=[d2])
        cp = org.sim.checkpoint(org.sim.root, parents=[r1, r2])
        fold_at, ancestry = org.seams()

        exact, _ = org.mint(org.member, covered=[r1, r2])
        assert accept_state(exact, fold_at, ancestry).creator_member

        # Strict superset: an extra non-contraction ancestor rides along.
        superset, _ = org.mint(org.member, covered=[d1, r1, r2])
        assert accept_state(superset, fold_at, ancestry).creator_member

        # Inclusive-ancestry coverage: one descendant covers both branches.
        union, _ = org.mint(org.member, covered=[cp])
        assert accept_state(union, fold_at, ancestry).creator_member

    def test_omission_and_digest_mismatch_rejected(self, org):
        d = org.sim.delegate(org.sim.root, KeyPair.generate(), ["link:publish"])
        org.sim.revoke_event(org.sim.root, d)
        fold_at, ancestry = org.seams()
        uncovered, _ = org.mint(org.member, covered=[])
        with pytest.raises(LossCoverageError):
            accept_state(uncovered, fold_at, ancestry)
        bad_digest, _ = org.mint(org.member, digest="9c" * 32)
        with pytest.raises(LossCoverageError):
            accept_state(bad_digest, fold_at, ancestry)

    def test_domain_guard(self, org):
        fold_at, ancestry = org.seams()
        heads = sorted(org.sim.ledger.heads())
        f = org.sim.fold(heads=heads)
        wrong_dom, _ = state.generate(
            org.member, org.gen, "9c" * 32, (), heads, (), loss_projection_digest(f)
        )
        with pytest.raises(DomainError):
            accept_state(wrong_dom, fold_at, ancestry)
        wrong_gen, _ = state.generate(
            org.member, "9c" * 32, org.dom, (), heads, (), loss_projection_digest(f)
        )
        with pytest.raises(DomainError):
            accept_state(wrong_gen, fold_at, ancestry)

    def test_membership_is_the_sole_path(self, org):
        fold_at, ancestry = org.seams()
        # Root's universal wildcard does not reach storage.
        root_minted, _ = org.mint(org.sim.root)
        with pytest.raises(ScopeError):
            accept_state(root_minted, fold_at, ancestry)
        # A delegated key resolves through its chain to a member persona.
        via_agent, _ = org.mint(org.agent)
        assert (
            accept_state(via_agent, fold_at, ancestry).creator_member
            == org.member.public_hex
        )
        # A chain terminating outside the roster (at root) is void.
        for outsider in (org.outside, org.outside_leaf):
            outside_minted, _ = org.mint(outsider)
            with pytest.raises(ScopeError):
                accept_state(outside_minted, fold_at, ancestry)

    def test_removed_creator_rejected(self, org):
        org.sim.revoke_event(org.sim.root, org.member_claim)
        fold_at, ancestry = org.seams()
        descriptor, _ = org.mint(org.member)
        with pytest.raises(ScopeError):
            accept_state(descriptor, fold_at, ancestry)

    def test_rekey_keying(self, org):
        new_key = KeyPair.generate()
        org.sim.rekey(org.member, org.member, org.member, new_key)
        fold_at, ancestry = org.seams()
        retired, _ = org.mint(org.member)  # signed by the retired key
        with pytest.raises(ScopeError):
            accept_state(retired, fold_at, ancestry)
        current, _ = org.mint(new_key)  # same record shape, current key
        assert accept_state(current, fold_at, ancestry).creator_member == new_key.public_hex

    def test_history_complete_requires_every_bridge(self, org):
        fold_at, ancestry = org.seams()
        p1, s1 = org.mint(org.member)
        p2, s2 = org.mint(org.member)
        child, child_secret = org.mint(
            org.member, parents=[p1.state_id, p2.state_id]
        )

        def edge(parent, parent_secret):
            return bridge_mod.create(
                org.member,
                genesis_id=org.gen,
                domain_id=org.dom,
                child_state_id=child.state_id,
                parent_state_id=parent.state_id,
                child_state_secret=child_secret,
                parent_state_secret=parent_secret,
                authority_heads=list(child.authority_heads),
            )

        b1, b2 = edge(p1, s1), edge(p2, s2)
        assert accept_state(
            child, fold_at, ancestry, bridges=[b1, b2]
        ).history_complete
        # Missing bridge: incomplete, no raise.
        assert not accept_state(child, fold_at, ancestry, bridges=[b1]).history_complete
        # Invalid bridge: incomplete, no raise.
        forged = dataclasses.replace(
            b2, signature=KeyPair.generate().sign_hex(b2.signing_input())
        )
        assert not accept_state(
            child, fold_at, ancestry, bridges=[b1, forged]
        ).history_complete
        # Duplicate bridges for one edge: not exactly one.
        b2_dup = edge(p2, s2)
        assert not accept_state(
            child, fold_at, ancestry, bridges=[b1, b2, b2_dup]
        ).history_complete

    def test_forged_record_rejected_before_authority(self, org):
        descriptor, _ = org.mint(org.member)
        forged = dataclasses.replace(
            descriptor, signature=KeyPair.generate().sign_hex(descriptor.signing_input())
        )
        with pytest.raises(RecordSignatureError):
            accept_state(forged, _bomb, _bomb)


# -- accept_grant ----------------------------------------------------------------------


def _credential(org, persona, seed=KEM_SEED, heads=None):
    credential, kem_private = credentials.build(
        persona, org.gen, seed, heads or [org.gen], HLC0
    )
    return credential, kem_private


def _grant(org, descriptor, secret, credential, grantor=None, heads=None):
    return capability.issue(
        grantor or org.member,
        genesis_id=org.gen,
        domain_id=org.dom,
        storage_state_id=descriptor.state_id,
        recipient_credential=credential,
        state_secret=secret,
        state_secret_commitment=descriptor.secret_commitment,
        authority_heads=heads or sorted(org.sim.ledger.heads()),
    )


class TestAcceptGrant:
    def test_member_to_current_member_accepted(self, org):
        descriptor, secret = org.mint(org.member)
        credential, _ = _credential(org, org.recipient)
        grant = _grant(org, descriptor, secret, credential)
        fold_at, ancestry = org.seams()
        result = accept_grant(grant, fold_at, ancestry, credential, descriptor)
        assert result.grantor_member == org.member.public_hex
        assert result.recipient_persona == org.recipient.public_hex

    def test_frontier_recency(self, org):
        descriptor, secret = org.mint(org.member)  # cites current heads
        credential, _ = _credential(org, org.recipient)
        stale = _grant(org, descriptor, secret, credential, heads=[org.gen])
        fold_at, ancestry = org.seams()
        with pytest.raises(FrontierRecencyError):
            accept_grant(stale, fold_at, ancestry, credential, descriptor)

    def test_non_member_grantor_rejected(self, org):
        descriptor, secret = org.mint(org.member)
        credential, _ = _credential(org, org.recipient)
        fold_at, ancestry = org.seams()
        for bad_grantor in (org.sim.root, KeyPair.generate(), org.outside):
            grant = _grant(org, descriptor, secret, credential, grantor=bad_grantor)
            with pytest.raises(ScopeError):
                accept_grant(grant, fold_at, ancestry, credential, descriptor)

    def test_authority_rejections(self, org):
        descriptor, secret = org.mint(org.member)
        credential, _ = _credential(org, org.recipient)
        grant = _grant(org, descriptor, secret, credential)
        fold_at, ancestry = org.seams()

        other_credential, _ = _credential(org, org.recipient, seed=bytes(range(1, 33)))
        with pytest.raises(AuthorityError):  # key-id mismatch
            accept_grant(grant, fold_at, ancestry, other_credential, descriptor)

        wrong_commit = dataclasses.replace(grant, state_secret_commitment="9c" * 32)
        wrong_commit = dataclasses.replace(
            wrong_commit, signature=org.member.sign_hex(wrong_commit.signing_input())
        )
        with pytest.raises(AuthorityError):  # commitment mismatch
            accept_grant(wrong_commit, fold_at, ancestry, credential, descriptor)

        stranger_credential, _ = _credential(org, KeyPair.generate())
        stranger_grant = _grant(org, descriptor, secret, stranger_credential)
        with pytest.raises(AuthorityError):  # non-member recipient
            accept_grant(
                stranger_grant, fold_at, ancestry, stranger_credential, descriptor
            )

    def test_rekeyed_recipient_rejected(self, org):
        descriptor, secret = org.mint(org.member)
        credential, _ = _credential(org, org.recipient)
        grant = _grant(org, descriptor, secret, credential)
        org.sim.rekey(org.recipient, org.recipient, org.recipient, KeyPair.generate())
        fold_at, ancestry = org.seams()
        # Re-grant at the post-rekey frontier, still addressed to the old
        # persona key's credential: the roster is current-keyed.
        late = _grant(org, descriptor, secret, credential)
        with pytest.raises(AuthorityError):
            accept_grant(late, fold_at, ancestry, credential, descriptor)

    def test_superseded_credential_rejected(self, org):
        descriptor, secret = org.mint(org.member)
        old_credential, _ = _credential(org, org.recipient, heads=[org.gen])
        fresh_credential, _ = _credential(
            org,
            org.recipient,
            seed=bytes(range(1, 33)),
            heads=sorted(org.sim.ledger.heads()),
        )
        grant = _grant(org, descriptor, secret, old_credential)
        fold_at, ancestry = org.seams()
        with pytest.raises(AuthorityError):
            accept_grant(
                grant,
                fold_at,
                ancestry,
                old_credential,
                descriptor,
                known_credentials=[fresh_credential],
            )
        # Addressed to the current credential, same pool: accepted.
        good = _grant(org, descriptor, secret, fresh_credential)
        assert accept_grant(
            good,
            fold_at,
            ancestry,
            fresh_credential,
            descriptor,
            known_credentials=[old_credential],
        ).recipient_persona == org.recipient.public_hex

    def test_forged_grant_rejected_before_authority(self, org):
        descriptor, secret = org.mint(org.member)
        credential, _ = _credential(org, org.recipient)
        grant = _grant(org, descriptor, secret, credential)
        forged = dataclasses.replace(
            grant, signature=KeyPair.generate().sign_hex(grant.signing_input())
        )
        with pytest.raises(RecordSignatureError):
            accept_grant(forged, _bomb, _bomb, credential, descriptor)


# -- accept_object ---------------------------------------------------------------------


def _header(org, descriptor, author=None, heads=None):
    cek, body_nonce, wrap_nonce = os.urandom(32), os.urandom(12), os.urandom(12)
    ids = dict(
        genesis_id=org.gen,
        domain_id=org.dom,
        object_id=os.urandom(32).hex(),
        revision_id=os.urandom(32).hex(),
        storage_state_id=descriptor.state_id,
    )
    blob = object_header.seal_body(
        cek,
        b"body",
        body_suite_id=object_header.suites.BODY_SUITE_DEFAULT,
        body_nonce=body_nonce,
        **ids,
    )
    return object_header.build(
        author or org.member,
        b"\x11" * 32,  # any state secret; acceptance never unwraps
        cek,
        writer_authority_heads=heads or sorted(org.sim.ledger.heads()),
        body_suite_id=object_header.suites.BODY_SUITE_DEFAULT,
        body_nonce=body_nonce,
        wrap_nonce=wrap_nonce,
        ciphertext_hash=hashlib.sha256(blob).hexdigest(),
        **ids,
    )


class TestAcceptObject:
    def test_member_author_accepted(self, org):
        descriptor, _ = org.mint(org.member)
        header = _header(org, descriptor)
        fold_at, ancestry = org.seams()
        assert (
            accept_object(header, fold_at, ancestry, descriptor).author_member
            == org.member.public_hex
        )

    def test_non_member_author_and_mismatch_rejected(self, org):
        descriptor, _ = org.mint(org.member)
        fold_at, ancestry = org.seams()
        with pytest.raises(AuthorityError):
            accept_object(
                _header(org, descriptor, author=KeyPair.generate()),
                fold_at,
                ancestry,
                descriptor,
            )
        other_descriptor, _ = org.mint(org.member)
        with pytest.raises(AuthorityError):
            accept_object(
                _header(org, descriptor), fold_at, ancestry, other_descriptor
            )

    def test_pre_contraction_state_rejected(self, org):
        early_descriptor, _ = org.mint(org.member)  # covers nothing yet
        d = org.sim.delegate(org.sim.root, KeyPair.generate(), ["link:publish"])
        org.sim.revoke_event(org.sim.root, d)
        fold_at, ancestry = org.seams()
        header = _header(org, early_descriptor)  # written at the new frontier
        with pytest.raises(LossCoverageError):
            accept_object(header, fold_at, ancestry, early_descriptor)

    def test_forged_header_rejected_before_authority(self, org):
        descriptor, _ = org.mint(org.member)
        header = _header(org, descriptor)
        forged = dataclasses.replace(
            header, signature=KeyPair.generate().sign_hex(header.signing_input())
        )
        with pytest.raises(RecordSignatureError):
            accept_object(forged, _bomb, _bomb, descriptor)


# -- determinism -----------------------------------------------------------------------


def test_outcomes_are_order_independent(org):
    descriptor, _ = org.mint(org.agent)  # delegated creator, resolves to member
    fold_at, ancestry = org.seams()
    reference = accept_state(descriptor, fold_at, ancestry)

    events = org.sim.ledger.events()
    rng = random.Random(20260726)
    for _ in range(3):
        batch = list(events)
        rng.shuffle(batch)
        replica = Ledger()
        replica.ingest(batch)
        result = accept_state(
            descriptor,
            lambda heads: fold(replica, heads=list(heads)),
            lambda ids: replica.ancestry(ids),
        )
        assert result == reference


def test_resolve_member_key_direct(org):
    state_fold = org.sim.fold()
    assert resolve_member_key(state_fold, org.member.public_hex) == org.member.public_hex
    assert resolve_member_key(state_fold, org.agent.public_hex) == org.member.public_hex
    assert resolve_member_key(state_fold, org.sim.root.public_hex) is None
    assert resolve_member_key(state_fold, org.outside_leaf.public_hex) is None
