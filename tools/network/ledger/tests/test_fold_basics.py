"""Fold fundamentals: root authority, queries, fingerprints, TTL, heads."""

from __future__ import annotations

import pytest

from tools.network.idkit import KeyPair
from tools.network.ledger import GenesisError, Ledger, fold

from .conftest import Sim


def test_genesis_only_root_holds_universe(sim):
    state = sim.fold()
    assert state.root == sim.root.public_hex
    assert state.holds(sim.root.public_hex, "anything:at:all")
    assert state.authority(sim.root.public_hex) == frozenset({"*"})
    assert state.members == {}
    assert state.invites == {}


def test_empty_ledger_cannot_fold():
    with pytest.raises(GenesisError):
        fold(Ledger())


def test_delegation_grants_scopes(sim):
    a = KeyPair.generate()
    sim.delegate(sim.root, a, ["link:publish", "link:revoke"])
    state = sim.fold()
    assert state.holds(a.public_hex, "link:publish")
    assert state.holds(a.public_hex, "link:revoke")
    assert not state.holds(a.public_hex, "tunnel:serve")
    assert state.delegable(a.public_hex) == frozenset()  # can_redelegate=False


def test_unknown_key_holds_nothing(sim):
    state = sim.fold()
    assert state.authority("ab" * 32) == frozenset()
    assert not state.holds("ab" * 32, "link:publish")


def test_fingerprint_reflects_state_changes(sim):
    fp0 = sim.fold().fingerprint()
    assert fp0 == sim.fold().fingerprint()  # stable
    a = KeyPair.generate()
    sim.delegate(sim.root, a, ["link:publish"])
    fp1 = sim.fold().fingerprint()
    assert fp1 != fp0


def test_fold_at_earlier_heads_sees_earlier_state(sim):
    a = KeyPair.generate()
    g = sim.delegate(sim.root, a, ["link:publish"])
    sim.revoke_event(sim.root, g)
    now_state = sim.fold()
    then_state = sim.fold(heads=[g])
    assert not now_state.holds(a.public_hex, "link:publish")
    assert then_state.holds(a.public_hex, "link:publish")


def test_invalid_events_are_inert_but_retained(sim):
    stranger, b = KeyPair.generate(), KeyPair.generate()
    bad = sim.delegate(stranger, b, ["link:publish"])
    state = sim.fold()
    assert bad in state.valid and state.valid[bad] is False
    assert bad in sim.ledger  # still part of the DAG for propagation
    # and later events can build on top of it without harm
    ok = sim.delegate(sim.root, b, ["link:publish"], parents=[bad])
    state = sim.fold()
    assert state.valid[ok] is True
    assert state.holds(b.public_hex, "link:publish")


def test_ttl_expiry_with_now(sim):
    a = KeyPair.generate()
    start = sim.next_ts()
    sim.delegate(sim.root, a, ["link:publish"], ttl=5_000, ts=start)
    live = sim.fold(now=start + 1_000)
    dead = sim.fold(now=start + 10_000)
    timeless = sim.fold()
    assert live.holds(a.public_hex, "link:publish")
    assert not dead.holds(a.public_hex, "link:publish")
    assert timeless.holds(a.public_hex, "link:publish")  # no clock ⇒ no expiry


def test_ttl_expiry_cascades_through_chain(sim):
    a, b = KeyPair.generate(), KeyPair.generate()
    start = sim.next_ts()
    sim.delegate(sim.root, a, ["link:publish"], redelegate=True, ttl=5_000, ts=start)
    sim.delegate(a, b, ["link:publish"], ts=start + 1_000)  # no own ttl
    state = sim.fold(now=start + 10_000)
    assert not state.holds(a.public_hex, "link:publish")
    assert not state.holds(b.public_hex, "link:publish")  # support expired


def test_expired_grant_cannot_authorize_new_events(sim):
    a, b = KeyPair.generate(), KeyPair.generate()
    start = sim.next_ts()
    sim.delegate(sim.root, a, ["link:publish"], redelegate=True, ttl=5_000, ts=start)
    late = sim.delegate(a, b, ["link:publish"], ts=start + 60_000)
    state = sim.fold()
    assert state.valid[late] is False


def test_state_exposes_heads_and_org(sim):
    a = KeyPair.generate()
    e = sim.delegate(sim.root, a, ["link:publish"])
    state = sim.fold()
    assert state.heads == (e,)
    from .conftest import ORG

    assert state.org == ORG
