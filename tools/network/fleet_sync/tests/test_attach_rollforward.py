"""Opening an activated store rolls it forward cheaply: retired objects are
dropped, a trigger recompile never triggers a full backfill, and only a table
that gained capture with existing rows is backfilled (auto-boa0j, 2026-09-07)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tools.graph.db import GraphDB
from tools.graph.models import Source
from tools.network.fleet_sync import catalog as catalog_module
from tools.network.fleet_sync.policies import audit_schema

ORIGIN = "b" * 64


def _activated(path: Path) -> None:
    with GraphDB(path) as db:
        assert db.activate_fleet_sync_writers(ORIGIN)
        db.insert_source(Source(id="s-1", type="note", title="one"))


def test_retired_journal_table_is_dropped_on_open_and_never_fails_the_audit(tmp_path: Path) -> None:
    path = tmp_path / "personal.db"
    _activated(path)
    raw = sqlite3.connect(path)
    raw.execute(
        "CREATE TABLE fleet_sync_journal(transaction_ref INTEGER, operation_index INTEGER, frame BLOB)"
    )
    raw.execute("INSERT INTO fleet_sync_journal VALUES (1, 0, x'00')")
    raw.commit()
    # A store not yet opened by new code still passes the schema audit.
    audit_schema(raw)
    raw.close()
    with GraphDB(path) as db:
        names = {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "fleet_sync_journal" not in names
        audit_schema(db.conn)


def test_trigger_text_change_alone_runs_no_backfill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "personal.db"
    _activated(path)
    raw = sqlite3.connect(path)
    sql, = raw.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='fleet_sync_sources_insert'"
    ).fetchone()
    raw.execute("DROP TRIGGER fleet_sync_sources_insert")
    raw.execute(sql.replace("CREATE TRIGGER", "CREATE TRIGGER /* stale text */", 1))
    raw.commit()
    raw.close()

    def no_backfill(self, *a, **k):
        raise AssertionError("reconcile_catalog must not run for a trigger recompile")

    monkeypatch.setattr(catalog_module.MutationCatalog, "reconcile_catalog", no_backfill)
    with GraphDB(path) as db:
        text, = db.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='fleet_sync_sources_insert'"
        ).fetchone()
        assert "stale text" not in text
        assert db._fleet_catalog.newly_captured_tables == frozenset()
        db.insert_source(Source(id="s-2", type="note", title="two"))
        assert db.conn.execute(
            "SELECT COUNT(*) FROM fleet_sync_catalog"
        ).fetchone()[0] == 2


def test_table_that_gains_capture_with_rows_is_backfilled(tmp_path: Path) -> None:
    path = tmp_path / "personal.db"
    _activated(path)
    raw = sqlite3.connect(path)
    for op in ("insert", "update", "delete"):
        raw.execute(f"DROP TRIGGER IF EXISTS fleet_sync_tags_{op}")
    raw.execute("INSERT INTO tags(name) VALUES ('untracked')")
    raw.commit()
    raw.close()
    with GraphDB(path) as db:
        assert db._fleet_catalog.newly_captured_tables == frozenset({"tags"})
        tracked = [
            m.mutation.address for m in db._fleet_catalog.iter_mutations()
            if m.mutation.table == "tags"
        ]
        assert tracked == [("untracked",)]
