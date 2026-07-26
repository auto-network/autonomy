"""Distribution: head grants, eager provisioning, receipts, commit status."""

from __future__ import annotations

import hashlib
import os
import random

import pytest

from tools.network.idkit import KeyPair
from tools.network.ledger.projections import organization_content_domain_id
from tools.network.ledger.tests.conftest import Sim
from tools.network.storagekit import capability, credentials, object_header, state
from tools.network.storagekit.acceptance import accept_object, loss_projection_digest
from tools.network.storagekit.distribution import (
    AT_RISK,
    COMMITTED,
    ORPHANED,
    branch_holders,
    collect_receipts,
    commit_status,
    grant_current_head,
    provision_missing,
)
from tools.network.storagekit.errors import CommitmentError, StorageError

HLC0 = (1_800_000_000_000, 0)


class Org:
    """Founder + two members (threshold 2), credentials, a state DAG."""

    def __init__(self):
        self.sim = Sim()
        s = self.sim
        s.role_define(s.root, "member", scope_set=["link:publish"])
        self.founder, self.founder_claim = self._admit()
        self.m1, self.m1_claim = self._admit()
        self.m2, self.m2_claim = self._admit()
        self.gen = s.genesis_id
        self.dom = organization_content_domain_id(self.gen)
        self.creds = {}
        self.kem_privates = {}
        for i, persona in enumerate((self.founder, self.m1, self.m2)):
            cred, priv = credentials.build(
                persona, self.gen, bytes(range(i, i + 32)), [self.gen], HLC0
            )
            self.creds[persona.public_hex] = cred
            self.kem_privates[persona.public_hex] = priv
        # State DAG: base <- h1, base <- h2, {h1,h2} <- union.
        self.base, self.s_base = self.mint(self.m1)
        self.h1, self.s_h1 = self.mint(self.m1, parents=[self.base.state_id])
        self.h2, self.s_h2 = self.mint(self.m2, parents=[self.base.state_id])
        self.union, self.s_union = self.mint(
            self.m1, parents=sorted([self.h1.state_id, self.h2.state_id])
        )
        self.descriptors = {
            d.state_id: d for d in (self.base, self.h1, self.h2, self.union)
        }

    def _admit(self):
        persona, invite_key = KeyPair.generate(), KeyPair.generate()
        iid = self.sim.invite(self.sim.root, "member", invite_key=invite_key)
        return persona, self.sim.claim(iid, invite_key, persona)

    def mint(self, creator, parents=()):
        heads = sorted(self.sim.ledger.heads())
        f = self.sim.fold(heads=heads)
        return state.generate(
            creator, self.gen, self.dom, parents, heads,
            sorted(f.loss_heads), loss_projection_digest(f),
        )

    def fold(self):
        return self.sim.fold()

    def receipt(self, persona, descriptor, secret):
        return capability.issue_receipt(
            persona,
            genesis_id=self.gen,
            domain_id=self.dom,
            storage_state_id=descriptor.state_id,
            state_secret=secret,
        )

    def frontier(self):
        return sorted(self.sim.ledger.heads())


@pytest.fixture
def org() -> Org:
    return Org()


# -- grant_current_head ------------------------------------------------------------


def test_grant_current_head_roundtrip(org):
    cred = org.creds[org.m2.public_hex]
    grant = grant_current_head(org.m1, org.dom, cred, org.h1, org.s_h1, org.frontier())
    assert grant.storage_state_id == org.h1.state_id
    assert grant.state_secret_commitment == org.h1.secret_commitment
    assert grant.recipient_kem_key_id == cred.kem_key_id
    assert grant.authority_heads == tuple(org.frontier())
    recovered = capability.accept(
        grant, org.kem_privates[org.m2.public_hex], org.h1
    )
    assert recovered == org.s_h1


def test_grant_current_head_refusals(org):
    cred = org.creds[org.m2.public_hex]
    with pytest.raises(StorageError):
        grant_current_head(org.m1, "9c" * 32, cred, org.h1, org.s_h1, org.frontier())
    with pytest.raises(CommitmentError):  # secret is not the named head's
        grant_current_head(org.m1, org.dom, cred, org.h1, org.s_h2, org.frontier())


# -- provision_missing --------------------------------------------------------------


def test_provision_missing_mint_set(org):
    fold_state = org.fold()
    creds = {
        org.founder.public_hex: org.creds[org.founder.public_hex],
        org.m1.public_hex: org.creds[org.m1.public_hex],
        # m2 has published no credential: skipped, surfaces via status.
    }
    existing = [
        grant_current_head(
            org.m1, org.dom, org.creds[org.m1.public_hex], org.h1, org.s_h1,
            org.frontier(),
        )
    ]
    minted = provision_missing(
        org.m1, org.dom, fold_state, creds,
        [org.h1, org.h2],  # h2's secret is not held: skipped entirely
        {org.h1.state_id: org.s_h1},
        existing, org.frontier(),
    )
    assert len(minted) == 1  # founder x h1 only
    assert minted[0].recipient_kem_key_id == org.creds[org.founder.public_hex].kem_key_id
    assert minted[0].storage_state_id == org.h1.state_id

    # Nothing left to mint once the minted grants are recorded.
    assert provision_missing(
        org.m1, org.dom, fold_state, creds, [org.h1],
        {org.h1.state_id: org.s_h1}, list(existing) + list(minted), org.frontier(),
    ) == ()


def test_provision_missing_covers_newly_admitted(org):
    newcomer, invite_key = KeyPair.generate(), KeyPair.generate()
    iid = org.sim.invite(org.sim.root, "member", invite_key=invite_key)
    org.sim.claim(iid, invite_key, newcomer)
    new_cred, _ = credentials.build(
        newcomer, org.gen, bytes(range(7, 39)), [org.gen], HLC0
    )
    creds = {newcomer.public_hex: new_cred}
    # Invoked from the approval act: mints for the just-admitted persona.
    minted = provision_missing(
        org.m1, org.dom, org.fold(), creds, [org.h1],
        {org.h1.state_id: org.s_h1}, [], org.frontier(),
    )
    assert [g.recipient_kem_key_id for g in minted] == [new_cred.kem_key_id]


# -- collect_receipts ----------------------------------------------------------------


def test_persona_level_counting_and_tag_filter(org):
    r_m1a = org.receipt(org.m1, org.h1, org.s_h1)
    r_m1b = org.receipt(org.m1, org.h1, org.s_h1)  # second device
    r_m2 = org.receipt(org.m2, org.h1, org.s_h1)
    pretender = org.receipt(org.founder, org.h1, os.urandom(32))
    fold_state = org.fold()

    receivers = collect_receipts(
        org.h1.state_id, [r_m1a, r_m1b, r_m2, pretender], fold_state
    )
    # Without the secret the pretender's tag cannot be checked...
    assert receivers == frozenset(
        {org.m1.public_hex, org.m2.public_hex, org.founder.public_hex}
    )
    # ...with it, a signed-but-unproven receipt does not count.
    proven = collect_receipts(
        org.h1.state_id,
        [r_m1a, r_m1b, r_m2, pretender],
        fold_state,
        state_secret=org.s_h1,
    )
    assert proven == frozenset({org.m1.public_hex, org.m2.public_hex})


def test_receipt_exclusions(org):
    import dataclasses

    good = org.receipt(org.m1, org.h1, org.s_h1)
    forged = dataclasses.replace(
        good, signature=KeyPair.generate().sign_hex(good.signing_input())
    )
    non_canonical = good.to_json().decode("ascii").replace(":", ": ", 1).encode("ascii")
    cross_state = org.receipt(org.m1, org.h2, org.s_h2)
    other_org = Org()
    cross_org = capability.issue_receipt(
        org.m1,
        genesis_id=other_org.gen,
        domain_id=other_org.dom,
        storage_state_id=org.h1.state_id,
        state_secret=org.s_h1,
    )
    stranger = org.receipt(KeyPair.generate(), org.h1, org.s_h1)

    org.sim.revoke_event(org.sim.root, org.m2_claim)  # revoked persona
    revoked = org.receipt(org.m2, org.h1, org.s_h1)
    org.sim.rekey(org.m1, org.m1, org.m1, KeyPair.generate())  # retire m1's key
    fold_state = org.fold()

    receivers = collect_receipts(
        org.h1.state_id,
        [good, forged, non_canonical, cross_state, cross_org, stranger, revoked],
        fold_state,
        state_secret=org.s_h1,
    )
    # good is now signed by a rekey-retired key: excluded with the rest.
    assert receivers == frozenset()


# -- branch holders and commit status ---------------------------------------------------


def test_descendant_receipt_subsumes_ancestors(org):
    fold_state = org.fold()
    r_union = org.receipt(org.m2, org.union, org.s_union)
    holders = branch_holders(
        org.h1.state_id, [r_union], org.descriptors, fold_state
    )
    # m2 receipted the union (descendant of h1); creators count too:
    # h1's creator m1 and union's creator m1 collapse to one persona.
    assert holders == frozenset({org.m1.public_hex, org.m2.public_hex})
    # A receipt on h2's branch does not leak onto h1's disjoint ancestor set.
    assert branch_holders(
        org.h2.state_id, [r_union], org.descriptors, fold_state
    ) == frozenset({org.m2.public_hex, org.m1.public_hex})


def test_commit_status_thresholds(org):
    fold_state = org.fold()
    # Creator alone in a three-member org: AT_RISK, threshold 2.
    alone = commit_status(org.h1.state_id, [], org.descriptors, fold_state)
    assert (alone.status, alone.threshold) == (AT_RISK, 2)
    assert alone.holders == frozenset({org.m1.public_hex})
    # A second qualifying holder commits the branch.
    r_m2 = org.receipt(org.m2, org.h1, org.s_h1)
    two = commit_status(
        org.h1.state_id, [r_m2], org.descriptors, fold_state,
        state_secrets={org.h1.state_id: org.s_h1},
    )
    assert two.status == COMMITTED
    assert two.holders == frozenset({org.m1.public_hex, org.m2.public_hex})


def test_single_member_org_commits_on_creator_alone():
    solo = Org.__new__(Org)
    solo.sim = Sim()
    solo.sim.role_define(solo.sim.root, "member", scope_set=["link:publish"])
    founder, invite_key = KeyPair.generate(), KeyPair.generate()
    iid = solo.sim.invite(solo.sim.root, "member", invite_key=invite_key)
    solo.sim.claim(iid, invite_key, founder)
    solo.gen = solo.sim.genesis_id
    solo.dom = organization_content_domain_id(solo.gen)
    descriptor, secret = Org.mint(solo, founder)
    status = commit_status(
        descriptor.state_id, [], {descriptor.state_id: descriptor}, solo.sim.fold()
    )
    assert (status.status, status.threshold) == (COMMITTED, 1)
    assert status.holders == frozenset({founder.public_hex})


def test_orphaned_when_every_holder_leaves(org):
    r_m2 = org.receipt(org.m2, org.h1, org.s_h1)
    org.sim.revoke_event(org.sim.root, org.m1_claim)  # creator removed
    org.sim.revoke_event(org.sim.root, org.m2_claim)  # receipted holder removed
    status = commit_status(org.h1.state_id, [r_m2], org.descriptors, org.fold())
    assert status.status == ORPHANED
    assert status.holders == frozenset()


def test_status_is_acknowledgment_not_a_gate(org):
    """Ruling D pinned: an under-receipted state still WRITES — the object
    header is accepted and would store/replicate — only its status says
    AT_RISK. There is no write-eligibility predicate in this module."""
    fold_state = org.fold()
    cek, body_nonce, wrap_nonce = os.urandom(32), os.urandom(12), os.urandom(12)
    ids = dict(
        genesis_id=org.gen,
        domain_id=org.dom,
        object_id=os.urandom(32).hex(),
        revision_id=os.urandom(32).hex(),
        storage_state_id=org.h1.state_id,
    )
    blob = object_header.seal_body(
        cek, b"under-receipted but fully writable",
        body_suite_id=object_header.suites.BODY_SUITE_DEFAULT,
        body_nonce=body_nonce, **ids,
    )
    header = object_header.build(
        org.m1, org.s_h1, cek,
        writer_authority_heads=org.frontier(),
        body_suite_id=object_header.suites.BODY_SUITE_DEFAULT,
        body_nonce=body_nonce, wrap_nonce=wrap_nonce,
        ciphertext_hash=hashlib.sha256(blob).hexdigest(), **ids,
    )
    accepted = accept_object(
        header,
        lambda heads: org.sim.fold(heads=list(heads)),
        lambda i: org.sim.ledger.ancestry(i),
        org.h1,
    )
    assert accepted.author_member == org.m1.public_hex  # the write stands
    status = commit_status(org.h1.state_id, [], org.descriptors, fold_state)
    assert status.status == AT_RISK  # only the acknowledgment lags


def test_order_independence_and_no_mutation(org):
    fold_state = org.fold()
    receipts = [
        org.receipt(org.m2, org.h1, org.s_h1),
        org.receipt(org.m1, org.h1, org.s_h1),
        org.receipt(org.m2, org.union, org.s_union),
        org.receipt(org.founder, org.h1, os.urandom(32)),  # unproven
    ]
    snapshot = list(receipts)
    reference = commit_status(
        org.h1.state_id, receipts, org.descriptors, fold_state,
        state_secrets={org.h1.state_id: org.s_h1},
    )
    rng = random.Random(20260726)
    for _ in range(4):
        rng.shuffle(receipts)
        assert commit_status(
            org.h1.state_id, receipts, org.descriptors, fold_state,
            state_secrets={org.h1.state_id: org.s_h1},
        ) == reference
    assert sorted(r.receipt_id for r in receipts) == sorted(
        r.receipt_id for r in snapshot
    )
