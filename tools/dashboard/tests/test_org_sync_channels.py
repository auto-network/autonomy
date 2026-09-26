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
