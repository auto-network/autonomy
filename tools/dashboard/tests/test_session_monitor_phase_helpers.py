"""Tests for SessionMonitor.register_pending + update_phase (auto-ja51w C2).

The two new helpers form the dashboard's register-early pattern: the row
gets INSERTed at session-create POST entry, before prepare_session_mounts
runs, so the dashboard can broadcast per-step progress during the 7-9s
of host-side git fetches + credential resolution.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tools.dashboard.dao import dashboard_db


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Pin dashboard.db to a fresh per-test sqlite file."""
    db_path = tmp_path / "dashboard.db"
    monkeypatch.setenv("DASHBOARD_DB", str(db_path))
    # Force reconnection
    dashboard_db._conn = None  # type: ignore[attr-defined]
    conn = dashboard_db.get_conn()
    yield conn
    dashboard_db._conn = None  # type: ignore[attr-defined]


@pytest.fixture
def monitor():
    """Fresh SessionMonitor instance (no global state)."""
    from tools.dashboard.session_monitor import SessionMonitor
    m = SessionMonitor()
    yield m


def _read_row(tmux_name: str) -> dict | None:
    conn = dashboard_db.get_conn()
    row = conn.execute(
        "SELECT * FROM tmux_sessions WHERE tmux_name = ?", (tmux_name,)
    ).fetchone()
    return dict(row) if row else None


# ── register_pending ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_register_pending_inserts_row(db, monitor):
    """register_pending creates a row with the seed setup_phase."""
    await monitor.register_pending(
        "auto-test-001",
        session_type="container",
        project="enterprise-ng",
        harness="claude",
        setup_phase="requesting",
    )
    row = _read_row("auto-test-001")
    assert row is not None
    assert row["type"] == "container"
    assert row["project"] == "enterprise-ng"
    assert row["harness"] == "claude"
    assert row["setup_phase"] == "requesting"
    assert row["is_live"] == 1


@pytest.mark.asyncio
async def test_register_pending_no_jsonl_path(db, monitor):
    """register_pending writes no jsonl_path/resolution_dir — that's the
    normal register() call's job later."""
    await monitor.register_pending(
        "auto-test-002",
        session_type="container",
        project="enterprise-v5",
    )
    row = _read_row("auto-test-002")
    assert row is not None
    assert row["jsonl_path"] is None
    assert row["resolution_dir"] is None


@pytest.mark.asyncio
async def test_register_pending_idempotent_on_duplicate(db, monitor):
    """A second register_pending for the same tmux_name no-ops the INSERT
    but still applies the setup_phase update (so callers can re-invoke
    safely from re-entrant create paths)."""
    await monitor.register_pending(
        "auto-test-003",
        session_type="container",
        project="x",
        setup_phase="requesting",
    )
    await monitor.register_pending(
        "auto-test-003",
        session_type="container",
        project="x",
        setup_phase="preparing_workspace",
    )
    row = _read_row("auto-test-003")
    assert row is not None
    assert row["setup_phase"] == "preparing_workspace"


@pytest.mark.asyncio
async def test_register_pending_creates_tail_state(db, monitor):
    """Tail state gets a needs_resolution=True placeholder so the later
    register() call is idempotent."""
    await monitor.register_pending("auto-test-004", project="x")
    assert "auto-test-004" in monitor._tail_states
    ts = monitor._tail_states["auto-test-004"]
    assert ts.needs_resolution is True


# ── update_phase ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_phase_persists_setup_phase(db, monitor):
    await monitor.register_pending("auto-test-010", project="x", setup_phase="requesting")
    await monitor.update_phase("auto-test-010", setup_phase="preparing_workspace")
    row = _read_row("auto-test-010")
    assert row["setup_phase"] == "preparing_workspace"


@pytest.mark.asyncio
async def test_update_phase_persists_harness_phase(db, monitor):
    await monitor.register_pending("auto-test-011", project="x", setup_phase="requesting")
    await monitor.update_phase(
        "auto-test-011",
        setup_phase="setup_complete",
        harness_phase="composer_ready",
    )
    row = _read_row("auto-test-011")
    assert row["setup_phase"] == "setup_complete"
    assert row["harness_phase"] == "composer_ready"


@pytest.mark.asyncio
async def test_update_phase_progress_in_registry(db, monitor):
    """progress dict surfaces in the registry payload under phase_progress."""
    await monitor.register_pending(
        "auto-test-012",
        project="enterprise-ng",
        setup_phase="preparing_workspace",
    )
    await monitor.update_phase(
        "auto-test-012",
        progress={"repo_index": 1, "total": 3, "current_repo": "autonomy"},
    )
    reg = monitor.get_registry()
    entry = next((e for e in reg if e["session_id"] == "auto-test-012"), None)
    assert entry is not None
    assert entry["phase_progress"] == {
        "repo_index": 1, "total": 3, "current_repo": "autonomy",
    }


@pytest.mark.asyncio
async def test_update_phase_progress_omitted_when_absent(db, monitor):
    """Registry entry has no phase_progress key when none set."""
    await monitor.register_pending("auto-test-013", project="x")
    reg = monitor.get_registry()
    entry = next((e for e in reg if e["session_id"] == "auto-test-013"), None)
    assert entry is not None
    assert "phase_progress" not in entry


@pytest.mark.asyncio
async def test_update_phase_progress_empty_dict_clears(db, monitor):
    """Passing progress={} explicitly clears any prior progress dict."""
    await monitor.register_pending("auto-test-014", project="x")
    await monitor.update_phase(
        "auto-test-014",
        progress={"repo_index": 2, "total": 3, "current_repo": "x"},
    )
    assert "auto-test-014" in monitor._phase_progress
    await monitor.update_phase("auto-test-014", progress={})
    assert "auto-test-014" not in monitor._phase_progress


@pytest.mark.asyncio
async def test_update_phase_progress_none_preserves_prior(db, monitor):
    """progress=None (default) leaves any existing progress dict untouched."""
    await monitor.register_pending("auto-test-015", project="x")
    await monitor.update_phase(
        "auto-test-015",
        progress={"repo_index": 1, "total": 3, "current_repo": "x"},
    )
    await monitor.update_phase("auto-test-015", setup_phase="preparing_workspace")
    assert monitor._phase_progress.get("auto-test-015") == {
        "repo_index": 1, "total": 3, "current_repo": "x",
    }
