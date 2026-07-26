"""Access-loss projection (RequiredLossHeads) and the organization-content
domain identifier: per-type contractions, ancestor dominance, coverage-based
role.define judgement, validity guard, order independence, wrapping."""

from __future__ import annotations

import hashlib
import random

import pytest

from tools.network.idkit import KeyPair
from tools.network.ledger import Ledger, fold
from tools.network.ledger.errors import MalformedEventError
from tools.network.ledger.projections import (
    PROJECTION_NAMES,
    build_loss_heads,
    organization_content_domain_id,
    projection_bytes,
)
from tools.network.ledger.scopes import attenuates

from .conftest import Sim, random_events


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

    # A stale (below the effective version) redefinition that drops scopes
    # relative to a definition it outranks IS flagged — the rule is
    # deliberately over-inclusive in the safe direction (§6): it judges
    # against every definition in the frontier's closure, not just the
    # effective one.
    v_stale = sim.role_define(sim.root, "member", scope_set=[], version=3)
    assert sim.fold().loss_heads == (v_stale,)  # dominates its ancestor v2


def test_concurrent_branch_narrowing_is_flagged(sim):
    # Review repro L1: y's own ancestry sees only e1 (against which it
    # removes nothing), but at the merged frontier y is the effective
    # definition and drops link:revoke relative to the concurrent x.
    e1 = sim.role_define(sim.root, "member", scope_set=["link:publish"], version=1)
    x = sim.role_define(
        sim.root, "member", scope_set=["link:publish", "link:revoke"], version=2,
        parents=[e1],
    )
    y = sim.role_define(
        sim.root, "member", scope_set=["link:publish"], version=3, parents=[e1]
    )
    sim.checkpoint(sim.root, parents=[x, y])
    state = sim.fold()
    assert state.role_defs["member"].event_id == y
    assert state.loss_heads == (y,)


def test_equal_version_narrowing_is_flagged(sim):
    # Review repro L2: an equal-version redefinition never exceeds the
    # prior version, but it can still narrow the role. The frontier rule
    # flags it via any definition it outranks (here v1), independent of
    # which equal-version event wins the hash tie-break.
    sim.role_define(
        sim.root, "member", scope_set=["link:publish", "link:revoke"], version=1
    )
    d_a = sim.role_define(
        sim.root, "member", scope_set=["link:publish", "link:revoke"], version=2
    )
    d_b = sim.role_define(sim.root, "member", scope_set=["link:publish"], version=2)
    state = sim.fold()
    assert d_b in state.loss_heads
    assert d_a not in state.loss_heads
    assert state.loss_heads == (d_b,)


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


def test_invalid_define_rekey_and_rotate_are_absent(sim):
    stranger = KeyPair.generate()
    invite_key, persona, new_key = (KeyPair.generate() for _ in range(3))
    sim.role_define(
        sim.root, "member", scope_set=["link:publish", "link:revoke"], version=1
    )
    iid = sim.invite(sim.root, "member", invite_key=invite_key)
    sim.claim(iid, invite_key, persona)

    # Unauthorized narrowing redefinition: would be a contraction if valid.
    bad_define = sim.role_define(
        stranger, "member", scope_set=["link:publish"], version=2
    )
    # Rekey citing the wrong current key: inert.
    bad_rekey = sim.rekey(persona, persona, KeyPair.generate(), new_key)
    # Rotation signed by a non-root key: inert.
    bad_rotate = sim.rotate(stranger, KeyPair.generate())

    state = sim.fold()
    for eid in (bad_define, bad_rekey, bad_rotate):
        assert state.valid[eid] is False
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


def _independent_loss_heads(ledger, state) -> tuple:
    """Re-derive the projection from raw event types + ancestry only —
    independent of _Folder's internal record maps and caches."""
    ctx = ledger.ancestry(ledger.heads())

    def rank(ev):
        return (ev.payload["version"], tuple(-b for b in bytes.fromhex(ev.event_id)))

    defs = [
        ledger.get(eid)
        for eid in ctx
        if state.valid.get(eid) and ledger.get(eid).type == "role.define"
    ]
    candidates = set()
    for eid in ctx:
        if not state.valid.get(eid):
            continue
        ev = ledger.get(eid)
        if ev.type in ("revoke", "role.revoke", "member.rekey", "key.rotate"):
            candidates.add(eid)
        elif ev.type == "role.define":
            mine = frozenset(ev.payload["scope_set"])
            for other in defs:
                if other.event_id == eid or other.payload["name"] != ev.payload["name"]:
                    continue
                if rank(other) < rank(ev) and not attenuates(
                    frozenset(other.payload["scope_set"]), mine
                ):
                    candidates.add(eid)
                    break
    return tuple(
        sorted(
            c
            for c in candidates
            if not any(o != c and c in ledger.ancestry([o]) for o in candidates)
        )
    )


@pytest.mark.parametrize("seed", range(15))
def test_independent_rederivation_matches_fold(seed):
    events = random_events(seed, n=40)
    ledger = Ledger()
    ledger.ingest(list(events))
    state = fold(ledger)
    assert state.loss_heads == _independent_loss_heads(ledger, state)


# -- domain identifier -------------------------------------------------------------


def test_domain_identifier_rejects_malformed_input():
    for bad in ("", "AB" * 32, "1f" * 31, "zz" * 32, "  " + "1f" * 31, None, 42):
        with pytest.raises(MalformedEventError):
            organization_content_domain_id(bad)


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
