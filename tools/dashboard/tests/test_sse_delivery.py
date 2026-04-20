"""E2E SSE delivery tests — file write to event bus broadcast.

Tests the core tailer pipeline: write bytes to JSONL file → inotify detects →
parser runs → event bus broadcasts session:messages.

No browser, no HTTP server, no Alpine. Pure Boundary A output testing.
Uses real inotify, real files (tmp_path), real SessionMonitor, captured EventBus.
"""

import asyncio
import json
import os
import sqlite3
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Skip entire module when inotify_simple or pytest-asyncio aren't installed
pytest.importorskip("inotify_simple")
pytest.importorskip("pytest_asyncio")

from tools.dashboard.server import _parse_jsonl_entry


# ── Helpers ────────────────────────────────────────────────────────────


def _init_test_db(db_path: Path) -> None:
    """Create a minimal dashboard.db for testing."""
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


def _write_jsonl_entry(path: Path, entry: dict) -> None:
    """Append a single JSONL entry to a file."""
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def _make_user_entry(text: str = "Hello from user") -> dict:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": text}],
        },
        "timestamp": "2026-03-28T00:00:00Z",
    }


def _make_assistant_entry(text: str = "Hello from assistant") -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
        "timestamp": "2026-03-28T00:00:01Z",
    }


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


@pytest.fixture
def setup_env(tmp_path):
    """Set up a test environment with DB."""
    db_path = tmp_path / "dashboard.db"
    dispatch_db_path = tmp_path / "dispatch.db"
    _init_test_db(db_path)
    os.environ["DASHBOARD_DB"] = str(db_path)
    os.environ["DISPATCH_DB"] = str(dispatch_db_path)

    # Reload DAO to pick up test DB
    import importlib
    from tools.dashboard.dao import dashboard_db as db_mod
    importlib.reload(db_mod)
    from agents import dispatch_db as dispatch_db_mod
    importlib.reload(dispatch_db_mod)

    yield tmp_path, db_path

    # Cleanup
    os.environ.pop("DASHBOARD_DB", None)
    os.environ.pop("DISPATCH_DB", None)


async def _start_monitor(bus):
    """Create and start a SessionMonitor with mock tmux.

    Returns (monitor, patcher) — patcher must stay active for the test duration
    so the liveness loop doesn't kill test sessions.
    """
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


# ── TestFileWriteToSSE ─────────────────────────────────────────────────


class TestFileWriteToSSE:
    """The most basic test: does writing to a JSONL file produce an SSE broadcast?"""

    @pytest.mark.asyncio
    async def test_write_triggers_broadcast(self, setup_env):
        """Create a tailable session with JSONL. Write entry. Assert broadcast arrives."""
        tmp_path, db_path = setup_env
        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir()
        jsonl = sess_dir / "test-uuid.jsonl"
        jsonl.touch()
        _insert_session(db_path, "auto-sse-1", str(jsonl))

        bus = MockEventBus()
        mon, patcher = await _start_monitor(bus)
        try:
            assert mon._use_inotify is True
            # Clear startup events
            bus.events.clear()

            await asyncio.sleep(0.05)
            _write_jsonl_entry(jsonl, _make_assistant_entry("broadcast test"))

            events = await bus.wait_for_event(
                "session:messages", session_id="auto-sse-1", timeout=2.0,
            )
            assert len(events) >= 1, f"Expected session:messages broadcast, got {len(events)}"
            _, data = events[0]
            assert data["session_id"] == "auto-sse-1"
            assert len(data["entries"]) >= 1
        finally:
            await _stop_monitor(mon, patcher)

    @pytest.mark.asyncio
    async def test_multiple_writes_multiple_broadcasts(self, setup_env):
        """Write 3 entries. Assert broadcasts arrive."""
        tmp_path, db_path = setup_env
        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir()
        jsonl = sess_dir / "multi-uuid.jsonl"
        jsonl.touch()
        _insert_session(db_path, "auto-sse-multi", str(jsonl))

        bus = MockEventBus()
        mon, patcher = await _start_monitor(bus)
        try:
            bus.events.clear()
            await asyncio.sleep(0.05)

            for i in range(3):
                _write_jsonl_entry(jsonl, _make_assistant_entry(f"msg {i}"))
                await asyncio.sleep(0.15)  # small gap for separate IN_MODIFY events

            events = await bus.wait_for_event(
                "session:messages", session_id="auto-sse-multi",
                timeout=3.0, min_count=1,
            )
            assert len(events) >= 1, "Expected at least 1 broadcast"
            # Count total entries across all broadcasts
            total_entries = sum(len(d["entries"]) for _, d in events)
            assert total_entries >= 3, f"Expected ≥3 entries total, got {total_entries}"
        finally:
            await _stop_monitor(mon, patcher)

    @pytest.mark.asyncio
    async def test_broadcast_contains_parsed_entry(self, setup_env):
        """Write a user message. Assert broadcast has type: user and matching content."""
        tmp_path, db_path = setup_env
        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir()
        jsonl = sess_dir / "parsed-uuid.jsonl"
        jsonl.touch()
        _insert_session(db_path, "auto-sse-parsed", str(jsonl))

        bus = MockEventBus()
        mon, patcher = await _start_monitor(bus)
        try:
            bus.events.clear()
            await asyncio.sleep(0.05)

            _write_jsonl_entry(jsonl, _make_user_entry("specific user message"))

            events = await bus.wait_for_event(
                "session:messages", session_id="auto-sse-parsed", timeout=2.0,
            )
            assert len(events) >= 1
            _, data = events[0]
            entry = data["entries"][0]
            assert entry["type"] == "user"
            assert entry["content"] == "specific user message"
        finally:
            await _stop_monitor(mon, patcher)

    @pytest.mark.asyncio
    async def test_broadcast_contains_seq(self, setup_env):
        """Assert each broadcast has a monotonically increasing seq field."""
        tmp_path, db_path = setup_env
        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir()
        jsonl = sess_dir / "seq-uuid.jsonl"
        jsonl.touch()
        _insert_session(db_path, "auto-sse-seq", str(jsonl))

        bus = MockEventBus()
        mon, patcher = await _start_monitor(bus)
        try:
            bus.events.clear()
            await asyncio.sleep(0.05)

            for i in range(3):
                _write_jsonl_entry(jsonl, _make_assistant_entry(f"seq msg {i}"))
                await asyncio.sleep(0.15)

            events = await bus.wait_for_event(
                "session:messages", session_id="auto-sse-seq",
                timeout=3.0, min_count=2,
            )
            if len(events) >= 2:
                seqs = [d["seq"] for _, d in events]
                for i in range(1, len(seqs)):
                    assert seqs[i] > seqs[i - 1], (
                        f"seq not monotonically increasing: {seqs}"
                    )
            # With timing, all 3 may land in 1 broadcast; verify seq exists
            for _, data in events:
                assert "seq" in data
                assert isinstance(data["seq"], int)
                assert data["seq"] > 0
        finally:
            await _stop_monitor(mon, patcher)


# ── TestContainerFirstFileToSSE ────────────────────────────────────────


class TestContainerFirstFileToSSE:
    """Does a new container session go from unresolved to broadcasting?"""

    @pytest.mark.asyncio
    async def test_first_file_triggers_resolution_and_broadcast(self, setup_env):
        """Register container session with resolution_dir, no JSONL.
        Create JSONL file (triggers IN_CREATE). Assert: session resolves,
        first write produces SSE broadcast.
        """
        tmp_path, db_path = setup_env
        sess_dir = tmp_path / "container_sessions"
        sess_dir.mkdir()

        # Register session with no JSONL, just resolution_dir
        _insert_session(
            db_path, "auto-sse-resolve", None,
            resolution_dir=str(sess_dir),
        )

        bus = MockEventBus()
        mon, patcher = await _start_monitor(bus)
        try:
            assert mon._use_inotify is True

            # Manually set up tail state and dir watch (simulating recovery)
            from tools.dashboard.session_monitor import _TailState
            mon._tail_states["auto-sse-resolve"] = _TailState(
                needs_resolution=True,
                resolution_dir=sess_dir,
            )
            mon._add_dir_watch("auto-sse-resolve", str(sess_dir))

            bus.events.clear()
            await asyncio.sleep(0.05)

            # Create JSONL file — triggers IN_CREATE
            # Mock link_and_enrich to avoid graph CLI call but still update DB
            from tools.dashboard.dao.dashboard_db import update_jsonl_link
            with patch(
                "tools.dashboard.dao.dashboard_db.link_and_enrich",
                side_effect=lambda tn, session_uuid, jsonl_path, project=None:
                    update_jsonl_link(tn, session_uuid, jsonl_path, project),
            ):
                new_jsonl = sess_dir / "first-uuid.jsonl"
                new_jsonl.touch()

                # Wait for IN_CREATE to be processed and watch to be added
                await asyncio.sleep(0.5)

                # Write to the new file
                _write_jsonl_entry(new_jsonl, _make_assistant_entry("first file entry"))

                events = await bus.wait_for_event(
                    "session:messages", session_id="auto-sse-resolve", timeout=3.0,
                )
                assert len(events) >= 1, (
                    f"Expected broadcast after first file resolution, got {len(events)}"
                )
        finally:
            await _stop_monitor(mon, patcher)


# ── TestHostWatcherToSSE ───────────────────────────────────────────────


class TestHostWatcherToSSE:
    """Does the host watcher path lead to live SSE? (auto-5h9y bug test.)"""

    @pytest.mark.asyncio
    async def test_host_watcher_link_enables_sse(self, setup_env):
        """Register host session with no JSONL. Simulate link_and_enrich + add watch.
        Write to file. Assert: SSE broadcast arrives.
        This test fails without the auto-5h9y fix (missing inotify watch).
        """
        tmp_path, db_path = setup_env
        host_dir = tmp_path / "host_sessions"
        host_dir.mkdir()

        # Register host session with no JSONL
        _insert_session(
            db_path, "auto-sse-host", None,
            session_type="host",
            resolution_dir=str(host_dir),
        )

        bus = MockEventBus()
        mon, patcher = await _start_monitor(bus)
        try:
            assert mon._use_inotify is True

            from tools.dashboard.session_monitor import _TailState
            mon._tail_states["auto-sse-host"] = _TailState(
                needs_resolution=True,
                resolution_dir=host_dir,
            )

            bus.events.clear()
            await asyncio.sleep(0.05)

            # Simulate what _watch_for_host_session_jsonl does:
            # 1. Create JSONL file
            host_jsonl = host_dir / "host-uuid.jsonl"
            host_jsonl.touch()

            # 2. Call link_and_enrich (mocked to skip graph CLI)
            from tools.dashboard.dao.dashboard_db import update_jsonl_link
            update_jsonl_link("auto-sse-host", "host-uuid", str(host_jsonl), "test")

            # 3. Add inotify watch (the fix from auto-5h9y)
            mon._add_file_watch("auto-sse-host", str(host_jsonl))

            await asyncio.sleep(0.05)

            # Write to the file
            _write_jsonl_entry(host_jsonl, _make_user_entry("host session message"))

            events = await bus.wait_for_event(
                "session:messages", session_id="auto-sse-host", timeout=2.0,
            )
            assert len(events) >= 1, (
                "Expected SSE broadcast after host watcher link — "
                "this fails without auto-5h9y fix"
            )
            _, data = events[0]
            assert data["session_id"] == "auto-sse-host"
        finally:
            await _stop_monitor(mon, patcher)


# ── TestConfirmLinkEndpointToSSE ───────────────────────────────────────


class TestConfirmLinkEndpointToSSE:
    """Drives /api/session/confirm-link end-to-end.

    Earlier host-linking tests (TestHostWatcherToSSE) simulated the fix by
    calling _add_file_watch from the test body. This class does NOT do that.
    It POSTs to the real endpoint, then asserts that a subsequent JSONL write
    produces an SSE broadcast — which requires the endpoint itself to install
    the inotify watch.
    """

    @pytest.mark.xfail(reason="investigating — see auto-c1zj5", strict=False)
    @pytest.mark.asyncio
    async def test_confirm_link_installs_tailing(self, setup_env, monkeypatch):
        """Real endpoint → JSONL write → SSE broadcast. No simulation."""
        tmp_path, db_path = setup_env

        # Fake ~/.claude/projects layout
        fake_home = tmp_path / "home"
        projects = fake_home / ".claude" / "projects"
        proj = projects / "-workspace-repo"
        proj.mkdir(parents=True)

        handshake = "PROBE-xyz-E2E"
        jsonl = proj / "aabbccdd-1111-2222-3333-444455556666.jsonl"
        jsonl.write_text(json.dumps({
            "type": "user",
            "message": {"role": "user",
                        "content": [{"type": "text", "text": handshake}]},
            "timestamp": "2026-04-15T00:00:00Z",
        }) + "\n")

        monkeypatch.setattr(Path, "home", lambda: fake_home)

        _insert_session(
            db_path, "host-e2e", None,
            session_type="host",
            resolution_dir=None,
        )

        bus = MockEventBus()
        mon, patcher = await _start_monitor(bus)

        from tools.dashboard import server as server_mod
        orig_bus = server_mod.event_bus
        orig_mon = server_mod.session_monitor
        server_mod.event_bus = bus
        server_mod.session_monitor = mon

        # Stub graph CLI shell-out inside link_and_enrich without bypassing
        # link_and_enrich itself — we want the real DB update to happen.
        from tools.dashboard.dao import dashboard_db as _ddb
        link_patcher = patch.object(
            _ddb, "update_graph_source", lambda tn, src: None,
        )
        import subprocess as _sub
        run_patcher = patch.object(
            _sub, "run",
            return_value=MagicMock(returncode=0, stdout="", stderr=""),
        )
        link_patcher.start()
        run_patcher.start()

        try:
            from starlette.testclient import TestClient
            with TestClient(server_mod.app) as client:
                resp = client.post("/api/session/confirm-link", json={
                    "tmux_session": "host-e2e",
                    "handshake": handshake,
                })
                assert resp.status_code == 200, resp.text
                body = resp.json()
                assert body["ok"] is True

            await asyncio.sleep(0.1)
            bus.events.clear()

            # Now simulate Claude appending a new entry to the linked JSONL.
            # If confirm-link did its job, the monitor has an IN_MODIFY watch
            # on this file and will broadcast session:messages.
            _write_jsonl_entry(jsonl, _make_assistant_entry("after-link msg"))

            events = await bus.wait_for_event(
                "session:messages", session_id="host-e2e", timeout=3.0,
            )
            assert len(events) >= 1, (
                "confirm-link did not install tailing: JSONL write after "
                "successful handshake produced no session:messages broadcast. "
                "The endpoint must call session_monitor._add_file_watch() "
                "after link_and_enrich()."
            )
            _, data = events[0]
            assert data["session_id"] == "host-e2e"
            assert any(
                "after-link msg" in (e.get("content") or "")
                for e in data["entries"]
            )
        finally:
            run_patcher.stop()
            link_patcher.stop()
            server_mod.event_bus = orig_bus
            server_mod.session_monitor = orig_mon
            await _stop_monitor(mon, patcher)


# ── TestRolloverToSSE ──────────────────────────────────────────────────


class TestRolloverToSSE:
    """Does rollover maintain SSE delivery?"""

    @pytest.mark.asyncio
    async def test_container_rollover_continues_sse(self, setup_env):
        """Session tailing file A. Create file B (IN_CREATE rollover).
        Write to file B. Assert broadcast arrives from file B.
        """
        tmp_path, db_path = setup_env
        sess_dir = tmp_path / "container_rollover"
        sess_dir.mkdir()

        # Start with file A
        file_a = sess_dir / "uuid-a.jsonl"
        _write_jsonl_entry(file_a, _make_assistant_entry("file A initial"))

        _insert_session(
            db_path, "auto-sse-rollover", str(file_a),
            resolution_dir=str(sess_dir),
            session_uuids=json.dumps(["uuid-a"]),
        )

        bus = MockEventBus()
        mon, patcher = await _start_monitor(bus)
        try:
            assert mon._use_inotify is True
            bus.events.clear()
            await asyncio.sleep(0.05)

            # Create file B — triggers IN_CREATE (container rollover)
            from tools.dashboard.dao.dashboard_db import update_jsonl_link
            with patch(
                "tools.dashboard.dao.dashboard_db.link_and_enrich",
                side_effect=lambda tn, session_uuid, jsonl_path, project=None:
                    update_jsonl_link(tn, session_uuid, jsonl_path, project),
            ):
                file_b = sess_dir / "uuid-b.jsonl"
                file_b.touch()

                # Wait for IN_CREATE processing
                await asyncio.sleep(0.5)

                bus.events.clear()

                # Write to file B
                _write_jsonl_entry(file_b, _make_assistant_entry("file B after rollover"))

                events = await bus.wait_for_event(
                    "session:messages", session_id="auto-sse-rollover", timeout=2.0,
                )
                assert len(events) >= 1, "Expected broadcast from file B after rollover"
                _, data = events[0]
                assert data["session_id"] == "auto-sse-rollover"
                # Verify content is from file B
                found_b = any(
                    "file B" in e.get("content", "")
                    for e in data["entries"]
                )
                assert found_b, "Broadcast should contain entries from file B"
        finally:
            await _stop_monitor(mon, patcher)

    @pytest.mark.asyncio
    async def test_host_rollover_continues_sse(self, setup_env):
        """Session tailing file A. Create file B with parentUuid linking to A.
        Write to file B. Assert broadcast arrives from file B.
        """
        tmp_path, db_path = setup_env
        host_dir = tmp_path / "host_rollover"
        host_dir.mkdir()

        # File A with a known uuid
        file_a = host_dir / "uuid-aaa.jsonl"
        first_entry_a = {
            "type": "system",
            "message": {"role": "system", "content": "init"},
            "uuid": "uuid-aaa",
            "parentUuid": None,
            "timestamp": "2026-03-28T00:00:00Z",
        }
        _write_jsonl_entry(file_a, first_entry_a)

        _insert_session(
            db_path, "auto-sse-host-roll", str(file_a),
            session_type="host",
            resolution_dir=str(host_dir),
            session_uuids=json.dumps(["uuid-aaa"]),
        )

        bus = MockEventBus()
        mon, patcher = await _start_monitor(bus)
        try:
            assert mon._use_inotify is True
            bus.events.clear()
            await asyncio.sleep(0.05)

            # Create file B with parentUuid pointing to uuid-aaa
            file_b = host_dir / "uuid-bbb.jsonl"
            first_entry_b = {
                "type": "system",
                "message": {"role": "system", "content": "rollover"},
                "uuid": "uuid-bbb",
                "parentUuid": "uuid-aaa",
                "timestamp": "2026-03-28T00:01:00Z",
            }

            # Mock link_and_enrich
            from tools.dashboard.dao.dashboard_db import update_jsonl_link
            with patch(
                "tools.dashboard.dao.dashboard_db.link_and_enrich",
                side_effect=lambda tn, session_uuid, jsonl_path, project=None:
                    update_jsonl_link(tn, session_uuid, jsonl_path, project),
            ):
                _write_jsonl_entry(file_b, first_entry_b)

                # Wait for IN_CREATE + handler
                await asyncio.sleep(0.5)

                bus.events.clear()

                # Write to file B
                _write_jsonl_entry(file_b, _make_assistant_entry("host rollover content"))

                events = await bus.wait_for_event(
                    "session:messages", session_id="auto-sse-host-roll", timeout=2.0,
                )
                assert len(events) >= 1, "Expected broadcast from file B after host rollover"
                _, data = events[0]
                assert data["session_id"] == "auto-sse-host-roll"
        finally:
            await _stop_monitor(mon, patcher)


# ── TestRegistryBroadcast ──────────────────────────────────────────────


class TestRegistryBroadcast:
    """Does session lifecycle produce registry SSE events?"""

    @pytest.mark.asyncio
    async def test_register_broadcasts_registry(self, setup_env):
        """Register a new session. Assert: event bus receives session:registry."""
        tmp_path, db_path = setup_env
        sess_dir = tmp_path / "reg_sessions"
        sess_dir.mkdir()
        jsonl = sess_dir / "reg-uuid.jsonl"
        jsonl.touch()

        bus = MockEventBus()
        mon, patcher = await _start_monitor(bus)
        try:
            bus.events.clear()

            await mon.register(
                tmux_name="auto-sse-reg",
                session_type="container",
                project="test",
                jsonl_path=jsonl,
            )

            events = await bus.wait_for_event("session:registry", timeout=2.0)
            assert len(events) >= 1, "Expected session:registry broadcast on register"
        finally:
            await _stop_monitor(mon, patcher)

    @pytest.mark.asyncio
    async def test_death_broadcasts_registry(self, setup_env):
        """Mark session as dead via liveness check. Assert: session:registry broadcast
        with isLive=false.
        """
        tmp_path, db_path = setup_env
        sess_dir = tmp_path / "death_sessions"
        sess_dir.mkdir()
        jsonl = sess_dir / "death-uuid.jsonl"
        jsonl.touch()
        _insert_session(db_path, "auto-sse-death", str(jsonl))

        bus = MockEventBus()
        mon, patcher = await _start_monitor(bus)
        try:
            bus.events.clear()

            # Now simulate liveness check finding the session dead
            from tools.dashboard.dao.dashboard_db import mark_dead
            mon._remove_watches("auto-sse-death")
            mark_dead("auto-sse-death")
            mon._tail_states.pop("auto-sse-death", None)
            await mon._broadcast_registry()

            events = await bus.wait_for_event("session:registry", timeout=2.0)
            assert len(events) >= 1, "Expected session:registry on death"
            _, registry_data = events[0]
            # Registry should contain the session with is_live=False or
            # the session should be absent (dead sessions are filtered)
            assert isinstance(registry_data, list)
            dead = [s for s in registry_data if s.get("session_id") == "auto-sse-death"]
            if dead:
                assert dead[0]["is_live"] is False
            # If not in list at all, that also means the registry reflects death
        finally:
            await _stop_monitor(mon, patcher)


# ── TestRestartRecovery ────────────────────────────────────────────────


class TestRestartRecovery:
    """Does the tailer resume after restart?"""

    @pytest.mark.asyncio
    async def test_restart_resumes_from_offset(self, setup_env):
        """Create session with file_offset > 0 (simulating prior tailing).
        Restart monitor. Write new bytes after the offset.
        Assert: broadcast contains only the new entries.
        """
        tmp_path, db_path = setup_env
        sess_dir = tmp_path / "recovery_sessions"
        sess_dir.mkdir()
        jsonl = sess_dir / "recovery-uuid.jsonl"

        # Write some "old" content that should be skipped
        _write_jsonl_entry(jsonl, _make_assistant_entry("old entry 1"))
        _write_jsonl_entry(jsonl, _make_assistant_entry("old entry 2"))
        old_size = jsonl.stat().st_size

        # Insert session with file_offset at end of old content
        _insert_session(
            db_path, "auto-sse-recovery", str(jsonl),
            file_offset=old_size,
        )

        bus = MockEventBus()
        mon, patcher = await _start_monitor(bus)
        try:
            assert mon._use_inotify is True
            bus.events.clear()
            await asyncio.sleep(0.05)

            # Write new content after the offset
            _write_jsonl_entry(jsonl, _make_assistant_entry("new entry after restart"))

            events = await bus.wait_for_event(
                "session:messages", session_id="auto-sse-recovery", timeout=2.0,
            )
            assert len(events) >= 1, "Expected broadcast for new content after restart"

            # Verify only new entry, not old ones
            all_entries = []
            for _, data in events:
                all_entries.extend(data["entries"])
            contents = [e.get("content", "") for e in all_entries]
            assert any("new entry" in c for c in contents), (
                f"Expected 'new entry after restart' in entries, got {contents}"
            )
            assert not any("old entry" in c for c in contents), (
                f"Old entries should be skipped (file_offset={old_size}), got {contents}"
            )
        finally:
            await _stop_monitor(mon, patcher)
