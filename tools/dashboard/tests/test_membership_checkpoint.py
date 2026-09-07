"""Sign-on checkpoint prep (auto-tmers): the local due-check + assembly.

Drives a REAL org ledger built with the ledger testkit Sim, points the org
resolver at it, and asserts the sign-on decision for each case: seed when no
cache, up-to-date when the fold matches the cache, assemble-advance when
membership changed, not-checkpointer for a plain member, not-eligible for a
just-granted checkpointer, and the cache round-trip. No signing, no network.
"""

from __future__ import annotations

import pytest

from tools.graph import settings_ops
from tools.network.idkit import KeyPair
from tools.network.ledger import membership_commitment as mc
from tools.network.ledger.tests.conftest import Sim
from tools.network.ledger.tests.test_membership_commitment import (
    add_member,
    org_with_owner,
)

from tools.dashboard import membership_checkpoint as cp

ORG = "acme"
TS = 1_800_000_000


@pytest.fixture(autouse=True)
def _org_ledger(tmp_path, monkeypatch):
    """Point both the ledger resolver and the settings store at tmp_path, and
    give this module one Sim-built org whose ledger the prep folds."""
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.setenv("AUTONOMY_DATA_ROOT", str(tmp_path))
    from tools.graph.db import GraphDB
    (tmp_path / "orgs").mkdir(parents=True, exist_ok=True)
    # This fixture repoints the graph/org stores; tools.graph.db pools
    # connections keyed by path, so a pooled handle to this temp dir would
    # leak into the next module in the same xdist worker (deleted dir ->
    # phantom cross-module failures). Drain on both edges.
    GraphDB.close_all_pooled()
    try:
        yield
    finally:
        GraphDB.close_all_pooled()


def _install_org(sim: Sim):
    """Copy every event from the Sim's in-memory ledger into ORG's ledger DB
    (append is idempotent and parents-before-children is preserved by the
    Sim's own append order)."""
    from tools.network.ledger.store import LedgerStore, org_ledger_db_path
    dest = LedgerStore(org_ledger_db_path(ORG))
    try:
        # Genesis first, then HLC order — the Sim stamps a strictly
        # increasing hlc, so parents always precede children.
        events = sorted(sim.ledger.events(),
                        key=lambda e: (e.type != "genesis", e.hlc.ts, e.hlc.count))
        for event in events:
            dest.append(event)
    finally:
        dest.close()
    return sim


def _persona(sim, member_kp):
    return member_kp.public_hex


def _fold(sim):
    return sim.fold()


def test_seed_assembled_when_no_cache():
    sim, founder = org_with_owner()
    _install_org(sim)
    d = cp.checkpoint_due(ORG, founder.public_hex, ts=TS,
                          genesis_id=sim.genesis_id)
    assert d.action == "assemble" and d.sign_with == cp.SIGN_WITH_ROOT
    assert d.record["seq"] == 0 and d.record["prev"] == sim.genesis_id
    assert d.record["members_root"] == mc.members_root(sim.fold())
    assert "sig" not in d.record and "signer" not in d.record


def test_up_to_date_when_cache_matches_fold():
    sim, founder = org_with_owner()
    _install_org(sim)
    state = sim.fold()
    seed = mc.build_root_checkpoint(
        org=ORG, seq=0, genesis_id=sim.genesis_id, ledger_head=sim.genesis_id,
        members_root_hex=mc.members_root(state),
        checkpointers_root_hex=mc.checkpointers_root(state),
        ts=TS, root=sim.root)
    cp.record_adopted(ORG, seed)
    d = cp.checkpoint_due(ORG, founder.public_hex, ts=TS,
                          genesis_id=sim.genesis_id)
    assert d.action == "up-to-date"


def test_advance_assembled_after_membership_change():
    sim, founder = org_with_owner()
    state0 = sim.fold()
    head0 = sorted(state0.heads)[0] if state0.heads else sim.genesis_id
    seed = mc.build_root_checkpoint(
        org=ORG, seq=0, genesis_id=sim.genesis_id, ledger_head=head0,
        members_root_hex=mc.members_root(state0),
        checkpointers_root_hex=mc.checkpointers_root(state0),
        ts=TS, root=sim.root)
    # Membership changes: a new member joins after the seed.
    joiner = add_member(sim)
    _install_org(sim)
    cp.record_adopted(ORG, seed)
    d = cp.checkpoint_due(ORG, founder.public_hex, ts=TS,
                          genesis_id=sim.genesis_id)
    assert d.action == "assemble" and d.sign_with == cp.SIGN_WITH_PERSONA
    assert d.record["seq"] == 1
    assert d.record["prev"] == mc.checkpoint_hash(seed)
    assert d.record["members_root"] == mc.members_root(sim.fold())
    # The signer proved itself under the SEED's checkpointers_root.
    mc.verify_inclusion(seed["checkpointers_root"], founder.public_hex,
                        d.record["proof_index"], d.record["proof"])


def test_plain_member_is_not_a_checkpointer():
    sim, founder = org_with_owner()
    member = add_member(sim)
    _install_org(sim)
    # A stale cache (owner-only roots) forces past the up-to-date short
    # circuit so the checkpointer gate is what decides.
    cp.record_adopted(ORG, {"seq": 0, "members_root": "0" * 64,
                            "checkpointers_root": "0" * 64,
                            "ledger_head": sim.genesis_id, "org": ORG,
                            "v": 1, "prev": sim.genesis_id, "ts": TS,
                            "signer": sim.root.public_hex, "sig": "ab" * 64})
    d = cp.checkpoint_due(ORG, member.public_hex, ts=TS,
                          genesis_id=sim.genesis_id)
    assert d.action == "not-checkpointer"


def test_just_granted_checkpointer_not_eligible_first():
    sim, founder = org_with_owner()
    state0 = sim.fold()
    head0 = sorted(state0.heads)[0]
    seed = mc.build_root_checkpoint(
        org=ORG, seq=0, genesis_id=sim.genesis_id, ledger_head=head0,
        members_root_hex=mc.members_root(state0),
        checkpointers_root_hex=mc.checkpointers_root(state0),
        ts=TS, root=sim.root)
    # A member is added and granted the checkpoint scope AFTER the seed.
    member = add_member(sim)
    sim.role_define(sim.root, "steward", [mc.CHECKPOINT_SCOPE], requires="self")
    sim.role_grant(sim.root, member, "steward")
    _install_org(sim)
    cp.record_adopted(ORG, seed)
    d = cp.checkpoint_due(ORG, member.public_hex, ts=TS,
                          genesis_id=sim.genesis_id)
    assert d.action == "not-eligible"
    # The founder, who WAS in the seed's checkpointer set, can publish it.
    d2 = cp.checkpoint_due(ORG, founder.public_hex, ts=TS,
                           genesis_id=sim.genesis_id)
    assert d2.action == "assemble"


def test_checkpoint_status_is_the_persona_independent_verdict():
    # The unlock-plan verdict: needed + the checkpointer permission set, no
    # persona, no signing. A fresh org has no adopted checkpoint -> needed;
    # the owner (holds *) is a checkpointer, a plain member never is.
    sim, founder = org_with_owner()
    member = add_member(sim)
    _install_org(sim)
    st = cp.checkpoint_status(ORG)
    assert st["needed"] is True
    assert founder.public_hex in st["checkpointer_pubs"]
    assert member.public_hex not in st["checkpointer_pubs"]

    state = sim.fold()
    seed = mc.build_root_checkpoint(
        org=ORG, seq=0, genesis_id=sim.genesis_id, ledger_head=sim.genesis_id,
        members_root_hex=mc.members_root(state),
        checkpointers_root_hex=mc.checkpointers_root(state),
        ts=TS, root=sim.root)
    cp.record_adopted(ORG, seed)
    assert cp.checkpoint_status(ORG)["needed"] is False


def test_checkpoint_status_on_an_unfounded_org_is_benign():
    # The plan must not fail on an org with no ledger — a benign verdict, never
    # a raise.
    st = cp.checkpoint_status("no-such-org")
    assert st["needed"] is False and st["checkpointer_pubs"] == []


def test_record_adopted_round_trips():
    sim, founder = org_with_owner()
    _install_org(sim)
    rec = {"seq": 3, "members_root": "aa" * 32, "checkpointers_root": "bb" * 32,
           "ledger_head": "cc" * 32, "org": ORG, "v": 1, "prev": "dd" * 32,
           "ts": TS, "signer": founder.public_hex, "sig": "ef" * 64,
           "proof": [], "proof_index": 0}
    cp.record_adopted(ORG, rec)
    assert cp._cached_adopted(ORG) == rec
