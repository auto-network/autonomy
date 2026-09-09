"""schema.sql is the complete current shape, and a new install proves it.

The 2026-09-08 outage was a migration that could not complete taking every
writable open with it. The deeper defect underneath was that "is this
database current?" was answered by a hand-maintained integer, and that
schema.sql was NOT the current shape -- a fresh install was correct only
because it also ran fourteen historical migrations, one of which created
sources_fts and two of which added the columns it indexes.

These tests make both properties structural rather than remembered.
"""

from pathlib import Path
import sqlite3

from tools.graph import db as dbmod
from tools.graph.db import (
    GraphDB,
    _SCHEMA_USER_VERSION,
    schema_fingerprint,
    schema_is_current,
)


def _shape(conn) -> tuple[set, dict]:
    objects = {
        (str(t), str(n)) for t, n, _sql in conn.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        )
    }
    columns = {}
    for _t, name in [o for o in objects if o[0] == "table"]:
        try:
            columns[name] = {str(r[1]) for r in conn.execute(f'PRAGMA table_info("{name}")')}
        except sqlite3.Error:
            pass
    return objects, columns


def test_schema_sql_alone_equals_schema_plus_every_migration(tmp_path: Path) -> None:
    """THE GUARD. A migration that creates something schema.sql does not
    fails here, at the moment it is written, instead of shipping a new
    install that is missing an index nobody notices until a query is slow or
    a uniqueness constraint does not hold."""
    fresh = GraphDB(tmp_path / "fresh.db")
    try:
        fresh_objects, fresh_columns = _shape(fresh.conn)
    finally:
        fresh.close()

    migrated = GraphDB(tmp_path / "migrated.db")
    try:
        for _version, name in dbmod._SCHEMA_PHASE_MIGRATIONS:
            getattr(migrated, name)()
        migrated_objects, migrated_columns = _shape(migrated.conn)
    finally:
        migrated.close()

    missing = sorted(migrated_objects - fresh_objects)
    assert not missing, (
        "these objects exist only after running migrations, so a new install "
        f"does not get them: {missing}. Add them to schema.sql."
    )
    for table, expected in migrated_columns.items():
        absent = sorted(expected - fresh_columns.get(table, set()))
        assert not absent, (
            f"{table} is missing {absent} on a fresh install; a migration adds "
            "them and schema.sql does not."
        )


def test_a_new_install_runs_no_migrations(tmp_path: Path, monkeypatch) -> None:
    """Structurally impossible for a new install to think it needs updating.

    Not 'the migrations happen to no-op' -- they are never called at all, and
    the check is emptiness of the database, not equality of an integer.
    """
    called: list[str] = []
    for _version, name in dbmod._SCHEMA_PHASE_MIGRATIONS:
        original = getattr(GraphDB, name)

        def wrapper(self, *a, _n=name, _o=original, **k):
            called.append(_n)
            return _o(self, *a, **k)

        monkeypatch.setattr(GraphDB, name, wrapper)

    db = GraphDB(tmp_path / "new.db")
    try:
        version = db.conn.execute("PRAGMA user_version").fetchone()[0]
        assert schema_is_current(db.conn)
    finally:
        db.close()

    assert called == [], f"a brand new install ran migrations: {called}"
    assert version == _SCHEMA_USER_VERSION


def test_an_older_store_still_migrates(tmp_path: Path) -> None:
    """The gate must not become 'never migrate anything'."""
    path = tmp_path / "old.db"
    GraphDB(path).close()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 5")
    conn.commit()
    conn.close()

    db = GraphDB(path)
    try:
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == _SCHEMA_USER_VERSION
        assert schema_is_current(db.conn)
    finally:
        db.close()


def test_a_missing_object_is_repaired_even_when_the_stamp_says_current(
    tmp_path: Path,
) -> None:
    """The forgotten-bump case, which the integer alone could never catch.

    A store whose stamp equals the constant but whose shape has drifted is
    NOT current, and saying so is the whole point of asking the database.
    """
    path = tmp_path / "drift.db"
    GraphDB(path).close()

    conn = sqlite3.connect(path)
    conn.execute("DROP INDEX idx_sources_type")
    conn.commit()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == _SCHEMA_USER_VERSION
    assert not schema_is_current(conn)
    conn.close()

    db = GraphDB(path)
    try:
        assert schema_is_current(db.conn), "the open did not repair the drift"
    finally:
        db.close()


def test_the_fingerprint_is_derived_and_stable() -> None:
    """It is a hash OF schema.sql's product, not a value restated beside it,
    so it cannot disagree with the file and cannot be forgotten."""
    first = schema_fingerprint()
    assert first == schema_fingerprint()
    assert len(first) == 64 and int(first, 16) >= 0


def test_the_retired_backfill_is_not_in_the_migration_chain() -> None:
    """It is not a valid migration: not deterministic (it writes a
    machine-specific persona), no completion condition (new NULLs keep
    arriving), and its trigger was unrelated to its purpose. It remains
    callable deliberately."""
    names = [name for _version, name in dbmod._SCHEMA_PHASE_MIGRATIONS]
    assert "backfill_content_persona" not in names
    assert "_backfill_content_persona" not in names
    assert callable(GraphDB.backfill_content_persona)
