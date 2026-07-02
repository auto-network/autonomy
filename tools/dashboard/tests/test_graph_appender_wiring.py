"""Tests for GraphAppender wiring into SessionMonitor (W3, auto-ea9g3).

GraphAppender's own extraction/write/dedup logic is covered in
tools/graph/tests/test_graph_appender.py. This file covers the
session_monitor.py integration: the single _graph_appender_tick() call
site self-heals (lazy build, rollover rebuild, gap catch-up on resume),
truncation triggers a reset, and the death path does an in-process final
catch-up instead of shelling out to `graph ingest-session`.
"""

from __future__ import annotations

import importlib
import json
import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _evict_pooled_orgs():
    from tools.graph.db import GraphDB
    GraphDB.close_all_pooled()
    yield
    GraphDB.close_all_pooled()


def _init_dashboard_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute("""CREATE TABLE IF NOT EXISTS tmux_sessions (
        tmux_name TEXT PRIMARY KEY, session_uuid TEXT, graph_source_id TEXT,
        harness TEXT NOT NULL DEFAULT 'claude', harness_state TEXT NOT NULL DEFAULT '{}',
        type TEXT NOT NULL, project TEXT NOT NULL, jsonl_path TEXT,
        bead_id TEXT, created_at REAL NOT NULL, is_live INTEGER DEFAULT 1,
        file_offset INTEGER DEFAULT 0, last_activity REAL,
        last_message TEXT DEFAULT '', entry_count INTEGER DEFAULT 0,
        context_tokens INTEGER DEFAULT 0, label TEXT DEFAULT '',
        topics TEXT DEFAULT '[]', role TEXT DEFAULT '',
        resolution_dir TEXT, session_uuids TEXT DEFAULT '[]',
        curr_jsonl_file TEXT, activity_state TEXT DEFAULT 'idle',
        todos TEXT DEFAULT '[]'
    )""")
    conn.commit()
    conn.close()


def _insert_row(
    db_path: Path, tmux_name: str, *, jsonl_path: str | None,
    project: str = "autonomy", is_live: int = 1, graph_source_id: str = "",
) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO tmux_sessions"
        " (tmux_name, type, project, jsonl_path, graph_source_id, created_at, is_live)"
        " VALUES (?, 'container', ?, ?, ?, ?, ?)",
        (tmux_name, project, jsonl_path, graph_source_id, time.time(), is_live),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def setup_env(tmp_path, monkeypatch):
    db_path = tmp_path / "dashboard.db"
    _init_dashboard_db(db_path)
    orgs_dir = tmp_path / "orgs"
    orgs_dir.mkdir()
    from tools.graph.db import GraphDB
    GraphDB(orgs_dir / "autonomy.db").close()

    monkeypatch.setenv("DASHBOARD_DB", str(db_path))
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.delenv("GRAPH_DB", raising=False)

    from tools.dashboard.dao import dashboard_db as ddb
    importlib.reload(ddb)
    from tools.dashboard import session_monitor as sm_mod
    importlib.reload(sm_mod)

    yield tmp_path, db_path, orgs_dir


def _fresh_monitor():
    from tools.dashboard import session_monitor as sm_mod
    return sm_mod, sm_mod.SessionMonitor()


def _entry_bytes(text: str, ts: str, role: str = "user", uuid: str = "u1") -> bytes:
    entry = {
        "type": role, "uuid": uuid,
        "message": {"role": role, "content": text},
        "timestamp": ts,
    }
    return (json.dumps(entry) + "\n").encode("utf-8")


def _thoughts_for(orgs_dir: Path, org: str, source_id: str) -> list[dict]:
    from tools.graph.db import GraphDB
    g = GraphDB(orgs_dir / f"{org}.db")
    rows = [dict(r) for r in g.conn.execute(
        "SELECT * FROM thoughts WHERE source_id = ? ORDER BY turn_number", (source_id,)
    ).fetchall()]
    g.close()
    return rows


# ══════════════════════════════════════════════════════════════════════
# _graph_appender_tick — lazy build, feed, resume, rollover
# ══════════════════════════════════════════════════════════════════════


class TestGraphAppenderTick:
    @pytest.mark.asyncio
    async def test_noop_when_flag_disabled(self, setup_env):
        tmp_path, db_path, orgs_dir = setup_env
        jsonl = tmp_path / "sess.jsonl"
        jsonl.write_bytes(_entry_bytes("hello", "2026-05-01T10:00:00Z"))
        _insert_row(db_path, "auto-flagoff", jsonl_path=str(jsonl))

        sm_mod, mon = _fresh_monitor()
        with patch("tools.dashboard.feature_flags.is_enabled", return_value=False):
            await mon._graph_appender_tick("auto-flagoff", jsonl)

        assert "auto-flagoff" not in mon._graph_appenders

    @pytest.mark.asyncio
    async def test_builds_and_feeds_on_first_tick(self, setup_env):
        tmp_path, db_path, orgs_dir = setup_env
        jsonl = tmp_path / "sess.jsonl"
        jsonl.write_bytes(_entry_bytes("Do the thing", "2026-05-01T10:00:00Z"))
        _insert_row(db_path, "auto-first", jsonl_path=str(jsonl))

        sm_mod, mon = _fresh_monitor()
        with patch("tools.dashboard.feature_flags.is_enabled", return_value=True):
            await mon._graph_appender_tick("auto-first", jsonl)

        appender = mon._graph_appenders.get("auto-first")
        assert appender is not None
        assert appender.graph_ingest_offset == jsonl.stat().st_size

        from tools.dashboard.dao import dashboard_db as ddb
        row = ddb.get_session("auto-first")
        thoughts = _thoughts_for(orgs_dir, "autonomy", row["graph_source_id"])
        assert len(thoughts) == 1
        assert thoughts[0]["content"] == "Do the thing"

    @pytest.mark.asyncio
    async def test_second_tick_feeds_only_the_delta(self, setup_env):
        tmp_path, db_path, orgs_dir = setup_env
        jsonl = tmp_path / "sess.jsonl"
        jsonl.write_bytes(_entry_bytes("First", "2026-05-01T10:00:00Z", uuid="u1"))
        _insert_row(db_path, "auto-delta", jsonl_path=str(jsonl))

        sm_mod, mon = _fresh_monitor()
        with patch("tools.dashboard.feature_flags.is_enabled", return_value=True):
            await mon._graph_appender_tick("auto-delta", jsonl)
            with open(jsonl, "ab") as f:
                f.write(_entry_bytes("Second", "2026-05-01T10:01:00Z", uuid="u2"))
            await mon._graph_appender_tick("auto-delta", jsonl)

        from tools.dashboard.dao import dashboard_db as ddb
        row = ddb.get_session("auto-delta")
        thoughts = _thoughts_for(orgs_dir, "autonomy", row["graph_source_id"])
        assert [t["content"] for t in thoughts] == ["First", "Second"]

    @pytest.mark.asyncio
    async def test_rollover_rebuilds_against_new_path(self, setup_env):
        """A new jsonl_path (rollover) must produce a NEW source, not
        append onto the old one — 'codex rollover creates new source'."""
        tmp_path, db_path, orgs_dir = setup_env
        jsonl_a = tmp_path / "a.jsonl"
        jsonl_a.write_bytes(_entry_bytes("Session A content", "2026-05-01T10:00:00Z"))
        _insert_row(db_path, "auto-roll", jsonl_path=str(jsonl_a))

        sm_mod, mon = _fresh_monitor()
        with patch("tools.dashboard.feature_flags.is_enabled", return_value=True):
            await mon._graph_appender_tick("auto-roll", jsonl_a)
            source_id_a = mon._graph_appenders["auto-roll"].source_id

            jsonl_b = tmp_path / "b.jsonl"
            jsonl_b.write_bytes(_entry_bytes("Session B content", "2026-05-01T10:05:00Z"))
            await mon._graph_appender_tick("auto-roll", jsonl_b)

        appender = mon._graph_appenders["auto-roll"]
        assert appender.file_path == jsonl_b
        assert appender.source_id != source_id_a

        thoughts_a = _thoughts_for(orgs_dir, "autonomy", source_id_a)
        thoughts_b = _thoughts_for(orgs_dir, "autonomy", appender.source_id)
        assert [t["content"] for t in thoughts_a] == ["Session A content"]
        assert [t["content"] for t in thoughts_b] == ["Session B content"]

    @pytest.mark.asyncio
    async def test_truncation_triggers_reset_not_crash(self, setup_env):
        tmp_path, db_path, orgs_dir = setup_env
        jsonl = tmp_path / "sess.jsonl"
        jsonl.write_bytes(
            _entry_bytes("First", "2026-05-01T10:00:00Z", uuid="u1")
            + _entry_bytes("Second", "2026-05-01T10:01:00Z", uuid="u2")
        )
        _insert_row(db_path, "auto-trunc", jsonl_path=str(jsonl))

        sm_mod, mon = _fresh_monitor()
        with patch("tools.dashboard.feature_flags.is_enabled", return_value=True):
            await mon._graph_appender_tick("auto-trunc", jsonl)
            appender_before = mon._graph_appenders["auto-trunc"]
            assert appender_before.graph_ingest_offset > 0

            # Truncate in place (same path, smaller size) — simulates a
            # compaction rewrite without an inode change.
            jsonl.write_bytes(_entry_bytes("Restarted", "2026-05-01T10:02:00Z", uuid="u3"))
            await mon._graph_appender_tick("auto-trunc", jsonl)

        appender_after = mon._graph_appenders["auto-trunc"]
        assert appender_after.graph_ingest_offset == jsonl.stat().st_size

    @pytest.mark.asyncio
    async def test_resumes_from_persisted_offset_after_reinit(self, setup_env):
        """Simulates a dashboard restart: the in-memory appender dict is
        empty, but the source's metadata still has the prior offset —
        the next tick must resume, not duplicate."""
        tmp_path, db_path, orgs_dir = setup_env
        jsonl = tmp_path / "sess.jsonl"
        jsonl.write_bytes(_entry_bytes("Before restart", "2026-05-01T10:00:00Z", uuid="u1"))
        _insert_row(db_path, "auto-restart", jsonl_path=str(jsonl))

        sm_mod, mon1 = _fresh_monitor()
        with patch("tools.dashboard.feature_flags.is_enabled", return_value=True):
            await mon1._graph_appender_tick("auto-restart", jsonl)
        source_id = mon1._graph_appenders["auto-restart"].source_id

        with open(jsonl, "ab") as f:
            f.write(_entry_bytes("After restart", "2026-05-01T10:05:00Z", uuid="u2"))

        # Fresh monitor instance == empty _graph_appenders, as after a
        # process restart.
        mon2 = sm_mod.SessionMonitor()
        with patch("tools.dashboard.feature_flags.is_enabled", return_value=True):
            await mon2._graph_appender_tick("auto-restart", jsonl)

        thoughts = _thoughts_for(orgs_dir, "autonomy", source_id)
        assert [t["content"] for t in thoughts] == ["Before restart", "After restart"]


# ══════════════════════════════════════════════════════════════════════
# Death path — final in-process catch-up, no subprocess
# ══════════════════════════════════════════════════════════════════════


class TestFinalGraphCatchup:
    @pytest.mark.asyncio
    async def test_uses_active_appender_and_drops_it(self, setup_env):
        tmp_path, db_path, orgs_dir = setup_env
        jsonl = tmp_path / "sess.jsonl"
        jsonl.write_bytes(_entry_bytes("First", "2026-05-01T10:00:00Z", uuid="u1"))
        _insert_row(db_path, "auto-death", jsonl_path=str(jsonl))

        sm_mod, mon = _fresh_monitor()
        with patch("tools.dashboard.feature_flags.is_enabled", return_value=True):
            await mon._graph_appender_tick("auto-death", jsonl)
            source_id = mon._graph_appenders["auto-death"].source_id

            with open(jsonl, "ab") as f:
                f.write(_entry_bytes("Final words before death", "2026-05-01T10:10:00Z", uuid="u2"))

            await mon._final_graph_catchup("auto-death", str(jsonl))

        assert "auto-death" not in mon._graph_appenders
        thoughts = _thoughts_for(orgs_dir, "autonomy", source_id)
        assert [t["content"] for t in thoughts] == ["First", "Final words before death"]

    @pytest.mark.asyncio
    async def test_falls_back_to_full_reparse_when_no_appender_active(self, setup_env):
        """Session died before ever getting a tail tick (flag was off, or
        it died immediately) — no subprocess, but content still lands via
        an in-process full reparse."""
        tmp_path, db_path, orgs_dir = setup_env
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        jsonl = sessions_dir / "sess.jsonl"
        jsonl.write_bytes(_entry_bytes("Never tailed", "2026-05-01T10:00:00Z", uuid="u1"))
        (sessions_dir / ".session_meta.json").write_text(json.dumps({"graph_org": "autonomy"}))
        _insert_row(db_path, "auto-untailed", jsonl_path=str(jsonl))

        sm_mod, mon = _fresh_monitor()
        assert "auto-untailed" not in mon._graph_appenders

        await mon._final_graph_catchup("auto-untailed", str(jsonl))

        from tools.graph.db import GraphDB
        g = GraphDB(orgs_dir / "autonomy.db")
        row = g.conn.execute(
            "SELECT id FROM sources WHERE file_path LIKE ?", (f"%{jsonl.name}",)
        ).fetchone()
        g.close()
        assert row is not None, "final full-reparse fallback must still create a source"
