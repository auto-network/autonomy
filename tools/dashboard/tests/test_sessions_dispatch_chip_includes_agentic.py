"""Tests for the /sessions Dispatch chip including agentic sources
(bead auto-5k2j4).

The /sessions Dispatch chip is the dashboard's after-the-fact view of
short-lived agentic runs (alongside librarian and bead-driven dispatches).
The DAO that backs the chip groups sources by ``session_type``; agentic
rows must:

  * be selected by the underlying SQL (sources.type filter widened from
    ``'session'`` to ``IN ('session', 'agentic')``),
  * derive a ``session_type`` of ``'agentic'`` so the bucket lookup
    routes them to the dispatch group,
  * pass through the dispatch chip / type_group filter unchanged.

The "All" chip behaviour must not change.
"""

from __future__ import annotations

import importlib
import json
import time
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _evict_pooled_orgs():
    """Drop the per-org connection pool between tests."""
    from tools.graph.db import GraphDB
    GraphDB.close_all_pooled()
    yield
    GraphDB.close_all_pooled()


@pytest.fixture
def isolated_sessions_dao(tmp_path, monkeypatch):
    """Build a graph.db with a session row and an agentic row, plus an
    empty dashboard.db, and return the (reloaded) sessions DAO."""
    orgs_dir = tmp_path / "orgs"
    orgs_dir.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    graph_db_path = orgs_dir / "autonomy.db"
    dashboard_db_path = tmp_path / "dashboard.db"

    from tools.graph.db import GraphDB

    g = GraphDB(graph_db_path)

    # A vanilla bead-run session — should appear under both All and Dispatch chips.
    g.conn.execute(
        """INSERT INTO sources
           (id, type, platform, title, file_path, metadata, created_at,
            ingested_at, last_activity_at)
           VALUES (?, 'session', 'claude-code', ?, ?, ?, ?, ?, ?)""",
        (
            "src-bead-run",
            "auto-foo run",
            "/tmp/sessions/auto-foo.jsonl",
            json.dumps({
                "session_uuid": "uuid-bead-run",
                "bead_id": "auto-foo",
                "ended_at": "2026-04-27T10:00:00Z",
                "total_turns": 5,
            }),
            "2026-04-27T09:55:00Z",
            "2026-04-27T09:56:00Z",
            "2026-04-27T10:00:00Z",
        ),
    )

    # An agentic source — Round 5's dashboard agent-action run.
    g.conn.execute(
        """INSERT INTO sources
           (id, type, platform, title, file_path, metadata, created_at,
            ingested_at, last_activity_at)
           VALUES (?, 'agentic', 'local', ?, ?, ?, ?, ?, ?)""",
        (
            "src-agentic-run",
            "Update summary on src-target",
            "agentic:agentic-update-summary-7f3a",
            json.dumps({
                "kind": "agent-action",
                "set_id": "dashboard.agent-actions",
                "member_key": "note.update-summary",
                "model": "claude-haiku-4-5-20251001",
                "target_source_id": "src-target",
                "session_type": "agentic",
                "ended_at": "2026-04-27T11:00:00Z",
                "total_turns": 2,
            }),
            "2026-04-27T10:58:00Z",
            "2026-04-27T10:59:00Z",
            "2026-04-27T11:00:00Z",
        ),
    )

    # An interactive session — must remain in the interactive bucket.
    g.conn.execute(
        """INSERT INTO sources
           (id, type, platform, title, file_path, metadata, created_at,
            ingested_at, last_activity_at)
           VALUES (?, 'session', 'claude-code', ?, ?, ?, ?, ?, ?)""",
        (
            "src-interactive",
            "user terminal",
            "/tmp/sessions/interactive.jsonl",
            json.dumps({
                "session_uuid": "uuid-interactive",
                "ended_at": "2026-04-27T08:00:00Z",
                "total_turns": 12,
            }),
            "2026-04-27T07:00:00Z",
            "2026-04-27T07:01:00Z",
            "2026-04-27T08:00:00Z",
        ),
    )
    g.commit()
    g.close()

    monkeypatch.setenv("DASHBOARD_DB", str(dashboard_db_path))
    from tools.dashboard.dao import dashboard_db as ddb
    importlib.reload(ddb)
    ddb.init_db(dashboard_db_path)

    from tools.dashboard.dao import sessions as sessions_dao

    return sessions_dao


def test_sessions_dispatch_chip_returns_agentic_sources(isolated_sessions_dao):
    """The Dispatch chip query (type_group='dispatch') surfaces agentic rows."""
    rows = isolated_sessions_dao.get_recent_sessions(
        limit=50, sort="lastActivity", since="all", type_group="dispatch",
    )
    ids = [r["id"] for r in rows]
    types = {r["id"]: r.get("session_type") for r in rows}

    # The agentic source row must appear and be classified as 'agentic'.
    assert "src-agentic-run" in ids, (
        f"agentic row missing from /sessions Dispatch chip; got ids={ids}"
    )
    assert types["src-agentic-run"] == "agentic"

    # The bead-run session is also dispatch-bucketed.
    assert "src-bead-run" in ids

    # Interactive must NOT appear under the Dispatch chip.
    assert "src-interactive" not in ids


def test_sessions_all_chip_unchanged(isolated_sessions_dao):
    """The All chip behaviour is unchanged: agentic + bead + interactive
    all appear under their respective buckets."""
    rows = isolated_sessions_dao.get_recent_sessions(
        limit=50, sort="lastActivity", since="all", type_group="all",
    )
    ids = [r["id"] for r in rows]

    # All three rows visible under "All".
    assert "src-bead-run" in ids
    assert "src-agentic-run" in ids
    assert "src-interactive" in ids

    # Confirm the agentic row's session_type is 'agentic' (not 'session'
    # or silently 'interactive').
    by_id = {r["id"]: r for r in rows}
    assert by_id["src-agentic-run"].get("session_type") == "agentic"
    assert by_id["src-bead-run"].get("session_type") == "dispatch"
    assert by_id["src-interactive"].get("session_type") == "interactive"


def test_agentic_run_pair_collapses_to_identity_row_with_stats(
    isolated_sessions_dao, tmp_path,
):
    """An agent-action run's TWO source rows (agentic identity + ingested
    run-JSONL 'session' row) serve as ONE row: the identity wins the list
    (badge, title, no resume), grafted with the JSONL sibling's stats and
    freshest activity (host dump 148ead24 t346, defect 2: 4 runs served
    as 10 rows)."""
    import sqlite3, os
    graph_db_path = (
        tmp_path / "orgs" / "autonomy.db"
    )
    conn = sqlite3.connect(graph_db_path)
    jsonl = tmp_path / "run.jsonl"
    jsonl.write_text("{}\n")
    conn.execute(
        """INSERT INTO sources
           (id, type, platform, title, file_path, metadata, created_at,
            ingested_at, last_activity_at)
           VALUES (?, 'session', 'claude-code', ?, ?, ?, ?, ?, ?)""",
        (
            "src-agentic-jsonl",
            "agentic-update-summary-7f3a",
            "/data/agent-runs/agentic-update-summary-7f3a-20260429-110000"
            "/sessions/x/uuid.jsonl",
            json.dumps({
                "session_uuid": "uuid-agentic-run",
                "ended_at": "2026-04-27T11:05:00Z",
                "total_turns": 9,
                "total_input_tokens": 1000,
                "total_output_tokens": 500,
            }),
            "2026-04-27T10:58:30Z",
            "2026-04-27T10:59:30Z",
            "2026-04-27T11:05:00Z",
        ),
    )
    conn.commit()
    conn.close()

    rows = isolated_sessions_dao.get_recent_sessions(since="all")
    slugged = [r for r in rows
               if "agentic-update-summary-7f3a" in (r.get("file_path") or "")
               or (r.get("file_path") or "").endswith("uuid.jsonl")]
    assert len(slugged) == 1, [
        (r["id"], r["type"], r["file_path"]) for r in slugged]
    row = slugged[0]
    assert row["type"] == "agentic"          # identity row won
    assert row["session_type"] == "agentic"
    assert row["total_turns"] == 9           # stats grafted from the sibling
    assert row["total_tokens"] == 1500
    assert row["last_activity_at"] == "2026-04-27T11:05:00Z"
    assert row["resumable"] is False


def test_resumable_requires_interactive_group(isolated_sessions_dao, tmp_path):
    """A dispatch/agentic row never offers resume even when its JSONL
    exists on disk (host dump defect 1)."""
    import sqlite3
    jsonl = tmp_path / "bead-run.jsonl"
    jsonl.write_text("{}\n")
    conn = sqlite3.connect(tmp_path / "orgs" / "autonomy.db")
    conn.execute(
        "UPDATE sources SET file_path = ? WHERE id = 'src-bead-run'",
        (str(jsonl),),
    )
    inter = tmp_path / "interactive.jsonl"
    inter.write_text("{}\n")
    conn.execute(
        "UPDATE sources SET file_path = ? WHERE id = 'src-interactive'",
        (str(inter),),
    )
    conn.commit()
    conn.close()

    rows = {r["id"]: r for r in
            isolated_sessions_dao.get_recent_sessions(since="all")}
    assert rows["src-bead-run"]["session_type"] == "dispatch"
    assert rows["src-bead-run"]["resumable"] is False
    assert rows["src-interactive"]["session_type"] == "interactive"
    assert rows["src-interactive"]["resumable"] is True
