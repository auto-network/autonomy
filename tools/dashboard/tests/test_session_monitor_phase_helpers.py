"""Tests for SessionMonitor's unified startup_state FSM.

Covers:
- advance_startup_state: forward-only transitions, setup_failed sticky-terminal,
  clear-on-None semantics, broadcast-on-real-change.
- register_pending + update_phase: thin wrappers around the FSM helper.
- _process_tail_entries clear-on-assistant-role: when a JSONL append carries
  an assistant-role entry, advance_startup_state(None) must fire AND broadcast,
  so the chip flips from "Awaiting first reply" the moment the model speaks.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from tools.dashboard.dao import dashboard_db


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Pin dashboard.db to a fresh per-test sqlite file.

    Closes the prior module-level connection before reinit so the SQLite
    write lock from the previous test releases — otherwise later writes
    fail with "database is locked".
    """
    db_path = tmp_path / "dashboard.db"
    monkeypatch.setenv("DASHBOARD_DB", str(db_path))
    prior = getattr(dashboard_db, "_conn", None)
    if prior is not None:
        try:
            prior.close()
        except Exception:
            pass
    dashboard_db._conn = None  # type: ignore[attr-defined]
    dashboard_db._DB_PATH = db_path  # type: ignore[attr-defined]
    conn = dashboard_db.get_conn()
    yield conn
    try:
        conn.close()
    except Exception:
        pass
    dashboard_db._conn = None  # type: ignore[attr-defined]


@pytest.fixture
def monitor():
    """SessionMonitor with a mock event_bus so broadcasts are observable."""
    from tools.dashboard.session_monitor import SessionMonitor
    m = SessionMonitor()
    m._event_bus = AsyncMock()
    m._event_bus.broadcast = AsyncMock()
    yield m


def _read_state(tmux_name: str) -> str | None:
    conn = dashboard_db.get_conn()
    row = conn.execute(
        "SELECT startup_state FROM tmux_sessions WHERE tmux_name = ?", (tmux_name,),
    ).fetchone()
    return None if row is None else row[0]


def _broadcast_count(monitor) -> int:
    return monitor._event_bus.broadcast.await_count


# ── advance_startup_state: forward-only rule ────────────────────────


@pytest.mark.asyncio
async def test_advance_from_null_to_requesting_writes(db, monitor):
    await monitor.register_pending("auto-test", project="x")
    assert _read_state("auto-test") == "requesting"


@pytest.mark.asyncio
async def test_advance_forward_writes_and_broadcasts(db, monitor):
    await monitor.register_pending("auto-test", project="x")
    monitor._event_bus.broadcast.reset_mock()
    changed = await monitor.advance_startup_state("auto-test", "preparing_workspace")
    assert changed is True
    assert _read_state("auto-test") == "preparing_workspace"
    assert _broadcast_count(monitor) == 1


@pytest.mark.asyncio
async def test_advance_backward_refuses_no_broadcast(db, monitor):
    await monitor.register_pending("auto-test", project="x")
    await monitor.advance_startup_state("auto-test", "composer_ready")
    monitor._event_bus.broadcast.reset_mock()
    changed = await monitor.advance_startup_state("auto-test", "requesting")
    assert changed is False
    assert _read_state("auto-test") == "composer_ready"
    assert _broadcast_count(monitor) == 0


@pytest.mark.asyncio
async def test_advance_same_state_no_broadcast(db, monitor):
    await monitor.register_pending("auto-test", project="x")
    monitor._event_bus.broadcast.reset_mock()
    changed = await monitor.advance_startup_state("auto-test", "requesting")
    assert changed is False
    assert _broadcast_count(monitor) == 0


# ── setup_failed: sticky terminal off-ramp ──────────────────────────


@pytest.mark.asyncio
async def test_setup_failed_writes_from_any_state(db, monitor):
    await monitor.register_pending("auto-test", project="x")
    await monitor.advance_startup_state("auto-test", "preparing_workspace")
    monitor._event_bus.broadcast.reset_mock()
    changed = await monitor.advance_startup_state("auto-test", "setup_failed")
    assert changed is True
    assert _read_state("auto-test") == "setup_failed"
    assert _broadcast_count(monitor) == 1


@pytest.mark.asyncio
async def test_setup_failed_writes_from_late_state_too(db, monitor):
    await monitor.register_pending("auto-test", project="x")
    await monitor.advance_startup_state("auto-test", "awaiting_first_response")
    monitor._event_bus.broadcast.reset_mock()
    changed = await monitor.advance_startup_state("auto-test", "setup_failed")
    assert changed is True
    assert _read_state("auto-test") == "setup_failed"


@pytest.mark.asyncio
async def test_setup_failed_idempotent(db, monitor):
    await monitor.register_pending("auto-test", project="x")
    await monitor.advance_startup_state("auto-test", "setup_failed")
    monitor._event_bus.broadcast.reset_mock()
    changed = await monitor.advance_startup_state("auto-test", "setup_failed")
    assert changed is False
    assert _broadcast_count(monitor) == 0


# ── Clearing to NULL: terminal-success path ─────────────────────────


@pytest.mark.asyncio
async def test_clear_succeeds_from_non_failed_state(db, monitor):
    await monitor.register_pending("auto-test", project="x")
    await monitor.advance_startup_state("auto-test", "awaiting_first_response")
    monitor._event_bus.broadcast.reset_mock()
    changed = await monitor.advance_startup_state("auto-test", None)
    assert changed is True
    assert _read_state("auto-test") is None
    assert _broadcast_count(monitor) == 1


@pytest.mark.asyncio
async def test_clear_already_null_no_op(db, monitor):
    dashboard_db.insert_session(
        tmux_name="auto-test", session_type="container", project="x",
    )
    monitor._event_bus.broadcast.reset_mock()
    changed = await monitor.advance_startup_state("auto-test", None)
    assert changed is False
    assert _read_state("auto-test") is None
    assert _broadcast_count(monitor) == 0


@pytest.mark.asyncio
async def test_clear_refused_when_setup_failed_sticks(db, monitor):
    """setup_failed never gets cleared by the tailer — operator must see it."""
    await monitor.register_pending("auto-test", project="x")
    await monitor.advance_startup_state("auto-test", "setup_failed")
    monitor._event_bus.broadcast.reset_mock()
    changed = await monitor.advance_startup_state("auto-test", None)
    assert changed is False
    assert _read_state("auto-test") == "setup_failed"
    assert _broadcast_count(monitor) == 0


# ── Unknown tmux_name ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_advance_unknown_session_no_op(db, monitor):
    changed = await monitor.advance_startup_state("auto-nonexistent", "requesting")
    assert changed is False
    assert _broadcast_count(monitor) == 0


# ── register_pending: seeds the FSM ─────────────────────────────────


@pytest.mark.asyncio
async def test_register_pending_seeds_requesting(db, monitor):
    await monitor.register_pending(
        "auto-test", session_type="container", project="enterprise-ng", harness="claude",
    )
    assert _read_state("auto-test") == "requesting"


@pytest.mark.asyncio
async def test_register_pending_idempotent_on_duplicate(db, monitor):
    await monitor.register_pending("auto-test", project="x")
    await monitor.register_pending("auto-test", project="x")
    assert _read_state("auto-test") == "requesting"


# ── update_phase: thin wrapper over advance + progress ──────────────


@pytest.mark.asyncio
async def test_update_phase_advances_state(db, monitor):
    await monitor.register_pending("auto-test", project="x")
    monitor._event_bus.broadcast.reset_mock()
    await monitor.update_phase("auto-test", startup_state="preparing_workspace")
    assert _read_state("auto-test") == "preparing_workspace"
    assert _broadcast_count(monitor) == 1


@pytest.mark.asyncio
async def test_update_phase_clear_when_clear_flag_set(db, monitor):
    await monitor.register_pending("auto-test", project="x")
    await monitor.update_phase("auto-test", startup_state="awaiting_first_response")
    monitor._event_bus.broadcast.reset_mock()
    await monitor.update_phase("auto-test", clear_startup_state=True)
    assert _read_state("auto-test") is None
    assert _broadcast_count(monitor) == 1


@pytest.mark.asyncio
async def test_update_phase_progress_only_broadcasts(db, monitor):
    """progress-only updates broadcast once so the in-memory progress dict
    flushes to the registry payload."""
    await monitor.register_pending("auto-test", project="x")
    await monitor.update_phase("auto-test", startup_state="preparing_workspace")
    monitor._event_bus.broadcast.reset_mock()
    await monitor.update_phase(
        "auto-test",
        progress={"repo_index": 2, "total": 3, "current_repo": "autonomy"},
    )
    assert _broadcast_count(monitor) == 1
    assert monitor._phase_progress["auto-test"]["repo_index"] == 2


# ── Tailer clear-on-assistant-role (the prod outage we just fixed) ──


@pytest.mark.asyncio
async def test_tailer_clears_on_assistant_entry_and_broadcasts(db, monitor):
    """Models the exact path _process_tail_entries takes when an
    assistant-role entry is parsed: clear startup_state to NULL AND
    broadcast. Without the broadcast the chip stays on 'Awaiting first
    reply' until some unrelated event triggers one."""
    await monitor.register_pending("auto-test", project="x")
    await monitor.update_phase("auto-test", startup_state="awaiting_first_response")
    monitor._event_bus.broadcast.reset_mock()

    new_entries = [{"role": "assistant", "content": "hi"}]
    if any(e.get("role") == "assistant" for e in new_entries):
        await monitor.advance_startup_state("auto-test", None)

    assert _read_state("auto-test") is None
    assert _broadcast_count(monitor) == 1


@pytest.mark.asyncio
async def test_tailer_does_not_clear_on_user_only_entries(db, monitor):
    """Orientation echo lands as a user-role entry. Must NOT clear."""
    await monitor.register_pending("auto-test", project="x")
    await monitor.update_phase("auto-test", startup_state="awaiting_first_response")
    monitor._event_bus.broadcast.reset_mock()

    new_entries = [{"role": "user", "content": "orientation message"}]
    if any(e.get("role") == "assistant" for e in new_entries):
        await monitor.advance_startup_state("auto-test", None)

    assert _read_state("auto-test") == "awaiting_first_response"
    assert _broadcast_count(monitor) == 0


@pytest.mark.asyncio
async def test_tailer_clears_on_thinking_entry(db, monitor):
    """thinking entries are model-authored — role=assistant per the harness
    adapter. Should clear."""
    await monitor.register_pending("auto-test", project="x")
    await monitor.update_phase("auto-test", startup_state="awaiting_first_response")
    monitor._event_bus.broadcast.reset_mock()

    new_entries = [{"role": "assistant", "type": "thinking", "content": "..."}]
    if any(e.get("role") == "assistant" for e in new_entries):
        await monitor.advance_startup_state("auto-test", None)

    assert _read_state("auto-test") is None
    assert _broadcast_count(monitor) == 1


# ── Registry payload includes startup_state ─────────────────────────


@pytest.mark.asyncio
async def test_registry_payload_includes_startup_state(db, monitor):
    await monitor.register_pending("auto-test", project="x")
    await monitor.update_phase("auto-test", startup_state="preparing_workspace")
    payload = monitor.get_registry()
    entry = next(e for e in payload if e["session_id"] == "auto-test")
    assert entry["startup_state"] == "preparing_workspace"


@pytest.mark.asyncio
async def test_registry_payload_startup_state_null_after_clear(db, monitor):
    await monitor.register_pending("auto-test", project="x")
    await monitor.update_phase("auto-test", startup_state="awaiting_first_response")
    await monitor.update_phase("auto-test", clear_startup_state=True)
    payload = monitor.get_registry()
    entry = next(e for e in payload if e["session_id"] == "auto-test")
    assert entry["startup_state"] is None
