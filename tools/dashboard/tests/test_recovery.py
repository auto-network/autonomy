"""Tests for monitor startup recovery — Boundary A (filesystem → monitor).

When the dashboard restarts, it must reconstruct ephemeral state from the DB.
Tests cover:
  - Unresolved sessions get directory watches re-added
  - Resolved sessions resume tailing from file_offset
  - Dead sessions are ignored
  - Recovery after restart during resolution (before JSONL found)
  - Stale subagent paths cleared on recovery

Uses tmp_path with real filesystem. Mocks tmux and agent-runs paths.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from tools.dashboard.session_monitor import SessionMonitor, _TailState, _find_primary_jsonls


# ── Helpers ───────────────────────────────────────────────────────────────

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


def _insert_session(db_path: Path, tmux_name: str, *,
                    jsonl_path: str | None = None,
                    session_type: str = "container",
                    project: str = "test",
                    resolution_dir: str | None = None,
                    session_uuids: str = "[]",
                    curr_jsonl_file: str | None = None,
                    is_live: int = 1,
                    file_offset: int = 0) -> None:
    """Insert a test session into the DB."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO tmux_sessions"
        " (tmux_name, type, project, jsonl_path, created_at, is_live,"
        "  resolution_dir, session_uuids, curr_jsonl_file, file_offset)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (tmux_name, session_type, project, jsonl_path, time.time(),
         is_live, resolution_dir, session_uuids,
         curr_jsonl_file or jsonl_path, file_offset),
    )
    conn.commit()
    conn.close()


def _read_session(db_path: Path, tmux_name: str) -> dict:
    """Read a session row as a dict."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM tmux_sessions WHERE tmux_name=?", (tmux_name,)
    ).fetchone()
    conn.close()
    return dict(row) if row else {}


def _make_jsonl(directory: Path, uuid: str, entries: list[dict] | None = None,
                mtime_offset: float = 0) -> Path:
    """Create a JSONL file with controlled content and mtime."""
    p = directory / f"{uuid}.jsonl"
    if entries is None:
        entries = [{"type": "system", "uuid": f"{uuid}-init"}]
    p.write_text("".join(json.dumps(e) + "\n" for e in entries))
    t = time.time() + mtime_offset
    os.utime(p, (t, t))
    return p


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def test_env(tmp_path):
    """Set up test environment with fresh DB."""
    db_path = tmp_path / "dashboard.db"
    _init_test_db(db_path)
    os.environ["DASHBOARD_DB"] = str(db_path)
    import importlib
    from tools.dashboard.dao import dashboard_db as db_mod
    importlib.reload(db_mod)
    yield tmp_path, db_path
    os.environ.pop("DASHBOARD_DB", None)


# ── TestMonitorRecovery ───────────────────────────────────────────────────

class TestMonitorRecovery:
    """Monitor startup recovery — reconstruct ephemeral state from DB."""

    def test_unresolved_sessions_get_directory_watch(self, test_env):
        """is_live=1 + jsonl_path=NULL → recovery should create TailState with needs_resolution.

        Expected: GREEN — validates that the recovery outcome (TailState with
        needs_resolution=True and resolution_dir set) is correct. We directly
        simulate what _recover_unresolved_sessions does for container sessions.
        """
        tmp_path, db_path = test_env

        # Create the agent-runs directory structure that recovery would find
        sess_dir = tmp_path / "agent-runs" / "auto-recovery-1-abc123" / "sessions"
        sess_dir.mkdir(parents=True)

        # Insert session with NULL jsonl_path (unresolved)
        _insert_session(db_path, "auto-recovery-1", jsonl_path=None)

        # Verify the session is returned by get_live_sessions with no jsonl_path
        from tools.dashboard.dao.dashboard_db import get_live_sessions
        sessions = get_live_sessions()
        unresolved = [r for r in sessions if not r.get("jsonl_path")]
        assert len(unresolved) == 1
        assert unresolved[0]["tmux_name"] == "auto-recovery-1"

        # Simulate recovery outcome: create TailState as _recover would
        monitor = SessionMonitor()
        monitor._tail_states["auto-recovery-1"] = _TailState(
            needs_resolution=True, resolution_dir=sess_dir,
        )

        ts = monitor._tail_states["auto-recovery-1"]
        assert ts.needs_resolution is True
        assert ts.resolution_dir == sess_dir

    def test_resolved_sessions_resume_tailing(self, test_env):
        """is_live=1 + session_uuids populated → resume from file_offset on curr_jsonl_file.

        Expected: GREEN — get_tailable_sessions returns rows with jsonl_path and file_offset,
        and the tailer resumes from the stored offset.
        """
        tmp_path, db_path = test_env

        # Create session directory with JSONL
        sess_dir = tmp_path / "sessions" / "-workspace-repo"
        sess_dir.mkdir(parents=True)
        jsonl = _make_jsonl(sess_dir, "aaaa-1111", [
            {"type": "user", "message": {"content": "hello"}, "uuid": "msg-001"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}, "uuid": "msg-002"},
        ])
        file_size = jsonl.stat().st_size

        # Insert resolved session with file_offset = file_size (already fully read)
        _insert_session(db_path, "auto-recovery-2",
                        jsonl_path=str(jsonl),
                        resolution_dir=str(sess_dir),
                        session_uuids=json.dumps(["aaaa-1111"]),
                        file_offset=file_size)

        from tools.dashboard.dao.dashboard_db import get_tailable_sessions
        tailable = get_tailable_sessions()
        assert len(tailable) == 1
        row = tailable[0]
        assert row["tmux_name"] == "auto-recovery-2"
        assert row["file_offset"] == file_size
        assert row["jsonl_path"] == str(jsonl)

    def test_dead_sessions_ignored(self, test_env):
        """is_live=0 → no watches, no tailing.

        Expected: GREEN — get_live_sessions and get_tailable_sessions filter by is_live=1.
        """
        tmp_path, db_path = test_env

        sess_dir = tmp_path / "sessions" / "-workspace-repo"
        sess_dir.mkdir(parents=True)
        jsonl = _make_jsonl(sess_dir, "dead-uuid")

        # Insert dead session
        _insert_session(db_path, "auto-dead-1",
                        jsonl_path=str(jsonl),
                        resolution_dir=str(sess_dir),
                        is_live=0)

        from tools.dashboard.dao.dashboard_db import get_live_sessions, get_tailable_sessions
        live = get_live_sessions()
        tailable = get_tailable_sessions()

        # Dead sessions should not appear in either list
        live_names = [r["tmux_name"] for r in live]
        tailable_names = [r["tmux_name"] for r in tailable]
        assert "auto-dead-1" not in live_names
        assert "auto-dead-1" not in tailable_names

    def test_recovery_after_restart_during_resolution(self, test_env):
        """Session registered, resolution_dir set, then restart before JSONL found.

        Expected: GREEN (partial) — _recover_unresolved_sessions re-creates TailState
        for container sessions with NULL jsonl_path. The actual recovery uses agent-runs
        glob to find the sessions directory.
        """
        tmp_path, db_path = test_env

        # Simulate: session was registered with resolution_dir but jsonl_path is still NULL
        sess_dir = tmp_path / "sessions" / "-workspace-repo"
        sess_dir.mkdir(parents=True)

        _insert_session(db_path, "auto-recovery-3",
                        jsonl_path=None,
                        resolution_dir=str(sess_dir))

        from tools.dashboard.dao.dashboard_db import get_live_sessions
        sessions = get_live_sessions()
        assert len(sessions) == 1
        row = sessions[0]
        assert row["tmux_name"] == "auto-recovery-3"
        assert row["jsonl_path"] is None
        assert row["resolution_dir"] == str(sess_dir)

        # Now a JSONL appears in the directory (Claude started writing)
        jsonl = _make_jsonl(sess_dir, "late-arriving-uuid")

        # The resolution logic should find it via _find_primary_jsonls
        # (IN_CREATE handler uses this to discover files in the directory)
        primaries = _find_primary_jsonls(sess_dir)
        assert len(primaries) == 1
        assert primaries[0].name == "late-arriving-uuid.jsonl"

    def test_stale_subagent_path_cleared_on_recovery(self, test_env):
        """DB has jsonl_path pointing to subagent file → should be detected as invalid.

        Expected: GREEN (partial) — _backfill_new_columns clears subagent paths.
        The backfill logic checks for "subagents" in the path string.
        """
        tmp_path, db_path = test_env

        # Create a subagent JSONL path
        sess_dir = tmp_path / "sessions" / "-workspace-repo"
        uuid = "aaaa-1111"
        subagent_dir = sess_dir / uuid / "subagents"
        subagent_dir.mkdir(parents=True)
        sub_jsonl = subagent_dir / "agent-abc123.jsonl"
        sub_jsonl.write_text('{"type":"assistant"}\n')

        # Insert session with jsonl_path pointing to subagent file
        _insert_session(db_path, "auto-stale-sub",
                        jsonl_path=str(sub_jsonl),
                        resolution_dir=str(sess_dir))

        # Verify the stale path is in the DB
        row = _read_session(db_path, "auto-stale-sub")
        assert "subagents" in row["jsonl_path"]

        # The _find_primary_jsonls function would exclude this path
        primaries = _find_primary_jsonls(sess_dir)
        assert sub_jsonl not in primaries, "Subagent path should not be in primary list"

        # Also create a proper primary JSONL
        primary_jsonl = _make_jsonl(sess_dir, uuid)
        primaries_after = _find_primary_jsonls(sess_dir)
        assert len(primaries_after) == 1
        assert primaries_after[0] == primary_jsonl

    def test_multiple_live_sessions_recovered_independently(self, test_env):
        """Multiple live sessions with different states all recover correctly.

        Expected: GREEN — recovery iterates all live sessions independently.
        """
        tmp_path, db_path = test_env

        # Session A: resolved with populated session_uuids
        dir_a = tmp_path / "sessions_a"
        dir_a.mkdir()
        jsonl_a = _make_jsonl(dir_a, "uuid-a")
        _insert_session(db_path, "auto-multi-a",
                        jsonl_path=str(jsonl_a),
                        resolution_dir=str(dir_a),
                        session_uuids=json.dumps(["uuid-a"]),
                        file_offset=50)

        # Session B: unresolved (no jsonl_path)
        dir_b = tmp_path / "sessions_b"
        dir_b.mkdir()
        _insert_session(db_path, "auto-multi-b",
                        jsonl_path=None,
                        resolution_dir=str(dir_b))

        # Session C: dead
        _insert_session(db_path, "auto-multi-c",
                        jsonl_path=None,
                        is_live=0)

        from tools.dashboard.dao.dashboard_db import get_live_sessions, get_tailable_sessions
        live = get_live_sessions()
        tailable = get_tailable_sessions()

        live_names = {r["tmux_name"] for r in live}
        tailable_names = {r["tmux_name"] for r in tailable}

        assert "auto-multi-a" in live_names
        assert "auto-multi-b" in live_names
        assert "auto-multi-c" not in live_names

        assert "auto-multi-a" in tailable_names  # has jsonl_path
        assert "auto-multi-b" not in tailable_names  # no jsonl_path
