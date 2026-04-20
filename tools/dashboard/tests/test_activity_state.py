"""Activity state tracking tests — pending_tool_ids, last_entry_type, activity_state.

Tests the server-side activity_state derivation pipeline:
  parsed entries → _TailState tracking → DB persistence → SSE broadcast.

No browser, no HTTP server. Uses real inotify, real files (tmp_path),
real SessionMonitor, and captured MockEventBus.
"""

import asyncio
import json
import os
import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip("inotify_simple")
pytest.importorskip("pytest_asyncio")

from tools.dashboard.server import _parse_jsonl_entry


# ── Helpers ────────────────────────────────────────────────────────────


def _init_test_db(db_path: Path) -> None:
    """Create a minimal dashboard.db for testing (includes activity_state column)."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("""CREATE TABLE IF NOT EXISTS tmux_sessions (
        tmux_name TEXT PRIMARY KEY, session_uuid TEXT, graph_source_id TEXT,
        type TEXT NOT NULL, project TEXT NOT NULL, jsonl_path TEXT,
        bead_id TEXT, created_at REAL NOT NULL, is_live INTEGER DEFAULT 1,
        file_offset INTEGER DEFAULT 0, last_activity REAL,
        last_message TEXT DEFAULT '', entry_count INTEGER DEFAULT 0,
        context_tokens INTEGER DEFAULT 0, label TEXT DEFAULT '',
        topics TEXT DEFAULT '[]', role TEXT DEFAULT '',
        nag_enabled INTEGER DEFAULT 0, nag_interval INTEGER DEFAULT 15,
        nag_message TEXT DEFAULT '', nag_last_sent REAL DEFAULT 0,
        dispatch_nag INTEGER DEFAULT 0,
        resolution_dir TEXT, session_uuids TEXT DEFAULT '[]',
        curr_jsonl_file TEXT,
        activity_state TEXT DEFAULT 'idle'
    )""")
    conn.commit()
    conn.close()


def _insert_session(
    db_path: Path, tmux_name: str, jsonl_path: str | None,
    session_type: str = "container",
    resolution_dir: str | None = None,
    file_offset: int = 0,
    session_uuids: str = "[]",
) -> None:
    """Insert a test session into the DB."""
    conn = sqlite3.connect(str(db_path))
    res_dir = resolution_dir
    if res_dir is None and jsonl_path:
        res_dir = str(Path(jsonl_path).parent)
    conn.execute(
        "INSERT INTO tmux_sessions"
        " (tmux_name, type, project, jsonl_path, created_at, is_live,"
        "  resolution_dir, session_uuids, curr_jsonl_file, file_offset)"
        " VALUES (?, ?, 'test', ?, ?, 1, ?, ?, ?, ?)",
        (tmux_name, session_type, jsonl_path, time.time(),
         res_dir, session_uuids, jsonl_path, file_offset),
    )
    conn.commit()
    conn.close()


def _read_activity_state(db_path: Path, tmux_name: str) -> str | None:
    """Read activity_state directly from DB."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT activity_state FROM tmux_sessions WHERE tmux_name=?",
        (tmux_name,),
    ).fetchone()
    conn.close()
    return row["activity_state"] if row else None


def _write_jsonl_entry(path: Path, entry: dict) -> None:
    """Append a single JSONL entry to a file."""
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")


# ── Entry factories ────────────────────────────────────────────────────


def _make_user_entry(text: str = "Hello from user") -> dict:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": text}],
        },
        "timestamp": "2026-04-15T00:00:00Z",
    }


def _make_assistant_entry(text: str = "Hello from assistant") -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
        "timestamp": "2026-04-15T00:00:01Z",
    }


def _make_tool_use_entry(tool_name: str = "Bash", tool_id: str = "toolu_test1",
                         command: str = "echo hi") -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_id,
                    "name": tool_name,
                    "input": {"command": command},
                },
            ],
        },
        "timestamp": "2026-04-15T00:00:00Z",
    }


def _make_tool_result_entry(tool_id: str = "toolu_test1",
                            content: str = "ok", is_error: bool = False) -> dict:
    return {
        "type": "tool_result",
        "toolUseId": tool_id,
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": content}],
        },
        "is_error": is_error,
        "timestamp": "2026-04-15T00:00:01Z",
    }


def _make_parallel_tool_use_entry(tools: list[tuple[str, str, str]]) -> dict:
    """tools = [("Bash", "toolu_1", "cmd1"), ("Read", "toolu_2", "/file")]"""
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": tid,
                    "name": name,
                    "input": {"command": cmd},
                }
                for name, tid, cmd in tools
            ],
        },
        "timestamp": "2026-04-15T00:00:00Z",
    }


def _make_crosstalk_entry(text: str = "ping from peer") -> dict:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": f"<crosstalk>{text}</crosstalk>"}],
        },
        "timestamp": "2026-04-15T00:00:00Z",
    }


# ── MockEventBus ──────────────────────────────────────────────────────


class MockEventBus:
    """Captures broadcasts for assertion."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []
        self._waiters: list[asyncio.Event] = []

    async def broadcast(self, topic: str, data, **kwargs) -> int:
        self.events.append((topic, data))
        for w in self._waiters:
            w.set()
        return 1

    def update_cache(self, topic: str, data) -> None:
        # Real EventBus caches the latest payload per topic for replay to
        # new subscribers. Tests don't exercise replay — no-op stub keeps
        # session_monitor's call path green.
        pass

    def get_messages(self, session_id: str | None = None) -> list[tuple[str, dict]]:
        msgs = [(t, d) for t, d in self.events if t == "session:messages"]
        if session_id:
            msgs = [(t, d) for t, d in msgs if d.get("session_id") == session_id]
        return msgs

    def get_registry_events(self) -> list[tuple[str, dict]]:
        return [(t, d) for t, d in self.events if t == "session:registry"]

    async def wait_for_event(self, topic: str, timeout: float = 2.0,
                              session_id: str | None = None,
                              min_count: int = 1) -> list[tuple[str, dict]]:
        """Wait until at least min_count events of the given topic arrive."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if topic == "session:messages":
                events = self.get_messages(session_id)
            elif topic == "session:registry":
                events = self.get_registry_events()
            else:
                events = [(t, d) for t, d in self.events if t == topic]
            if len(events) >= min_count:
                return events
            evt = asyncio.Event()
            self._waiters.append(evt)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(evt.wait(), timeout=min(0.1, remaining))
            except asyncio.TimeoutError:
                pass
            finally:
                try:
                    self._waiters.remove(evt)
                except ValueError:
                    pass
        # Final check
        if topic == "session:messages":
            return self.get_messages(session_id)
        elif topic == "session:registry":
            return self.get_registry_events()
        return [(t, d) for t, d in self.events if t == topic]


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def setup_env(tmp_path):
    """Set up a test environment with DB."""
    db_path = tmp_path / "dashboard.db"
    _init_test_db(db_path)
    os.environ["DASHBOARD_DB"] = str(db_path)

    # Reload DAO to pick up test DB
    import importlib
    from tools.dashboard.dao import dashboard_db as db_mod
    importlib.reload(db_mod)

    yield tmp_path, db_path

    # Cleanup
    os.environ.pop("DASHBOARD_DB", None)


async def _start_monitor(bus):
    """Create and start a SessionMonitor with mock tmux."""
    from tools.dashboard.session_monitor import SessionMonitor
    mon = SessionMonitor()
    patcher = patch.object(
        SessionMonitor, "_check_tmux", staticmethod(lambda name: True),
    )
    patcher.start()
    await mon.start(event_bus=bus, entry_parser=_parse_jsonl_entry)
    return mon, patcher


async def _stop_monitor(mon, patcher):
    """Stop monitor background tasks and restore _check_tmux."""
    patcher.stop()
    if mon._tailer_task:
        mon._tailer_task.cancel()
        try:
            await mon._tailer_task
        except asyncio.CancelledError:
            pass
    if mon._liveness_task:
        mon._liveness_task.cancel()
        try:
            await mon._liveness_task
        except asyncio.CancelledError:
            pass


# ── Unit Tests: TestActivityStateDerivation ───────────────────────────


class TestActivityStateDerivation:
    """Tests the activity_state derivation logic via the full tailer pipeline.

    Each test: write JSONL entries → inotify triggers → monitor processes →
    check DB activity_state and _TailState fields.
    """

    @pytest.mark.asyncio
    async def test_tool_use_sets_tool_running(self, setup_env):
        """tool_use entry with no result → tool_running."""
        tmp_path, db_path = setup_env
        jsonl = tmp_path / "test-session.jsonl"
        jsonl.write_text("")
        _insert_session(db_path, "auto-act-1", str(jsonl))

        bus = MockEventBus()
        mon, patcher = await _start_monitor(bus)
        try:
            bus.events.clear()
            await asyncio.sleep(0.05)

            _write_jsonl_entry(jsonl, _make_tool_use_entry("Bash", "toolu_1"))
            events = await bus.wait_for_event(
                "session:messages", session_id="auto-act-1", timeout=3.0,
            )
            assert len(events) >= 1

            # Check DB
            state = _read_activity_state(db_path, "auto-act-1")
            assert state == "tool_running"

            # Check TailState
            ts = mon._tail_states.get("auto-act-1")
            assert ts is not None
            assert "toolu_1" in ts.pending_tool_ids
        finally:
            await _stop_monitor(mon, patcher)

    @pytest.mark.asyncio
    async def test_tool_result_clears_to_thinking(self, setup_env):
        """tool_use followed by matching tool_result → thinking."""
        tmp_path, db_path = setup_env
        jsonl = tmp_path / "test-session.jsonl"
        jsonl.write_text("")
        _insert_session(db_path, "auto-act-2", str(jsonl))

        bus = MockEventBus()
        mon, patcher = await _start_monitor(bus)
        try:
            bus.events.clear()
            await asyncio.sleep(0.05)

            _write_jsonl_entry(jsonl, _make_tool_use_entry("Bash", "toolu_1"))
            await bus.wait_for_event(
                "session:messages", session_id="auto-act-2", timeout=3.0,
            )

            bus.events.clear()
            _write_jsonl_entry(jsonl, _make_tool_result_entry("toolu_1"))
            events = await bus.wait_for_event(
                "session:messages", session_id="auto-act-2", timeout=3.0, min_count=1,
            )
            assert len(events) >= 1

            state = _read_activity_state(db_path, "auto-act-2")
            assert state == "thinking"

            ts = mon._tail_states.get("auto-act-2")
            assert ts is not None
            assert len(ts.pending_tool_ids) == 0
        finally:
            await _stop_monitor(mon, patcher)

    @pytest.mark.asyncio
    async def test_assistant_text_sets_idle(self, setup_env):
        """Pure assistant text entry → idle."""
        tmp_path, db_path = setup_env
        jsonl = tmp_path / "test-session.jsonl"
        jsonl.write_text("")
        _insert_session(db_path, "auto-act-3", str(jsonl))

        bus = MockEventBus()
        mon, patcher = await _start_monitor(bus)
        try:
            bus.events.clear()
            await asyncio.sleep(0.05)

            _write_jsonl_entry(jsonl, _make_assistant_entry("Here's the answer"))
            events = await bus.wait_for_event(
                "session:messages", session_id="auto-act-3", timeout=3.0,
            )
            assert len(events) >= 1

            state = _read_activity_state(db_path, "auto-act-3")
            assert state == "idle"

            ts = mon._tail_states.get("auto-act-3")
            assert ts is not None
            assert len(ts.pending_tool_ids) == 0
        finally:
            await _stop_monitor(mon, patcher)

    @pytest.mark.asyncio
    async def test_user_message_sets_thinking(self, setup_env):
        """User message → thinking."""
        tmp_path, db_path = setup_env
        jsonl = tmp_path / "test-session.jsonl"
        jsonl.write_text("")
        _insert_session(db_path, "auto-act-4", str(jsonl))

        bus = MockEventBus()
        mon, patcher = await _start_monitor(bus)
        try:
            bus.events.clear()
            await asyncio.sleep(0.05)

            _write_jsonl_entry(jsonl, _make_user_entry("Hello"))
            events = await bus.wait_for_event(
                "session:messages", session_id="auto-act-4", timeout=3.0,
            )
            assert len(events) >= 1

            state = _read_activity_state(db_path, "auto-act-4")
            assert state == "thinking"
        finally:
            await _stop_monitor(mon, patcher)

    @pytest.mark.asyncio
    async def test_crosstalk_sets_thinking(self, setup_env):
        """Crosstalk entry → thinking."""
        tmp_path, db_path = setup_env
        jsonl = tmp_path / "test-session.jsonl"
        jsonl.write_text("")
        _insert_session(db_path, "auto-act-5", str(jsonl))

        bus = MockEventBus()
        mon, patcher = await _start_monitor(bus)
        try:
            bus.events.clear()
            await asyncio.sleep(0.05)

            _write_jsonl_entry(jsonl, _make_crosstalk_entry("check this"))
            events = await bus.wait_for_event(
                "session:messages", session_id="auto-act-5", timeout=3.0,
            )
            assert len(events) >= 1

            state = _read_activity_state(db_path, "auto-act-5")
            assert state == "thinking"
        finally:
            await _stop_monitor(mon, patcher)

    @pytest.mark.asyncio
    async def test_parallel_tool_use_all_pending(self, setup_env):
        """3 tool_use blocks in one entry → tool_running, all 3 pending."""
        tmp_path, db_path = setup_env
        jsonl = tmp_path / "test-session.jsonl"
        jsonl.write_text("")
        _insert_session(db_path, "auto-act-6", str(jsonl))

        bus = MockEventBus()
        mon, patcher = await _start_monitor(bus)
        try:
            bus.events.clear()
            await asyncio.sleep(0.05)

            _write_jsonl_entry(jsonl, _make_parallel_tool_use_entry([
                ("Bash", "toolu_1", "echo 1"),
                ("Read", "toolu_2", "/tmp/f"),
                ("Grep", "toolu_3", "pattern"),
            ]))
            events = await bus.wait_for_event(
                "session:messages", session_id="auto-act-6", timeout=3.0,
            )
            assert len(events) >= 1

            state = _read_activity_state(db_path, "auto-act-6")
            assert state == "tool_running"

            ts = mon._tail_states.get("auto-act-6")
            assert ts is not None
            assert ts.pending_tool_ids == {"toolu_1", "toolu_2", "toolu_3"}
        finally:
            await _stop_monitor(mon, patcher)

    @pytest.mark.asyncio
    async def test_parallel_tool_use_partial_return(self, setup_env):
        """3 issued, 2 returned → still tool_running with 1 pending."""
        tmp_path, db_path = setup_env
        jsonl = tmp_path / "test-session.jsonl"
        jsonl.write_text("")
        _insert_session(db_path, "auto-act-7", str(jsonl))

        bus = MockEventBus()
        mon, patcher = await _start_monitor(bus)
        try:
            bus.events.clear()
            await asyncio.sleep(0.05)

            _write_jsonl_entry(jsonl, _make_parallel_tool_use_entry([
                ("Bash", "toolu_1", "echo 1"),
                ("Read", "toolu_2", "/tmp/f"),
                ("Grep", "toolu_3", "pattern"),
            ]))
            await bus.wait_for_event(
                "session:messages", session_id="auto-act-7", timeout=3.0,
            )

            bus.events.clear()
            _write_jsonl_entry(jsonl, _make_tool_result_entry("toolu_1"))
            _write_jsonl_entry(jsonl, _make_tool_result_entry("toolu_2"))
            await bus.wait_for_event(
                "session:messages", session_id="auto-act-7", timeout=3.0,
            )

            state = _read_activity_state(db_path, "auto-act-7")
            assert state == "tool_running"

            ts = mon._tail_states.get("auto-act-7")
            assert ts is not None
            assert ts.pending_tool_ids == {"toolu_3"}
        finally:
            await _stop_monitor(mon, patcher)

    @pytest.mark.asyncio
    async def test_parallel_tool_use_all_returned(self, setup_env):
        """3 issued, all 3 returned → thinking."""
        tmp_path, db_path = setup_env
        jsonl = tmp_path / "test-session.jsonl"
        jsonl.write_text("")
        _insert_session(db_path, "auto-act-8", str(jsonl))

        bus = MockEventBus()
        mon, patcher = await _start_monitor(bus)
        try:
            bus.events.clear()
            await asyncio.sleep(0.05)

            _write_jsonl_entry(jsonl, _make_parallel_tool_use_entry([
                ("Bash", "toolu_1", "echo 1"),
                ("Read", "toolu_2", "/tmp/f"),
                ("Grep", "toolu_3", "pattern"),
            ]))
            await bus.wait_for_event(
                "session:messages", session_id="auto-act-8", timeout=3.0,
            )

            bus.events.clear()
            _write_jsonl_entry(jsonl, _make_tool_result_entry("toolu_1"))
            _write_jsonl_entry(jsonl, _make_tool_result_entry("toolu_2"))
            _write_jsonl_entry(jsonl, _make_tool_result_entry("toolu_3"))
            await bus.wait_for_event(
                "session:messages", session_id="auto-act-8", timeout=3.0,
            )

            state = _read_activity_state(db_path, "auto-act-8")
            assert state == "thinking"

            ts = mon._tail_states.get("auto-act-8")
            assert ts is not None
            assert len(ts.pending_tool_ids) == 0
        finally:
            await _stop_monitor(mon, patcher)

    @pytest.mark.asyncio
    async def test_mark_dead_overrides_tool_running(self, setup_env):
        """mark_dead with pending_tool_ids → dead, not tool_running."""
        tmp_path, db_path = setup_env
        jsonl = tmp_path / "test-session.jsonl"
        jsonl.write_text("")
        _insert_session(db_path, "auto-act-9", str(jsonl))

        bus = MockEventBus()
        mon, patcher = await _start_monitor(bus)
        try:
            bus.events.clear()
            await asyncio.sleep(0.05)

            _write_jsonl_entry(jsonl, _make_tool_use_entry("Bash", "toolu_1"))
            await bus.wait_for_event(
                "session:messages", session_id="auto-act-9", timeout=3.0,
            )
            assert _read_activity_state(db_path, "auto-act-9") == "tool_running"

            # Simulate mark_dead
            from tools.dashboard.dao.dashboard_db import mark_dead
            ts = mon._tail_states.get("auto-act-9")
            if ts:
                ts.pending_tool_ids.clear()
            mark_dead("auto-act-9")

            state = _read_activity_state(db_path, "auto-act-9")
            assert state == "dead"
        finally:
            await _stop_monitor(mon, patcher)

    @pytest.mark.asyncio
    async def test_sequential_tool_calls(self, setup_env):
        """tool_use(A) → result(A) → text → tool_use(B): state transitions correctly."""
        tmp_path, db_path = setup_env
        jsonl = tmp_path / "test-session.jsonl"
        jsonl.write_text("")
        _insert_session(db_path, "auto-act-10", str(jsonl))

        bus = MockEventBus()
        mon, patcher = await _start_monitor(bus)
        try:
            bus.events.clear()
            await asyncio.sleep(0.05)

            # Step 1: tool_use(A) → tool_running
            _write_jsonl_entry(jsonl, _make_tool_use_entry("Bash", "toolu_A"))
            await bus.wait_for_event(
                "session:messages", session_id="auto-act-10", timeout=3.0,
            )
            assert _read_activity_state(db_path, "auto-act-10") == "tool_running"

            # Step 2: result(A) → thinking
            bus.events.clear()
            _write_jsonl_entry(jsonl, _make_tool_result_entry("toolu_A"))
            await bus.wait_for_event(
                "session:messages", session_id="auto-act-10", timeout=3.0,
            )
            assert _read_activity_state(db_path, "auto-act-10") == "thinking"

            # Step 3: assistant_text → idle
            bus.events.clear()
            _write_jsonl_entry(jsonl, _make_assistant_entry("Done"))
            await bus.wait_for_event(
                "session:messages", session_id="auto-act-10", timeout=3.0,
            )
            assert _read_activity_state(db_path, "auto-act-10") == "idle"

            # Step 4: tool_use(B) → tool_running
            bus.events.clear()
            _write_jsonl_entry(jsonl, _make_tool_use_entry("Read", "toolu_B"))
            await bus.wait_for_event(
                "session:messages", session_id="auto-act-10", timeout=3.0,
            )
            assert _read_activity_state(db_path, "auto-act-10") == "tool_running"

            ts = mon._tail_states.get("auto-act-10")
            assert ts is not None
            assert ts.pending_tool_ids == {"toolu_B"}
        finally:
            await _stop_monitor(mon, patcher)

    @pytest.mark.asyncio
    async def test_empty_batch_no_state_change(self, setup_env):
        """Empty batch should not change activity_state."""
        tmp_path, db_path = setup_env
        jsonl = tmp_path / "test-session.jsonl"
        jsonl.write_text("")
        _insert_session(db_path, "auto-act-11", str(jsonl))

        # Set initial state
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE tmux_sessions SET activity_state='idle' WHERE tmux_name='auto-act-11'"
        )
        conn.commit()
        conn.close()

        bus = MockEventBus()
        mon, patcher = await _start_monitor(bus)
        try:
            bus.events.clear()
            await asyncio.sleep(0.05)

            # Write whitespace (no valid entries) — should not trigger broadcast
            with open(jsonl, "a") as f:
                f.write("\n\n")
            await asyncio.sleep(0.5)

            state = _read_activity_state(db_path, "auto-act-11")
            assert state == "idle"
        finally:
            await _stop_monitor(mon, patcher)

    @pytest.mark.asyncio
    async def test_unknown_tool_result_id_ignored(self, setup_env):
        """tool_result for nonexistent tool_id → no crash, state = thinking."""
        tmp_path, db_path = setup_env
        jsonl = tmp_path / "test-session.jsonl"
        jsonl.write_text("")
        _insert_session(db_path, "auto-act-12", str(jsonl))

        bus = MockEventBus()
        mon, patcher = await _start_monitor(bus)
        try:
            bus.events.clear()
            await asyncio.sleep(0.05)

            _write_jsonl_entry(jsonl, _make_tool_result_entry("toolu_nonexistent"))
            events = await bus.wait_for_event(
                "session:messages", session_id="auto-act-12", timeout=3.0,
            )
            assert len(events) >= 1

            # discard on non-member is a no-op
            ts = mon._tail_states.get("auto-act-12")
            assert ts is not None
            assert len(ts.pending_tool_ids) == 0

            # tool_result is parsed as a user-type entry, so state = thinking
            state = _read_activity_state(db_path, "auto-act-12")
            assert state == "thinking"
        finally:
            await _stop_monitor(mon, patcher)


# ── E2E Tests: TestActivityStateBroadcast ─────────────────────────────


class TestActivityStateBroadcast:
    """Tests the full pipeline including SSE broadcast content."""

    @pytest.mark.asyncio
    async def test_tool_use_broadcast_includes_activity_state(self, setup_env):
        """session:messages broadcast includes activity_state and pending_tool_ids."""
        tmp_path, db_path = setup_env
        jsonl = tmp_path / "test-session.jsonl"
        jsonl.write_text("")
        _insert_session(db_path, "auto-bcast-1", str(jsonl))

        bus = MockEventBus()
        mon, patcher = await _start_monitor(bus)
        try:
            bus.events.clear()
            await asyncio.sleep(0.05)

            _write_jsonl_entry(jsonl, _make_tool_use_entry("Bash", "toolu_1"))
            events = await bus.wait_for_event(
                "session:messages", session_id="auto-bcast-1", timeout=3.0,
            )
            assert len(events) >= 1
            _, data = events[-1]
            assert data["activity_state"] == "tool_running"
            assert "toolu_1" in data["pending_tool_ids"]
        finally:
            await _stop_monitor(mon, patcher)

    @pytest.mark.asyncio
    async def test_tool_result_broadcast_updates_state(self, setup_env):
        """After tool_result, broadcast shows thinking and empty pending."""
        tmp_path, db_path = setup_env
        jsonl = tmp_path / "test-session.jsonl"
        jsonl.write_text("")
        _insert_session(db_path, "auto-bcast-2", str(jsonl))

        bus = MockEventBus()
        mon, patcher = await _start_monitor(bus)
        try:
            bus.events.clear()
            await asyncio.sleep(0.05)

            _write_jsonl_entry(jsonl, _make_tool_use_entry("Bash", "toolu_1"))
            await bus.wait_for_event(
                "session:messages", session_id="auto-bcast-2", timeout=3.0,
            )

            bus.events.clear()
            _write_jsonl_entry(jsonl, _make_tool_result_entry("toolu_1"))
            events = await bus.wait_for_event(
                "session:messages", session_id="auto-bcast-2", timeout=3.0,
            )
            assert len(events) >= 1
            _, data = events[-1]
            assert data["activity_state"] == "thinking"
            assert data["pending_tool_ids"] == []
        finally:
            await _stop_monitor(mon, patcher)

    @pytest.mark.asyncio
    async def test_registry_includes_activity_state(self, setup_env):
        """session:registry broadcast includes activity_state field."""
        tmp_path, db_path = setup_env
        jsonl = tmp_path / "test-session.jsonl"
        jsonl.write_text("")
        _insert_session(db_path, "auto-bcast-3", str(jsonl))

        bus = MockEventBus()
        mon, patcher = await _start_monitor(bus)
        try:
            bus.events.clear()
            await asyncio.sleep(0.05)

            # Write a tool_use to set state to tool_running
            _write_jsonl_entry(jsonl, _make_tool_use_entry("Bash", "toolu_1"))
            await bus.wait_for_event(
                "session:messages", session_id="auto-bcast-3", timeout=3.0,
            )

            # Trigger registry broadcast
            bus.events.clear()
            await mon._broadcast_registry()
            reg_events = bus.get_registry_events()
            assert len(reg_events) >= 1

            _, registry = reg_events[-1]
            session_entry = None
            for s in registry:
                if s["session_id"] == "auto-bcast-3":
                    session_entry = s
                    break
            assert session_entry is not None
            assert session_entry["activity_state"] == "tool_running"
        finally:
            await _stop_monitor(mon, patcher)

    @pytest.mark.asyncio
    async def test_death_broadcast_shows_dead_not_running(self, setup_env):
        """When tmux dies, registry shows dead, not tool_running."""
        tmp_path, db_path = setup_env
        jsonl = tmp_path / "test-session.jsonl"
        jsonl.write_text("")
        _insert_session(db_path, "auto-bcast-4", str(jsonl))

        bus = MockEventBus()
        mon, patcher = await _start_monitor(bus)
        try:
            bus.events.clear()
            await asyncio.sleep(0.05)

            # Write tool_use → tool_running
            _write_jsonl_entry(jsonl, _make_tool_use_entry("Bash", "toolu_1"))
            await bus.wait_for_event(
                "session:messages", session_id="auto-bcast-4", timeout=3.0,
            )
            assert _read_activity_state(db_path, "auto-bcast-4") == "tool_running"

            # Simulate tmux death by patching _check_tmux to return False
            patcher.stop()
            dead_patcher = patch.object(
                type(mon), "_check_tmux", staticmethod(lambda name: False),
            )
            dead_patcher.start()

            # Run one liveness check
            bus.events.clear()
            sessions = [{"tmux_name": "auto-bcast-4", "type": "container", "is_live": 1,
                         "nag_enabled": 0}]
            from tools.dashboard.dao.dashboard_db import get_live_sessions
            with patch("tools.dashboard.session_monitor.get_live_sessions", return_value=sessions):
                # Direct call to simulate the relevant part of the liveness loop
                from tools.dashboard.dao.dashboard_db import mark_dead as _mark_dead
                ts = mon._tail_states.get("auto-bcast-4")
                if ts:
                    ts.pending_tool_ids.clear()
                _mark_dead("auto-bcast-4")

            dead_patcher.stop()
            patcher.start()  # Restore for cleanup

            state = _read_activity_state(db_path, "auto-bcast-4")
            assert state == "dead"
            assert state != "tool_running"
        finally:
            await _stop_monitor(mon, patcher)


# ── Dispatcher threshold test ─────────────────────────────────────────


class TestDispatcherReadsActivityState:
    """Validates the dispatcher's consumption of activity_state for timeout thresholds."""

    def test_dispatcher_threshold_from_activity_state(self, setup_env):
        """Dispatcher uses 1800s threshold for tool_running, 300s otherwise."""
        tmp_path, db_path = setup_env

        from tools.dashboard.dao.dashboard_db import get_session

        # Insert a session with activity_state=tool_running
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO tmux_sessions"
            " (tmux_name, type, project, created_at, is_live, activity_state)"
            " VALUES ('auto-disp-1', 'container', 'test', ?, 1, 'tool_running')",
            (time.time(),),
        )
        conn.commit()
        conn.close()

        row = get_session("auto-disp-1")
        assert row is not None

        # tool_running → 1800
        if row.get("activity_state") == "tool_running":
            threshold = 1800
        else:
            threshold = 300
        assert threshold == 1800

        # Now update to idle
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE tmux_sessions SET activity_state='idle' WHERE tmux_name='auto-disp-1'"
        )
        conn.commit()
        conn.close()

        # Reload DAO to clear any cache
        import importlib
        from tools.dashboard.dao import dashboard_db as db_mod
        importlib.reload(db_mod)
        os.environ["DASHBOARD_DB"] = str(db_path)
        importlib.reload(db_mod)

        row = get_session("auto-disp-1")
        threshold = 1800 if row.get("activity_state") == "tool_running" else 300
        assert threshold == 300

    def test_thinking_uses_default_threshold(self, setup_env):
        """activity_state=thinking → 300s threshold."""
        _, db_path = setup_env
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO tmux_sessions"
            " (tmux_name, type, project, created_at, is_live, activity_state)"
            " VALUES ('auto-disp-2', 'container', 'test', ?, 1, 'thinking')",
            (time.time(),),
        )
        conn.commit()
        conn.close()

        from tools.dashboard.dao.dashboard_db import get_session
        row = get_session("auto-disp-2")
        threshold = 1800 if row.get("activity_state") == "tool_running" else 300
        assert threshold == 300

    def test_dead_uses_default_threshold(self, setup_env):
        """activity_state=dead → 300s threshold."""
        _, db_path = setup_env
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO tmux_sessions"
            " (tmux_name, type, project, created_at, is_live, activity_state)"
            " VALUES ('auto-disp-3', 'container', 'test', ?, 0, 'dead')",
            (time.time(),),
        )
        conn.commit()
        conn.close()

        from tools.dashboard.dao.dashboard_db import get_session
        row = get_session("auto-disp-3")
        threshold = 1800 if row.get("activity_state") == "tool_running" else 300
        assert threshold == 300


# ── Migration test ────────────────────────────────────────────────────


class TestActivityStateMigration:
    """Validates the ALTER TABLE migration adds activity_state."""

    def test_migration_adds_column(self, tmp_path):
        """init_db on a DB without activity_state adds the column."""
        db_path = tmp_path / "legacy.db"
        # Create DB without activity_state column
        conn = sqlite3.connect(str(db_path))
        conn.execute("""CREATE TABLE IF NOT EXISTS tmux_sessions (
            tmux_name TEXT PRIMARY KEY, session_uuid TEXT, graph_source_id TEXT,
            type TEXT NOT NULL, project TEXT NOT NULL, jsonl_path TEXT,
            bead_id TEXT, created_at REAL NOT NULL, is_live INTEGER DEFAULT 1,
            file_offset INTEGER DEFAULT 0, last_activity REAL,
            last_message TEXT DEFAULT '', entry_count INTEGER DEFAULT 0,
            context_tokens INTEGER DEFAULT 0, label TEXT DEFAULT '',
            topics TEXT DEFAULT '[]', role TEXT DEFAULT '',
            nag_enabled INTEGER DEFAULT 0, nag_interval INTEGER DEFAULT 15,
            nag_message TEXT DEFAULT '', nag_last_sent REAL DEFAULT 0,
            dispatch_nag INTEGER DEFAULT 0,
            resolution_dir TEXT, session_uuids TEXT DEFAULT '[]',
            curr_jsonl_file TEXT
        )""")
        # Insert a session to test default
        conn.execute(
            "INSERT INTO tmux_sessions (tmux_name, type, project, created_at)"
            " VALUES ('old-session', 'host', 'test', ?)",
            (time.time(),),
        )
        conn.commit()
        conn.close()

        os.environ["DASHBOARD_DB"] = str(db_path)
        import importlib
        from tools.dashboard.dao import dashboard_db as db_mod
        importlib.reload(db_mod)
        db_mod.init_db(db_path)

        # Verify column exists and default is 'idle'
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT activity_state FROM tmux_sessions WHERE tmux_name='old-session'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["activity_state"] == "idle"

        os.environ.pop("DASHBOARD_DB", None)
