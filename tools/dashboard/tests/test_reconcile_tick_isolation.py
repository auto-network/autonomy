"""Tests for reconciliation_tick step isolation + health surfacing (W6, auto-4uvpx).

``SessionMonitor.reconciliation_tick`` runs three steps in sequence:
pending-session resolution scan, inode safety net, and the
``graph_source_id`` reconciler. Before this bead, an unhandled exception
in step 1 or 2 aborted the whole tick — so the ``graph_source_id``
backfill (the one thing keeping the Recent-sessions list honest) silently
stopped running until whatever broke step 1/2 was fixed, with nothing
visible to an operator. Each step is now independently isolated, and the
monitor tracks a failure streak exposed via ``get_health()`` /
``/api/health``.
"""

from __future__ import annotations

import importlib
import json
import sqlite3
import time
from pathlib import Path

import pytest


# ══════════════════════════════════════════════════════════════════════
# Fixture scaffolding — mirrors test_graph_source_id_reconcile.py
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture(autouse=True)
def _evict_pooled_orgs():
    from tools.graph.db import GraphDB
    GraphDB.close_all_pooled()
    yield
    GraphDB.close_all_pooled()


def _init_dashboard_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute("""CREATE TABLE IF NOT EXISTS tmux_sessions (
        tmux_name TEXT PRIMARY KEY,
        session_uuid TEXT,
        graph_source_id TEXT,
        type TEXT NOT NULL,
        project TEXT NOT NULL,
        jsonl_path TEXT,
        bead_id TEXT,
        created_at REAL NOT NULL,
        is_live INTEGER DEFAULT 1,
        file_offset INTEGER DEFAULT 0,
        last_activity REAL,
        last_message TEXT DEFAULT '',
        entry_count INTEGER DEFAULT 0,
        context_tokens INTEGER DEFAULT 0,
        label TEXT DEFAULT '',
        topics TEXT DEFAULT '[]',
        role TEXT DEFAULT '',
        resolution_dir TEXT,
        session_uuids TEXT DEFAULT '[]',
        curr_jsonl_file TEXT,
        activity_state TEXT DEFAULT 'idle'
    )""")
    conn.commit()
    conn.close()


def _insert_row(
    db_path: Path, tmux_name: str, *, jsonl_path: str | None,
    graph_source_id: str | None, is_live: int = 1,
) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO tmux_sessions"
        " (tmux_name, type, project, jsonl_path, graph_source_id,"
        "  created_at, is_live)"
        " VALUES (?, 'host', 'autonomy', ?, ?, ?, ?)",
        (tmux_name, jsonl_path, graph_source_id, time.time(), is_live),
    )
    conn.commit()
    conn.close()


def _insert_org_source(db_path: Path, *, source_id: str, file_path: str) -> None:
    from tools.graph.db import GraphDB
    g = GraphDB(db_path)
    g.conn.execute(
        "INSERT INTO sources"
        " (id, type, platform, title, file_path,"
        "  metadata, created_at, ingested_at, last_activity_at)"
        " VALUES (?, 'session', 'claude-code', 'test', ?, ?,"
        "         '2026-04-30T00:00:00Z', '2026-04-30T00:00:00Z', '2026-04-30T00:00:00Z')",
        (source_id, file_path, json.dumps({"session_uuid": Path(file_path).stem})),
    )
    g.commit()
    g.close()


@pytest.fixture
def setup_env(tmp_path, monkeypatch):
    db_path = tmp_path / "dashboard.db"
    _init_dashboard_db(db_path)
    orgs_dir = tmp_path / "orgs"
    orgs_dir.mkdir()
    monkeypatch.setenv("DASHBOARD_DB", str(db_path))
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    from tools.dashboard.dao import dashboard_db as ddb
    importlib.reload(ddb)
    yield tmp_path, db_path, orgs_dir


def _fresh_monitor():
    from tools.dashboard import session_monitor as sm_mod
    importlib.reload(sm_mod)
    return sm_mod, sm_mod.SessionMonitor()


# ══════════════════════════════════════════════════════════════════════
# Step isolation — a broken step 1 must not block step 3
# ══════════════════════════════════════════════════════════════════════


class TestStepIsolation:
    @pytest.mark.asyncio
    async def test_pending_resolution_failure_does_not_block_graph_source_id_reconcile(
        self, setup_env, monkeypatch,
    ):
        """Step 1 (pending-resolution scan) raising must not prevent step 3
        (graph_source_id backfill) from running in the same tick."""
        tmp_path, db_path, orgs_dir = setup_env
        jsonl = str(tmp_path / "tick.jsonl")
        Path(jsonl).touch()
        _insert_org_source(
            orgs_dir / "autonomy.db",
            source_id="isolated-real-id",
            file_path=jsonl,
        )
        _insert_row(
            db_path, "auto-isolated",
            jsonl_path=jsonl,
            graph_source_id="bogus-uuid",
        )

        sm_mod, mon = _fresh_monitor()
        monkeypatch.setattr(
            "tools.dashboard.dao.dashboard_db.get_sessions_needing_resolution",
            lambda: (_ for _ in ()).throw(RuntimeError("simulated step-1 failure")),
        )

        await mon.reconciliation_tick()

        from tools.dashboard.dao import dashboard_db as ddb
        row = ddb.get_session("auto-isolated")
        assert row["graph_source_id"] == "isolated-real-id", (
            "graph_source_id reconciler (step 3) must run even though "
            "step 1 raised — that backfill is what keeps Recent-sessions honest"
        )

    @pytest.mark.asyncio
    async def test_tick_does_not_raise_when_a_step_fails(self, setup_env, monkeypatch):
        """reconciliation_tick itself must not propagate a step failure —
        the caller (_reconciliation_loop) relies on this to keep looping."""
        sm_mod, mon = _fresh_monitor()
        monkeypatch.setattr(
            "tools.dashboard.dao.dashboard_db.get_sessions_needing_resolution",
            lambda: (_ for _ in ()).throw(RuntimeError("simulated failure")),
        )
        result = await mon.reconciliation_tick()
        assert result == 0


# ══════════════════════════════════════════════════════════════════════
# Health surfacing — failure streak visible via get_health()
# ══════════════════════════════════════════════════════════════════════


class TestHealthSurfacing:
    @pytest.mark.asyncio
    async def test_clean_tick_reports_healthy(self, setup_env):
        sm_mod, mon = _fresh_monitor()
        await mon.reconciliation_tick()
        health = mon.get_health()
        assert health["reconcile_failure_streak"] == 0
        assert health["reconcile_degraded_since"] is None
        assert health["reconcile_degraded"] is False

    @pytest.mark.asyncio
    async def test_failing_tick_increments_streak(self, setup_env, monkeypatch):
        sm_mod, mon = _fresh_monitor()
        monkeypatch.setattr(
            "tools.dashboard.dao.dashboard_db.get_sessions_needing_resolution",
            lambda: (_ for _ in ()).throw(RuntimeError("simulated failure")),
        )
        await mon.reconciliation_tick()
        await mon.reconciliation_tick()
        health = mon.get_health()
        assert health["reconcile_failure_streak"] == 2
        assert health["reconcile_degraded_since"] is not None

    @pytest.mark.asyncio
    async def test_streak_resets_on_next_clean_tick(self, setup_env, monkeypatch):
        sm_mod, mon = _fresh_monitor()
        monkeypatch.setattr(
            "tools.dashboard.dao.dashboard_db.get_sessions_needing_resolution",
            lambda: (_ for _ in ()).throw(RuntimeError("simulated failure")),
        )
        await mon.reconciliation_tick()
        assert mon.get_health()["reconcile_failure_streak"] == 1

        monkeypatch.undo()
        await mon.reconciliation_tick()
        health = mon.get_health()
        assert health["reconcile_failure_streak"] == 0
        assert health["reconcile_degraded_since"] is None

    @pytest.mark.asyncio
    async def test_not_degraded_before_threshold(self, setup_env, monkeypatch):
        """A short failure streak is visible in the streak counter but not
        yet flagged 'degraded' — avoids paging on a single transient blip."""
        sm_mod, mon = _fresh_monitor()
        monkeypatch.setattr(
            "tools.dashboard.dao.dashboard_db.get_sessions_needing_resolution",
            lambda: (_ for _ in ()).throw(RuntimeError("simulated failure")),
        )
        await mon.reconciliation_tick()
        health = mon.get_health()
        assert health["reconcile_failure_streak"] == 1
        assert health["reconcile_degraded"] is False

    @pytest.mark.asyncio
    async def test_degraded_after_threshold_elapsed(self, setup_env, monkeypatch):
        """Once the streak has persisted past the threshold, get_health()
        flags degraded=True."""
        sm_mod, mon = _fresh_monitor()
        mon._DEGRADED_THRESHOLD_SECONDS = 0.01
        monkeypatch.setattr(
            "tools.dashboard.dao.dashboard_db.get_sessions_needing_resolution",
            lambda: (_ for _ in ()).throw(RuntimeError("simulated failure")),
        )
        await mon.reconciliation_tick()
        time.sleep(0.02)
        health = mon.get_health()
        assert health["reconcile_degraded"] is True
