"""auto-diqwv: a launch that fails before a container exists backs off
(1, 5, 25 min), is noted once, then held -- instead of being reselected
every cycle (1,488 attempts in 3 h on 2026-09-23)."""
from __future__ import annotations

import pytest

import agents.dispatcher as dispatcher


@pytest.fixture
def bd(monkeypatch, tmp_path):
    monkeypatch.setattr(dispatcher, "LAUNCH_BACKOFF_PATH", tmp_path / "backoff.json")
    calls = []
    monkeypatch.setattr(dispatcher, "run_bd", lambda args, **k: calls.append(args) or "")
    monkeypatch.setattr(dispatcher, "_retry_bd", lambda args, **k: calls.append(args) or "")
    return calls


def _notes(calls):
    return [c for c in calls if "--append-notes" in c]


def test_at_most_three_attempts_in_the_first_half_hour_then_held(bd):
    t0 = 1_000_000.0
    attempts = []
    t = t0
    while t < t0 + 3600:
        if not dispatcher.launch_backoff_active("auto-x", now=t):
            attempts.append(t)
            if dispatcher.record_launch_failure("auto-x", "No Claude credentials found",
                                                now=t) == "held":
                break
        t += 8.0                      # the dispatcher's cycle
    assert len([a for a in attempts if a < t0 + 1800]) == 3
    assert len(attempts) == 4         # 0, +1, +6, +31 min; the 4th holds
    notes = _notes(bd)
    assert len(notes) == 2            # once on the first failure, once on hold
    held = notes[-1]
    assert "--remove-label" in held and "readiness:approved" in held
    assert not dispatcher.launch_backoff_active("auto-x", now=t + 1)


def test_every_failure_reopens_the_bead(bd):
    dispatcher.record_launch_failure("auto-y", "image missing", now=0.0)
    assert ["update", "auto-y", "-s", "open"] in bd


def test_a_successful_launch_clears_the_backoff(bd):
    dispatcher.record_launch_failure("auto-z", "worktree", now=0.0)
    assert dispatcher.launch_backoff_active("auto-z", now=1.0)
    dispatcher.clear_launch_backoff("auto-z")
    assert not dispatcher.launch_backoff_active("auto-z", now=1.0)


def test_pause_stops_selection_of_a_labelled_bead(monkeypatch, tmp_path):
    """The pause endpoint writes {label: true}; the dispatcher reads it as a
    paused label (the endpoint answers the whole map under "paused")."""
    monkeypatch.setattr(dispatcher, "DISPATCH_STATE_PATH", tmp_path / "dispatch.state")
    (tmp_path / "dispatch.state").write_text('{"dashboard": true}')
    assert dispatcher.get_paused_labels() == {"dashboard"}


def test_a_hold_that_bd_refuses_is_not_reported_and_stays_backed_off(bd, monkeypatch):
    """Review of 1155d6a6: if removing readiness:approved fails, the bead is
    still approved; keep it backed off and retry the hold, never report it."""
    def failing(args, **k):
        if "--remove-label" in args:
            raise dispatcher.BdCommandError(args, 1, "tracker unreachable")
        bd.append(args)
        return ""
    monkeypatch.setattr(dispatcher, "_retry_bd", failing)
    now = 0.0
    for _ in range(len(dispatcher.LAUNCH_BACKOFF_S)):
        dispatcher.record_launch_failure("auto-h", "credentials", now=now)
        now += 10_000
    assert dispatcher.record_launch_failure("auto-h", "credentials", now=now) == "hold_failed"
    assert dispatcher.launch_backoff_active("auto-h", now=now + 1)
    entry = dispatcher._read_launch_backoff()["auto-h"]
    assert entry["count"] == len(dispatcher.LAUNCH_BACKOFF_S)   # the hold is retried next
    assert entry["next_at"] == now + dispatcher.LAUNCH_BACKOFF_S[-1]
