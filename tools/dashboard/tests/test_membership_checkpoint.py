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


# ── Adoption by fold (OrgAdmission.tla rule bundle_adopt) ─────────────────
def _binding_row(monkeypatch, org_uuid="11111111-1111-4111-8111-111111111111"):
    """network_routes._adopt_state_by_fold reads the org's binding for the uuid
    it stamps on the adopted record; give it one without a registry."""
    from tools.dashboard import network_routes
    from types import SimpleNamespace
    payload = {"org_uuid": org_uuid, "root_pub": "r" * 64,
               "registry_url": "https://registry.test"}

    def first_member(set_id, org):
        return SimpleNamespace(payload=payload)
    monkeypatch.setattr(network_routes, "_first_member", first_member)
    return org_uuid


def _state_of(sim, seq):
    state = sim.fold()
    head = sorted(state.heads)[0] if state.heads else sim.genesis_id
    return {"seq": seq, "members_root": mc.members_root(state),
            "checkpointers_root": mc.checkpointers_root(state),
            "ledger_head": head}


def test_adopt_by_fold_accepts_a_state_the_ledger_reproduces(monkeypatch):
    from tools.dashboard import network_routes
    sim, _founder = org_with_owner()
    add_member(sim)
    _install_org(sim)
    org_uuid = _binding_row(monkeypatch)
    state = _state_of(sim, 1)
    result = network_routes._adopt_state_by_fold(ORG, state, source="join bundle's")
    assert result == {"ok": True, "action": "adopted", "seq": 1}
    adopted = cp._cached_adopted(ORG)
    assert adopted["seq"] == 1 and adopted["org"] == org_uuid
    assert adopted["members_root"] == state["members_root"]
    assert adopted["ledger_head"] == state["ledger_head"]


def test_adopt_by_fold_refuses_a_root_the_ledger_does_not_produce(monkeypatch):
    from tools.dashboard import network_routes
    sim, _founder = org_with_owner()
    _install_org(sim)
    _binding_row(monkeypatch)
    state = _state_of(sim, 1)
    state["members_root"] = "f" * 64  # a roster this ledger never folded
    result = network_routes._adopt_state_by_fold(ORG, state, source="join bundle's")
    assert result["ok"] is False and "does not match" in result["error"]
    assert cp._cached_adopted(ORG) is None


def test_adopt_by_fold_refuses_a_head_the_ledger_lacks(monkeypatch):
    from tools.dashboard import network_routes
    sim, _founder = org_with_owner()
    _install_org(sim)
    _binding_row(monkeypatch)
    state = _state_of(sim, 1)
    state["ledger_head"] = "a" * 64
    result = network_routes._adopt_state_by_fold(ORG, state, source="join bundle's")
    assert result["ok"] is False and "no head" in result["error"]
    assert cp._cached_adopted(ORG) is None


def test_adopt_by_fold_is_monotone_and_names_its_source(monkeypatch):
    """NoRegression: an older or equal seq never replaces the cache; an
    absent or malformed state is refused naming where it came from."""
    from tools.dashboard import network_routes
    sim, _founder = org_with_owner()
    _install_org(sim)
    _binding_row(monkeypatch)
    state = _state_of(sim, 3)
    assert network_routes._adopt_state_by_fold(ORG, state, source="registry's")["action"] == "adopted"
    older = dict(state, seq=2)
    assert network_routes._adopt_state_by_fold(ORG, older, source="registry's") == {
        "ok": True, "action": "up-to-date", "seq": 3}
    assert cp._cached_adopted(ORG)["seq"] == 3
    none = network_routes._adopt_state_by_fold(ORG, None, source="join bundle's")
    assert none["ok"] is False and none["error"] == "join bundle's carried no membership checkpoint"
    bad = network_routes._adopt_state_by_fold(ORG, {"seq": "x"}, source="join bundle's")
    assert bad["ok"] is False and "malformed" in bad["error"]


def _root_signed_state(sim, seq):
    state = sim.fold()
    head = sorted(state.heads)[0] if state.heads else sim.genesis_id
    return mc.build_root_checkpoint(
        org="11111111-1111-4111-8111-111111111111", seq=seq, genesis_id=sim.genesis_id,
        ledger_head=head, members_root_hex=mc.members_root(state),
        checkpointers_root_hex=mc.checkpointers_root(state), ts=TS, root=sim.root)


def test_adopt_by_fold_verifies_a_record_that_claims_the_root_signature(monkeypatch):
    """A bundle record naming the org root as signer is verified by signature
    against the binding's root_pub before the fold (deviation D3): a forged
    root-signed record is refused; a genuine one is adopted and retained in
    its signed form."""
    from tools.dashboard import network_routes
    sim, _founder = org_with_owner()
    _install_org(sim)
    monkeypatch.setattr(network_routes, "_first_member", lambda set_id, org: __import__("types").SimpleNamespace(
        payload={"org_uuid": "11111111-1111-4111-8111-111111111111",
                 "root_pub": sim.root.public_hex, "registry_url": "https://registry.test"}))
    forged = _root_signed_state(sim, 2)
    forged["sig"] = "0" * 128
    result = network_routes._adopt_state_by_fold(ORG, forged, source="join bundle's")
    assert result["ok"] is False and "does not verify" in result["error"]
    assert cp._cached_adopted(ORG) is None
    genuine = _root_signed_state(sim, 2)
    assert network_routes._adopt_state_by_fold(ORG, genuine, source="join bundle's")["action"] == "adopted"
    retained = cp._cached_adopted(ORG)
    assert retained["sig"] == genuine["sig"] and retained["signer"] == sim.root.public_hex


def test_adoption_never_regresses_from_any_source(monkeypatch):
    """NoRegression (OrgAdmission.tla): a replayed or rolled-back older record,
    from the registry or a bundle, never replaces a newer adopted one."""
    from tools.dashboard import network_routes
    sim, _founder = org_with_owner()
    _install_org(sim)
    _binding_row(monkeypatch)
    assert network_routes._adopt_state_by_fold(ORG, _state_of(sim, 3), source="join bundle's")["action"] == "adopted"
    for source in ("registry's", "join bundle's"):
        assert network_routes._adopt_state_by_fold(ORG, _state_of(sim, 2), source=source) == {
            "ok": True, "action": "up-to-date", "seq": 3}
    assert cp._cached_adopted(ORG)["seq"] == 3


# ── Retention (auto-qrmlg.3: checkpoint cache revision 2) ────────────────
def _tuple_record(seq, head="cc" * 32):
    return {"seq": seq, "members_root": "aa" * 32, "checkpointers_root": "bb" * 32,
            "ledger_head": head, "org": ORG}


def test_record_adopted_retains_every_record_and_never_regresses_the_newest():
    """E-any-adm needs every adopted record by seq; the newest is the row's
    record; recording an older seq retains it without changing the newest;
    re-recording a seq replaces that entry."""
    sim, _founder = org_with_owner()
    _install_org(sim)
    cp.record_adopted(ORG, _tuple_record(2))
    cp.record_adopted(ORG, _tuple_record(4))
    cp.record_adopted(ORG, _tuple_record(3))
    assert cp._cached_adopted(ORG)["seq"] == 4
    assert sorted(cp.adopted_history(ORG)) == [2, 3, 4]
    assert cp.adopted_record_for(ORG, 3) == _tuple_record(3)
    assert cp.adopted_record_for(ORG, 9) is None
    cp.record_adopted(ORG, _tuple_record(3, head="dd" * 32))
    assert cp.adopted_record_for(ORG, 3)["ledger_head"] == "dd" * 32
    assert cp._cached_adopted(ORG)["seq"] == 4


def test_retention_is_bounded_newest_kept():
    from tools.graph.schemas.network_identity import NETWORK_CHECKPOINT_HISTORY_LIMIT as LIMIT
    sim, _founder = org_with_owner()
    _install_org(sim)
    for seq in range(LIMIT + 5):
        cp.record_adopted(ORG, _tuple_record(seq))
    history = cp.adopted_history(ORG)
    assert len(history) == LIMIT and min(history) == 5 and max(history) == LIMIT + 4


def test_a_revision_one_cache_row_reads_as_a_one_record_history():
    from tools.graph.schemas.network_identity import NETWORK_CHECKPOINT_CACHE_SET_ID
    sim, _founder = org_with_owner()
    _install_org(sim)
    settings_ops.upsert_by_key(NETWORK_CHECKPOINT_CACHE_SET_ID, 1, "default",
                               {"seq": 7, "record": _tuple_record(7)}, org=ORG)
    assert cp._cached_adopted(ORG) == _tuple_record(7)
    assert cp.adopted_history(ORG) == {7: _tuple_record(7)}
    cp.record_adopted(ORG, _tuple_record(8))
    assert sorted(cp.adopted_history(ORG)) == [7, 8]


def test_adopt_by_fold_retains_a_member_signed_record_as_its_state_tuple(monkeypatch):
    """D3a: a signed form is retained only when its signature was verified.
    A member-signed record cannot be (no prev chain here), so the tuple is
    what is retained, never an unverified signature to be served onward."""
    from tools.dashboard import network_routes
    sim, founder = org_with_owner()
    _install_org(sim)
    org_uuid = _binding_row(monkeypatch)
    state = _state_of(sim, 1)
    signed = dict(state, org=org_uuid, v=1, prev="dd" * 32, ts=TS,
                  signer=founder.public_hex, sig="ef" * 64, proof=[], proof_index=0)
    assert network_routes._adopt_state_by_fold(ORG, signed, source="join bundle's")["action"] == "adopted"
    retained = cp._cached_adopted(ORG)
    assert "sig" not in retained and "signer" not in retained
    assert retained == {"org": org_uuid, **state}


def test_adopt_by_fold_refuses_a_record_for_another_organization(monkeypatch):
    """D3b: a state naming another org is refused, not re-stamped."""
    from tools.dashboard import network_routes
    sim, _founder = org_with_owner()
    _install_org(sim)
    _binding_row(monkeypatch)
    state = dict(_state_of(sim, 1), org="22222222-2222-4222-8222-222222222222")
    result = network_routes._adopt_state_by_fold(ORG, state, source="join bundle's")
    assert result["ok"] is False and "another organization" in result["error"]
    assert cp._cached_adopted(ORG) is None


def test_adopt_by_fold_bounds_a_bundle_seq_by_the_registry(monkeypatch):
    """The registry assigns seqs: a bundle record above the registry's seq
    is refused, one AT the registry's seq must be the registry's record, and
    one below is adopted by fold (its seq is the sponsor's word, bounded)."""
    from tools.dashboard import network_routes
    sim, _founder = org_with_owner()
    _install_org(sim)
    _binding_row(monkeypatch)
    registry = _state_of(sim, 3)
    above = network_routes._adopt_state_by_fold(ORG, _state_of(sim, 4), source="join bundle's", bound=registry)
    assert above["ok"] is False and "the registry is at 3" in above["error"]
    other_at_3 = dict(_state_of(sim, 3), ledger_head="a" * 64)
    at = network_routes._adopt_state_by_fold(ORG, other_at_3, source="join bundle's", bound=registry)
    assert at["ok"] is False and "not the registry's record" in at["error"]
    assert cp._cached_adopted(ORG) is None
    assert network_routes._adopt_state_by_fold(ORG, _state_of(sim, 2), source="join bundle's", bound=registry)["action"] == "adopted"
    assert network_routes._adopt_state_by_fold(ORG, _state_of(sim, 3), source="join bundle's", bound=registry)["action"] == "adopted"
    assert cp._cached_adopted(ORG)["seq"] == 3


# ── AutoAdopt after membership events (OrgAdmissionBundleBound.tla) ─────────
def test_adopt_after_membership_events_reads_the_registry_only_when_the_fold_moved():
    sim, _founder = org_with_owner()
    _install_org(sim)
    state = sim.fold()
    cp.record_adopted(ORG, {"seq": 0, "members_root": mc.members_root(state),
                            "checkpointers_root": mc.checkpointers_root(state),
                            "ledger_head": sim.genesis_id, "org": ORG})
    calls: list[str] = []

    def adopt(slug):
        calls.append(slug)
        return {"ok": True, "action": "adopted", "seq": 1}

    out = cp.adopt_after_membership_events([ORG], adopt=adopt)
    assert calls == [] and "skipped" in out[ORG]
    add_member(sim)
    _install_org(sim)  # the new claim arrives by sync
    out = cp.adopt_after_membership_events([ORG], adopt=adopt)
    assert calls == [ORG] and out[ORG]["action"] == "adopted"
