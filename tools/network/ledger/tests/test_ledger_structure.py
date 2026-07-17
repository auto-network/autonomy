"""Structural admission: parents, HLC monotonicity, genesis rules, ingest."""

from __future__ import annotations

import random

import pytest

from tools.network.idkit import KeyPair
from tools.network.ledger import (
    HLC,
    CausalityError,
    GenesisError,
    Ledger,
    LedgerError,
    SignatureError,
    UnknownParentError,
    fold,
    make_event,
)

from .conftest import ORG, T0, Sim


def test_add_is_idempotent(sim):
    a = KeyPair.generate()
    eid = sim.delegate(sim.root, a, ["link:publish"])
    event = sim.ledger.get(eid)
    assert sim.ledger.add(event) == eid
    assert len(sim.ledger) == 2


def test_unknown_parent_rejected(sim):
    ev = make_event(
        sim.root,
        {"type": "checkpoint", "state_hash": "ab" * 32, "signers": [sim.root.public_hex]},
        ["99" * 32],
        HLC(T0 + 10),
    )
    with pytest.raises(UnknownParentError):
        sim.ledger.add(ev)


def test_hlc_must_advance_past_parents(sim):
    with pytest.raises(CausalityError):
        sim.emit(
            sim.root,
            {"type": "checkpoint", "state_hash": "ab" * 32, "signers": [sim.root.public_hex]},
            parents=[sim.genesis_id],
            ts=T0,  # equal to genesis, not strictly greater
        )


def test_second_genesis_rejected(sim):
    with pytest.raises(GenesisError):
        sim.emit(
            sim.root,
            {"type": "genesis", "org": ORG, "root_pub": sim.root.public_hex},
            parents=[],
        )


def test_genesis_must_be_self_signed():
    ledger = Ledger()
    signer = KeyPair.generate()
    other = KeyPair.generate()
    ev = make_event(
        signer,
        {"type": "genesis", "org": ORG, "root_pub": other.public_hex},
        [],
        HLC(T0),
    )
    with pytest.raises(GenesisError):
        ledger.add(ev)


def test_no_events_before_genesis():
    ledger = Ledger()
    kp = KeyPair.generate()
    ev = make_event(
        kp,
        {"type": "checkpoint", "state_hash": "ab" * 32, "signers": [kp.public_hex]},
        ["77" * 32],
        HLC(T0),
    )
    with pytest.raises(GenesisError):
        ledger.add(ev)


def test_forged_signature_rejected_at_add(sim):
    a = KeyPair.generate()
    eid = sim.delegate(sim.root, a, ["link:publish"])
    good = sim.ledger.get(eid)
    forged_dict = good.to_dict()
    forged_dict["payload"] = dict(forged_dict["payload"], can_redelegate=True)
    from tools.network.ledger import Event

    forged = Event.from_dict(forged_dict)
    fresh = Ledger()
    fresh.add(sim.ledger.genesis)
    with pytest.raises(SignatureError):
        fresh.add(forged)


def test_heads_track_frontier(sim):
    a = KeyPair.generate()
    e1 = sim.delegate(sim.root, a, ["link:publish"], parents=[sim.genesis_id])
    e2 = sim.delegate(sim.root, a, ["link:revoke"], parents=[sim.genesis_id])
    assert set(sim.ledger.heads()) == {e1, e2}
    e3 = sim.checkpoint(sim.root, parents=[e1, e2])
    assert sim.ledger.heads() == (e3,)


def test_ingest_out_of_order_equals_ordered(sim):
    a, b = KeyPair.generate(), KeyPair.generate()
    sim.delegate(sim.root, a, ["invite:member", "link:publish"], redelegate=True)
    sim.delegate(a, b, ["link:publish"])
    sim.role_define(sim.root, "member", ["link:publish"])
    events = list(sim.ledger.events())

    rng = random.Random(7)
    for _ in range(5):
        shuffled = events[:]
        rng.shuffle(shuffled)
        replica = Ledger()
        replica.ingest(shuffled)
        assert replica.all_ids() == sim.ledger.all_ids()
        assert fold(replica).fingerprint() == fold(sim.ledger).fingerprint()


def test_ingest_unresolvable_raises(sim):
    a = KeyPair.generate()
    e1 = sim.delegate(sim.root, a, ["link:publish"])
    e2 = sim.delegate(sim.root, a, ["link:revoke"])
    events = [sim.ledger.get(e2), sim.ledger.get(e1)]  # missing genesis
    replica = Ledger()
    with pytest.raises(LedgerError):
        replica.ingest(events)


def test_ancestry_closure(sim):
    a = KeyPair.generate()
    e1 = sim.delegate(sim.root, a, ["link:publish"])
    e2 = sim.checkpoint(sim.root, parents=[e1])
    assert sim.ledger.ancestry([e2]) == frozenset({sim.genesis_id, e1, e2})
