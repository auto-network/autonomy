"""Access-loss projection (RequiredLossHeads) and the organization-content
domain identifier: per-type contractions, ancestor dominance, coverage-based
role.define judgement, validity guard, order independence, wrapping."""

from __future__ import annotations

import hashlib
import random

from tools.network.idkit import KeyPair
from tools.network.ledger import Ledger, fold
from tools.network.ledger.projections import (
    PROJECTION_NAMES,
    build_loss_heads,
    organization_content_domain_id,
    projection_bytes,
)

from .conftest import Sim


# -- per-type contraction (each appears when it is the maximal leaf) ------------


def test_event_target_revoke_is_a_loss_head(sim):
    d = sim.delegate(sim.root, KeyPair.generate(), ["link:publish"])
    r = sim.revoke_event(sim.root, d)
    assert sim.fold().loss_heads == (r,)


def test_key_target_revoke_is_a_loss_head(sim):
    a = KeyPair.generate()
    sim.delegate(sim.root, a, ["link:publish"])
    r = sim.revoke_key(sim.root, a)
    assert sim.fold().loss_heads == (r,)


def test_role_revoke_is_a_loss_head(sim):
    persona = KeyPair.generate()
    sim.role_define(sim.root, "member", scope_set=["link:publish"])
    sim.role_grant(sim.root, persona, "member")
    rr = sim.role_revoke(sim.root, persona, "member")
    assert sim.fold().loss_heads == (rr,)


def test_member_rekey_is_a_loss_head(sim):
    invite_key, persona, new_key = (KeyPair.generate() for _ in range(3))
    sim.role_define(sim.root, "member", scope_set=["link:publish"])
    iid = sim.invite(sim.root, "member", invite_key=invite_key)
    sim.claim(iid, invite_key, persona)
    rk = sim.rekey(persona, persona, persona, new_key)
    state = sim.fold()
    assert state.members[persona.public_hex].current_key == new_key.public_hex
    assert state.loss_heads == (rk,)


def test_key_rotate_is_a_loss_head(sim):
    new_root = KeyPair.generate()
    rot = sim.rotate(sim.root, new_root)
    state = sim.fold()
    assert state.root == new_root.public_hex
    assert state.loss_heads == (rot,)


# -- maximality over the ancestry map -------------------------------------------


def test_ancestor_contraction_is_dominated_across_types(sim):
    d = sim.delegate(sim.root, KeyPair.generate(), ["link:publish"])
    r = sim.revoke_event(sim.root, d)
    rr = sim.role_revoke(sim.root, KeyPair.generate(), "member")
    rot = sim.rotate(sim.root, KeyPair.generate())
    # r < rr < rot causally: only the terminal contraction survives.
    assert sim.fold().loss_heads == (rot,)


def test_concurrent_contractions_are_all_heads(sim):
    a, b = KeyPair.generate(), KeyPair.generate()
    d1 = sim.delegate(sim.root, a, ["link:publish"])
    d2 = sim.delegate(sim.root, b, ["link:revoke"], parents=[d1])
    r1 = sim.revoke_event(sim.root, d1, parents=[d2])
    r2 = sim.revoke_key(sim.root, b, parents=[d2])
    state = sim.fold()
    assert state.loss_heads == tuple(sorted((r1, r2)))


# -- role.define: coverage judgement, not raw set difference ---------------------


def test_role_define_scope_removal(sim):
    v1 = sim.role_define(
        sim.root, "member", scope_set=["link:publish", "link:revoke"], version=1
    )
    assert sim.fold().loss_heads == ()  # first definition is never a contraction

    v2 = sim.role_define(sim.root, "member", scope_set=["link:publish"], version=2)
    assert sim.fold().loss_heads == (v2,)  # drops link:revoke

    # Coverage-tested: the raw sets differ, but link:* covers link:publish,
    # so this redefinition removes nothing (no role-scope smuggling).
    v3 = sim.role_define(sim.root, "member", scope_set=["link:*"], version=3)
    assert sim.fold().loss_heads == (v2,)

    # Widening never contracts.
    v4 = sim.role_define(
        sim.root, "member", scope_set=["link:*", "tunnel:serve"], version=4
    )
    assert sim.fold().loss_heads == (v2,)

    # A stale (non-superseding) redefinition with an empty scope set is not
    # effective and therefore not a contraction.
    sim.role_define(sim.root, "member", scope_set=[], version=3)
    assert sim.fold().loss_heads == (v2,)


# -- expansions, neutral events, invalid contractions ----------------------------


def test_expansions_and_neutral_events_never_appear(sim):
    invite_key, persona = KeyPair.generate(), KeyPair.generate()
    sim.role_define(sim.root, "member", scope_set=["link:publish"])
    sim.delegate(sim.root, KeyPair.generate(), ["link:publish"])
    sim.role_grant(sim.root, persona, "member")
    iid = sim.invite(sim.root, "member", invite_key=invite_key)
    sim.claim(iid, invite_key, KeyPair.generate())
    sim.checkpoint(sim.root)
    assert sim.fold().loss_heads == ()


def test_issuance_invalid_contractions_are_absent(sim):
    stranger = KeyPair.generate()
    d = sim.delegate(sim.root, KeyPair.generate(), ["link:publish"])
    bad_revoke = sim.revoke_event(stranger, d)
    bad_role_revoke = sim.role_revoke(stranger, KeyPair.generate(), "member")
    state = sim.fold()
    assert state.valid[bad_revoke] is False
    assert state.valid[bad_role_revoke] is False
    assert state.loss_heads == ()


# -- determinism ------------------------------------------------------------------


def _concurrent_sim() -> Sim:
    sim = Sim()
    a, b = KeyPair.generate(), KeyPair.generate()
    d1 = sim.delegate(sim.root, a, ["link:publish"])
    d2 = sim.delegate(sim.root, b, ["link:revoke"], parents=[d1])
    r1 = sim.revoke_event(sim.root, d1, parents=[d2])
    sim.revoke_key(sim.root, b, parents=[d2])  # concurrent with r1
    sim.role_revoke(sim.root, a, "member", parents=[r1])  # dominates r1 only
    return sim


def test_loss_heads_sorted_and_order_independent():
    sim = _concurrent_sim()
    ref_state = sim.fold()
    assert ref_state.loss_heads == tuple(sorted(ref_state.loss_heads))
    ref_bytes = projection_bytes(build_loss_heads(ref_state))

    events = sim.ledger.events()
    rng = random.Random(20260726)
    for _trial in range(4):
        batch = list(events)
        rng.shuffle(batch)
        replica = Ledger()
        replica.ingest(batch)
        state = fold(replica)
        assert state.loss_heads == ref_state.loss_heads
        assert projection_bytes(build_loss_heads(state)) == ref_bytes


# -- domain identifier -------------------------------------------------------------


def test_domain_identifier_formula(sim):
    state = sim.fold()
    expected = hashlib.sha256(
        b"autonomy/storage-domain/v1"
        + state.genesis_id.encode("ascii")
        + b"organization-content"
    ).hexdigest()
    assert organization_content_domain_id(state.genesis_id) == expected


def test_domain_identifier_stable_across_rotation(sim):
    before = organization_content_domain_id(sim.fold().genesis_id)
    new_root = KeyPair.generate()
    sim.rotate(sim.root, new_root)
    state = sim.fold()
    assert state.root == new_root.public_hex
    assert organization_content_domain_id(state.genesis_id) == before


# -- projection wrapping -------------------------------------------------------------


def test_build_loss_heads_projection(sim):
    d = sim.delegate(sim.root, KeyPair.generate(), ["link:publish"])
    r = sim.revoke_event(sim.root, d)
    state = sim.fold()
    proj = build_loss_heads(state)
    assert proj["projection"] == "loss-heads"
    assert proj["org"] == state.org
    assert proj["heads"] == list(state.heads)
    assert proj["fingerprint"] == state.fingerprint()
    assert proj["body"]["domain_id"] == organization_content_domain_id(state.genesis_id)
    assert proj["body"]["loss_heads"] == [r]
    assert isinstance(projection_bytes(proj), bytes)
    # The dashboard read-model schema is unaffected.
    assert PROJECTION_NAMES == ("live-keys", "roles", "roster")
