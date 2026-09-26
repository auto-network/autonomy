"""The org sync channel's authenticator callables (org_sync_channels._callables)
against a real org ledger: the rider proves under the seq a peer proved under
when retained (prover-downgrade) or the newest retained record that includes
this persona; the admission floor is the persona's claim id at the record's
head against its current claim (OrgAdmission.tla E-any-adm, auto-qrmlg.3)."""

from __future__ import annotations

import pytest

from tools.network.idkit import KeyPair
from tools.network.ledger import membership_commitment as mc
from tools.network.ledger.tests.test_membership_commitment import add_member, org_with_owner

from tools.dashboard import membership_checkpoint as cp
from tools.dashboard import org_sync_channels as osc
from tools.dashboard.tests.test_membership_checkpoint import ORG, _install_org


@pytest.fixture(autouse=True)
def _org_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.setenv("AUTONOMY_DATA_ROOT", str(tmp_path))
    from tools.graph.db import GraphDB
    (tmp_path / "orgs").mkdir(parents=True, exist_ok=True)
    GraphDB.close_all_pooled()
    try:
        yield
    finally:
        GraphDB.close_all_pooled()


def _record(sim, seq):
    state = sim.fold()
    head = sorted(state.heads)[0] if state.heads else sim.genesis_id
    return {"org": ORG, "seq": seq, "members_root": mc.members_root(state),
            "checkpointers_root": mc.checkpointers_root(state), "ledger_head": head}


def test_rider_proves_under_the_newest_retained_record_that_includes_the_persona():
    sim, founder = org_with_owner()
    before = _record(sim, 0)          # founder only
    member = add_member(sim)
    after = _record(sim, 1)           # founder + member
    _install_org(sim)
    cp.record_adopted(ORG, before)
    cp.record_adopted(ORG, after)
    founder_calls = osc._callables(ORG, founder.public_hex)
    member_calls = osc._callables(ORG, member.public_hex)
    assert founder_calls["newest_adopted_seq"]() == 1
    assert founder_calls["membership_proof_for"](None)["checkpoint_seq"] == 1
    assert member_calls["membership_proof_for"](None)["checkpoint_seq"] == 1
    # Prover-downgrade: asked to prove under seq 0's root, the founder does,
    # in the peer's labelling; the member is not in that set, so it proves
    # under the newest record that has it.
    root0 = before["members_root"]
    assert founder_calls["membership_proof_for"](0, root0)["checkpoint_seq"] == 0
    assert founder_calls["membership_proof_for"](7, root0)["checkpoint_seq"] == 7
    assert member_calls["membership_proof_for"](0, root0)["checkpoint_seq"] == 1
    assert {r["seq"] for r in founder_calls["retained_checkpoints"]()} == {0, 1}
    # Each rider verifies against the retained root it names.
    for calls, persona in ((founder_calls, founder.public_hex), (member_calls, member.public_hex)):
        rider = calls["membership_proof_for"](None)
        root = calls["adopted_checkpoint_for"](rider["checkpoint_seq"])["members_root"]
        mc.verify_inclusion(root, persona, rider["index"], rider["path"])
    assert set(founder_calls["adopted_members_for"](1)) == {founder.public_hex, member.public_hex}
    assert founder_calls["adopted_members_for"](7) is None


def test_admission_floor_is_the_claim_at_the_record_against_the_current_claim():
    sim, founder = org_with_owner()
    before = _record(sim, 0)
    member = add_member(sim)
    after = _record(sim, 1)
    _install_org(sim)
    cp.record_adopted(ORG, before)
    cp.record_adopted(ORG, after)
    calls = osc._callables(ORG, founder.public_hex)
    ok = calls["admission_ok_for"]
    assert ok(0, founder.public_hex) is True and ok(1, founder.public_hex) is True
    # Seq 0 predates the member's admission: not a floor it can be admitted at.
    assert ok(0, member.public_hex) is False and ok(1, member.public_hex) is True
    assert ok(0, KeyPair.generate().public_hex) is False
    assert ok(5, founder.public_hex) is None  # not retained: cannot tell


def test_no_retained_record_gives_the_seed_rider():
    sim, founder = org_with_owner()
    _install_org(sim)
    calls = osc._callables(ORG, founder.public_hex)
    assert calls["newest_adopted_seq"]() is None
    assert calls["membership_proof_for"](None) == {"v": 1, "checkpoint_seq": 0, "index": 0, "path": []}
    assert calls["admission_ok_for"](0, founder.public_hex) is None


def test_with_nothing_retained_the_prover_uses_its_own_fold_and_rotates_back_to_its_claim():
    """OrgAdmissionBundleBound.tla ProveOwnFold + Rotate (master ac114f59):
    no usable retained record, so the rider is under the current fold's
    root; each refusal rotates to the fold at an earlier held head, back to
    this persona's admission claim, never past it."""
    sim, founder = org_with_owner()
    root_founder_only = mc.members_root(sim.fold())
    member = add_member(sim)
    root_two = mc.members_root(sim.fold())
    third = add_member(sim)
    root_three = mc.members_root(sim.fold())
    _install_org(sim)
    calls = osc._callables(ORG, member.public_hex)
    riders = [calls["membership_proof_for"](attempt=a) for a in range(4)]
    roots = []
    for rider in riders:
        # Recover the root each rider proves under by trying the known ones.
        for root in (root_three, root_two, root_founder_only):
            try:
                mc.verify_inclusion(root, member.public_hex, rider["index"], rider["path"])
                roots.append(root)
                break
            except mc.MembershipCommitmentError:
                continue
    # Newest first, then back to the member's own admission (root_two), then
    # around again; the founder-only root predates the member and is never
    # a candidate for it.
    assert roots == [root_three, root_two, root_three, root_two]
    assert all(r["checkpoint_seq"] == 0 for r in riders)  # label: no retained seq
    founder_calls = osc._callables(ORG, founder.public_hex)
    founder_roots = []
    for a in range(3):
        rider = founder_calls["membership_proof_for"](attempt=a)
        for root in (root_three, root_two, root_founder_only):
            try:
                mc.verify_inclusion(root, founder.public_hex, rider["index"], rider["path"])
                founder_roots.append(root)
                break
            except mc.MembershipCommitmentError:
                continue
    assert founder_roots == [root_three, root_two, root_founder_only]
    assert third.public_hex  # the third member's admission is the newest head


def test_retained_records_come_before_own_fold_candidates():
    sim, founder = org_with_owner()
    first = _record(sim, 0)
    add_member(sim)
    _install_org(sim)
    cp.record_adopted(ORG, first)
    calls = osc._callables(ORG, founder.public_hex)
    r0 = calls["membership_proof_for"](attempt=0)
    mc.verify_inclusion(first["members_root"], founder.public_hex, r0["index"], r0["path"])
    assert r0["checkpoint_seq"] == 0
    r1 = calls["membership_proof_for"](attempt=1)
    mc.verify_inclusion(mc.members_root(sim.fold()), founder.public_hex, r1["index"], r1["path"])


def test_own_fold_candidates_fold_only_at_leaf_changing_events_and_are_memoized(monkeypatch):
    """Reviewer (b)/(c) on 3d82dd60: a hello never re-folds; the candidate
    walk folds once per leaf-changing event back to the admission claim, so
    intervening non-membership events cost nothing."""
    from tools.network.ledger import store as ledger_store
    sim, founder = org_with_owner()
    member = add_member(sim)
    for _ in range(3):
        add_member(sim)                        # three leaf-changing events after
    # Non-leaf-changing events after the last claim: role definitions.
    for i in range(4):
        sim.role_define(sim.root, f"role{i}", ["link:publish"], requires="self")
    _install_org(sim)
    osc._own_fold_cache.clear()
    folds = []
    original = ledger_store.LedgerStore.fold

    def counting_fold(self, heads=None, now=None):
        folds.append(tuple(heads) if heads else ())
        return original(self, heads=heads, now=now)

    monkeypatch.setattr(ledger_store.LedgerStore, "fold", counting_fold)
    calls = osc._callables(ORG, member.public_hex)
    calls["membership_proof_for"](attempt=1)
    # Current fold + one per leaf-changing event back to the member's claim
    # (its own claim, three later claims), never the four role definitions.
    assert len(folds) == 1 + 4, folds
    folds.clear()
    calls["membership_proof_for"](attempt=2)
    assert folds == []  # memoized: same heads, no fold
