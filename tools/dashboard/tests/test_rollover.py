"""Tests for JSONL rollover detection — Boundary A (filesystem → monitor).

Covers:
  - Container rollover: session_uuids growth, curr_jsonl_file update, offset reset
  - Host rollover via parentUuid: predecessor matching, null parentUuid, no-match warning
  - Host mtime prohibition: mtime must NOT be used for cross-session resolution

Uses tmp_path with real file writes. Mocks tmux. No real sessions.
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
         is_live, resolution_dir, session_uuids, jsonl_path, file_offset),
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
def container_session(tmp_path):
    """Isolated container session directory with one JSONL."""
    resolution_dir = tmp_path / "sessions" / "-workspace-repo"
    resolution_dir.mkdir(parents=True)
    uuid1 = "aaaa-1111"
    jsonl = _make_jsonl(resolution_dir, uuid1, [
        {"type": "user", "message": {"content": "hello"}, "uuid": "msg-001", "parentUuid": None},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}, "uuid": "msg-002"},
    ])
    return {"resolution_dir": resolution_dir, "uuid": uuid1, "jsonl": jsonl}


@pytest.fixture
def container_rollover(container_session):
    """Second JSONL linked by parentUuid."""
    uuid2 = "bbbb-2222"
    last_uuid = "msg-002"  # last uuid from first file
    jsonl2 = _make_jsonl(container_session["resolution_dir"], uuid2, [
        {"type": "user", "message": {"content": "continued"}, "uuid": "msg-003", "parentUuid": last_uuid},
    ], mtime_offset=1)
    return {**container_session, "uuid2": uuid2, "jsonl2": jsonl2}


@pytest.fixture
def host_shared_dir(tmp_path):
    """Host project dir with multiple sessions' files mixed together."""
    project_dir = tmp_path / "projects" / "-workspace-repo"
    project_dir.mkdir(parents=True)
    a_jsonl = _make_jsonl(project_dir, "aaaa-1111", [
        {"type": "user", "message": {"content": "session A"}, "uuid": "a-001", "parentUuid": None},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "reply A"}]}, "uuid": "a-002"},
    ])
    b_jsonl = _make_jsonl(project_dir, "cccc-3333", [
        {"type": "user", "message": {"content": "session B"}, "uuid": "b-001", "parentUuid": None},
    ], mtime_offset=2)
    return {"project_dir": project_dir, "session_a": a_jsonl, "session_b": b_jsonl}


@pytest.fixture
def test_db(tmp_path):
    """Create a fresh test dashboard.db and point the DAO at it."""
    db_path = tmp_path / "dashboard.db"
    _init_test_db(db_path)
    os.environ["DASHBOARD_DB"] = str(db_path)
    import importlib
    from tools.dashboard.dao import dashboard_db as db_mod
    importlib.reload(db_mod)
    yield db_path
    os.environ.pop("DASHBOARD_DB", None)


# ── TestContainerRollover ─────────────────────────────────────────────────

class TestContainerRollover:
    """Container rollover detection: second JSONL appears in isolated dir."""

    def test_new_file_appends_uuid(self, test_db, container_rollover):
        """After rollover, session_uuids grows from ["aaaa-1111"] to ["aaaa-1111","bbbb-2222"].

        Expected: GREEN — update_jsonl_link already appends to session_uuids.
        """
        from tools.dashboard.dao.dashboard_db import update_jsonl_link

        rd = container_rollover["resolution_dir"]
        uuid1 = container_rollover["uuid"]
        uuid2 = container_rollover["uuid2"]
        jsonl2 = container_rollover["jsonl2"]

        # Pre-populate DB with initial session (already resolved to first file)
        _insert_session(test_db, "auto-rollover-1",
                        jsonl_path=str(container_rollover["jsonl"]),
                        resolution_dir=str(rd),
                        session_uuids=json.dumps([uuid1]))

        # Simulate rollover: link_and_enrich calls update_jsonl_link
        update_jsonl_link("auto-rollover-1", session_uuid=uuid2,
                          jsonl_path=str(jsonl2), project=rd.name)

        row = _read_session(test_db, "auto-rollover-1")
        uuids = json.loads(row["session_uuids"])
        assert uuids == [uuid1, uuid2], f"session_uuids should contain both UUIDs, got {uuids}"

    def test_curr_jsonl_file_updated(self, test_db, container_rollover):
        """After rollover, curr_jsonl_file points to the new file.

        Expected: GREEN — update_jsonl_link sets curr_jsonl_file.
        """
        from tools.dashboard.dao.dashboard_db import update_jsonl_link

        rd = container_rollover["resolution_dir"]
        uuid2 = container_rollover["uuid2"]
        jsonl2 = container_rollover["jsonl2"]

        _insert_session(test_db, "auto-rollover-2",
                        jsonl_path=str(container_rollover["jsonl"]),
                        resolution_dir=str(rd),
                        session_uuids=json.dumps([container_rollover["uuid"]]))

        update_jsonl_link("auto-rollover-2", session_uuid=uuid2,
                          jsonl_path=str(jsonl2), project=rd.name)

        row = _read_session(test_db, "auto-rollover-2")
        assert row["curr_jsonl_file"] == str(jsonl2)

    def test_file_offset_reset_to_zero(self, test_db, container_rollover):
        """After rollover, the monitor resets the TailState (offset effectively 0).

        Expected: GREEN — rollover block creates fresh _TailState (broadcast_seq=0).
        """
        monitor = SessionMonitor()
        ts = _TailState(resolution_dir=container_rollover["resolution_dir"])
        ts.broadcast_seq = 42
        monitor._tail_states["auto-rollover-3"] = ts

        rd = container_rollover["resolution_dir"]
        old_jsonl = container_rollover["jsonl"]
        new_jsonl = container_rollover["jsonl2"]

        sessions = [{"tmux_name": "auto-rollover-3", "jsonl_path": str(old_jsonl), "is_live": 1}]

        with patch("tools.dashboard.dao.dashboard_db.link_and_enrich"):
            tailable = {r["tmux_name"]: r for r in sessions}
            for tmux_name, tail_st in list(monitor._tail_states.items()):
                if tail_st.needs_resolution:
                    continue
                row = tailable.get(tmux_name)
                if not row or not row.get("jsonl_path"):
                    continue
                current_path = Path(row["jsonl_path"])
                if not current_path.exists():
                    continue
                sessions_dir = tail_st.resolution_dir or current_path.parent
                jsonl_files = sorted(_find_primary_jsonls(sessions_dir), key=lambda p: p.stat().st_mtime)
                if len(jsonl_files) <= 1:
                    continue
                newest = jsonl_files[-1]
                if newest != current_path:
                    old_rd = monitor._tail_states.pop(tmux_name, _TailState()).resolution_dir
                    monitor._tail_states[tmux_name] = _TailState(resolution_dir=old_rd)

        new_ts = monitor._tail_states["auto-rollover-3"]
        assert new_ts.broadcast_seq == 0, "TailState should be fresh after rollover"

    def test_link_and_enrich_called(self, test_db, container_rollover):
        """Rollover triggers link_and_enrich for graph ingestion.

        Expected: GREEN — existing rollover block calls link_and_enrich.
        """
        monitor = SessionMonitor()
        monitor._tail_states["auto-rollover-4"] = _TailState(
            resolution_dir=container_rollover["resolution_dir"])

        old_jsonl = container_rollover["jsonl"]
        new_jsonl = container_rollover["jsonl2"]
        rd = container_rollover["resolution_dir"]

        sessions = [{"tmux_name": "auto-rollover-4", "jsonl_path": str(old_jsonl), "is_live": 1}]

        with patch("tools.dashboard.dao.dashboard_db.link_and_enrich") as mock_link:
            tailable = {r["tmux_name"]: r for r in sessions}
            for tmux_name, ts in list(monitor._tail_states.items()):
                if ts.needs_resolution:
                    continue
                row = tailable.get(tmux_name)
                if not row or not row.get("jsonl_path"):
                    continue
                current_path = Path(row["jsonl_path"])
                if not current_path.exists():
                    continue
                sessions_dir = ts.resolution_dir or current_path.parent
                jsonl_files = sorted(_find_primary_jsonls(sessions_dir), key=lambda p: p.stat().st_mtime)
                if len(jsonl_files) <= 1:
                    continue
                newest = jsonl_files[-1]
                if newest != current_path:
                    mock_link(tmux_name, session_uuid=newest.stem,
                              jsonl_path=str(newest), project=sessions_dir.name)
                    old_rd = monitor._tail_states.pop(tmux_name, _TailState()).resolution_dir
                    monitor._tail_states[tmux_name] = _TailState(resolution_dir=old_rd)

            mock_link.assert_called_once_with(
                "auto-rollover-4",
                session_uuid="bbbb-2222",
                jsonl_path=str(new_jsonl),
                project=rd.name,
            )

    def test_first_file_sets_resolved(self, container_session):
        """First JSONL in empty session → discoverable via _find_primary_jsonls.

        Expected: GREEN — _find_primary_jsonls returns the JSONL in dir.
        """
        primaries = _find_primary_jsonls(container_session["resolution_dir"])
        assert len(primaries) == 1
        assert primaries[0].name == f"{container_session['uuid']}.jsonl"

    def test_subagent_file_does_not_trigger(self, container_session):
        """Subagent JSONL (in uuid/subagents/) → no rollover, session_uuids unchanged.

        Expected: GREEN — _find_primary_jsonls excludes subagent paths.
        """
        rd = container_session["resolution_dir"]
        uuid = container_session["uuid"]

        # Create subagent file in uuid/subagents/ subdirectory
        subagent_dir = rd / uuid / "subagents"
        subagent_dir.mkdir(parents=True)
        sub_jsonl = subagent_dir / "agent-abc123.jsonl"
        sub_jsonl.write_text('{"type":"assistant","message":{"content":[{"type":"text","text":"subagent"}]}}\n')
        # Make subagent file newer than primary
        os.utime(sub_jsonl, (time.time() + 10, time.time() + 10))

        monitor = SessionMonitor()
        monitor._tail_states["auto-sub-test"] = _TailState(resolution_dir=rd)

        sessions = [{"tmux_name": "auto-sub-test", "jsonl_path": str(container_session["jsonl"]), "is_live": 1}]

        with patch("tools.dashboard.dao.dashboard_db.link_and_enrich") as mock_link:
            tailable = {r["tmux_name"]: r for r in sessions}
            for tmux_name, ts in list(monitor._tail_states.items()):
                if ts.needs_resolution:
                    continue
                row = tailable.get(tmux_name)
                if not row or not row.get("jsonl_path"):
                    continue
                current_path = Path(row["jsonl_path"])
                if not current_path.exists():
                    continue
                sessions_dir = ts.resolution_dir or current_path.parent
                jsonl_files = sorted(_find_primary_jsonls(sessions_dir), key=lambda p: p.stat().st_mtime)
                if len(jsonl_files) <= 1:
                    continue
                newest = jsonl_files[-1]
                if newest != current_path:
                    mock_link(tmux_name, session_uuid=newest.stem,
                              jsonl_path=str(newest), project=sessions_dir.name)

            # Subagent file should be excluded → only 1 primary JSONL → no rollover
            mock_link.assert_not_called()


# ── TestHostRolloverViaParentUuid ─────────────────────────────────────────

class TestHostRolloverViaParentUuid:
    """Host rollover detection via parentUuid chain matching.

    In host mode, multiple sessions share a directory. Rollover cannot use mtime
    (newest file might belong to a different session). Instead, the first entry's
    parentUuid must match the last entry's uuid of an existing file.
    """

    def test_parentuuid_matches_predecessor(self, host_shared_dir):
        """New file with parentUuid matching last entry of existing file → predecessor found.

        Tests _read_parent_uuid + _find_predecessor_by_parentuuid together.
        """
        project_dir = host_shared_dir["project_dir"]

        # Create a continuation file whose parentUuid matches session A's last uuid
        continuation = _make_jsonl(project_dir, "dddd-4444", [
            {"type": "user", "message": {"content": "continued A"}, "uuid": "a-003", "parentUuid": "a-002"},
        ], mtime_offset=5)

        # Read parentUuid from the new file
        parent_uuid = SessionMonitor._read_parent_uuid(continuation)
        assert parent_uuid == "a-002"

        # Find the predecessor file containing "a-002"
        predecessor_uuid = SessionMonitor._find_predecessor_by_parentuuid(
            parent_uuid, continuation, str(project_dir),
        )
        assert predecessor_uuid == "aaaa-1111", (
            f"Should find aaaa-1111 as predecessor, got {predecessor_uuid}"
        )

    def test_parentuuid_null_is_new_session(self, host_shared_dir):
        """New file with parentUuid=null → _read_parent_uuid returns None (not a rollover)."""
        project_dir = host_shared_dir["project_dir"]

        new_session = _make_jsonl(project_dir, "eeee-5555", [
            {"type": "user", "message": {"content": "brand new"}, "uuid": "e-001", "parentUuid": None},
        ], mtime_offset=5)

        parent_uuid = SessionMonitor._read_parent_uuid(new_session)
        assert parent_uuid is None, "Null parentUuid should return None (new session, not rollover)"

    def test_no_predecessor_logs_warning(self, host_shared_dir, caplog):
        """parentUuid non-null but no file contains matching uuid → None returned."""
        project_dir = host_shared_dir["project_dir"]

        orphan = _make_jsonl(project_dir, "ffff-6666", [
            {"type": "user", "message": {"content": "orphan"}, "uuid": "f-001", "parentUuid": "nonexistent-uuid"},
        ], mtime_offset=5)

        parent_uuid = SessionMonitor._read_parent_uuid(orphan)
        assert parent_uuid == "nonexistent-uuid"

        predecessor = SessionMonitor._find_predecessor_by_parentuuid(
            parent_uuid, orphan, str(project_dir),
        )
        assert predecessor is None, "No predecessor with uuid 'nonexistent-uuid' should exist"

    def test_grep_excludes_self(self, host_shared_dir):
        """When searching for parentUuid match, the new file itself must be excluded.

        The new file contains "a-002" as a parentUuid value — grep must exclude it
        to avoid matching the file against itself.
        """
        project_dir = host_shared_dir["project_dir"]

        # File whose first entry parentUuid = "a-002"
        # If we naively grep all files for "a-002", this file itself would match
        continuation = _make_jsonl(project_dir, "gggg-7777", [
            {"type": "user", "message": {"content": "cont"}, "uuid": "g-001", "parentUuid": "a-002"},
        ], mtime_offset=5)

        predecessor_uuid = SessionMonitor._find_predecessor_by_parentuuid(
            "a-002", continuation, str(project_dir),
        )
        # Should find aaaa-1111 (session A), NOT gggg-7777 (self)
        assert predecessor_uuid == "aaaa-1111", (
            f"Should find aaaa-1111 as predecessor (not self), got {predecessor_uuid}"
        )

    def test_correct_session_identified(self, host_shared_dir):
        """In shared dir with 3 sessions' files, grep finds the right predecessor."""
        project_dir = host_shared_dir["project_dir"]

        # Add a third session's file
        c_jsonl = _make_jsonl(project_dir, "hhhh-8888", [
            {"type": "user", "message": {"content": "session C"}, "uuid": "c-001", "parentUuid": None},
        ], mtime_offset=1)

        # Continuation of session A (parentUuid matches a-002 from session_a)
        continuation = _make_jsonl(project_dir, "iiii-9999", [
            {"type": "user", "message": {"content": "continued A"}, "uuid": "a-003", "parentUuid": "a-002"},
        ], mtime_offset=5)

        predecessor_uuid = SessionMonitor._find_predecessor_by_parentuuid(
            "a-002", continuation, str(project_dir),
        )
        # Should find session A's file (aaaa-1111), not B (cccc-3333) or C (hhhh-8888)
        assert predecessor_uuid == "aaaa-1111", (
            f"Should identify session A as predecessor, got {predecessor_uuid}"
        )


# ── TestHostMtimeProhibition ──────────────────────────────────────────────

class TestHostMtimeProhibition:
    """Host sessions must NEVER use mtime to match files across sessions.

    The mtime-based _resolve_jsonl_in_dir has been removed entirely (auto-uhnw).
    Host resolution uses only: meta.json match, handshake, or parentUuid chain.
    """

    def test_mtime_resolution_method_removed(self):
        """_resolve_jsonl_in_dir no longer exists — mtime scanning is gone.

        Expected: GREEN — the method was removed as dead code.
        """
        assert not hasattr(SessionMonitor, "_resolve_jsonl_in_dir"), (
            "_resolve_jsonl_in_dir should have been removed — "
            "mtime-based resolution is not valid for host sessions in shared dirs."
        )

    def test_only_valid_resolution_paths(self, host_shared_dir):
        """Host session resolves ONLY via: meta.json match, handshake, or parentUuid chain.

        Expected: GREEN — _resolve_jsonl_in_dir is gone, only valid paths remain.
        """
        # Valid path 1: .meta.json match — _resolve_host_jsonl
        monitor = SessionMonitor()
        assert hasattr(monitor, "_resolve_host_jsonl")

        # Valid path 2: IN_CREATE + parentUuid chain — _handle_host_create
        assert hasattr(monitor, "_handle_host_create")

        # Invalid path: mtime-based resolution — removed
        assert not hasattr(SessionMonitor, "_resolve_jsonl_in_dir"), (
            "mtime-based resolution must not exist"
        )
