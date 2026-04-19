"""DAO-level tests for get_recent_sessions sorting + dashboard.db overlay.

Covers auto-elz1: Recent Sessions fidelity, the DAO half.

  * Sessions sort by last_activity_at, not created_at — a 2-day-old session
    that was active 30 minutes ago beats a fresh session that died yesterday.

  * dashboard.db rows enrich graph.db rows when they share a session_uuid:
    label overrides the graph.db title; entry_count, role, bead_id flow
    through to the API payload.

  * Live sessions are excluded — they belong on the Active list.

  * The payload includes session_uuid (for click-through) and last_activity_at
    (for ordering and date display).
"""

from __future__ import annotations

import importlib
import json
import sqlite3
import time
from pathlib import Path

import pytest


@pytest.fixture
def isolated_dao(tmp_path, monkeypatch):
    """Build a graph.db + dashboard.db pair and reload the sessions DAO against them."""
    graph_db_path = tmp_path / "graph.db"
    dashboard_db_path = tmp_path / "dashboard.db"

    # ── Build graph.db with the new schema ──
    from tools.graph.db import GraphDB
    g = GraphDB(graph_db_path)
    # Insert three sessions with varying last_activity_at timestamps
    g.conn.execute("""INSERT INTO sources
        (id, type, platform, project, title, file_path, metadata, created_at,
         ingested_at, last_activity_at)
        VALUES (?, 'session', 'claude-code', 'autonomy', ?, ?, ?, ?, ?, ?)""",
        ("src-old-active", "[Image #1]",
         "/home/jeremy/sessions/old-active.jsonl",
         json.dumps({"session_uuid": "uuid-old-active",
                     "total_input_tokens": 100, "total_output_tokens": 200,
                     "total_turns": 627, "ended_at": "2026-04-18T22:00:00Z"}),
         "2026-04-15T00:00:00Z",  # created 4 days ago
         "2026-04-15T00:01:00Z",
         "2026-04-18T22:00:00Z"),  # but active 30 min ago
    )
    g.conn.execute("""INSERT INTO sources
        (id, type, platform, project, title, file_path, metadata, created_at,
         ingested_at, last_activity_at)
        VALUES (?, 'session', 'claude-code', 'autonomy', ?, ?, ?, ?, ?, ?)""",
        ("src-fresh-stale", "Some title",
         "/home/jeremy/sessions/fresh-stale.jsonl",
         json.dumps({"session_uuid": "uuid-fresh-stale",
                     "total_turns": 5, "ended_at": "2026-04-17T23:00:00Z"}),
         "2026-04-17T22:00:00Z",  # created an hour earlier than activity
         "2026-04-17T23:01:00Z",
         "2026-04-17T23:00:00Z"),
    )
    g.conn.execute("""INSERT INTO sources
        (id, type, platform, project, title, file_path, metadata, created_at,
         ingested_at, last_activity_at)
        VALUES (?, 'session', 'claude-code', 'autonomy', ?, ?, ?, ?, ?, ?)""",
        ("src-with-label", "Stale graph title — should be overridden",
         "/home/jeremy/sessions/with-label.jsonl",
         json.dumps({"session_uuid": "uuid-with-label",
                     "total_turns": 50, "bead_id": "auto-test"}),
         "2026-04-16T00:00:00Z",
         "2026-04-16T00:01:00Z",
         "2026-04-18T20:00:00Z"),
    )
    g.commit()
    g.close()

    # ── Build dashboard.db ──
    monkeypatch.setenv("DASHBOARD_DB", str(dashboard_db_path))
    from tools.dashboard.dao import dashboard_db as ddb
    importlib.reload(ddb)
    ddb.init_db(dashboard_db_path)

    # Insert a dead session with rich metadata for src-with-label
    conn = ddb.get_conn()
    conn.execute("""INSERT INTO tmux_sessions
        (tmux_name, type, project, jsonl_path, session_uuid, bead_id,
         created_at, is_live, last_activity, last_message, entry_count,
         context_tokens, label, role, activity_state)
        VALUES (?, 'container', 'autonomy', ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, 'dead')""",
        ("auto-0418-200000", "/home/jeremy/sessions/with-label.jsonl",
         "uuid-with-label", "auto-test",
         time.time() - 3600,  # created an hour ago
         time.time() - 600,   # last activity 10 min ago
         "Working on the thing", 240, 90000,
         "Session viewer redesign", "designer"),
    )
    # Insert a live session — should be filtered out of recent
    conn.execute("""INSERT INTO tmux_sessions
        (tmux_name, type, project, jsonl_path, session_uuid, bead_id,
         created_at, is_live, last_activity, last_message, entry_count,
         context_tokens, label, role, activity_state)
        VALUES (?, 'container', 'autonomy', ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, 'idle')""",
        ("auto-live-session", "/home/jeremy/sessions/old-active.jsonl",
         "uuid-old-active", None,
         time.time() - 1800, time.time() - 60,
         "still working", 100, 50000, "live label", "builder"),
    )
    conn.commit()

    # ── Reload sessions DAO with new graph.db path ──
    from tools.dashboard.dao import sessions as sessions_dao
    monkeypatch.setattr(sessions_dao, "_GRAPH_DB", graph_db_path)

    yield sessions_dao


# ══════════════════════════════════════════════════════════════════════
# TestActivityOrdering — sort by last activity, not creation
# ══════════════════════════════════════════════════════════════════════


class TestActivityOrdering:
    def test_sorts_by_last_activity_descending(self, isolated_dao):
        """A session active 30 min ago beats one created an hour ago but active yesterday."""
        results = isolated_dao.get_recent_sessions(limit=10)
        # Live session (uuid-old-active) is filtered, so we should see fresh-stale + with-label
        ids = [r["id"] for r in results]
        # with-label was active 10 min ago via dashboard.db
        # fresh-stale was active a day ago via graph.db
        assert ids[0] == "src-with-label", \
            f"Expected most-recent-active session first; got {ids}"


# ══════════════════════════════════════════════════════════════════════
# TestDashboardOverlay — dashboard.db enriches graph.db rows
# ══════════════════════════════════════════════════════════════════════


class TestDashboardOverlay:
    def test_label_overrides_graph_title(self, isolated_dao):
        """dashboard.db.label wins over graph.db.title (which may be stale)."""
        results = isolated_dao.get_recent_sessions(limit=10)
        with_label = next(r for r in results if r["id"] == "src-with-label")
        assert with_label["title"] == "Session viewer redesign"

    def test_entry_count_and_role_from_dashboard(self, isolated_dao):
        """Rich session-monitor fields flow through to the recent payload."""
        results = isolated_dao.get_recent_sessions(limit=10)
        with_label = next(r for r in results if r["id"] == "src-with-label")
        assert with_label["entry_count"] == 240
        assert with_label["context_tokens"] == 90000
        assert with_label["role"] == "designer"
        assert with_label["bead_id"] == "auto-test"


# ══════════════════════════════════════════════════════════════════════
# TestLiveExclusion — live sessions don't appear in Recent
# ══════════════════════════════════════════════════════════════════════


class TestLiveExclusion:
    def test_live_session_excluded(self, isolated_dao):
        """Sessions with is_live=1 in dashboard.db are filtered out of Recent."""
        results = isolated_dao.get_recent_sessions(limit=10)
        ids = [r["id"] for r in results]
        # src-old-active matches uuid-old-active (a live session) — must be excluded
        assert "src-old-active" not in ids, \
            "Live session leaked into Recent Sessions list"


# ══════════════════════════════════════════════════════════════════════
# TestPayloadShape — fields needed by the frontend
# ══════════════════════════════════════════════════════════════════════


class TestPayloadShape:
    def test_includes_session_uuid_for_click_through(self, isolated_dao):
        """session_uuid must be present so the frontend can navigate to the viewer."""
        results = isolated_dao.get_recent_sessions(limit=10)
        for row in results:
            assert "session_uuid" in row

    def test_includes_last_activity_at(self, isolated_dao):
        """last_activity_at must be present for sort + date display in the UI."""
        results = isolated_dao.get_recent_sessions(limit=10)
        for row in results:
            assert "last_activity_at" in row
            assert row["last_activity_at"], f"missing last_activity_at on {row['id']}"

    def test_includes_resumable_flag(self, isolated_dao):
        """resumable mirrors whether the JSONL still exists on disk."""
        results = isolated_dao.get_recent_sessions(limit=10)
        for row in results:
            assert "resumable" in row
            assert isinstance(row["resumable"], bool)
