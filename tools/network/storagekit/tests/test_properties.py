"""The contract §16 property suite — the storage epic's verification gate.

Thirteen named properties over the transport-independent harness
(conftest ``World``/``drive``), every one exercised through the REAL
modules and the REAL authority fold: ordering, exclusion, availability,
continuity, and fail-closed behavior.
"""

from __future__ import annotations

import dataclasses
import os

import pytest

from tools.network.idkit import KeyPair
from tools.network.ledger import fold as ledger_fold
from tools.network.storagekit import (
    bridge as bridge_mod,
    capability,
    distribution,
    lifecycle,
    object_header,
    objects,
    state as state_mod,
)
from tools.network.storagekit.acceptance import (
    AuthorityError,
    FrontierRecencyError,
    LossCoverageError,
    ScopeError,
    loss_projection_digest,
)
from tools.network.storagekit.distribution import AT_RISK, COMMITTED, ORPHANED
from tools.network.storagekit.errors import RecordSignatureError
from tools.network.storagekit.lifecycle import StateAdvanceRequired
from tools.network.storagekit.objects import StateUnreachableError

from .conftest import World, drive


def _contract(world: World):
    """One access contraction the projection reports at the frontier."""
    d = world.sim.delegate(world.sim.root, KeyPair.generate(), ["link:publish"])
    return world.sim.revoke_event(world.sim.root, d)


# 1 ------------------------------------------------------------------------------------


def test_receive_order_independence(world):
    m0, m1 = world.member(0), world.member(1)
    s0, _ = world.mint_initial_state(m0)
    world.grant(m0, m1, s0)
    o0, _ = world.create_object(m0, b"pre-contraction body")
    _contract(world)
    s1, _ = world.advance(m0, s0)
    world.grant(m0, world.member(2), s1)
    o1, _ = world.create_object(m0, b"post-contraction body")

    frontier = world.frontier()
    reference_fold = world.fold(frontier)
    reference_members = frozenset(reference_fold.members)
    reference_loss = reference_fold.loss_heads
    feed = world.full_feed()
    creds = world._credentials_by_kem_id

    reference_outcomes = None
    for seed in range(20):
        ledger, stores, undelivered = drive(feed, seed, creds)
        assert undelivered == []
        assert stores.rejected == []
        replica_fold = ledger_fold(ledger, heads=frontier)
        assert frozenset(replica_fold.members) == reference_members
        assert replica_fold.loss_heads == reference_loss
        outcomes = {}
        for persona_hex, entry in world.principals.items():
            for header, _body in stores.object_store.values():
                result = world.read(entry["held"], header, stores=stores)
                key = (persona_hex, header.ciphertext_hash)
                outcomes[key] = result if isinstance(result, bytes) else result.__name__
        if reference_outcomes is None:
            reference_outcomes = outcomes
        assert outcomes == reference_outcomes


# 2 ------------------------------------------------------------------------------------


def test_state_coverage_of_required_contractions(world):
    m0 = world.member(0)
    s0, _ = world.mint_initial_state(m0)
    _contract(world)
    s1, _ = world.advance(m0, s0)
    header, _ = world.create_object(m0, b"covered body")
    writer_fold = world.fold(header.writer_authority_heads)
    referenced = world.stores.kc.states[header.storage_state_id]
    assert referenced.state_id == s1.state_id
    assert lifecycle.state_covers(referenced, writer_fold.loss_heads, world.ancestry)


# 3 ------------------------------------------------------------------------------------


def test_expansion_without_state_advance(world):
    m0 = world.member(0)
    s0, _ = world.mint_initial_state(m0)
    old_headers = [world.create_object(m0, body)[0] for body in (b"one", b"two")]
    states_before = dict(world.stores.kc.states)

    newcomer = world.admit(seed_index=40)  # authority-stream expansion
    world.grant(m0, newcomer, s0)  # the current head — no state advance

    for header in old_headers:
        assert world.read(world.held(newcomer), header) == (
            b"one" if header is old_headers[0] else b"two"
        )
    assert dict(world.stores.kc.states) == states_before  # descriptor set unchanged


# 4 ------------------------------------------------------------------------------------


def test_removal_exclusion(world):
    m0, m1 = world.member(0), world.member(1)
    s0, _ = world.mint_initial_state(m0)
    world.grant(m0, m1, s0)
    pre_header, _ = world.create_object(m0, b"pre-removal body")

    world.remove(m1)  # snapshots m1's retained state, then revokes
    snapshot = world.snapshots[m1.public_hex]
    s1, _ = world.advance(m0, s0)
    post_header, _ = world.create_object(m0, b"post-removal body")

    assert world.read(snapshot, post_header) is StateUnreachableError
    assert world.read(snapshot, pre_header) == b"pre-removal body"


# 5 ------------------------------------------------------------------------------------


def test_concurrent_removal_then_union():
    world = World(member_count=4)
    m0, m1, m2, m3 = (world.member(i) for i in range(4))
    s0, _ = world.mint_initial_state(m0)
    for recipient in (m1, m2, m3):
        world.grant(m0, recipient, s0)

    base = world.frontier()
    r2 = world.remove(m2, parents=base)
    r3 = world.remove(m3, parents=base)  # concurrent with r2

    sa, _ = world.advance(m0, s0, heads=[r2])
    sb, _ = world.advance(m1, s0, heads=[r3])
    world.grant(m1, m0, sb)  # cross-grant so m0 can union
    su, su_secret = world.union(m0, [sa, sb])

    recovered = bridge_mod.recover_ancestors(
        su.state_id, su_secret, list(world.stores.kc.bridges), dict(world.stores.kc.states)
    )
    assert recovered[sa.state_id] == world.held(m0)[sa.state_id]
    assert recovered[sb.state_id] == world.held(m0)[sb.state_id]

    header, _ = world.create_object(m0, b"union body")
    assert header.storage_state_id == su.state_id
    for removed in (m2, m3):
        snapshot = world.snapshots[removed.public_hex]
        assert su.state_id not in snapshot
        assert world.read(snapshot, header) is StateUnreachableError


# 6 ------------------------------------------------------------------------------------


def test_no_stale_fallback(world):
    m0 = world.member(0)
    s0, _ = world.mint_initial_state(m0)
    header, _ = world.create_object(m0, b"still readable")
    _contract(world)  # now uncovered by every available state
    with pytest.raises(StateAdvanceRequired):
        world.create_object(m0, b"must not be written")
    assert world.read(world.held(m0), header) == b"still readable"


# 7 ------------------------------------------------------------------------------------


def test_missing_bridge_degrades_gracefully(world):
    m0 = world.member(0)
    s0, _ = world.mint_initial_state(m0)
    s0_header, _ = world.create_object(m0, b"ancestor body")
    _contract(world)

    # Advance but withhold the bridge from the feed.
    s1, s1_secret = world.advance(m0, s0, offer_bridges=False)
    assert world.stores.kc.history_complete[s1.state_id] is False  # degraded, no raise

    # Ancestor recovery from a principal holding ONLY the new state raises.
    assert world.read({s1.state_id: s1_secret}, s0_header) is StateUnreachableError

    # A following advance is accepted (bridge included this time).
    _contract(world)
    s2, _ = world.advance(m0, s1)
    assert world.stores.kc.history_complete[s2.state_id] is True


# 8 ------------------------------------------------------------------------------------


def test_injection_and_rollback_rejection(world):
    m0, m1 = world.member(0), world.member(1)
    s0, _ = world.mint_initial_state(m0)
    contraction = _contract(world)
    s1, _ = world.advance(m0, s0)
    stranger = KeyPair.generate()
    f = world.fold()

    fake_state, _ = state_mod.generate(
        stranger, world.gen, world.dom, (), sorted(f.heads),
        sorted(f.loss_heads), loss_projection_digest(f),
    )
    assert world.offer(("state", (fake_state, ()))) is True
    assert fake_state.state_id not in world.stores.kc.states
    assert isinstance(world.stores.rejected[-1][1], ScopeError)

    fake_grant = capability.issue(
        stranger,
        genesis_id=world.gen, domain_id=world.dom,
        storage_state_id=s1.state_id,
        recipient_credential=world.principals[m1.public_hex]["credential"],
        state_secret=os.urandom(32),
        state_secret_commitment=s1.secret_commitment,
        authority_heads=world.frontier(),
    )
    grants_before = len(world.stores.kc.grants)
    world.offer(("grant", fake_grant))
    assert len(world.stores.kc.grants) == grants_before
    assert isinstance(world.stores.rejected[-1][1], ScopeError)

    real_bridge = world.stores.kc.bridges[0]
    forged_bridge = dataclasses.replace(
        real_bridge, signature=stranger.sign_hex(real_bridge.signing_input())
    )
    bridges_before = len(world.stores.kc.bridges)
    world.offer(("bridge", forged_bridge))
    assert len(world.stores.kc.bridges) == bridges_before
    assert isinstance(world.stores.rejected[-1][1], RecordSignatureError)

    # A fabricated object header from outside the roster.
    body = object_header.seal_body(
        os.urandom(32), b"x", body_suite_id="aes-256-gcm-siv",
        body_nonce=os.urandom(12), genesis_id=world.gen, domain_id=world.dom,
        object_id=os.urandom(32).hex(), revision_id=os.urandom(32).hex(),
        storage_state_id=s1.state_id,
    )
    import hashlib

    fake_header = object_header.build(
        stranger, os.urandom(32), os.urandom(32),
        genesis_id=world.gen, domain_id=world.dom,
        object_id=os.urandom(32).hex(), revision_id=os.urandom(32).hex(),
        storage_state_id=s1.state_id,
        writer_authority_heads=world.frontier(),
        body_suite_id="aes-256-gcm-siv", body_nonce=os.urandom(12),
        wrap_nonce=os.urandom(12), ciphertext_hash=hashlib.sha256(body).hexdigest(),
    )
    world.offer(("object", (fake_header, body)))
    assert fake_header.ciphertext_hash not in world.stores.object_store
    assert isinstance(world.stores.rejected[-1][1], AuthorityError)

    # Rolled-back frontier: a member writing under the superseded state
    # while the frontier carries the contraction.
    stale_header = object_header.build(
        m0, world.held(m0)[s0.state_id], os.urandom(32),
        genesis_id=world.gen, domain_id=world.dom,
        object_id=os.urandom(32).hex(), revision_id=os.urandom(32).hex(),
        storage_state_id=s0.state_id,
        writer_authority_heads=world.frontier(),
        body_suite_id="aes-256-gcm-siv", body_nonce=os.urandom(12),
        wrap_nonce=os.urandom(12), ciphertext_hash=hashlib.sha256(body).hexdigest(),
    )
    world.offer(("object", (stale_header, body)))
    assert stale_header.ciphertext_hash not in world.stores.object_store
    assert isinstance(world.stores.rejected[-1][1], LossCoverageError)
    assert contraction in world.fold().loss_heads  # the frontier really contracted


# 9 ------------------------------------------------------------------------------------


def test_possession_tag_rejection(world):
    m0, m1 = world.member(0), world.member(1)
    s0, s0_secret = world.mint_initial_state(m0)
    pretender = capability.issue_receipt(
        m1, genesis_id=world.gen, domain_id=world.dom,
        storage_state_id=s0.state_id, state_secret=os.urandom(32),
    )
    world.offer(("receipt", pretender))  # signed: stored raw

    with pytest.raises(capability.PossessionTagError):
        capability.verify_possession_tag(pretender, s0_secret)
    status = world.commit_status(
        s0.state_id, state_secrets={s0.state_id: s0_secret}
    )
    assert m1.public_hex not in status.holders  # contributes to no holder set
    assert status.holders == frozenset({m0.public_hex})
    assert status.status == AT_RISK  # and therefore to no commit status


# 10 -----------------------------------------------------------------------------------


def test_frontier_recency_rejection(world):
    m0, m1 = world.member(0), world.member(1)
    _contract(world)
    s0, s0_secret = world.mint_initial_state(m0)  # cites the current frontier
    stale_grant = capability.issue(
        m0,
        genesis_id=world.gen, domain_id=world.dom,
        storage_state_id=s0.state_id,
        recipient_credential=world.principals[m1.public_hex]["credential"],
        state_secret=s0_secret,
        state_secret_commitment=s0.secret_commitment,
        authority_heads=[world.gen],  # omits the descriptor's heads
    )
    grants_before = len(world.stores.kc.grants)
    world.offer(("grant", stale_grant))
    assert len(world.stores.kc.grants) == grants_before
    assert isinstance(world.stores.rejected[-1][1], FrontierRecencyError)


# 11 -----------------------------------------------------------------------------------


def test_commit_status_across_partition_merge_and_orphaning():
    world = World(member_count=4)
    m0, m1, m2, m3 = (world.member(i) for i in range(4))
    s0, s0_secret = world.mint_initial_state(m0)
    world.grant(m0, m1, s0)
    world.create_object(m0, b"under s0")
    assert world.commit_status(s0.state_id).status == COMMITTED  # m0 + m1, threshold 2

    base = world.frontier()
    r2 = world.remove(m2, parents=base)
    r3 = world.remove(m3, parents=base)
    sa, _ = world.advance(m0, s0, heads=[r2])
    sb, _ = world.advance(m1, s0, heads=[r3])
    # During the partition each branch has one holder: AT_RISK, never worse.
    for branch in (sa, sb):
        status = world.commit_status(branch.state_id)
        assert (status.status, status.threshold) == (AT_RISK, 2)

    world.grant(m1, m0, sb)
    su, _ = world.union(m0, [sa, sb])
    world.grant(m0, m1, su)  # cross-grant + receipt heals both branches
    world.create_object(m0, b"under the union")
    for state in (sa, sb, su):
        assert world.commit_status(state.state_id).status == COMMITTED

    # Orphaning: a side branch whose sole recorded holder leaves.
    sc, _ = world.advance(m1, sb)
    assert world.commit_status(sc.state_id).status == AT_RISK
    world.remove(m1)
    assert world.commit_status(sc.state_id).status == ORPHANED
    assert world.commit_status(su.state_id).status == COMMITTED  # m0 alone, threshold 1

    # Per-state counters equal admitted counts, no object-row scan.
    assert world.stores.counters == {s0.state_id: 1, su.state_id: 1}


# 12 -----------------------------------------------------------------------------------


def test_storage_ancestry_domination():
    world = World(member_count=4)
    m0, m1 = world.member(0), world.member(1)
    s0, _ = world.mint_initial_state(m0)
    world.grant(m0, m1, s0)
    base = world.frontier()
    r2 = world.remove(world.member(2), parents=base)
    r3 = world.remove(world.member(3), parents=base)
    sa, _ = world.advance(m0, s0, heads=[r2])
    sb, _ = world.advance(m1, s0, heads=[r3])
    world.grant(m1, m0, sb)
    su, _ = world.union(m0, [sa, sb])

    ancestry = world.stores.kc.ancestry
    before = ancestry([su.state_id])
    assert {sa.state_id, sb.state_id, s0.state_id} <= before  # union dominates both
    assert sb.state_id not in ancestry([sa.state_id])  # neither branch dominates
    assert sa.state_id not in ancestry([sb.state_id])

    # Object headers contribute no heads: admitting one changes nothing.
    header, _ = world.create_object(m0, b"body under union")
    assert ancestry([su.state_id]) == before
    assert header.ciphertext_hash not in world.stores.kc.states
    assert ancestry([header.ciphertext_hash]) == frozenset({header.ciphertext_hash})


# 13 -----------------------------------------------------------------------------------


def test_receipt_frontier_compaction_invariance():
    world = World(member_count=4)
    m0, m1 = world.member(0), world.member(1)
    s0, _ = world.mint_initial_state(m0)
    world.grant(m0, m1, s0)
    base = world.frontier()
    r2 = world.remove(world.member(2), parents=base)
    r3 = world.remove(world.member(3), parents=base)
    sa, _ = world.advance(m0, s0, heads=[r2])
    sb, _ = world.advance(m1, s0, heads=[r3])
    world.grant(m1, m0, sb)
    su, _ = world.union(m0, [sa, sb])
    world.grant(m0, m1, su)

    full = list(world.stores.receipt_store.receipts)
    compacted = world.stores.receipt_store.compacted(world.stores.kc.ancestry)
    assert len(compacted) < len(full)  # something was actually behind a frontier

    fold_state = world.fold()
    descriptors = dict(world.stores.kc.states)
    for state_id in descriptors:
        assert distribution.branch_holders(
            state_id, compacted, descriptors, fold_state
        ) == distribution.branch_holders(state_id, full, descriptors, fold_state)
        assert distribution.commit_status(
            state_id, compacted, descriptors, fold_state
        ) == distribution.commit_status(state_id, full, descriptors, fold_state)
