"""The retired ``entities`` / ``entity_mentions`` tables leave cleanly.

They were a regex index over thought and derivation text that nothing read,
and they could not replicate: ``upsert_entity`` deduped on the UNIQUE
``canonical_name`` while minting the replication key ``id`` at random, so two
machines that matched the same string produced rows distinct under the key and
identical under the constraint. The colliding INSERT failed and materialize
aborted the whole batch, so the watermark never advanced and the batch replayed
every 24 seconds. One such row stalled a real scope for a day.

Three properties have to hold for the removal to be safe on a live fleet:
the drop must not strand catalog rows, the digest must not move a second time,
and a uniqueness collision must never again be able to abort a batch.
"""

from pathlib import Path
import sqlite3

import pytest

from tools.graph.db import GraphDB
from tools.network.fleet_sync.catalog import (
    _address_prefix,
    purge_retired_catalog_addresses,
)
from tools.network.fleet_sync.codec import encode_value
from tools.network.fleet_sync.compaction import WatermarkError
from tools.network.fleet_sync.materialize import SecondaryIdentityConflict
from tools.network.fleet_sync.policies import (
    RETIRED_LOGICAL_TABLES,
    PolicyKind,
    TABLE_POLICIES,
    compatibility_digest,
)


LEGACY_ENTITY_DDL = """
CREATE TABLE entities (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    canonical_name  TEXT NOT NULL UNIQUE,
    type            TEXT DEFAULT 'concept',
    description     TEXT,
    metadata        TEXT DEFAULT '{}',
    created_at      TEXT NOT NULL DEFAULT '2026-09-01T00:00:00Z'
);
CREATE TABLE entity_mentions (
    entity_id   TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    content_id  TEXT NOT NULL,
    content_type TEXT NOT NULL,
    count       INTEGER DEFAULT 1,
    PRIMARY KEY (entity_id, content_id)
);
"""


def test_fresh_schema_has_no_entity_tables(tmp_path: Path) -> None:
    db = GraphDB(tmp_path / "personal.db")
    try:
        tables = {
            r[0] for r in db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        db.close()
    assert not (tables & RETIRED_LOGICAL_TABLES)


def test_policy_entries_survive_the_drop(tmp_path: Path) -> None:
    """Retired tables keep a DERIVED policy on purpose.

    An unmigrated store still classifies under audit_schema, a legacy peer's
    frame gets a typed refusal rather than an unknown-table error, and the
    compatibility digest -- which folds the policy inventory and skips DERIVED
    shapes -- stays fixed across the drop.
    """
    for table in RETIRED_LOGICAL_TABLES:
        assert TABLE_POLICIES[table].kind is PolicyKind.DERIVED

    db = GraphDB(tmp_path / "personal.db")
    try:
        after = compatibility_digest(db.conn)
        db.conn.executescript(LEGACY_ENTITY_DDL)
        # Re-derive: the memo is keyed on PRAGMA schema_version, which the DDL
        # above just bumped, so this is a real recomputation.
        before = compatibility_digest(db.conn)
    finally:
        db.close()
    assert before == after, "resurrecting the tables must not move the digest"


def test_migration_drops_tables_and_purges_catalog_addresses(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "personal.db"
    db = GraphDB(db_path)
    try:
        # A store that predates the drop: the tables exist and the winner
        # catalog cites rows in them, exactly as a live machine's does.
        db.conn.executescript(LEGACY_ENTITY_DDL)
        db.conn.execute(
            "INSERT INTO entities(id,name,canonical_name) VALUES('e1','X','x')"
        )
        db.conn.execute(
            "CREATE TABLE IF NOT EXISTS fleet_sync_catalog("
            "address BLOB PRIMARY KEY, timestamp_ns INTEGER NOT NULL,"
            "tombstone INTEGER NOT NULL, transaction_ref INTEGER NOT NULL,"
            "operation_index INTEGER NOT NULL) WITHOUT ROWID"
        )
        keep = encode_value(["thoughts", ["t1"]])
        for address in (
            encode_value(["entities", ["e1"]]),
            encode_value(["entity_mentions", ["e1", "t1"]]),
            keep,
        ):
            db.conn.execute(
                "INSERT INTO fleet_sync_catalog VALUES(?,1,0,1,0)", (address,)
            )
        db.conn.commit()

        db.drop_retired_entity_tables()

        tables = {
            r[0] for r in db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert not (tables & RETIRED_LOGICAL_TABLES)
        remaining = [
            bytes(r[0]) for r in db.conn.execute(
                "SELECT address FROM fleet_sync_catalog"
            )
        ]
        assert remaining == [keep], "purge must spare unrelated addresses"
    finally:
        db.close()


def test_migration_is_a_no_op_on_a_migrated_store(tmp_path: Path) -> None:
    db = GraphDB(tmp_path / "personal.db")
    try:
        db.drop_retired_entity_tables()
        db.drop_retired_entity_tables()
    finally:
        db.close()


def test_purge_refuses_a_table_that_is_not_retired(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "x.db")
    try:
        with pytest.raises(WatermarkError, match="not retired"):
            purge_retired_catalog_addresses(conn, ["thoughts"])
    finally:
        conn.close()


def test_address_prefix_excludes_same_stemmed_tables() -> None:
    """The purge range is exact, not a substring match.

    ``entities`` must not sweep away a hypothetical ``entities_archive``: the
    encoded length prefix differs, so the half-open range cannot reach it.
    """
    low = _address_prefix("entities")
    high = low[:-1] + bytes([low[-1] + 1])
    assert low <= encode_value(["entities", ["e1"]]) < high
    assert not (low <= encode_value(["entities_archive", ["e1"]]) < high)
    assert not (low <= encode_value(["thoughts", ["t1"]]) < high)


def test_uniqueness_collision_skips_one_row_not_the_batch(
    tmp_path: Path,
) -> None:
    """The defect class that stalled the fleet, reproduced against any table.

    A row that is new under the replication key but collides with a local row
    on another UNIQUE column can never be realized here. It must be skipped and
    reported, so the surrounding transactions still apply and the watermark
    advances. Before the fix the collision aborted the whole batch and the same
    batch replayed forever.
    """
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE tags(name TEXT PRIMARY KEY, description TEXT, "
        "created_at TEXT)"
    )
    conn.execute("CREATE UNIQUE INDEX tags_desc ON tags(description)")
    conn.execute("INSERT INTO tags VALUES('local','shared','2026-01-01')")
    conn.commit()

    from tools.network.fleet_sync.materialize import _upsert
    from tools.network.fleet_sync.codec import Mutation

    colliding = Mutation(
        "tags", ("remote",), 1, False,
        (("created_at", "2026-01-02"), ("description", "shared")),
    )
    with pytest.raises(SecondaryIdentityConflict):
        _upsert(conn, colliding, {
            "name": "remote", "description": "shared",
            "created_at": "2026-01-02",
        })
    # The failed statement leaves the connection usable: that is what lets the
    # rest of the batch land.
    conn.execute("INSERT INTO tags VALUES('after','distinct','2026-01-03')")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0] == 2
    conn.close()


def test_activated_store_survives_the_drop(tmp_path: Path) -> None:
    """The live deploy path: an activated store with journaled entity rows.

    Home and the San Jose machine both carry an activated catalog whose
    triggers captured tens of thousands of entity rows. Dropping the tables
    without purging their addresses leaves the winner catalog citing rows that
    no longer exist, and _verify_catalog_integrity refuses every later open.
    """
    from tools.network.fleet_sync.catalog import MutationCatalog

    db_path = tmp_path / "personal.db"
    db = GraphDB(db_path)
    try:
        # Stand the store up as production does, then reintroduce the legacy
        # tables and let the capture triggers journal rows in them.
        db.conn.executescript(LEGACY_ENTITY_DDL)
        db.activate_fleet_sync_writers("aa" * 32)
        # Capture is trigger-driven, so an ordinary insert is journaled.
        db.conn.execute(
            "INSERT INTO sources(id,type,title,metadata,created_at,"
            "ingested_at) VALUES('s1','note','Real content','{}',"
            "'2026-09-01T00:00:00Z','2026-09-01T00:00:00Z')"
        )
        db.conn.commit()

        catalog = MutationCatalog(db.conn, "aa" * 32)
        # A pre-drop store may or may not have journaled entities depending on
        # when its triggers were installed; plant the addresses either way so
        # the purge has something to prove.
        for address in (
            encode_value(["entities", ["e1"]]),
            encode_value(["entity_mentions", ["e1", "t1"]]),
        ):
            db.conn.execute(
                "INSERT OR REPLACE INTO fleet_sync_catalog "
                "SELECT ?, timestamp_ns, tombstone, transaction_ref, "
                "operation_index FROM fleet_sync_catalog LIMIT 1",
                (address,),
            )
        db.conn.commit()
        with pytest.raises(WatermarkError, match="non-replicated table"):
            catalog._verify_catalog_integrity()

        db.drop_retired_entity_tables()

        live_rows, catalog_rows = catalog._verify_catalog_integrity()
        assert live_rows == catalog_rows == 1
    finally:
        db.close()


def test_reclassifying_a_live_table_removes_its_capture_triggers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trigger install must drop the DDL of tables it will not re-create.

    Reclassifying a live table to DERIVED (or LOCAL) leaves its capture
    triggers behind otherwise, and the schema check then never converges:
    every open raises "trigger refresh incomplete".
    """
    from tools.network.fleet_sync.catalog import MutationCatalog
    from tools.network.fleet_sync import policies as policies_module

    db = GraphDB(tmp_path / "personal.db")
    try:
        db.activate_fleet_sync_writers("bb" * 32)
        catalog = MutationCatalog(db.conn, "bb" * 32)
        assert db.conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'fleet_sync_claims_%'"
        ).fetchone()[0] == 3

        demoted = dict(policies_module.TABLE_POLICIES)
        demoted["claims"] = TABLE_POLICIES["claims"].__class__(
            "claims", PolicyKind.DERIVED, ("id",), ("metadata",),
        )
        monkeypatch.setattr(policies_module, "TABLE_POLICIES", demoted)
        import tools.network.fleet_sync.catalog as catalog_module
        monkeypatch.setattr(catalog_module, "TABLE_POLICIES", demoted)

        catalog.refresh_triggers_for_current_schema()
        assert db.conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'fleet_sync_claims_%'"
        ).fetchone()[0] == 0
    finally:
        db.close()
