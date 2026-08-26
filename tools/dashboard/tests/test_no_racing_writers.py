"""Grep-guard: session lifecycle state has exactly ONE writer, by contract.

The FSM contract (graph://92ed929a-3ec) requires that the transition
authority — ``SessionLifecycleStateWriter.transition`` on the shared
``STATE_AUTHORITY`` — is the only code that writes a session's lifecycle
state. The worker's steps, ``arm_startup_state`` (launch entry),
``mark_dead`` (death detection), and boot recovery are causes routed
through it. The historical multi-writer design (setup-exit watcher,
screen-poller, inject tasks, tailer clear, direct mark_dead/revive
column stamps) raced and produced the alive-and-dead-at-once class
(graph://afb67d11-7c4).

This test fails when anyone reintroduces a lifecycle-column write outside
the sanctioned sites. It scans SQL statements — the mechanism every writer
must ultimately go through. Guarded columns: ``state`` (the truth),
``startup_state`` / ``activity_state`` / ``is_live`` (write-through
projections until the drop), and ``attention`` (tracker-owned telemetry,
sanctioned only in its dedicated helper).
"""
from __future__ import annotations

import re
from pathlib import Path

DASHBOARD = Path(__file__).resolve().parent.parent
REPO_ROOT = DASHBOARD.parent.parent

# Files allowed to contain SQL that writes lifecycle columns, and why.
ALLOWED = {
    # The transition authority (SessionLifecycleStateWriter.transition).
    "tools/dashboard/session_lifecycle_worker.py",
    # Schema (column definition/migration/backfill), insert_session birth
    # states, and update_activity_state — the ACTIVE-guarded attention
    # telemetry writer (stamps the legacy activity_state projection
    # alongside until the column drop).
    "tools/dashboard/dao/dashboard_db.py",
}

_WRITE_RE = re.compile(
    r"(?i)\bset\b[^\n]*\b(state|startup_state|activity_state|is_live|attention)\s*=",
)


def _sql_writes_lifecycle_columns(text: str) -> list[str]:
    """Return SQL-ish lines that UPDATE a guarded lifecycle column.

    A line that names the table it updates is guarded only when that table
    is ``tmux_sessions`` — other stores legitimately own an unrelated
    ``state`` column (e.g. the web-push outbox/delivery queues). A SET on
    a continuation line with no table in sight stays guarded: better a
    loud false positive here than a silent second lifecycle writer."""
    hits = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith("--"):
            continue
        if not _WRITE_RE.search(line):
            continue
        table = re.search(r"(?i)\bupdate\s+([a-z_][a-z0-9_]*)", line)
        if table and table.group(1).lower() != "tmux_sessions":
            continue
        hits.append(stripped)
    return hits


def test_lifecycle_sql_writers_are_sanctioned():
    offenders: dict[str, list[str]] = {}
    for py in DASHBOARD.rglob("*.py"):
        rel = str(py.relative_to(REPO_ROOT))
        if "/tests/" in rel or rel in ALLOWED:
            continue
        hits = _sql_writes_lifecycle_columns(py.read_text(errors="replace"))
        if hits:
            offenders[rel] = hits
    assert not offenders, (
        "lifecycle state must only be written by the transition authority "
        f"(and dashboard_db's sanctioned sites); found SQL writers in: {offenders}"
    )


def test_deleted_writers_stay_deleted():
    """The racing writers removed by the FSM completion must not return."""
    monitor = (DASHBOARD / "session_monitor.py").read_text()
    server = (DASHBOARD / "server.py").read_text()
    db = (DASHBOARD / "dao" / "dashboard_db.py").read_text()
    assert "def advance_startup_state" not in monitor
    assert "_advance_startup_state_sql" not in monitor
    assert "_arm_startup_state_sql" not in monitor
    assert "advance_startup_state(" not in server
    assert "_watch_setup_exit" not in server
    assert "_finish_project_session_create" not in server
    # mark_dead and revive_session no longer stamp lifecycle columns.
    assert "SET is_live=0, activity_state='dead'" not in db
    assert "startup_state=NULL" not in db


def test_transition_legality_matrix_is_total():
    """Every state maps to a legal-move set; every target is a real state."""
    from tools.dashboard.session_lifecycle_worker import _LEGAL_TRANSITIONS

    states = {"LAUNCHING", "ACTIVE", "STOPPING", "ENDED", "FAILED"}
    assert set(_LEGAL_TRANSITIONS.keys()) == states | {None}
    for src, targets in _LEGAL_TRANSITIONS.items():
        assert targets <= states, (src, targets)
        if src is not None:
            # Self-transition is always representable (idempotent updates)
            # except where forbidden by design; assert targets non-empty.
            assert targets, src
