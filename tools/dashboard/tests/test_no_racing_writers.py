"""Grep-guard: startup_state has exactly two writers, by contract.

The June-18 FSM contract (musings/session-lifecycle-fsm-contract-2026-06-18.md)
requires that ONLY the lifecycle worker writes lifecycle state, with
``arm_startup_state`` as the single explicit FSM entry. The historical
multi-writer design (setup-exit watcher, screen-poller, inject tasks,
tailer clear) raced and produced the launching flip-flop/stuck class
(graph://afb67d11-7c4, validated 11x in dashboard.log).

This test fails when anyone reintroduces a ``startup_state`` write outside
the sanctioned sites. It scans SQL statements — the mechanism every writer
must ultimately go through.
"""
from __future__ import annotations

import re
from pathlib import Path

DASHBOARD = Path(__file__).resolve().parent.parent
REPO_ROOT = DASHBOARD.parent.parent

# Files allowed to contain SQL that writes startup_state, and why.
ALLOWED = {
    # The single lifecycle state writer (SessionLifecycleStateWriter).
    "tools/dashboard/session_lifecycle_worker.py",
    # _arm_startup_state_sql — the explicit FSM entry.
    "tools/dashboard/session_monitor.py",
    # Schema (column definition/migration) + revive_session's reset-to-NULL,
    # which prepares a row for the arm that immediately follows.
    "tools/dashboard/dao/dashboard_db.py",
}


def _sql_writes_startup_state(text: str) -> list[str]:
    """Return SQL-ish lines that UPDATE/INSERT startup_state."""
    hits = []
    for line in text.splitlines():
        if re.search(r"(?i)\bset\b[^\n]*startup_state\s*=", line) or re.search(
            r"(?i)update\s+tmux_sessions[^\n]*startup_state", line
        ):
            if line.strip().startswith("#") or line.strip().startswith("--"):
                continue
            hits.append(line.strip())
    return hits


def test_startup_state_sql_writers_are_sanctioned():
    offenders: dict[str, list[str]] = {}
    for py in DASHBOARD.rglob("*.py"):
        rel = str(py.relative_to(REPO_ROOT))
        if "/tests/" in rel or rel in ALLOWED:
            continue
        hits = _sql_writes_startup_state(py.read_text(errors="replace"))
        if hits:
            offenders[rel] = hits
    assert not offenders, (
        "startup_state must only be written by the lifecycle worker's "
        f"writer and arm_startup_state; found SQL writers in: {offenders}"
    )


def test_monitor_has_no_advance_writer():
    """The forward-only ``advance`` writer (and its callers: setup-exit
    watcher, poller advance, tailer clear) must stay deleted."""
    monitor = (DASHBOARD / "session_monitor.py").read_text()
    server = (DASHBOARD / "server.py").read_text()
    assert "def advance_startup_state" not in monitor
    assert "_advance_startup_state_sql" not in monitor
    assert "advance_startup_state(" not in server
    assert "_watch_setup_exit" not in server
    assert "_finish_project_session_create" not in server
