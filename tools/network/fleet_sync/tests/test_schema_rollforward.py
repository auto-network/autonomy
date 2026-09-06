"""Local catalog objects added after activation must reach production stores.

install() runs once at activation; every later open goes through
attach_active_production_catalog(), which never re-ran the schema
statements — so an index added to _create_schema_objects shipped in code
but never existed in the live store (2026-09-06)."""

from pathlib import Path
import sqlite3

from tools.graph.db import GraphDB
from tools.network.fleet_sync.catalog import MutationCatalog
from tools.network.fleet_sync_scheduler import SQLiteFleetSyncStore

ORIGIN = "a" * 64
INDEX = "idx_fleet_sync_catalog_transaction_ref"


def _names(path: Path) -> set[str]:
    with sqlite3.connect(path) as conn:
        return {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','index') "
                "AND name NOT LIKE 'sqlite_%'"
            )
        }


def _schema_version(path: Path) -> int:
    with sqlite3.connect(path) as conn:
        return int(conn.execute("PRAGMA schema_version").fetchone()[0])


def _activate(path: Path) -> None:
    db = GraphDB(path)
    try:
        db.activate_fleet_sync_writers(ORIGIN)
    finally:
        db.close()


def test_attach_recreates_an_object_missing_from_an_activated_store(tmp_path):
    path = tmp_path / "personal.db"
    _activate(path)
    assert INDEX in _names(path)
    # A store activated before the index existed in code.
    with sqlite3.connect(path) as conn:
        conn.execute(f"DROP INDEX {INDEX}")
    assert INDEX not in _names(path)

    conn, catalog = SQLiteFleetSyncStore(path)._open()  # production path
    conn.close()
    assert INDEX in _names(path), "attach must roll the schema forward"


def test_attach_is_read_only_when_the_schema_is_current(tmp_path):
    path = tmp_path / "personal.db"
    _activate(path)
    before = _schema_version(path)
    for _ in range(3):
        conn, _catalog = SQLiteFleetSyncStore(path)._open()
        conn.close()
    assert _schema_version(path) == before, (
        "no DDL may run on an already-current store (every open would "
        "otherwise take the write lock)"
    )


def test_expected_objects_track_the_schema_statements():
    """The rollforward's notion of 'expected' must be derived from the same
    statements install() runs, never a hand-kept list."""
    expected = MutationCatalog.expected_schema_object_names()
    assert INDEX in expected
    assert "fleet_sync_catalog" in expected and "fleet_sync_state" in expected
    assert not any(n.startswith("sqlite_") for n in expected)
