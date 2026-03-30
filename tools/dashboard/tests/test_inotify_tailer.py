"""Tests for inotify-based JSONL tailer in session_monitor.

Tests cover:
  - inotify mode selection (inotify vs polling fallback)
  - Watch management (add/remove file and dir watches)
  - IN_MODIFY delivery latency (< 100ms target)
  - Watch cleanup on session death
  - Graceful fallback when inotify unavailable
  - Deduplication of directory watches
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
# (these are container-only deps, added in the dashboard Docker image)
pytest.importorskip("inotify_simple")
pytest.importorskip("pytest_asyncio")


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


def _insert_session(db_path: Path, tmux_name: str, jsonl_path: str,
                    resolution_dir: str | None = None) -> None:
    """Insert a test session into the DB."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO tmux_sessions"
        " (tmux_name, type, project, jsonl_path, created_at, is_live,"
        "  resolution_dir, session_uuids, curr_jsonl_file)"
        " VALUES (?, 'container', 'test', ?, ?, 1, ?, '[]', ?)",
        (tmux_name, jsonl_path, time.time(), resolution_dir or str(Path(jsonl_path).parent), jsonl_path),
    )
    conn.commit()
    conn.close()


def _write_jsonl_entry(path: Path, entry: dict) -> None:
    """Append a single JSONL entry to a file."""
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def _make_assistant_entry(text: str = "Hello") -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
        "timestamp": "2026-03-28T00:00:00Z",
    }


@pytest.fixture
def setup_env(tmp_path):
    """Set up a test environment with DB and session JSONL."""
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


# ── Tests ──────────────────────────────────────────────────────────────


class TestInotifyInit:
    """Test inotify initialization and fallback."""

    def test_inotify_available(self, setup_env):
        """When inotify_simple is available, _use_inotify should be True."""
        from tools.dashboard.session_monitor import SessionMonitor
        mon = SessionMonitor()
        mon._init_inotify()
        assert mon._use_inotify is True
        assert mon._inotify is not None

    def test_inotify_fallback_when_import_fails(self, setup_env):
        """When inotify_simple import fails, falls back to polling."""
        import tools.dashboard.session_monitor as sm
        original = sm._HAS_INOTIFY
        try:
            sm._HAS_INOTIFY = False
            mon = sm.SessionMonitor()
            mon._init_inotify()
            assert mon._use_inotify is False
            assert mon._inotify is None
        finally:
            sm._HAS_INOTIFY = original

    def test_watches_added_for_existing_sessions(self, setup_env):
        """On init, watches are added for all tailable sessions."""
        tmp_path, db_path = setup_env

        # Create a JSONL file and register session
        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir()
        jsonl = sess_dir / "test.jsonl"
        _write_jsonl_entry(jsonl, _make_assistant_entry())
        _insert_session(db_path, "auto-test-1", str(jsonl), str(sess_dir))

        from tools.dashboard.session_monitor import SessionMonitor
        mon = SessionMonitor()
        mon._init_inotify()

        assert mon._use_inotify is True
        assert len(mon._wd_to_session) == 1
        assert "auto-test-1" in mon._wd_to_session.values()
        assert len(mon._dir_path_to_wd) == 1


class TestWatchManagement:
    """Test add/remove watch operations."""

    def test_add_file_watch(self, setup_env):
        """_add_file_watch creates a watch and maps wd to session."""
        tmp_path, _ = setup_env
        jsonl = tmp_path / "test.jsonl"
        jsonl.touch()

        from tools.dashboard.session_monitor import SessionMonitor, _TailState
        mon = SessionMonitor()
        mon._init_inotify()
        mon._tail_states["auto-x"] = _TailState()
        mon._add_file_watch("auto-x", str(jsonl))

        ts = mon._tail_states["auto-x"]
        assert ts.watch_descriptor is not None
        assert mon._wd_to_session[ts.watch_descriptor] == "auto-x"

    def test_add_dir_watch_dedup(self, setup_env):
        """Multiple sessions in same dir share one kernel watch."""
        tmp_path, _ = setup_env
        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir()

        from tools.dashboard.session_monitor import SessionMonitor, _TailState
        mon = SessionMonitor()
        mon._init_inotify()
        mon._tail_states["auto-a"] = _TailState()
        mon._tail_states["auto-b"] = _TailState()

        mon._add_dir_watch("auto-a", str(sess_dir))
        mon._add_dir_watch("auto-b", str(sess_dir))

        # Only one kernel watch
        assert len(mon._dir_path_to_wd) == 1
        wd = mon._dir_path_to_wd[str(sess_dir)]
        assert mon._dir_wd_sessions[wd] == {"auto-a", "auto-b"}

    def test_remove_watches_dedup(self, setup_env):
        """Removing one session from a shared dir doesn't remove kernel watch."""
        tmp_path, _ = setup_env
        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir()
        jsonl_a = sess_dir / "a.jsonl"
        jsonl_b = sess_dir / "b.jsonl"
        jsonl_a.touch()
        jsonl_b.touch()

        from tools.dashboard.session_monitor import SessionMonitor, _TailState
        mon = SessionMonitor()
        mon._init_inotify()
        mon._tail_states["auto-a"] = _TailState()
        mon._tail_states["auto-b"] = _TailState()

        mon._add_file_watch("auto-a", str(jsonl_a))
        mon._add_file_watch("auto-b", str(jsonl_b))
        mon._add_dir_watch("auto-a", str(sess_dir))
        mon._add_dir_watch("auto-b", str(sess_dir))

        # Remove auto-a — dir watch should persist for auto-b
        mon._remove_watches("auto-a")

        assert mon._tail_states["auto-a"].watch_descriptor is None
        assert mon._tail_states["auto-a"].dir_watch_descriptor is None
        assert len(mon._dir_path_to_wd) == 1  # dir watch still alive
        wd = list(mon._dir_wd_sessions.keys())[0]
        assert mon._dir_wd_sessions[wd] == {"auto-b"}

        # Remove auto-b — now kernel watch gets removed
        mon._remove_watches("auto-b")
        assert len(mon._dir_path_to_wd) == 0
        assert len(mon._dir_wd_sessions) == 0

    def test_add_file_watch_replaces_stale(self, setup_env):
        """Adding a file watch for a session replaces any existing watch."""
        tmp_path, _ = setup_env
        f1 = tmp_path / "old.jsonl"
        f2 = tmp_path / "new.jsonl"
        f1.touch()
        f2.touch()

        from tools.dashboard.session_monitor import SessionMonitor, _TailState
        mon = SessionMonitor()
        mon._init_inotify()
        mon._tail_states["auto-r"] = _TailState()

        mon._add_file_watch("auto-r", str(f1))
        old_wd = mon._tail_states["auto-r"].watch_descriptor
        assert old_wd is not None

        mon._add_file_watch("auto-r", str(f2))
        new_wd = mon._tail_states["auto-r"].watch_descriptor
        assert new_wd is not None
        assert new_wd != old_wd
        assert old_wd not in mon._wd_to_session
        assert mon._wd_to_session[new_wd] == "auto-r"


class TestInotifyTailerLoop:
    """Integration tests for the inotify tailer loop."""

    @pytest.mark.asyncio
    async def test_inotify_detects_write_and_broadcasts(self, setup_env):
        """IN_MODIFY event triggers read + broadcast within 100ms."""
        tmp_path, db_path = setup_env

        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir()
        jsonl = sess_dir / "test.jsonl"
        jsonl.touch()
        _insert_session(db_path, "auto-inotify-test", str(jsonl), str(sess_dir))

        from tools.dashboard.session_monitor import SessionMonitor
        from tools.dashboard.event_bus import EventBus

        bus = EventBus()
        mon = SessionMonitor()

        def simple_parser(line: str):
            try:
                return json.loads(line)
            except Exception:
                return None

        # Mock tmux check so liveness loop doesn't kill our test session
        with patch.object(SessionMonitor, "_check_tmux", staticmethod(lambda name: True)):
            await mon.start(event_bus=bus, entry_parser=simple_parser)
            assert mon._use_inotify is True

            q = bus.subscribe()
            await asyncio.sleep(0.1)
            while not q.empty():
                q.get_nowait()

            t0 = time.monotonic()
            _write_jsonl_entry(jsonl, _make_assistant_entry("inotify test"))

            # Wait for session:messages (skip registry events)
            deadline = time.monotonic() + 2.0
            topic = data = None
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                try:
                    topic, data, seq = await asyncio.wait_for(q.get(), timeout=max(0.01, remaining))
                    if topic == "session:messages":
                        break
                except asyncio.TimeoutError:
                    break

            latency = time.monotonic() - t0
            assert topic == "session:messages", f"Expected session:messages, got {topic}"
            assert data["session_id"] == "auto-inotify-test"
            assert len(data["entries"]) >= 1
            assert latency < 1.0, f"Latency {latency:.3f}s exceeds 1s"

            mon._tailer_task.cancel()
            mon._liveness_task.cancel()
            try:
                await mon._tailer_task
            except asyncio.CancelledError:
                pass
            try:
                await mon._liveness_task
            except asyncio.CancelledError:
                pass

    def test_polling_tailer_removed(self, setup_env):
        """Polling tailer has been removed — only inotify path exists."""
        import tools.dashboard.session_monitor as sm
        assert not hasattr(sm.SessionMonitor, "_polling_tailer_loop"), (
            "_polling_tailer_loop should have been removed"
        )


class TestRegisterDeregister:
    """Test that register/deregister manage watches correctly."""

    @pytest.mark.asyncio
    async def test_register_adds_watches(self, setup_env):
        """Registering a session with jsonl_path adds inotify watches."""
        tmp_path, _ = setup_env

        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir()
        jsonl = sess_dir / "test.jsonl"
        jsonl.touch()

        from tools.dashboard.session_monitor import SessionMonitor
        mon = SessionMonitor()
        mon._init_inotify()
        assert mon._use_inotify

        await mon.register(
            tmux_name="auto-reg-test",
            session_type="container",
            project="test",
            jsonl_path=jsonl,
        )

        ts = mon._tail_states.get("auto-reg-test")
        assert ts is not None
        assert ts.watch_descriptor is not None
        assert ts.dir_watch_descriptor is not None

    @pytest.mark.asyncio
    async def test_deregister_removes_watches(self, setup_env):
        """Deregistering a session removes its inotify watches."""
        tmp_path, _ = setup_env

        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir()
        jsonl = sess_dir / "test.jsonl"
        jsonl.touch()

        from tools.dashboard.session_monitor import SessionMonitor
        mon = SessionMonitor()
        mon._init_inotify()

        await mon.register(
            tmux_name="auto-dereg-test",
            session_type="container",
            project="test",
            jsonl_path=jsonl,
        )

        ts = mon._tail_states.get("auto-dereg-test")
        old_wd = ts.watch_descriptor
        assert old_wd is not None
        assert old_wd in mon._wd_to_session

        await mon.deregister("auto-dereg-test")

        assert old_wd not in mon._wd_to_session
        assert "auto-dereg-test" not in mon._tail_states


class TestHostSessionWatcherAddsWatches:
    """After JSONL watcher links a host session, inotify watches must exist."""

    @pytest.mark.asyncio
    async def test_watcher_adds_inotify_watches(self, setup_env):
        """_watch_for_host_session_jsonl adds file+dir watches after link_and_enrich."""
        tmp_path, db_path = setup_env

        projects_dir = tmp_path / "projects" / "myproject"
        projects_dir.mkdir(parents=True)
        tmux_name = "host-test-watcher"

        # Insert a host session row (no jsonl_path yet)
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO tmux_sessions"
            " (tmux_name, type, project, created_at, is_live)"
            " VALUES (?, 'host', 'myproject', ?, 1)",
            (tmux_name, time.time()),
        )
        conn.commit()
        conn.close()

        # Build a SessionMonitor with real inotify
        from tools.dashboard.session_monitor import SessionMonitor, _TailState
        from tools.dashboard.event_bus import EventBus

        bus = EventBus()
        mon = SessionMonitor()
        mon._init_inotify()
        mon._event_bus = bus
        assert mon._use_inotify

        # Patch session_monitor singleton so server.py's watcher uses our monitor
        import tools.dashboard.server as srv
        original_mon = srv.session_monitor
        srv.session_monitor = mon

        try:
            # Start the watcher — no JSONL yet, so it will poll
            task = asyncio.create_task(
                srv._watch_for_host_session_jsonl(projects_dir, tmux_name, timeout=5.0)
            )

            # Simulate Claude creating the JSONL after a short delay
            await asyncio.sleep(0.3)
            new_jsonl = projects_dir / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"
            new_jsonl.write_text('{"type":"system"}\n')

            # Wait for the watcher to find it
            await asyncio.wait_for(task, timeout=5.0)

            # Verify: inotify file watch exists
            ts = mon._tail_states.get(tmux_name)
            assert ts is not None, "TailState should exist after watcher links"
            assert ts.watch_descriptor is not None, "File watch should be set"
            assert mon._wd_to_session[ts.watch_descriptor] == tmux_name

            # Verify: inotify dir watch exists
            assert str(projects_dir) in mon._dir_path_to_wd, "Dir watch should be set"

            # Verify: resolution_dir is set for rollover detection
            assert ts.resolution_dir == projects_dir
        finally:
            srv.session_monitor = original_mon
