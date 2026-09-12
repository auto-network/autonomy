"""auto-0yxpm — dispatch-nag delivery routing, honest rc, single-fire.

The dispatcher must NOT touch tmux for nag delivery (it runs in a container
with no host tmux socket). It POSTs to the dashboard's
/api/monitor/dispatch-nag endpoint, logs a real WARN for anything that did
not land (never a false success), and fires exactly one nag per completion.
"""
from unittest.mock import patch
import time

from agents import dispatcher
from agents.dispatcher import (
    DispatchResult,
    RunningAgent,
    _log_dispatch_nag_result,
    _notify_dispatch_nag,
)


def _agent(**overrides):
    defaults = dict(
        bead_id="auto-0yxpm",
        container_name="agent-auto-0yxpm-1",
        container_id="abc",
        output_dir="/tmp/agent-runs/auto-0506-001257",
        worktree_path="/tmp/wt",
        branch="agent/auto-0yxpm",
        branch_base="base",
        image="autonomy-session",
        started_at=time.time() - 90,
    )
    defaults.update(overrides)
    return RunningAgent(**defaults)


# ── Single-fire (auto-0yxpm) ────────────────────────────────────────────


def _stub_nag_sessions(monkeypatch, targets):
    """Patch the real dashboard_db so init_db is a no-op and the nag
    subscriber list is fixed. `from ... import dashboard_db` returns the
    real module object, so patch its attributes rather than sys.modules."""
    from tools.dashboard.dao import dashboard_db as real_db
    monkeypatch.setattr(real_db, "init_db", lambda *a, **k: None)
    monkeypatch.setattr(real_db, "get_dispatch_nag_sessions", lambda: list(targets))
    monkeypatch.setattr(dispatcher, "run_bd", lambda *a, **k: "")


def test_notify_fires_exactly_once_per_completion(monkeypatch):
    """A re-entered collection path for the same run nags only once."""
    dispatcher._nagged_run_ids.clear()
    calls = []
    monkeypatch.setattr(dispatcher, "_send_dispatch_nag_crosstalk",
                        lambda targets, msg: calls.append((targets, msg)))
    _stub_nag_sessions(monkeypatch, ["auto-live-1"])

    agent = _agent()
    result = DispatchResult(bead_id=agent.bead_id, exit_code=0)
    with patch.object(dispatcher.subprocess, "run") as _sp:
        _sp.return_value.stdout = ""
        _notify_dispatch_nag(agent, "DONE", result)
        _notify_dispatch_nag(agent, "DONE", result)  # double-fire attempt

    assert len(calls) == 1, f"expected one nag, got {len(calls)}"


def test_distinct_runs_each_nag(monkeypatch):
    dispatcher._nagged_run_ids.clear()
    calls = []
    monkeypatch.setattr(dispatcher, "_send_dispatch_nag_crosstalk",
                        lambda targets, msg: calls.append((targets, msg)))
    _stub_nag_sessions(monkeypatch, ["auto-live-1"])

    with patch.object(dispatcher.subprocess, "run") as _sp:
        _sp.return_value.stdout = ""
        _notify_dispatch_nag(_agent(output_dir="/tmp/agent-runs/run-a"),
                             "DONE", DispatchResult(bead_id="a", exit_code=0))
        _notify_dispatch_nag(_agent(output_dir="/tmp/agent-runs/run-b"),
                             "DONE", DispatchResult(bead_id="b", exit_code=0))

    assert len(calls) == 2


# ── Honest delivery reporting (auto-0yxpm) ──────────────────────────────


def test_log_result_success_only_for_delivered(capsys):
    _log_dispatch_nag_result({
        "delivered": ["auto-live-1"], "failed": [], "offline": [],
    })
    err = capsys.readouterr().err
    assert "dispatch nag -> auto-live-1" in err
    assert "WARN" not in err


def test_log_result_failed_is_warn_not_success(capsys):
    """The exact regression: a failed delivery must NOT print a success
    line and MUST log a WARN carrying the underlying stderr."""
    _log_dispatch_nag_result({
        "delivered": [],
        "failed": [{"target": "auto-live-1",
                    "error": "tmux paste-buffer rc=1: error connecting to "
                             "/tmp/tmux-0/default"}],
        "offline": [],
    })
    err = capsys.readouterr().err
    assert "dispatch nag -> auto-live-1" not in err  # no phantom success
    assert "WARN" in err
    assert "/tmp/tmux-0/default" in err


def test_log_result_offline_is_warn(capsys):
    _log_dispatch_nag_result({
        "delivered": [], "failed": [], "offline": ["auto-dead"],
    })
    err = capsys.readouterr().err
    assert "dispatch nag -> auto-dead" not in err
    assert "WARN" in err


# ── The dispatcher does NOT shell out to tmux for delivery ──────────────


def test_send_crosstalk_does_not_touch_tmux(monkeypatch):
    """Under PYTEST guard the send is a no-op, but critically it never
    imports/uses tmux. Removing the guard, delivery must go via the POST
    helper, not subprocess tmux."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    posted = {}

    def fake_post(targets, msg):
        posted["call"] = (targets, msg)
        return {"delivered": list(targets), "failed": [], "offline": []}

    monkeypatch.setattr(dispatcher, "_post_dispatch_nag", fake_post)
    with patch.object(dispatcher.subprocess, "run") as sp_run:
        dispatcher._send_dispatch_nag_crosstalk(["auto-live-1"], "msg")
    assert posted["call"] == (["auto-live-1"], "msg")
    sp_run.assert_not_called()
