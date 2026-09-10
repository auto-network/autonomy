"""The dashboard's half of the relay fallback (auto-ew9wf).

It does not dial: fleet_relay_connect needs the adapter in the CONNECTOR
process. It sends an operation and polls, keeping the controller on this side
— this machine still has exactly one opener per peer.

Written after the delegate imported cleanly, built cleanly, and would have
raised NameError the first time a peer's direct addresses failed, because
asyncio was not imported in the module. "It imports" and "it runs" are
different claims, so these drive it.
"""

from __future__ import annotations

import asyncio

import pytest

from tools.dashboard import fleet_enrollment_routes as fer
from tools.dashboard import link_serving_supervisor as sup

PEER = "ab" * 32


@pytest.fixture
def control(monkeypatch):
    """Stand in for the connector's control socket, recording the exchange."""
    state = {"calls": [], "start": None, "statuses": []}

    def fake_control(org, op, args, timeout=30.0):
        state["calls"].append((op, args))
        if op == "fleet-relay-pull":
            return state["start"] or {
                "ok": True, "operation_id": args["operation_id"],
                "state": "running",
            }
        return state["statuses"].pop(0)

    monkeypatch.setattr(sup, "control", fake_control)
    return state


def _run(delegate):
    return asyncio.run(delegate(
        peer_machine_pub=PEER, scope="autonomy", operation_id="op-1",
    ))


def test_a_completed_pull_reports_relay_and_its_slot_source(control):
    control["statuses"] = [{"ok": True, "state": "done", "scope": "autonomy",
                            "slot_source": "org-slots"}]
    out = _run(fer._relay_pull_delegate(poll_interval=0.01))
    assert out == {"outcome": "ok", "channel": "relay",
                   "slot_source": "org-slots"}
    assert [op for op, _ in control["calls"]] == [
        "fleet-relay-pull", "fleet-relay-pull-status",
    ]


def test_the_dashboard_sends_no_slot_and_lets_the_connector_resolve(control):
    """It knows a peer by durable roster key; which slot that peer serves
    under is the relay's answer, and the relay connection is over there."""
    control["statuses"] = [{"ok": True, "state": "done", "scope": "autonomy"}]
    _run(fer._relay_pull_delegate(poll_interval=0.01))
    _, args = control["calls"][0]
    assert args["peer_machine_pub"] == PEER
    assert "persona_pub" not in args and "machine" not in args


def test_it_polls_until_the_pull_finishes(control):
    control["statuses"] = [
        {"ok": True, "state": "running"},
        {"ok": True, "state": "running"},
        {"ok": True, "state": "done", "scope": "autonomy"},
    ]
    _run(fer._relay_pull_delegate(poll_interval=0.01))
    polls = [op for op, _ in control["calls"] if op == "fleet-relay-pull-status"]
    assert len(polls) == 3


def test_a_failure_carries_the_carriers_fields_for_the_controller(control):
    """reason/pair_id/code decide stand-down vs retry vs stop upstream, so the
    delegate must raise with them attached rather than flatten them."""
    control["statuses"] = [{
        "ok": True, "state": "failed", "error": "closed",
        "reason": None, "pair_id": "pair-9", "code": 6,
    }]
    with pytest.raises(Exception) as caught:
        _run(fer._relay_pull_delegate(poll_interval=0.01))
    assert caught.value.pair_id == "pair-9"
    assert caught.value.code == 6


def test_a_refusal_to_start_is_raised_not_returned(control):
    control["start"] = {"ok": False, "error": "not-armed"}
    with pytest.raises(Exception, match="refused"):
        _run(fer._relay_pull_delegate(poll_interval=0.01))


def test_an_unknown_operation_is_not_retried_under_the_same_id(control):
    """The connector restarted mid-pull, or retention expired. Distinct from
    failure: the controller mints a fresh id rather than re-polling a job
    nobody remembers."""
    control["statuses"] = [{"ok": False, "error_kind": "unknown-operation"}]
    with pytest.raises(Exception, match="not known to the connector"):
        _run(fer._relay_pull_delegate(poll_interval=0.01))


def test_it_gives_up_at_the_deadline_rather_than_polling_forever(control):
    control["statuses"] = [{"ok": True, "state": "running"}] * 50
    with pytest.raises(Exception, match="did not finish"):
        _run(fer._relay_pull_delegate(poll_interval=0.01, deadline=0.02))
