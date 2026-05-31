"""auto-rsvzk integration: tailer-side echo matching round-trip.

Direct unit tests in test_pending_outbound.py cover the ring itself
and api_session_send wiring. This file closes the remaining gap —
exercising the actual `_process_tail_entries` path in
``session_monitor.SessionMonitor`` to verify the client_id from a
recorded send round-trips onto the broadcast ``session:messages``
payload.

Tested with a minimal SessionMonitor instance + mocked event bus.
No tmux, no inotify, no real DB writes — but the actual
``_process_tail_entries`` code path under test.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

from tools.dashboard import pending_outbound, session_monitor as sm
from tools.dashboard.dao import dashboard_db


@pytest.fixture(autouse=True)
def _clear_ring():
    pending_outbound._buffers.clear()
    yield
    pending_outbound._buffers.clear()


def _mock_bus():
    """AsyncMock that handles both async (broadcast) and sync
    (update_cache) calls cleanly. Without this the test suite emits
    a ``coroutine was never awaited`` warning on update_cache.
    """
    from unittest.mock import MagicMock
    bus = AsyncMock()
    bus.update_cache = MagicMock()  # sync
    return bus


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Fresh sqlite for the session row."""
    monkeypatch.setenv("DASHBOARD_DB", str(tmp_path / "test.db"))
    import importlib
    importlib.reload(dashboard_db)
    yield tmp_path / "test.db"
    if dashboard_db._conn is not None:
        dashboard_db._conn.close()
    dashboard_db._conn = None


def _seed_session(tmp_path, tmux_name: str = "auto-int") -> dict:
    """Insert a minimal tmux_sessions row with a real JSONL on disk."""
    jsonl = tmp_path / "test.jsonl"
    jsonl.write_text("")  # Empty but exists — broadcast reads its size.
    conn = dashboard_db.get_conn()
    conn.execute(
        "INSERT INTO tmux_sessions "
        "(tmux_name, type, project, harness, created_at, jsonl_path) "
        "VALUES (?, 'container', 'autonomy', 'claude', ?, ?)",
        (tmux_name, time.time(), str(jsonl)),
    )
    conn.commit()
    return dashboard_db.get_session(tmux_name)


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


@pytest.mark.asyncio
async def test_tailer_attaches_client_id_to_user_turn_broadcast(temp_db):
    """End-to-end: record a send, then drive _process_tail_entries with
    a synthetic user-turn whose text matches. The broadcast payload
    must carry the matched client_id on the entry."""
    row = _seed_session(temp_db.parent)
    pending_outbound.record_send("auto-int", "cid-xyz", "hello world")

    monitor = sm.SessionMonitor()
    monitor._event_bus = _mock_bus()
    # Stub out the harness so harness.parse_line etc. aren't invoked
    # inside _process_tail_entries — we're testing the match logic.
    ts = sm._TailState()
    new_entries = [
        {"type": "user", "text": "hello world", "id": "msg-001"},
    ]

    # _process_tail_entries calls _enrich_agent_entries +
    # _persist_turn_corrections + _warm_task_tracker_if_needed +
    # _persist_todos_if_changed. Patch them so the test focuses on
    # the broadcast path.
    with patch.object(monitor, "_enrich_agent_entries"), \
         patch.object(monitor, "_persist_turn_corrections"), \
         patch.object(
             monitor, "_warm_task_tracker_if_needed", new=AsyncMock()
         ), \
         patch.object(
             monitor, "_persist_todos_if_changed", new=AsyncMock()
         ):
        await monitor._process_tail_entries("auto-int", row, ts, new_entries)

    # Two broadcasts fire: update_cache for registry (in-memory only)
    # and broadcast for messages.
    broadcast_calls = [
        c for c in monitor._event_bus.broadcast.await_args_list
        if c.args and c.args[0] == "session:messages"
    ]
    assert len(broadcast_calls) == 1, (
        f"expected exactly one session:messages broadcast, got "
        f"{[c.args[0] for c in monitor._event_bus.broadcast.await_args_list]}"
    )
    payload = broadcast_calls[0].args[1]
    assert payload["session_id"] == "auto-int"
    entries = payload["entries"]
    assert len(entries) == 1
    # The critical assertion: the matched client_id is attached.
    assert entries[0].get("client_id") == "cid-xyz", (
        f"expected client_id 'cid-xyz' on broadcast entry, got "
        f"{entries[0].get('client_id')!r}"
    )

    # Ring is now empty for this session — the match consumed it.
    assert pending_outbound.is_in_flight("auto-int", "cid-xyz") is False


@pytest.mark.asyncio
async def test_tailer_skips_unmatched_user_turns(temp_db):
    """A user-turn whose text doesn't match any pending entry passes
    through with no client_id attached — operators typing directly at
    the harness still work."""
    row = _seed_session(temp_db.parent)
    pending_outbound.record_send("auto-int", "cid-A", "expected text")

    monitor = sm.SessionMonitor()
    monitor._event_bus = _mock_bus()
    ts = sm._TailState()
    new_entries = [
        {"type": "user", "text": "different text", "id": "msg-001"},
    ]

    with patch.object(monitor, "_enrich_agent_entries"), \
         patch.object(monitor, "_persist_turn_corrections"), \
         patch.object(monitor, "_warm_task_tracker_if_needed", new=AsyncMock()), \
         patch.object(monitor, "_persist_todos_if_changed", new=AsyncMock()):
        await monitor._process_tail_entries("auto-int", row, ts, new_entries)

    payload = next(
        c.args[1] for c in monitor._event_bus.broadcast.await_args_list
        if c.args and c.args[0] == "session:messages"
    )
    assert payload["entries"][0].get("client_id") is None
    # Original pending entry survives — the no-match did not consume it.
    assert pending_outbound.is_in_flight("auto-int", "cid-A") is True


@pytest.mark.asyncio
async def test_tailer_does_not_attach_to_non_user_turn_types(temp_db):
    """Only user-turn entries get client_id matching. Assistant turns,
    tool_use, tool_result, etc. pass through untouched even when their
    text happens to match a pending entry."""
    row = _seed_session(temp_db.parent)
    pending_outbound.record_send("auto-int", "cid-q", "the answer")

    monitor = sm.SessionMonitor()
    monitor._event_bus = _mock_bus()
    ts = sm._TailState()
    new_entries = [
        {"type": "assistant", "text": "the answer", "id": "msg-asst"},
        {"type": "tool_use", "input": {"text": "the answer"}, "id": "tu-1"},
    ]

    with patch.object(monitor, "_enrich_agent_entries"), \
         patch.object(monitor, "_persist_turn_corrections"), \
         patch.object(monitor, "_warm_task_tracker_if_needed", new=AsyncMock()), \
         patch.object(monitor, "_persist_todos_if_changed", new=AsyncMock()):
        await monitor._process_tail_entries("auto-int", row, ts, new_entries)

    payload = next(
        c.args[1] for c in monitor._event_bus.broadcast.await_args_list
        if c.args and c.args[0] == "session:messages"
    )
    for entry in payload["entries"]:
        assert entry.get("client_id") is None, (
            f"non-user-turn entry {entry['type']!r} should not get a "
            f"client_id attached"
        )
    # Pending entry still in flight — assistant text doesn't consume.
    assert pending_outbound.is_in_flight("auto-int", "cid-q") is True


@pytest.mark.asyncio
async def test_tailer_matches_content_field_for_claude_user_shape(temp_db):
    """Claude's user-turn entries use ``content`` rather than ``text``;
    the matcher checks both shapes."""
    row = _seed_session(temp_db.parent)
    pending_outbound.record_send("auto-int", "cid-c", "claude-shape")

    monitor = sm.SessionMonitor()
    monitor._event_bus = _mock_bus()
    ts = sm._TailState()
    new_entries = [
        {"type": "user", "content": "claude-shape", "id": "msg-c"},
    ]

    with patch.object(monitor, "_enrich_agent_entries"), \
         patch.object(monitor, "_persist_turn_corrections"), \
         patch.object(monitor, "_warm_task_tracker_if_needed", new=AsyncMock()), \
         patch.object(monitor, "_persist_todos_if_changed", new=AsyncMock()):
        await monitor._process_tail_entries("auto-int", row, ts, new_entries)

    payload = next(
        c.args[1] for c in monitor._event_bus.broadcast.await_args_list
        if c.args and c.args[0] == "session:messages"
    )
    assert payload["entries"][0].get("client_id") == "cid-c"
    assert pending_outbound.is_in_flight("auto-int", "cid-c") is False


@pytest.mark.asyncio
async def test_tailer_extracts_text_from_content_blocks(temp_db):
    """Some entries have content as a list of block dicts (Claude
    structured content). The matcher joins block.text values."""
    row = _seed_session(temp_db.parent)
    pending_outbound.record_send("auto-int", "cid-bx", "block text here")

    monitor = sm.SessionMonitor()
    monitor._event_bus = _mock_bus()
    ts = sm._TailState()
    new_entries = [
        {
            "type": "user",
            "content": [
                {"type": "text", "text": "block text "},
                {"type": "text", "text": "here"},
            ],
            "id": "msg-bx",
        },
    ]

    with patch.object(monitor, "_enrich_agent_entries"), \
         patch.object(monitor, "_persist_turn_corrections"), \
         patch.object(monitor, "_warm_task_tracker_if_needed", new=AsyncMock()), \
         patch.object(monitor, "_persist_todos_if_changed", new=AsyncMock()):
        await monitor._process_tail_entries("auto-int", row, ts, new_entries)

    payload = next(
        c.args[1] for c in monitor._event_bus.broadcast.await_args_list
        if c.args and c.args[0] == "session:messages"
    )
    assert payload["entries"][0].get("client_id") == "cid-bx"


@pytest.mark.asyncio
async def test_two_identical_sends_match_in_fifo_order(temp_db):
    """End-to-end FIFO: two identical text sends → first echo gets the
    first client_id, second echo gets the second."""
    row = _seed_session(temp_db.parent)
    pending_outbound.record_send("auto-int", "cid-1st", "ok")
    pending_outbound.record_send("auto-int", "cid-2nd", "ok")

    monitor = sm.SessionMonitor()
    monitor._event_bus = _mock_bus()
    ts = sm._TailState()

    # Two user-turn echoes arriving in the same tail read.
    new_entries = [
        {"type": "user", "text": "ok", "id": "msg-a"},
        {"type": "user", "text": "ok", "id": "msg-b"},
    ]

    with patch.object(monitor, "_enrich_agent_entries"), \
         patch.object(monitor, "_persist_turn_corrections"), \
         patch.object(monitor, "_warm_task_tracker_if_needed", new=AsyncMock()), \
         patch.object(monitor, "_persist_todos_if_changed", new=AsyncMock()):
        await monitor._process_tail_entries("auto-int", row, ts, new_entries)

    payload = next(
        c.args[1] for c in monitor._event_bus.broadcast.await_args_list
        if c.args and c.args[0] == "session:messages"
    )
    entries = payload["entries"]
    assert entries[0]["client_id"] == "cid-1st"
    assert entries[1]["client_id"] == "cid-2nd"
    # Both consumed.
    assert pending_outbound.pending_count("auto-int") == 0
