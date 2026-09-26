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
    # The authenticated tuple is the record; the signed bytes ride beside it
    # for chaining only (B1), never as the trusted form.
    assert {k: v for k, v in retained.items() if k != "chain_record"} == {"org": org_uuid, **state}
    assert retained["chain_record"] == signed


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


# ── Checkpoint at admission (OrgAdmission.tla P2; DelegateCheckpoint.spthy) ──
def _org_with_delegate(checkpointer=True):
    """A founded org whose founder minted a hot delegate; returns
    (sim, founder, delegate key, grant wire)."""
    from tools.network.storagekit.delegate import storage_delegate_scopes
    sim, founder = org_with_owner()
    child = KeyPair.generate()
    gid = sim.delegate(founder, child, storage_delegate_scopes("dd" * 32, checkpointer=checkpointer),
                       ttl=90 * 86_400_000)
    assert sim.fold().valid[gid], sim.fold().reasons.get(gid)
    return sim, founder, child, sim.ledger.get(gid).to_json().decode("utf-8")


def _seed_for(sim, org_uuid):
    state = sim.fold()
    return mc.build_root_checkpoint(
        org=org_uuid, seq=0, genesis_id=sim.genesis_id, ledger_head=cp._first_head(state),
        members_root_hex=mc.members_root(state), checkpointers_root_hex=mc.checkpointers_root(state),
        ts=TS, root=sim.root)


class _Registry:
    """A registry stand-in: adopts by the real rule, judged at *now*."""

    def __init__(self, root_pub, now):
        self.root_pub, self.now, self.records = root_pub, now, []

    def post(self, binding, record):
        from tools.network.registry.store import validate_membership_advance
        try:
            validate_membership_advance(self.records[-1] if self.records else None, record,
                                        self.root_pub, now=self.now)
        except mc.MembershipCommitmentError as exc:
            return 403, str(exc)
        self.records.append(record)
        return 201, "{}"

    def current(self):
        r = self.records[-1]
        state = {k: r[k] for k in ("seq", "members_root", "checkpointers_root", "ledger_head")}
        state["checkpoint"] = dict(r)   # the registry GET serves its signed record too
        return state

    def serve(self, monkeypatch):
        """Point network_routes' registry read at this stand-in."""
        monkeypatch.setattr("tools.dashboard.network_routes._registry_membership_state",
                            lambda binding: (self.current(), None))


def test_admission_publishes_a_delegate_signed_checkpoint_that_includes_the_member(monkeypatch):
    sim, founder, child, grant = _org_with_delegate()
    org_uuid = _binding_row(monkeypatch)
    monkeypatch.setattr("tools.dashboard.network_routes._first_member",
                        lambda set_id, org: __import__("types").SimpleNamespace(
                            payload={"org_uuid": org_uuid, "root_pub": sim.root.public_hex,
                                     "registry_url": "https://registry.test"}))
    seed = _seed_for(sim, org_uuid)
    registry = _Registry(sim.root.public_hex, now=TS)
    registry.records.append(seed)
    _install_org(sim)
    cp.record_adopted(ORG, seed)
    # Nothing to publish while the fold matches the newest retained record.
    assert cp.publish_after_membership_change(ORG, signer=(child, grant), post=registry.post, now=TS) == {
        "action": "up-to-date", "seq": 0}
    member = add_member(sim)          # the admission
    _install_org(sim)
    out = cp.publish_after_membership_change(ORG, signer=(child, grant), post=registry.post, now=TS)
    assert out == {"action": "published", "seq": 1, "sign_with": "delegate"}
    adopted = cp._cached_adopted(ORG)
    assert adopted["seq"] == 1 and adopted["signer"] == child.public_hex and "grant" in adopted
    assert mc.checkpoint_signer_persona(adopted) == founder.public_hex
    # The published set includes the member: it can prove under it.
    mc.verify_inclusion(adopted["members_root"], member.public_hex,
                        *mc.inclusion_proof(mc.member_pubs(sim.fold()), member.public_hex))
    assert registry.current()["seq"] == 1


def test_publication_race_re_assembles_on_the_registrys_newer_record(monkeypatch):
    """F3: the registry moved past our prev (another checkpointer published);
    on the seq/prev refusal we adopt its record and re-assemble, bounded."""
    sim, founder, child, grant = _org_with_delegate()
    org_uuid = _binding_row(monkeypatch)
    seed = _seed_for(sim, org_uuid)
    registry = _Registry(sim.root.public_hex, now=TS)
    registry.records.append(seed)
    _install_org(sim)
    cp.record_adopted(ORG, seed)
    add_member(sim)
    _install_org(sim)
    # Another checkpointer already published seq 1 on the seed (persona-signed).
    state = sim.fold()
    other = mc.build_checkpoint(
        org=org_uuid, seq=1, prev=mc.checkpoint_hash(seed), ledger_head=sorted(state.heads)[0],
        members_root_hex=mc.members_root(state), checkpointers_root_hex=mc.checkpointers_root(state),
        ts=TS, signer=founder, prev_checkpointer_pubs=mc.checkpointer_pubs(state))
    assert registry.post({}, other)[0] == 201
    add_member(sim)                   # and now a second admission on this node
    _install_org(sim)
    registry.serve(monkeypatch)       # the REAL adoption path reads this registry
    out = cp.publish_after_membership_change(ORG, signer=(child, grant), post=registry.post, now=TS)
    assert out["action"] == "published" and out["seq"] == 2
    assert cp._cached_adopted(ORG)["prev"] == mc.checkpoint_hash(other)
    assert sorted(cp.adopted_history(ORG)) == [0, 1, 2]


def test_publication_is_skipped_or_refused_with_the_reason_named(monkeypatch):
    sim, founder, child, grant = _org_with_delegate()
    org_uuid = _binding_row(monkeypatch)
    seed = _seed_for(sim, org_uuid)
    _install_org(sim)
    add_member(sim)
    _install_org(sim)
    # No adopted record yet: the seed is the operator's (root) ceremony.
    out = cp.publish_after_membership_change(ORG, signer=(child, grant), post=lambda b, r: (201, ""), now=TS)
    assert out["action"] == "skipped" and "seed" in out["reason"]
    cp.record_adopted(ORG, seed)
    # A registry that does not know the form: unpublished, reported, no retry loop.
    posts = []

    def old_registry(binding, record):
        posts.append(record)
        return 403, "checkpoint fields do not match its form (unknown ['genesis_id', 'grant'])"

    out = cp.publish_after_membership_change(ORG, signer=(child, grant), post=old_registry, now=TS)
    assert out["action"] == "refused" and "form" in out["reason"] and len(posts) == 1
    assert cp._cached_adopted(ORG)["seq"] == 0
    # A delegate whose persona became a checkpointer AFTER the adopted record:
    # not in the previous checkpointers root, so an existing checkpointer
    # must publish the record that first includes it.
    from tools.network.storagekit.delegate import storage_delegate_scopes
    steward = add_member(sim, role="steward", scopes=("*",))
    steward_child = KeyPair.generate()
    gid = sim.delegate(steward, steward_child, storage_delegate_scopes("dd" * 32, checkpointer=True),
                       ttl=90 * 86_400_000)
    assert sim.fold().valid[gid]
    _install_org(sim)
    steward_grant = sim.ledger.get(gid).to_json().decode("utf-8")
    out = cp.publish_after_membership_change(
        ORG, signer=(steward_child, steward_grant), post=lambda b, r: (201, ""), now=TS)
    assert out["action"] == "skipped" and "previous checkpointers" in out["reason"]


def test_without_a_checkpoint_scoped_delegate_nothing_is_published(monkeypatch):
    sim, founder, child, grant = _org_with_delegate(checkpointer=False)
    org_uuid = _binding_row(monkeypatch)
    seed = _seed_for(sim, org_uuid)
    _install_org(sim)
    cp.record_adopted(ORG, seed)
    add_member(sim)
    _install_org(sim)
    posts = []
    out = cp.publish_after_membership_change(ORG, post=lambda b, r: posts.append(r) or (201, ""), now=TS)
    assert out["action"] == "skipped" and posts == []


def test_prepare_marks_a_checkpointer_delegate_for_remint_until_it_carries_the_scope(monkeypatch):
    """org_storage_delegate.prepare: the scope list offered to the browser
    carries membership:checkpoint for a checkpointer, and an existing grant
    without it is due for a re-mint at this sign-on (a member who is not a
    checkpointer gets the two-scope shape and no re-mint)."""
    from tools.dashboard import org_storage_delegate as osd
    from tools.graph.schemas.network_identity import NETWORK_STORAGE_DELEGATE_SET_ID
    from tools.network.storagekit.delegate import storage_delegate_scopes
    sim, founder = org_with_owner()
    child = KeyPair.generate()
    old = sim.delegate(founder, child, storage_delegate_scopes("dd" * 32), ttl=90 * 86_400_000)
    member = add_member(sim)
    _install_org(sim)
    monkeypatch.setattr("tools.graph.org_ops.persona_pub_for_org", lambda genesis: founder.public_hex)
    prepared = osd.prepare(ORG)
    assert prepared["checkpointer"] is True and prepared["remint_required"] is False  # no grant recorded yet
    assert mc.CHECKPOINT_SCOPE in prepared["scope"] and len(prepared["scope"]) == 3
    settings_ops.upsert_by_key(NETWORK_STORAGE_DELEGATE_SET_ID, 1, sim.genesis_id, {
        "organization": ORG, "persona_pub": founder.public_hex, "public_key": child.public_hex,
        "key_reference": "storage-delegate.x", "expires_at": TS * 1000 + 10 ** 9, "grant_event_id": old,
    }, org=None)
    assert osd.prepare(ORG)["remint_required"] is True
    monkeypatch.setattr("tools.graph.org_ops.persona_pub_for_org", lambda genesis: member.public_hex)
    prepared = osd.prepare(ORG)
    assert prepared["checkpointer"] is False and prepared["remint_required"] is False
    assert len(prepared["scope"]) == 2


def test_a_node_that_adopted_by_fold_can_still_publish_the_next_checkpoint(monkeypatch):
    """B1 (reviewer, c7d6992a): the next record's prev is the hash of the
    registry's STORED signed record. A node whose newest record came from a
    fold adoption holds only the authenticated tuple, so the registry serves
    its signed bytes with the tuple and the adoption keeps them for chaining
    (never for trust). Publishing after an admission then chains correctly."""
    from tools.dashboard import network_routes
    sim, founder, child, grant = _org_with_delegate()
    org_uuid = _binding_row(monkeypatch)
    registry = _Registry(sim.root.public_hex, now=TS)
    registry.records.append(_seed_for(sim, org_uuid))
    _install_org(sim)
    registry.serve(monkeypatch)
    # Adopt the seed from the registry by fold: the retained entry is the
    # tuple plus the chain bytes; the tuple is what is trusted.
    assert network_routes._adopt_registry_checkpoint(ORG)["action"] == "adopted"
    retained = cp._cached_adopted(ORG)
    assert "sig" not in retained or retained["signer"] == sim.root.public_hex
    assert mc.chain_record_for(retained) == registry.records[-1]
    member = add_member(sim)
    _install_org(sim)
    out = cp.publish_after_membership_change(ORG, signer=(child, grant), post=registry.post, now=TS)
    assert out == {"action": "published", "seq": 1, "sign_with": "delegate"}
    assert registry.current()["seq"] == 1
    mc.verify_inclusion(registry.current()["members_root"], member.public_hex,
                        *mc.inclusion_proof(mc.member_pubs(sim.fold()), member.public_hex))


def test_a_member_signed_bundle_record_is_kept_for_chaining_but_not_trusted(monkeypatch):
    """D3a and B1 together: an unverifiable member-signed record from a bundle
    is retained as the authenticated tuple with its bytes beside it as the
    chain source; a signed record that does not describe the tuple is not."""
    from tools.dashboard import network_routes
    sim, founder = org_with_owner()
    _install_org(sim)
    org_uuid = _binding_row(monkeypatch)
    state = sim.fold()
    signed = mc.build_checkpoint(
        org=org_uuid, seq=1, prev="dd" * 32, ledger_head=cp._first_head(state),
        members_root_hex=mc.members_root(state), checkpointers_root_hex=mc.checkpointers_root(state),
        ts=TS, signer=founder, prev_checkpointer_pubs=[founder.public_hex])
    assert network_routes._adopt_state_by_fold(ORG, signed, source="join bundle's")["action"] == "adopted"
    retained = cp._cached_adopted(ORG)
    assert "sig" not in retained and retained["chain_record"] == signed
    assert mc.chain_record_for(retained) == signed
    # A tuple whose carried record describes another state keeps no chain.
    other = dict(_state_of(sim, 2), checkpoint=dict(signed, seq=7))
    assert network_routes._adopt_state_by_fold(ORG, other, source="registry's")["action"] == "adopted"
    assert mc.chain_record_for(cp._cached_adopted(ORG)) is None


def test_checkpoint_due_asks_for_the_registry_record_before_chaining(monkeypatch):
    sim, founder = org_with_owner()
    _install_org(sim)
    cp.record_adopted(ORG, _tuple_record(0, head=cp._first_head(sim.fold())))
    add_member(sim)
    _install_org(sim)
    d = cp.checkpoint_due(ORG, founder.public_hex, ts=TS, genesis_id=sim.genesis_id)
    assert d.action == "chain-missing" and "signed bytes" in d.reason


def test_a_chainless_tuple_at_the_registrys_seq_gets_its_chain_attached_in_place(monkeypatch):
    """G1 (reviewer, 1c68b9dd): a cache holding the tuple at the registry's
    current seq without its signed bytes (adopted before the registry served
    them) is not stuck: the registry read attaches the chain in place, and
    both the admission publish and the sign-on decision proceed."""
    from tools.dashboard import network_routes
    sim, founder, child, grant = _org_with_delegate()
    org_uuid = _binding_row(monkeypatch)
    seed = _seed_for(sim, org_uuid)
    registry = _Registry(sim.root.public_hex, now=TS)
    registry.records.append(seed)
    _install_org(sim)
    cp.record_adopted(ORG, {k: seed[k] for k in ("org", "seq", "members_root", "checkpointers_root", "ledger_head")})
    assert mc.chain_record_for(cp._cached_adopted(ORG)) is None
    registry.serve(monkeypatch)
    assert network_routes._adopt_registry_checkpoint(ORG) == {"ok": True, "action": "chain-attached", "seq": 0}
    assert mc.chain_record_for(cp._cached_adopted(ORG)) == seed
    assert network_routes._adopt_registry_checkpoint(ORG)["action"] == "up-to-date"
    member = add_member(sim)
    _install_org(sim)
    out = cp.publish_after_membership_change(ORG, signer=(child, grant), post=registry.post, now=TS)
    assert out["action"] == "published" and out["seq"] == 1
    # And the same situation at the sign-on decision: chain-less again at seq 1.
    cp.record_adopted(ORG, {k: registry.records[-1][k] for k in ("org", "seq", "members_root", "checkpointers_root", "ledger_head")})
    assert cp.checkpoint_due(ORG, founder.public_hex, ts=TS, genesis_id=sim.genesis_id, org_uuid=org_uuid).action == "up-to-date"
    add_member(sim)
    _install_org(sim)
    assert cp.checkpoint_due(ORG, founder.public_hex, ts=TS, genesis_id=sim.genesis_id, org_uuid=org_uuid).action == "chain-missing"
    assert network_routes._adopt_registry_checkpoint(ORG)["action"] == "chain-attached"
    d = cp.checkpoint_due(ORG, founder.public_hex, ts=TS, genesis_id=sim.genesis_id, org_uuid=org_uuid)
    assert d.action == "assemble" and d.record["prev"] == mc.checkpoint_hash(registry.records[-1])
    assert member.public_hex


def test_admission_offers_the_persona_form_checkpoint_when_no_delegate_can_publish(monkeypatch):
    """Reviewer A2(a) on 8038f999: with no checkpoint-scoped delegate on the
    admitting node, the approver's window still publishes: the admit
    response carries the persona-form record for the open persona to sign
    (prev = the registry's record), and nothing when the persona is not an
    eligible checkpointer."""
    from tools.dashboard import claim_service
    sim, founder = org_with_owner()
    org_uuid = _binding_row(monkeypatch)
    seed = _seed_for(sim, org_uuid)
    _install_org(sim)
    cp.record_adopted(ORG, seed)
    member = add_member(sim)
    _install_org(sim)
    work = claim_service._persona_checkpoint_work(ORG, founder.public_hex, sim.genesis_id)
    assert work["sign_with"] == "persona"
    assert work["record"]["seq"] == 1 and work["record"]["prev"] == mc.checkpoint_hash(seed)
    assert work["record"]["signer"] == founder.public_hex
    assert work["record"]["members_root"] == mc.members_root(sim.fold())
    assert claim_service._persona_checkpoint_work(ORG, member.public_hex, sim.genesis_id) is None
