"""Dashboard membership plane (auto-3bhy3): riders, mismatch alarm, re-prove.

The mismatch behavior is the bead's acceptance case: a registry state whose
members_root contradicts the local fold's commitment refuses the rider,
records the operator alarm naming the contested checkpoint, and therefore
withholds re-prove.
"""

from __future__ import annotations

import pytest

from tools.dashboard import membership_plane as mp
from tools.network.ledger.membership_commitment import compute_root, verify_inclusion

A, B, C = "11" * 32, "22" * 32, "33" * 32
ORG = "test-org"


@pytest.fixture(autouse=True)
def _clean_alarms():
    mp.clear_alarm(ORG)
    yield
    mp.clear_alarm(ORG)


def test_rider_builds_and_verifies():
    members = [A, B, C]
    rider = mp.build_rider(B, 4, compute_root(members), members, org=ORG)
    assert rider["v"] == 1 and rider["checkpoint_seq"] == 4
    verify_inclusion(compute_root(members), B, rider["index"], rider["path"])
    assert mp.membership_alarm(ORG) is None


def test_mismatch_refuses_and_alarms():
    members = [A, B]
    contested = compute_root([A, B, C])  # the registry's forged view
    with pytest.raises(mp.MembershipPlaneError, match="contested"):
        mp.build_rider(B, 9, contested, members, org=ORG)
    alarm = mp.membership_alarm(ORG)
    assert alarm is not None and "seq 9" in alarm and "reset checkpoint" in alarm


def test_non_member_persona_refused_without_alarm():
    members = [A, B]
    with pytest.raises(mp.MembershipPlaneError, match="not in the committed"):
        mp.build_rider(C, 2, compute_root(members), members, org=ORG)
    assert mp.membership_alarm(ORG) is None  # a local gap, not a contested chain


def test_reprove_withheld_on_mismatch(monkeypatch):
    calls = []

    def fake_control(org, op, args):  # must never be reached
        calls.append((org, op, args))
        return {"ok": True}

    import tools.dashboard.link_serving_supervisor as sup
    monkeypatch.setattr(sup, "control", fake_control)
    monkeypatch.setattr(
        mp, "commitment_for_org",
        lambda org, at_head=None: {"members": (A, B),
                                   "members_root": compute_root([A, B])})
    registry_state = {"seq": 3, "members_root": compute_root([A, B, C])}
    with pytest.raises(mp.MembershipPlaneError, match="contested"):
        mp.reprove_over_tunnel(ORG, A, registry_state)
    assert calls == []  # the control frame was withheld


def test_reprove_sends_rider_when_consistent(monkeypatch):
    sent = {}

    def fake_control(org, op, args):
        sent.update(org=org, op=op, args=args)
        return {"ok": True, "seq": 3}

    import tools.dashboard.link_serving_supervisor as sup
    monkeypatch.setattr(sup, "control", fake_control)
    monkeypatch.setattr(
        mp, "commitment_for_org",
        lambda org, at_head=None: {"members": (A, B),
                                   "members_root": compute_root([A, B])})
    registry_state = {"seq": 3, "members_root": compute_root([A, B])}
    reply = mp.reprove_over_tunnel(ORG, A, registry_state)
    assert reply["ok"] is True
    assert sent["op"] == "re-prove-membership"
    assert sent["args"]["checkpoint_seq"] == 3
    verify_inclusion(compute_root([A, B]), A,
                     sent["args"]["index"], sent["args"]["path"])
