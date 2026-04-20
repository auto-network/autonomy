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
from datetime import datetime, timezone
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

    def test_includes_ended_at(self, isolated_dao):
        """ended_at must flow through for the recent card 'Ended' field (auto-wa3d)."""
        results = isolated_dao.get_recent_sessions(limit=10)
        for row in results:
            assert "ended_at" in row, f"missing ended_at on {row['id']}"
            assert row["ended_at"], f"empty ended_at on {row['id']}"

    def test_includes_resolved_org(self, isolated_dao):
        """org identity (slug, name, color, initial) flows through (auto-jl9dc).

        Resolution must run on the bare project slug, not the bracketed
        legacy display value — `[autonomy]` would otherwise be hashed as
        a fresh unknown slug, drifting the colour for every consumer.
        """
        results = isolated_dao.get_recent_sessions(limit=10)
        for row in results:
            assert "org" in row, f"missing org on {row['id']}"
            org = row["org"]
            assert isinstance(org, dict)
            assert org["slug"] == "autonomy", \
                f"expected 'autonomy' slug, got {org['slug']!r} (project={row['project']!r})"
            assert org["color"].startswith("#")
            assert isinstance(org["initial"], str) and len(org["initial"]) == 1

    def test_ended_at_uses_metadata_when_present(self, isolated_dao):
        """When metadata.ended_at is present, the row's ended_at reflects it.

        src-fresh-stale has no dashboard.db overlay, so meta.ended_at flows
        through unchanged.
        """
        results = isolated_dao.get_recent_sessions(limit=10, since="all")
        fresh = next(r for r in results if r["id"] == "src-fresh-stale")
        assert fresh["ended_at"] == "2026-04-17T23:00:00Z"

    def test_ended_at_tracks_dashboard_overlay(self, isolated_dao):
        """Dashboard.db's last_activity wins for merged rows — ended_at follows.

        src-with-label has graph.db ended_at older than dashboard.db's
        last_activity (10 min ago); the merged row's ended_at must reflect
        the newer dashboard timestamp, not the stale graph metadata.
        """
        results = isolated_dao.get_recent_sessions(limit=10)
        with_label = next(r for r in results if r["id"] == "src-with-label")
        # Ended_at should equal last_activity_at after the dashboard overlay.
        assert with_label["ended_at"] == with_label["last_activity_at"], \
            f"ended_at ({with_label['ended_at']}) drifted from " \
            f"last_activity_at ({with_label['last_activity_at']})"


# ══════════════════════════════════════════════════════════════════════
# TestSortMode — `sort` query param selects the ordering column
# ══════════════════════════════════════════════════════════════════════


class TestSortMode:
    """The DAO accepts sort=lastActivity|created|turns|ctx (auto-d0mt)."""

    def test_sort_turns_puts_high_turn_row_first(self, isolated_dao):
        """Sort=turns orders by entry_count/total_turns DESC."""
        results = isolated_dao.get_recent_sessions(limit=10, sort="turns", since="all")
        # src-with-label has 240 entries (dashboard.db overlay),
        # src-fresh-stale has 5 turns from graph.db metadata.
        ids = [r["id"] for r in results]
        assert ids[0] == "src-with-label", f"Expected highest-turn row first; got {ids}"

    def test_sort_created_orders_by_created_at(self, isolated_dao):
        """Sort=created orders by graph.db created_at DESC."""
        results = isolated_dao.get_recent_sessions(limit=10, sort="created", since="all")
        ids = [r["id"] for r in results]
        # src-fresh-stale has the most recent created_at (2026-04-17)
        assert ids[0] == "src-fresh-stale", f"Expected newest created_at first; got {ids}"

    def test_unknown_sort_falls_back_to_last_activity(self, isolated_dao):
        """An unknown sort value must not raise — it falls back to default."""
        results = isolated_dao.get_recent_sessions(limit=10, sort="garbage", since="all")
        # Matches the default behaviour (lastActivity DESC)
        default = isolated_dao.get_recent_sessions(limit=10, since="all")
        assert [r["id"] for r in results] == [r["id"] for r in default]


# ══════════════════════════════════════════════════════════════════════
# TestSortDuration — sort=duration orders by (end - start) desc
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def duration_dao(tmp_path, monkeypatch):
    """graph.db seeded with sessions that have distinct durations."""
    graph_db_path = tmp_path / "graph.db"
    dashboard_db_path = tmp_path / "dashboard.db"

    from tools.graph.db import GraphDB
    g = GraphDB(graph_db_path)

    # Row A: 30 min duration (created 2h ago, active 90 min ago)
    g.conn.execute("""INSERT INTO sources
        (id, type, platform, project, title, file_path, metadata, created_at,
         ingested_at, last_activity_at)
        VALUES (?, 'session', 'claude-code', 'autonomy', ?, ?, ?, ?, ?, ?)""",
        ("src-short", "Short session",
         "/home/jeremy/sessions/short.jsonl",
         json.dumps({"session_uuid": "uuid-short", "session_type": "interactive"}),
         "2026-04-18T22:00:00Z", "2026-04-18T22:30:00Z",
         "2026-04-18T22:30:00Z"),
    )
    # Row B: 2 hour duration
    g.conn.execute("""INSERT INTO sources
        (id, type, platform, project, title, file_path, metadata, created_at,
         ingested_at, last_activity_at)
        VALUES (?, 'session', 'claude-code', 'autonomy', ?, ?, ?, ?, ?, ?)""",
        ("src-long", "Long session",
         "/home/jeremy/sessions/long.jsonl",
         json.dumps({"session_uuid": "uuid-long", "session_type": "interactive"}),
         "2026-04-18T20:00:00Z", "2026-04-18T22:00:00Z",
         "2026-04-18T22:00:00Z"),
    )
    # Row C: 5 min duration (most recent)
    g.conn.execute("""INSERT INTO sources
        (id, type, platform, project, title, file_path, metadata, created_at,
         ingested_at, last_activity_at)
        VALUES (?, 'session', 'claude-code', 'autonomy', ?, ?, ?, ?, ?, ?)""",
        ("src-tiny", "Tiny session",
         "/home/jeremy/sessions/tiny.jsonl",
         json.dumps({"session_uuid": "uuid-tiny", "session_type": "interactive"}),
         "2026-04-18T22:55:00Z", "2026-04-18T23:00:00Z",
         "2026-04-18T23:00:00Z"),
    )
    g.commit()
    g.close()

    monkeypatch.setenv("DASHBOARD_DB", str(dashboard_db_path))
    from tools.dashboard.dao import dashboard_db as ddb
    importlib.reload(ddb)
    ddb.init_db(dashboard_db_path)

    from tools.dashboard.dao import sessions as sessions_dao
    monkeypatch.setattr(sessions_dao, "_GRAPH_DB", graph_db_path)
    yield sessions_dao


class TestSortDuration:
    """sort=duration orders by (last_activity_at - created_at) desc (auto-ycry3)."""

    def test_duration_is_a_valid_sort_key(self):
        """sort=duration must survive the _VALID_RECENT_SORTS gate."""
        from tools.dashboard.dao import sessions as sessions_dao
        assert "duration" in sessions_dao._VALID_RECENT_SORTS

    def test_sort_duration_orders_by_end_minus_start_desc(self, duration_dao):
        """Longest-running sessions appear first."""
        results = duration_dao.get_recent_sessions(
            sort="duration", since="all", limit=10,
        )
        ids = [r["id"] for r in results]
        # src-long has 2h, src-short has 30m, src-tiny has 5m
        assert ids.index("src-long") < ids.index("src-short") < ids.index("src-tiny"), (
            f"Expected long > short > tiny by duration; got {ids}"
        )


# ══════════════════════════════════════════════════════════════════════
# TestLibrarianTitleFields — librarian rows expose type + target fields
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def librarian_dao(tmp_path, monkeypatch):
    """graph.db with a librarian session + dispatch.db with matching job row."""
    graph_db_path = tmp_path / "graph.db"
    dashboard_db_path = tmp_path / "dashboard.db"
    dispatch_db_path = tmp_path / "dispatch.db"

    from tools.graph.db import GraphDB
    g = GraphDB(graph_db_path)
    # Librarian session — metadata carries job_id + job_type so the DAO can
    # join to librarian_jobs.payload and extract the target bead_id.
    g.conn.execute("""INSERT INTO sources
        (id, type, platform, project, title, file_path, metadata, created_at,
         ingested_at, last_activity_at)
        VALUES (?, 'session', 'claude-code', 'autonomy', ?, ?, ?, ?, ?, ?)""",
        ("src-lib-1", "librarian-review_report-781221-65a80c94",
         "/home/jeremy/sessions/lib.jsonl",
         json.dumps({
             "session_uuid": "uuid-lib",
             "session_type": "librarian",
             "job_id": "job-abcd-1234",
             "job_type": "review_report",
         }),
         "2026-04-18T22:00:00Z", "2026-04-18T22:01:00Z",
         "2026-04-18T22:30:00Z"),
    )
    # Interactive session as a control to ensure non-librarian rows are
    # not annotated with librarian_type.
    g.conn.execute("""INSERT INTO sources
        (id, type, platform, project, title, file_path, metadata, created_at,
         ingested_at, last_activity_at)
        VALUES (?, 'session', 'claude-code', 'autonomy', ?, ?, ?, ?, ?, ?)""",
        ("src-inter-1", "Some interactive",
         "/home/jeremy/sessions/inter.jsonl",
         json.dumps({"session_uuid": "uuid-inter", "session_type": "interactive"}),
         "2026-04-18T22:00:00Z", "2026-04-18T22:01:00Z",
         "2026-04-18T22:30:00Z"),
    )
    g.commit()
    g.close()

    # Build dispatch.db with librarian_jobs table matching the seeded job_id.
    conn = sqlite3.connect(str(dispatch_db_path))
    conn.execute("""
        CREATE TABLE librarian_jobs (
            id TEXT PRIMARY KEY,
            job_type TEXT NOT NULL,
            payload TEXT,
            status TEXT DEFAULT 'pending',
            priority INTEGER DEFAULT 1,
            created_at DATETIME,
            started_at DATETIME,
            completed_at DATETIME,
            librarian_type TEXT,
            session_id TEXT,
            attempts INTEGER DEFAULT 0,
            max_attempts INTEGER DEFAULT 3
        )
    """)
    conn.execute(
        "INSERT INTO librarian_jobs (id, job_type, payload, status) VALUES (?, ?, ?, ?)",
        ("job-abcd-1234", "review_report",
         json.dumps({"bead_id": "auto-target-1", "run_id": "auto-target-1-2026"}),
         "done"),
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("DASHBOARD_DB", str(dashboard_db_path))
    from tools.dashboard.dao import dashboard_db as ddb
    importlib.reload(ddb)
    ddb.init_db(dashboard_db_path)

    from tools.dashboard.dao import sessions as sessions_dao
    monkeypatch.setattr(sessions_dao, "_GRAPH_DB", graph_db_path)
    monkeypatch.setattr(sessions_dao, "_DISPATCH_DB", dispatch_db_path)
    yield sessions_dao


class TestLibrarianTitleFields:
    """Librarian rows surface type + target for UI-side formatting (auto-ycry3)."""

    def test_librarian_type_populated_from_metadata(self, librarian_dao):
        results = librarian_dao.get_recent_sessions(since="all")
        lib = next(r for r in results if r["id"] == "src-lib-1")
        assert lib["librarian_type"] == "review_report"

    def test_librarian_target_bead_id_from_payload(self, librarian_dao):
        """librarian_target_bead_id is sourced from librarian_jobs.payload."""
        results = librarian_dao.get_recent_sessions(since="all")
        lib = next(r for r in results if r["id"] == "src-lib-1")
        assert lib["librarian_target_bead_id"] == "auto-target-1"

    def test_title_field_is_not_the_raw_process_name_via_ui(self, librarian_dao):
        """The DAO leaves the raw title in place (Alpine layers librarian_type ·
        target on top); the UI-facing value composed from the new fields
        must NOT start with 'librarian-' so the card title can't degrade
        back to the PID mess."""
        results = librarian_dao.get_recent_sessions(since="all")
        lib = next(r for r in results if r["id"] == "src-lib-1")
        # Formula the UI uses (mirrors _librarianTitle in sessions.js)
        ui_title = (
            f"{lib['librarian_type']} · {lib['librarian_target_bead_id']}"
            if lib["librarian_type"] and lib["librarian_target_bead_id"]
            else lib.get("librarian_type") or ""
        )
        assert ui_title, "UI title must be non-empty for librarian rows"
        assert not ui_title.startswith("librarian-"), (
            f"UI-facing title still resembles the raw process name: {ui_title!r}"
        )

    def test_non_librarian_rows_have_null_librarian_fields(self, librarian_dao):
        """Only librarian rows should carry librarian_* annotations."""
        results = librarian_dao.get_recent_sessions(since="all")
        inter = next(r for r in results if r["id"] == "src-inter-1")
        assert inter.get("librarian_type") in (None, "")
        assert inter.get("librarian_target_bead_id") in (None, "")

    def test_unresolvable_target_leaves_bead_id_none(self, tmp_path, monkeypatch):
        """When librarian_jobs has no matching row, target stays None but
        librarian_type is still populated from the metadata."""
        graph_db_path = tmp_path / "graph.db"
        dashboard_db_path = tmp_path / "dashboard.db"
        dispatch_db_path = tmp_path / "dispatch.db"

        from tools.graph.db import GraphDB
        g = GraphDB(graph_db_path)
        g.conn.execute("""INSERT INTO sources
            (id, type, platform, project, title, file_path, metadata, created_at,
             ingested_at, last_activity_at)
            VALUES (?, 'session', 'claude-code', 'autonomy', ?, ?, ?, ?, ?, ?)""",
            ("src-orphan", "librarian-review_report-1-deadbeef",
             "/home/jeremy/sessions/orphan.jsonl",
             json.dumps({
                 "session_uuid": "uuid-orphan",
                 "session_type": "librarian",
                 "job_id": "does-not-exist",
                 "job_type": "review_report",
             }),
             "2026-04-18T22:00:00Z", "2026-04-18T22:01:00Z",
             "2026-04-18T22:30:00Z"),
        )
        g.commit()
        g.close()

        # dispatch.db exists but lacks the referenced job_id
        conn = sqlite3.connect(str(dispatch_db_path))
        conn.execute("""
            CREATE TABLE librarian_jobs (
                id TEXT PRIMARY KEY, job_type TEXT, payload TEXT,
                status TEXT, priority INTEGER, created_at DATETIME,
                started_at DATETIME, completed_at DATETIME,
                librarian_type TEXT, session_id TEXT,
                attempts INTEGER, max_attempts INTEGER
            )
        """)
        conn.commit()
        conn.close()

        monkeypatch.setenv("DASHBOARD_DB", str(dashboard_db_path))
        from tools.dashboard.dao import dashboard_db as ddb
        importlib.reload(ddb)
        ddb.init_db(dashboard_db_path)
        from tools.dashboard.dao import sessions as sessions_dao
        monkeypatch.setattr(sessions_dao, "_GRAPH_DB", graph_db_path)
        monkeypatch.setattr(sessions_dao, "_DISPATCH_DB", dispatch_db_path)

        results = sessions_dao.get_recent_sessions(since="all")
        orphan = next(r for r in results if r["id"] == "src-orphan")
        assert orphan["librarian_type"] == "review_report"
        assert orphan["librarian_target_bead_id"] in (None, "")


# ══════════════════════════════════════════════════════════════════════
# TestSinceWindow — `since` query param filters rows by activity window
# ══════════════════════════════════════════════════════════════════════


class TestSinceWindow:
    """The DAO accepts since=6h|1d|1w|all (auto-d0mt, auto-0r86 parity)."""

    def test_since_all_returns_everything(self, isolated_dao):
        results = isolated_dao.get_recent_sessions(limit=10, since="all")
        # Both rows survive (live session already filtered elsewhere)
        ids = {r["id"] for r in results}
        assert "src-fresh-stale" in ids
        assert "src-with-label" in ids

    def test_since_1d_includes_recent_rows(self, isolated_dao):
        """src-with-label's activity is 10 minutes ago — within 1d."""
        results = isolated_dao.get_recent_sessions(limit=10, since="1d")
        ids = {r["id"] for r in results}
        assert "src-with-label" in ids

    def test_unknown_since_does_not_filter(self, isolated_dao):
        """An unparseable since value disables the filter (no crash)."""
        results = isolated_dao.get_recent_sessions(limit=10, since="zzz")
        default = isolated_dao.get_recent_sessions(limit=10, since="all")
        assert len(results) == len(default)


# ══════════════════════════════════════════════════════════════════════
# TestTypeQuotas — per-type bucket quotas keep interactive alive when
# dispatch / librarian volume spikes (auto-wyo79).
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def quota_dao(tmp_path, monkeypatch):
    """graph.db seeded with many dispatch + librarian rows + a few interactive."""
    graph_db_path = tmp_path / "graph.db"
    dashboard_db_path = tmp_path / "dashboard.db"

    from tools.graph.db import GraphDB
    g = GraphDB(graph_db_path)

    now = time.time()

    def _iso(delta_sec: float) -> str:
        return datetime.fromtimestamp(now - delta_sec, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    # Seed 50 dispatch rows (all within 1d), 3 interactive rows, 5 librarian rows
    for i in range(50):
        g.conn.execute(
            """INSERT INTO sources
            (id, type, platform, project, title, file_path, metadata, created_at,
             ingested_at, last_activity_at)
            VALUES (?, 'session', 'claude-code', 'autonomy', ?, ?, ?, ?, ?, ?)""",
            (
                f"src-dispatch-{i:03d}",
                f"Dispatch run {i}",
                f"/home/jeremy/sessions/dispatch-{i}.jsonl",
                json.dumps({
                    "session_uuid": f"uuid-dispatch-{i}",
                    "session_type": "dispatch",
                    "bead_id": f"auto-d{i:03d}",
                    "total_turns": 10 + i,
                }),
                _iso(7200 + i * 60),
                _iso(7200 + i * 60),
                # Stagger within 1d: 1 minute apart, so all within 1 day
                _iso(60 + i * 30),
            ),
        )
    for i in range(5):
        g.conn.execute(
            """INSERT INTO sources
            (id, type, platform, project, title, file_path, metadata, created_at,
             ingested_at, last_activity_at)
            VALUES (?, 'session', 'claude-code', 'autonomy', ?, ?, ?, ?, ?, ?)""",
            (
                f"src-librarian-{i:03d}",
                f"Librarian task {i}",
                f"/home/jeremy/sessions/librarian-{i}.jsonl",
                json.dumps({
                    "session_uuid": f"uuid-librarian-{i}",
                    "session_type": "librarian",
                    "total_turns": 5,
                }),
                _iso(3600 + i * 60),
                _iso(3600 + i * 60),
                _iso(300 + i * 30),
            ),
        )
    for i in range(3):
        g.conn.execute(
            """INSERT INTO sources
            (id, type, platform, project, title, file_path, metadata, created_at,
             ingested_at, last_activity_at)
            VALUES (?, 'session', 'claude-code', 'autonomy', ?, ?, ?, ?, ?, ?)""",
            (
                f"src-interactive-{i:03d}",
                f"Interactive session {i}",
                f"/home/jeremy/sessions/interactive-{i}.jsonl",
                json.dumps({
                    "session_uuid": f"uuid-interactive-{i}",
                    "session_type": "interactive",
                    "total_turns": 20 + i,
                }),
                _iso(1800 + i * 60),
                _iso(1800 + i * 60),
                _iso(120 + i * 30),
            ),
        )
    g.commit()
    g.close()

    # Stub out dashboard.db
    monkeypatch.setenv("DASHBOARD_DB", str(dashboard_db_path))
    from tools.dashboard.dao import dashboard_db as ddb
    importlib.reload(ddb)
    ddb.init_db(dashboard_db_path)

    from tools.dashboard.dao import sessions as sessions_dao
    monkeypatch.setattr(sessions_dao, "_GRAPH_DB", graph_db_path)
    yield sessions_dao


class TestTypeQuotas:
    """Per-type quotas preserve interactive sessions under dispatch pressure."""

    def test_quotas_preserve_interactive_under_dispatch_pressure(self, quota_dao):
        """50 dispatch + 5 librarian + 3 interactive — defaults cap each group.

        Default quota: 20 interactive + 10 dispatch + 10 librarian.
        Expected: all 3 interactive + 10 dispatch + 5 librarian = 18 rows.
        """
        from datetime import datetime, timezone
        results = quota_dao.get_recent_sessions(since="1d", type_group="all")
        groups = {"interactive": [], "dispatch": [], "librarian": []}
        for r in results:
            groups[r["session_type"]].append(r)
        assert len(groups["interactive"]) == 3, \
            f"expected all 3 interactive rows; got {len(groups['interactive'])}"
        assert len(groups["dispatch"]) == 10, \
            f"expected 10 dispatch rows (quota cap); got {len(groups['dispatch'])}"
        assert len(groups["librarian"]) == 5, \
            f"expected 5 librarian rows (all of them); got {len(groups['librarian'])}"
        assert len(results) == 18

    def test_type_chip_routes_entire_budget(self, quota_dao):
        """type_group='interactive' returns only interactive rows, up to 50."""
        results = quota_dao.get_recent_sessions(since="1d", type_group="interactive")
        assert all(r["session_type"] == "interactive" for r in results), \
            f"non-interactive leaked: {[r['session_type'] for r in results]}"
        assert len(results) == 3

    def test_type_chip_dispatch_routes_budget(self, quota_dao):
        """type_group='dispatch' funnels all 50 budget into dispatch (50 rows available)."""
        results = quota_dao.get_recent_sessions(since="1d", type_group="dispatch")
        assert all(r["session_type"] == "dispatch" for r in results)
        assert len(results) == 50

    def test_sort_applies_across_union(self, quota_dao):
        """sort=turns sorts the merged union, not per-group."""
        results = quota_dao.get_recent_sessions(
            since="1d", type_group="all", sort="turns"
        )
        turn_counts = [(r.get("entry_count") or r.get("total_turns") or 0) for r in results]
        assert turn_counts == sorted(turn_counts, reverse=True), \
            f"turns not monotonically decreasing: {turn_counts}"

    def test_quotas_respect_since_window(self, quota_dao):
        """Rows outside the since window don't count toward quotas."""
        # The fixture staggers dispatch rows at 60 + i*30 seconds (up to 1590s).
        # A tight since=30m window should keep only the first few dispatches.
        results = quota_dao.get_recent_sessions(since="30m", type_group="dispatch")
        # Dispatch rows within the 30m window: all with last_activity_at <= 1800s
        # Those are at delta 60, 90, ..., 1590 → 50 rows all fit in 30m.
        # Retry with a tighter window:
        results = quota_dao.get_recent_sessions(since="5m", type_group="dispatch")
        # Within 5m (300s), deltas 60, 90, 120, 150, 180, 210, 240, 270 → 8 rows
        assert len(results) == 8, \
            f"since=5m should admit only dispatch rows within 5m, got {len(results)}"

    def test_unknown_type_group_falls_back_to_all(self, quota_dao):
        """Invalid type_group values route to the 'all' quota table."""
        default = quota_dao.get_recent_sessions(since="1d", type_group="all")
        bogus = quota_dao.get_recent_sessions(since="1d", type_group="garbage")
        assert [r["id"] for r in default] == [r["id"] for r in bogus]

    def test_empty_group_consumes_no_quota(self, quota_dao):
        """If a group has zero rows in the window, its quota is simply unused.

        type_group='librarian' with a tight since window that excludes all
        librarian rows returns an empty list (not padded from other groups).
        """
        results = quota_dao.get_recent_sessions(
            since="30s", type_group="librarian"
        )
        assert results == [], \
            f"expected no padding from other groups; got {[r['session_type'] for r in results]}"
