"""Reconciler tests for ``tmux_sessions.graph_source_id`` (auto-4jpa8).

The dashboard ``tmux_sessions`` row carries a ``graph_source_id`` that is
supposed to point at the source row the ingester wrote for the session's
JSONL. Two failure modes have been observed in production:

  * **Drift** — the dashboard stores a UUID that exists in *no* org DB.
    Most live example: ``auto-0428-005821`` carried
    ``d15ca523-643e-4d61-8161-a82bda65dbe2`` while the real source for the
    JSONL was ``a5134fd1-…``.

  * **Race** — the dashboard registered the row before the ingester
    finished writing the source. ``graph_source_id`` was empty and never
    got back-filled.

The fix has three parts, all exercised below:

  1. :func:`reconcile_graph_source_ids` — selects rows whose stored ID is
     empty/null or doesn't resolve in any org DB and rewrites it from the
     real ``sources.id WHERE file_path = jsonl_path`` lookup.

  2. :func:`set_graph_source_validated` — used by registration code paths
     (``link_and_enrich`` + the seed-monitor ENRICH pass) to log a warning
     when a non-resolving ID is being written. Empty is allowed (the
     reconciler will fill it).

  3. ``SessionMonitor.reconciliation_tick`` — invokes the reconciler each
     pass so drift + race rows are repaired without operator intervention.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import logging
import sqlite3
import time
from pathlib import Path

import pytest


# ══════════════════════════════════════════════════════════════════════
# Fixture scaffolding
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture(autouse=True)
def _evict_pooled_orgs():
    """Per-org ``GraphDB`` connections are pooled for the process lifetime;
    each test rebuilds the orgs dir, so we have to drop the pool around
    every test or the second test sees the first test's connections."""
    from tools.graph.db import GraphDB
    GraphDB.close_all_pooled()
    yield
    GraphDB.close_all_pooled()


def _init_dashboard_db(db_path: Path) -> None:
    """Create the minimal ``tmux_sessions`` schema the reconciler needs.

    This mirrors the columns set by the live ``dashboard_db._SCHEMA`` —
    the reconciler only reads ``tmux_name``, ``graph_source_id``,
    ``jsonl_path`` and ``is_live``, so the rest is just enough to make
    INSERTs succeed.
    """
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
    db_path: Path,
    tmux_name: str,
    *,
    jsonl_path: str | None,
    graph_source_id: str | None,
    is_live: int = 1,
    label: str = "",
) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO tmux_sessions"
        " (tmux_name, type, project, jsonl_path, graph_source_id,"
        "  created_at, is_live, label)"
        " VALUES (?, 'host', 'autonomy', ?, ?, ?, ?, ?)",
        (tmux_name, jsonl_path, graph_source_id, time.time(), is_live, label),
    )
    conn.commit()
    conn.close()


def _insert_org_source(db_path: Path, *, source_id: str, file_path: str) -> None:
    """Write a minimal session source row into a per-org DB."""
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
    """Build dashboard.db + an empty orgs/ root the reconciler can see."""
    db_path = tmp_path / "dashboard.db"
    _init_dashboard_db(db_path)
    orgs_dir = tmp_path / "orgs"
    orgs_dir.mkdir()
    monkeypatch.setenv("DASHBOARD_DB", str(db_path))
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    # Reload the DAO so it binds to the new env.
    from tools.dashboard.dao import dashboard_db as ddb
    importlib.reload(ddb)
    yield tmp_path, db_path, orgs_dir


# ══════════════════════════════════════════════════════════════════════
# reconcile_graph_source_ids — drift, race, idempotence
# ══════════════════════════════════════════════════════════════════════


class TestReconcileGraphSourceIds:
    """Direct DAO tests — no inotify, no async. Exercises the writer-side
    repair path described in auto-4jpa8 §A."""

    def test_repairs_drifted_id(self, setup_env):
        """A row whose stored ID resolves in NO org DB gets overwritten
        with the real ID looked up by ``file_path``.

        Reproduces auto-0428-005821: the dashboard had a stale UUID,
        the actual source row was keyed by the JSONL path."""
        tmp_path, db_path, orgs_dir = setup_env
        jsonl = str(tmp_path / "session.jsonl")
        Path(jsonl).touch()
        _insert_org_source(
            orgs_dir / "autonomy.db",
            source_id="real-source-id-aaaa",
            file_path=jsonl,
        )
        _insert_row(
            db_path, "auto-drifted",
            jsonl_path=jsonl,
            graph_source_id="bogus-uuid-no-org-has-this",
        )

        from tools.dashboard.dao import dashboard_db as ddb
        repaired = ddb.reconcile_graph_source_ids()

        assert repaired == 1
        row = ddb.get_session("auto-drifted")
        assert row is not None
        assert row["graph_source_id"] == "real-source-id-aaaa", \
            f"reconciler must replace the bogus UUID; got {row['graph_source_id']!r}"

    def test_backfills_empty_id(self, setup_env):
        """A row registered before ingest finished (race case) gets its
        empty ``graph_source_id`` filled in on the next tick."""
        tmp_path, db_path, orgs_dir = setup_env
        jsonl = str(tmp_path / "race.jsonl")
        Path(jsonl).touch()
        _insert_org_source(
            orgs_dir / "autonomy.db",
            source_id="race-source-id-bbbb",
            file_path=jsonl,
        )
        _insert_row(
            db_path, "auto-race",
            jsonl_path=jsonl,
            graph_source_id="",  # empty — registration-before-ingest race
        )

        from tools.dashboard.dao import dashboard_db as ddb
        repaired = ddb.reconcile_graph_source_ids()

        assert repaired == 1
        row = ddb.get_session("auto-race")
        assert row["graph_source_id"] == "race-source-id-bbbb"

    def test_backfills_null_id(self, setup_env):
        """NULL behaves identically to ''."""
        tmp_path, db_path, orgs_dir = setup_env
        jsonl = str(tmp_path / "null-id.jsonl")
        Path(jsonl).touch()
        _insert_org_source(
            orgs_dir / "autonomy.db",
            source_id="null-source-id-cccc",
            file_path=jsonl,
        )
        _insert_row(
            db_path, "auto-null",
            jsonl_path=jsonl,
            graph_source_id=None,
        )

        from tools.dashboard.dao import dashboard_db as ddb
        repaired = ddb.reconcile_graph_source_ids()

        assert repaired == 1
        row = ddb.get_session("auto-null")
        assert row["graph_source_id"] == "null-source-id-cccc"

    def test_skips_when_jsonl_not_yet_ingested(self, setup_env):
        """A truly-not-yet-ingested row stays empty so the next tick
        retries. Acceptance criterion 4 in the bead: 'If not found, leaves
        it empty and tries again next tick.'"""
        tmp_path, db_path, orgs_dir = setup_env
        # autonomy.db file present but no source row matches our JSONL.
        _insert_org_source(
            orgs_dir / "autonomy.db",
            source_id="some-other-session-id",
            file_path=str(tmp_path / "different.jsonl"),
        )
        _insert_row(
            db_path, "auto-pending",
            jsonl_path=str(tmp_path / "pending.jsonl"),
            graph_source_id="",
        )

        from tools.dashboard.dao import dashboard_db as ddb
        repaired = ddb.reconcile_graph_source_ids()

        assert repaired == 0
        row = ddb.get_session("auto-pending")
        assert row["graph_source_id"] in (None, "")

    def test_does_not_overwrite_correct_id(self, setup_env):
        """Idempotent: a row whose ID already resolves is left alone.

        Verifies the reconciler short-circuits before doing the
        ``file_path`` lookup so a healthy row isn't churned."""
        tmp_path, db_path, orgs_dir = setup_env
        jsonl = str(tmp_path / "happy.jsonl")
        Path(jsonl).touch()
        _insert_org_source(
            orgs_dir / "autonomy.db",
            source_id="happy-source-id-dddd",
            file_path=jsonl,
        )
        _insert_row(
            db_path, "auto-happy",
            jsonl_path=jsonl,
            graph_source_id="happy-source-id-dddd",
        )

        from tools.dashboard.dao import dashboard_db as ddb
        repaired = ddb.reconcile_graph_source_ids()

        assert repaired == 0
        row = ddb.get_session("auto-happy")
        assert row["graph_source_id"] == "happy-source-id-dddd"

    def test_skips_rows_without_jsonl_path(self, setup_env):
        """A pending row without a resolved JSONL path is out of scope —
        the reconciler can't repair what it can't look up. Future ticks
        repair the row only after the JSONL watcher / link path resolves
        the path."""
        tmp_path, db_path, _ = setup_env
        _insert_row(
            db_path, "auto-no-jsonl",
            jsonl_path=None,
            graph_source_id="",
        )

        from tools.dashboard.dao import dashboard_db as ddb
        repaired = ddb.reconcile_graph_source_ids()

        assert repaired == 0
        row = ddb.get_session("auto-no-jsonl")
        assert (row["graph_source_id"] or "") == ""

    def test_finds_source_in_non_autonomy_org(self, setup_env):
        """The lookup must scan every org DB, not just autonomy.db.

        A session whose JSONL was ingested into ``personal.db`` (e.g. a
        session with no ``graph_org`` in its meta) must still be
        reconcilable."""
        tmp_path, db_path, orgs_dir = setup_env
        jsonl = str(tmp_path / "personal-session.jsonl")
        Path(jsonl).touch()
        _insert_org_source(
            orgs_dir / "personal.db",
            source_id="personal-source-id-eeee",
            file_path=jsonl,
        )
        _insert_row(
            db_path, "auto-personal",
            jsonl_path=jsonl,
            graph_source_id="",
        )

        from tools.dashboard.dao import dashboard_db as ddb
        repaired = ddb.reconcile_graph_source_ids()

        assert repaired == 1
        row = ddb.get_session("auto-personal")
        assert row["graph_source_id"] == "personal-source-id-eeee"


# ══════════════════════════════════════════════════════════════════════
# Label write-through on repair (W6, auto-4uvpx) — closes the post-W5 gap
# ══════════════════════════════════════════════════════════════════════


class TestReconcileLabelWriteThrough:
    """W5 made title derivation creation-only, which opened a gap: a label
    set via ``set-label`` before ``graph_source_id`` gets linked used to
    heal on the session's next re-ingest (``_derive_session_title`` picking
    up the label). That no longer runs. The reconciler now pushes a
    pending label onto the title the moment it links/repairs the id."""

    def test_repair_pushes_pending_label_to_new_source_title(self, setup_env):
        tmp_path, db_path, orgs_dir = setup_env
        jsonl = str(tmp_path / "labeled.jsonl")
        Path(jsonl).touch()
        _insert_org_source(
            orgs_dir / "autonomy.db",
            source_id="labeled-source-id",
            file_path=jsonl,
        )
        _insert_row(
            db_path, "auto-labeled",
            jsonl_path=jsonl,
            graph_source_id="",  # not linked yet — set-label ran first
            label="Operator-set title",
        )

        from tools.dashboard.dao import dashboard_db as ddb
        repaired = ddb.reconcile_graph_source_ids()
        assert repaired == 1

        from tools.graph.db import GraphDB
        g = GraphDB(orgs_dir / "autonomy.db")
        row = g.conn.execute(
            "SELECT title FROM sources WHERE id = ?", ("labeled-source-id",)
        ).fetchone()
        g.close()
        assert row["title"] == "Operator-set title"

    def test_repair_without_label_does_not_touch_title(self, setup_env):
        """No label pending → no write-through call, title stays as
        whatever ingest set it to (untouched by the reconciler)."""
        tmp_path, db_path, orgs_dir = setup_env
        jsonl = str(tmp_path / "unlabeled.jsonl")
        Path(jsonl).touch()
        _insert_org_source(
            orgs_dir / "autonomy.db",
            source_id="unlabeled-source-id",
            file_path=jsonl,
        )
        _insert_row(
            db_path, "auto-unlabeled",
            jsonl_path=jsonl,
            graph_source_id="",
            label="",
        )

        from tools.dashboard.dao import dashboard_db as ddb
        repaired = ddb.reconcile_graph_source_ids()
        assert repaired == 1

        from tools.graph.db import GraphDB
        g = GraphDB(orgs_dir / "autonomy.db")
        row = g.conn.execute(
            "SELECT title FROM sources WHERE id = ?", ("unlabeled-source-id",)
        ).fetchone()
        g.close()
        assert row["title"] == "test"  # the placeholder title from _insert_org_source


# ══════════════════════════════════════════════════════════════════════
# set_graph_source_validated — registration-time verification (§B)
# ══════════════════════════════════════════════════════════════════════


class TestRegistrationVerification:
    """Bead §B: ``graph_source_id`` written at registration time must
    either be empty or resolve in some org DB. Anything else is the bug
    that landed ``d15ca523-…`` on auto-0428-005821."""

    def test_empty_id_does_not_warn(self, setup_env, caplog):
        """Empty IDs are explicitly allowed — the reconciler will fill
        them on the next tick. The validated writer must not log a
        warning for the race case."""
        tmp_path, db_path, _ = setup_env
        _insert_row(
            db_path, "auto-empty",
            jsonl_path=str(tmp_path / "x.jsonl"),
            graph_source_id="",
        )

        from tools.dashboard.dao import dashboard_db as ddb
        with caplog.at_level(logging.WARNING, logger="tools.dashboard.dao.dashboard_db"):
            ddb.set_graph_source_validated("auto-empty", "")

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING
                    and "non-resolving graph_source_id" in r.getMessage()]
        assert warnings == []

    def test_resolving_id_does_not_warn(self, setup_env, caplog):
        """When the new ID resolves in an org DB, the writer must not
        log a warning — that path is the happy case."""
        tmp_path, db_path, orgs_dir = setup_env
        jsonl = str(tmp_path / "ok.jsonl")
        Path(jsonl).touch()
        _insert_org_source(
            orgs_dir / "autonomy.db",
            source_id="real-id-ffff",
            file_path=jsonl,
        )
        _insert_row(
            db_path, "auto-ok",
            jsonl_path=jsonl,
            graph_source_id="",
        )

        from tools.dashboard.dao import dashboard_db as ddb
        with caplog.at_level(logging.WARNING, logger="tools.dashboard.dao.dashboard_db"):
            ddb.set_graph_source_validated("auto-ok", "real-id-ffff")

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING
                    and "non-resolving graph_source_id" in r.getMessage()]
        assert warnings == []
        row = ddb.get_session("auto-ok")
        assert row["graph_source_id"] == "real-id-ffff"

    def test_non_resolving_id_logs_warning(self, setup_env, caplog):
        """A registration-time write of a UUID that no org DB has must
        emit a warning so future drift bugs are visible. The write still
        succeeds — the reconciler will repair it next tick — but
        operators see the bad path in logs immediately."""
        tmp_path, db_path, orgs_dir = setup_env
        # Empty orgs dir — no org DB will ever resolve this ID.
        _insert_row(
            db_path, "auto-bogus",
            jsonl_path=str(tmp_path / "x.jsonl"),
            graph_source_id="",
        )

        from tools.dashboard.dao import dashboard_db as ddb
        with caplog.at_level(logging.WARNING, logger="tools.dashboard.dao.dashboard_db"):
            ddb.set_graph_source_validated(
                "auto-bogus", "definitely-not-a-real-source-id",
            )

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING
                    and "non-resolving graph_source_id" in r.getMessage()]
        assert len(warnings) == 1, \
            f"expected exactly one warning; got {[r.getMessage() for r in warnings]}"
        assert "auto-bogus" in warnings[0].getMessage()
        # The write still happens — caller has nothing better to write
        # and the reconciler is the durable correction path.
        row = ddb.get_session("auto-bogus")
        assert row["graph_source_id"] == "definitely-not-a-real-source-id"


# ══════════════════════════════════════════════════════════════════════
# Reconciliation tick — wired into the live ingest loop
# ══════════════════════════════════════════════════════════════════════


class TestReconciliationTick:
    """Bead acceptance: 'L2 dashboard test that reconciliation runs every
    tick: register a row with ``graph_source_id = 'bogus-uuid'``, run one
    ingest tick, assert it gets repaired.'"""

    @pytest.mark.asyncio
    async def test_tick_repairs_drift(self, setup_env):
        tmp_path, db_path, orgs_dir = setup_env
        jsonl = str(tmp_path / "tick.jsonl")
        Path(jsonl).touch()
        _insert_org_source(
            orgs_dir / "autonomy.db",
            source_id="tick-real-id-gggg",
            file_path=jsonl,
        )
        _insert_row(
            db_path, "auto-tick-drift",
            jsonl_path=jsonl,
            graph_source_id="bogus-uuid",
        )

        # Reload session_monitor against the new DASHBOARD_DB env.
        from tools.dashboard import session_monitor as sm_mod
        importlib.reload(sm_mod)
        mon = sm_mod.SessionMonitor()
        await mon.reconciliation_tick()

        from tools.dashboard.dao import dashboard_db as ddb
        row = ddb.get_session("auto-tick-drift")
        assert row["graph_source_id"] == "tick-real-id-gggg", \
            f"reconciliation_tick must run the graph_source_id reconciler; got {row['graph_source_id']!r}"

    @pytest.mark.asyncio
    async def test_tick_backfills_race(self, setup_env):
        """The other half of the same loop: empty IDs are filled too."""
        tmp_path, db_path, orgs_dir = setup_env
        jsonl = str(tmp_path / "tick-race.jsonl")
        Path(jsonl).touch()
        _insert_org_source(
            orgs_dir / "autonomy.db",
            source_id="tick-race-id-hhhh",
            file_path=jsonl,
        )
        _insert_row(
            db_path, "auto-tick-race",
            jsonl_path=jsonl,
            graph_source_id="",
        )

        from tools.dashboard import session_monitor as sm_mod
        importlib.reload(sm_mod)
        mon = sm_mod.SessionMonitor()
        await mon.reconciliation_tick()

        from tools.dashboard.dao import dashboard_db as ddb
        row = ddb.get_session("auto-tick-race")
        assert row["graph_source_id"] == "tick-race-id-hhhh"
