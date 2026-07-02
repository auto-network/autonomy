"""Tests for W2: eager source creation at session init (auto-u6o1j).

``SessionMonitor._eager_create_source`` generalizes the ``agentic_source_id``
pattern to every session type — a zero-turn ``type='session'`` row appears
in the org DB (and ``tmux_sessions.graph_source_id`` gets linked) the
instant a JSONL is discovered, instead of waiting on an ingest sweep.

Coverage:
  1. Flag-gated: no-op unless ``ingest.eager_sources`` is enabled.
  2. Creates a source + links graph_source_id; idempotent on retry (no
     duplicate row by file_path).
  3. Fails closed (skips, no crash) when org can't be resolved.
  4. ``_handle_jsonl_appeared`` triggers it — source exists before any
     turns are ingested.
  5. ``_eager_create_missing_sources`` retry sweep catches sessions that
     missed eager creation (flag off then on, transient failure).
  6. Recent-sessions visibility: once the session ends, get_recent_sessions
     finds it via the eager source alone (Recent excludes live sessions —
     they belong on Active — so this is the "closed-session lag" case the
     project plan's B11 measured as 1-2 min pre-W2).
"""

from __future__ import annotations

import importlib
import json
import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

import pytest


# ══════════════════════════════════════════════════════════════════════
# Fixture scaffolding
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
    # Pre-create the org DB — resolve_caller_db_path falls back to the
    # legacy single-file DB when data/orgs/<org>.db doesn't exist yet
    # (real deployments always have orgs pre-provisioned before any
    # session lands; tests must mirror that or writes silently land
    # somewhere other than where the test expects to read them).
    from tools.graph.db import GraphDB
    GraphDB(orgs_dir / "autonomy.db").close()

    monkeypatch.setenv("DASHBOARD_DB", str(db_path))
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.delenv("GRAPH_DB", raising=False)

    # _handle_jsonl_appeared's container-session path runs
    # dashboard_db.link_and_enrich(), which shells out to `graph
    # ingest-session`. Stub it so tests don't depend on the real graph CLI —
    # our eager-creation code runs after this and overwrites whatever
    # (possibly non-resolving) id the stub sets, matching production
    # behavior where both mechanisms can legitimately race.
    import subprocess as _sp
    real_run = _sp.run

    class _CompletedStub:
        def __init__(self, stdout: str = "stub-graph-id\n"):
            self.returncode = 0
            self.stdout = stdout
            self.stderr = ""

    def fake_run(cmd, *args, **kwargs):
        if isinstance(cmd, (list, tuple)) and len(cmd) >= 2 and cmd[0] == "graph":
            return _CompletedStub()
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(_sp, "run", fake_run)

    from tools.dashboard.dao import dashboard_db as ddb
    importlib.reload(ddb)
    from tools.dashboard import session_monitor as sm_mod
    importlib.reload(sm_mod)

    yield tmp_path, db_path, orgs_dir


def _fresh_monitor():
    from tools.dashboard import session_monitor as sm_mod
    return sm_mod, sm_mod.SessionMonitor()


def _write_jsonl_with_content(path: Path, text: str = "hello there") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "type": "user", "uuid": "u1",
        "message": {"role": "user", "content": text},
        "timestamp": "2026-05-01T10:00:00Z",
    }
    path.write_text(json.dumps(entry) + "\n")


def _org_source_rows(orgs_dir: Path, org: str) -> list[dict]:
    from tools.graph.db import GraphDB
    g = GraphDB(orgs_dir / f"{org}.db")
    rows = [dict(r) for r in g.conn.execute("SELECT * FROM sources").fetchall()]
    g.close()
    return rows


# ══════════════════════════════════════════════════════════════════════
# Flag gating + basic creation
# ══════════════════════════════════════════════════════════════════════


class TestEagerCreateSourceFlagGating:
    def test_noop_when_flag_disabled(self, setup_env):
        tmp_path, db_path, orgs_dir = setup_env
        jsonl = tmp_path / "session.jsonl"
        _write_jsonl_with_content(jsonl)
        _insert_row(db_path, "auto-flagoff", jsonl_path=str(jsonl))

        sm_mod, mon = _fresh_monitor()
        with patch("tools.dashboard.feature_flags.is_enabled", return_value=False):
            result = mon._eager_create_source("auto-flagoff", jsonl)

        assert result is False
        assert _org_source_rows(orgs_dir, "autonomy") == []

    def test_creates_source_and_links_id_when_enabled(self, setup_env):
        tmp_path, db_path, orgs_dir = setup_env
        jsonl = tmp_path / "session.jsonl"
        _write_jsonl_with_content(jsonl)
        _insert_row(db_path, "auto-flagon", jsonl_path=str(jsonl))

        sm_mod, mon = _fresh_monitor()
        with patch("tools.dashboard.feature_flags.is_enabled", return_value=True):
            result = mon._eager_create_source("auto-flagon", jsonl)

        assert result is True

        from tools.dashboard.dao import dashboard_db as ddb
        row = ddb.get_session("auto-flagon")
        assert row["graph_source_id"], "graph_source_id must be written"

        sources = _org_source_rows(orgs_dir, "autonomy")
        assert len(sources) == 1
        src = sources[0]
        assert src["type"] == "session"
        assert src["id"] == row["graph_source_id"]
        assert src["title"] is None
        meta = json.loads(src["metadata"])
        assert meta["eager"] is True
        assert meta["file_size"] == 0
        assert meta["ingest_offset"] == 0
        assert meta["session_uuid"] == jsonl.stem


class TestEagerCreateSourceOrgResolution:
    def test_skips_when_org_unresolvable(self, setup_env):
        """No project on the row, no .session_meta.json, no host-path
        mapping → fail-closed (matches pre-W2 skip behavior), no crash."""
        tmp_path, db_path, orgs_dir = setup_env
        # Registered with an empty project and a jsonl path that doesn't
        # match any host-project mapping.
        jsonl = tmp_path / "unrouteable" / "session.jsonl"
        _write_jsonl_with_content(jsonl)
        _insert_row(db_path, "auto-noorg", jsonl_path=str(jsonl), project="")

        sm_mod, mon = _fresh_monitor()
        with patch("tools.dashboard.feature_flags.is_enabled", return_value=True):
            result = mon._eager_create_source("auto-noorg", jsonl)

        assert result is False
        from tools.dashboard.dao import dashboard_db as ddb
        row = ddb.get_session("auto-noorg")
        assert not row["graph_source_id"]


class TestEagerCreateSourceIdempotence:
    def test_retry_does_not_duplicate_source(self, setup_env):
        tmp_path, db_path, orgs_dir = setup_env
        jsonl = tmp_path / "session.jsonl"
        _write_jsonl_with_content(jsonl)
        _insert_row(db_path, "auto-retry", jsonl_path=str(jsonl))

        sm_mod, mon = _fresh_monitor()
        with patch("tools.dashboard.feature_flags.is_enabled", return_value=True):
            r1 = mon._eager_create_source("auto-retry", jsonl)
            r2 = mon._eager_create_source("auto-retry", jsonl)

        assert r1 is True
        assert r2 is True
        assert len(_org_source_rows(orgs_dir, "autonomy")) == 1


# ══════════════════════════════════════════════════════════════════════
# _handle_jsonl_appeared triggers eager creation
# ══════════════════════════════════════════════════════════════════════


class TestHandleJsonlAppearedTriggersEagerCreation:
    def test_source_exists_before_any_turns_ingested(self, setup_env):
        """AC: the source row + graph_source_id exist the moment the JSONL
        is linked — before any content-ingest pass has run."""
        tmp_path, db_path, orgs_dir = setup_env
        jsonl = tmp_path / "session.jsonl"
        _write_jsonl_with_content(jsonl, "Do the thing")
        # Registered but not yet linked to a jsonl_path — the state
        # _handle_jsonl_appeared expects for a first-resolution call.
        _insert_row(db_path, "auto-discover", jsonl_path=None)

        sm_mod, mon = _fresh_monitor()
        with patch("tools.dashboard.feature_flags.is_enabled", return_value=True):
            linked = mon._handle_jsonl_appeared("auto-discover", jsonl)

        assert linked is True

        from tools.dashboard.dao import dashboard_db as ddb
        row = ddb.get_session("auto-discover")
        assert row["graph_source_id"], "eager source must be linked by _handle_jsonl_appeared"

        sources = _org_source_rows(orgs_dir, "autonomy")
        assert len(sources) == 1
        assert sources[0]["id"] == row["graph_source_id"]

        # No turns ingested — nothing has called the real ingest pipeline.
        from tools.graph.db import GraphDB
        g = GraphDB(orgs_dir / "autonomy.db")
        thought_count = g.conn.execute(
            "SELECT COUNT(*) c FROM thoughts WHERE source_id = ?",
            (sources[0]["id"],),
        ).fetchone()["c"]
        g.close()
        assert thought_count == 0


# ══════════════════════════════════════════════════════════════════════
# Retry sweep
# ══════════════════════════════════════════════════════════════════════


class TestEagerCreateMissingSourcesSweep:
    def test_sweep_creates_source_for_session_missing_one(self, setup_env):
        tmp_path, db_path, orgs_dir = setup_env
        jsonl = tmp_path / "session.jsonl"
        _write_jsonl_with_content(jsonl)
        # Simulate: jsonl already linked, but eager creation never ran
        # (e.g. flag was off at discovery time).
        _insert_row(db_path, "auto-sweep", jsonl_path=str(jsonl), graph_source_id="")

        sm_mod, mon = _fresh_monitor()
        with patch("tools.dashboard.feature_flags.is_enabled", return_value=True):
            created = mon._eager_create_missing_sources()

        assert created == 1
        from tools.dashboard.dao import dashboard_db as ddb
        row = ddb.get_session("auto-sweep")
        assert row["graph_source_id"]

    def test_sweep_skips_sessions_already_linked(self, setup_env):
        tmp_path, db_path, orgs_dir = setup_env
        jsonl = tmp_path / "session.jsonl"
        _write_jsonl_with_content(jsonl)
        _insert_row(db_path, "auto-linked", jsonl_path=str(jsonl), graph_source_id="already-set")

        sm_mod, mon = _fresh_monitor()
        with patch("tools.dashboard.feature_flags.is_enabled", return_value=True):
            created = mon._eager_create_missing_sources()

        assert created == 0

    def test_sweep_skips_sessions_without_jsonl_path(self, setup_env):
        tmp_path, db_path, orgs_dir = setup_env
        _insert_row(db_path, "auto-nopath", jsonl_path=None, graph_source_id="")

        sm_mod, mon = _fresh_monitor()
        with patch("tools.dashboard.feature_flags.is_enabled", return_value=True):
            created = mon._eager_create_missing_sources()

        assert created == 0


# ══════════════════════════════════════════════════════════════════════
# Recent-sessions visibility for closed eager sessions
# ══════════════════════════════════════════════════════════════════════


class TestRecentSessionsVisibility:
    def test_closed_eager_session_visible_in_recent(self, setup_env):
        """Recent excludes *live* sessions (they belong on Active) — the
        lag this bead fixes is specifically the closed-session case
        (project plan B11: 1-2 min via the old death-path ingest). Once
        the session goes non-live, its eager source alone must be enough
        for get_recent_sessions to find it — no ingest required."""
        tmp_path, db_path, orgs_dir = setup_env
        jsonl = tmp_path / "session.jsonl"
        _write_jsonl_with_content(jsonl)
        _insert_row(db_path, "auto-closed", jsonl_path=str(jsonl), is_live=1)

        sm_mod, mon = _fresh_monitor()
        with patch("tools.dashboard.feature_flags.is_enabled", return_value=True):
            assert mon._eager_create_source("auto-closed", jsonl) is True

        from tools.dashboard.dao import dashboard_db as ddb
        ddb.mark_dead("auto-closed")

        from tools.dashboard.dao.sessions import get_recent_sessions
        recent = get_recent_sessions(since="all")
        ids = {
            r.get("session_id") or r.get("tmux_name") or r.get("id")
            for r in recent
        }
        assert "auto-closed" in ids or ddb.get_session("auto-closed")["graph_source_id"] in ids, (
            f"closed eager session not found in Recent; got ids={ids}"
        )
