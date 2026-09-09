from pathlib import Path
import sqlite3

import pytest

from tools.graph.db import GraphDB
from tools.network.fleet_sync.catalog import (
    CATALOG_SCHEMA_VERSION,
    MutationCatalog,
)
from tools.network.fleet_sync.compaction import WatermarkError
from tools.network.fleet_sync.policies import audit_schema


ORIGIN = "a" * 64


def _insert_source(conn: sqlite3.Connection, identity: str) -> None:
    conn.execute(
        "INSERT INTO sources(id,type,title,metadata,created_at,ingested_at) "
        "VALUES(?,?,?,?,?,?)",
        (
            identity,
            "note",
            identity,
            "{}",
            "2026-08-21T00:00:00Z",
            "2026-08-21T00:00:00Z",
        ),
    )


def _fleet_objects(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE (type='table' OR type='index' OR type='trigger') "
            "AND name LIKE 'fleet_sync_%'"
        )
    }


def test_personal_catalog_migration_bootstraps_once_and_remains_writable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "personal.db"
    db = GraphDB(path)
    try:
        _insert_source(db.conn, "before-migration")
        db.conn.commit()
        first = db.migrate_fleet_sync_catalog(ORIGIN)

        assert first.schema_created
        assert not first.schema_upgraded
        assert first.bootstrapped_rows > 0
        assert first.live_rows == first.catalog_rows
        assert not first.triggers_active
        state = db.conn.execute(
            "SELECT schema_version,origin_incarnation FROM fleet_sync_state"
        ).fetchone()
        assert tuple(state) == (CATALOG_SCHEMA_VERSION, ORIGIN)
        assert not db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'fleet_sync_%'"
        ).fetchall()

        second_startup = GraphDB(path)
        try:
            no_op = second_startup.migrate_fleet_sync_catalog(ORIGIN)
            assert not no_op.schema_created
            assert not no_op.schema_upgraded
            assert no_op.bootstrapped_rows == 0
            assert no_op.live_rows == no_op.catalog_rows
            assert second_startup.conn.execute(
                "SELECT bootstrap_generation FROM fleet_sync_state"
            ).fetchone()[0] == 1
        finally:
            second_startup.close()

        # Writer conversion is the next rollout.  This migration must not
        # break today's direct writers while that work is still outstanding.
        _insert_source(db.conn, "between-rollouts")
        db.conn.commit()
        second = db.migrate_fleet_sync_catalog(ORIGIN)
        assert not second.schema_created
        assert not second.schema_upgraded
        assert second.bootstrapped_rows == 1
        assert second.live_rows == second.catalog_rows
        assert db.conn.execute(
            "SELECT bootstrap_generation FROM fleet_sync_state"
        ).fetchone()[0] == 2
        bootstrap_ids = {
            str(row[0])
            for row in db.conn.execute(
                "SELECT transaction_id FROM fleet_sync_transactions "
                "WHERE transaction_id LIKE 'bootstrap-v1:%'"
            )
        }
        assert any(value.startswith("bootstrap-v1:1:") for value in bootstrap_ids)
        assert any(value.startswith("bootstrap-v1:2:") for value in bootstrap_ids)
    finally:
        db.close()

    reopened = GraphDB(path)
    try:
        # The logical-key index uses fleet_sha256_text(content).  A normal
        # post-migration GraphDB connection must register that deterministic
        # function before any note-version writer needs to maintain the index.
        reopened.insert_note_version(
            "before-migration", 1, "written after a clean reopen"
        )
        reopened.conn.commit()
        third = reopened.migrate_fleet_sync_catalog(ORIGIN)
        assert not third.schema_created
        assert not third.schema_upgraded
        assert third.bootstrapped_rows == 1
        assert third.live_rows == third.catalog_rows
        assert reopened.conn.execute(
            "SELECT bootstrap_generation FROM fleet_sync_state"
        ).fetchone()[0] == 3

        fourth = reopened.migrate_fleet_sync_catalog(ORIGIN)
        assert fourth.bootstrapped_rows == 0
        assert fourth.live_rows == fourth.catalog_rows
        assert reopened.conn.execute(
            "SELECT bootstrap_generation FROM fleet_sync_state"
        ).fetchone()[0] == 3
    finally:
        reopened.close()


def test_catalog_migration_includes_identity_settings(tmp_path: Path) -> None:
    db = GraphDB(tmp_path / "personal.db")
    try:
        for row_id, set_id in (
            ("ordinary", "autonomy.example"),
            ("tunnel-server", "autonomy.fleet.tunnel-server"),
            ("root", "autonomy.identity.personal"),
            ("passkey", "autonomy.identity.passkey"),
        ):
            db.conn.execute(
                "INSERT INTO settings(id,set_id,schema_revision,key,payload,"
                "publication_state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    row_id,
                    set_id,
                    1,
                    "one",
                    '{"secret":"not logged"}',
                    "raw",
                    "2026-08-21T00:00:00Z",
                    "2026-08-21T00:00:00Z",
                ),
            )
        db.conn.commit()
        db.migrate_fleet_sync_catalog(ORIGIN)
        catalog = MutationCatalog(db.conn, ORIGIN)
        setting_sets = {
            dict(item.mutation.values)["set_id"]
            for item in catalog.iter_mutations()
            if item.mutation.table == "settings"
        }
        assert "autonomy.example" in setting_sets
        assert "autonomy.fleet.tunnel-server" in setting_sets
        assert "autonomy.identity.personal" in setting_sets
        assert "autonomy.identity.passkey" in setting_sets
    finally:
        db.close()


def test_unknown_durable_table_fails_before_catalog_ddl(tmp_path: Path) -> None:
    db = GraphDB(tmp_path / "personal.db")
    try:
        db.conn.execute("CREATE TABLE unknown_personal_state(id TEXT PRIMARY KEY)")
        db.conn.commit()
        with pytest.raises(ValueError, match="unknown_personal_state"):
            db.migrate_fleet_sync_catalog(ORIGIN)
        assert not _fleet_objects(db.conn)
    finally:
        db.close()


def test_invalid_machine_public_key_fails_before_catalog_ddl(tmp_path: Path) -> None:
    db = GraphDB(tmp_path / "personal.db")
    try:
        with pytest.raises(WatermarkError, match="machine public key"):
            db.migrate_fleet_sync_catalog("machine-a")
        assert not _fleet_objects(db.conn)
    finally:
        db.close()


def test_catalog_migration_failure_rolls_back_all_schema_and_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = GraphDB(tmp_path / "personal.db")
    try:
        _insert_source(db.conn, "survives")
        db.conn.commit()

        def fail_after_schema(_self: MutationCatalog) -> int:
            raise RuntimeError("injected bootstrap failure")

        monkeypatch.setattr(
            MutationCatalog, "_bootstrap_existing_rows", fail_after_schema
        )
        with pytest.raises(RuntimeError, match="injected bootstrap failure"):
            db.migrate_fleet_sync_catalog(ORIGIN)

        assert db.conn.execute(
            "SELECT title FROM sources WHERE id='survives'"
        ).fetchone()[0] == "survives"
        assert not _fleet_objects(db.conn)
        assert not db.conn.in_transaction
    finally:
        db.close()


def test_catalog_migration_refuses_partial_capture_activation(
    tmp_path: Path,
) -> None:
    db = GraphDB(tmp_path / "personal.db")
    try:
        db.conn.execute(
            "CREATE TRIGGER fleet_sync_partial AFTER INSERT ON sources "
            "BEGIN SELECT 1; END"
        )
        db.conn.commit()
        before = _fleet_objects(db.conn)
        with pytest.raises(WatermarkError, match="capture triggers present"):
            db.migrate_fleet_sync_catalog(ORIGIN)
        assert _fleet_objects(db.conn) == before
    finally:
        db.close()


def test_catalog_migration_refuses_an_unrecorded_delete_before_activation(
    tmp_path: Path,
) -> None:
    db = GraphDB(tmp_path / "personal.db")
    try:
        _insert_source(db.conn, "deleted-between-rollouts")
        db.conn.commit()
        db.migrate_fleet_sync_catalog(ORIGIN)

        db.conn.execute("DELETE FROM sources WHERE id='deleted-between-rollouts'")
        db.conn.commit()
        with pytest.raises(WatermarkError, match="missing live row"):
            db.migrate_fleet_sync_catalog(ORIGIN)
        assert not db.conn.in_transaction
    finally:
        db.close()


def test_v2_catalog_upgrades_peer_state_without_rewriting_winners(
    tmp_path: Path,
) -> None:
    db = GraphDB(tmp_path / "personal.db")
    try:
        catalog = MutationCatalog(db.conn, ORIGIN)
        db.conn.execute("BEGIN IMMEDIATE")
        catalog._create_schema_objects()
        catalog._ensure_state()
        db.conn.execute("UPDATE fleet_sync_state SET schema_version=2")
        db.conn.execute("DROP INDEX idx_fleet_sync_peer_state_online")
        db.conn.execute("DROP TABLE fleet_sync_peer_state")
        db.conn.commit()

        report = db.migrate_fleet_sync_catalog(ORIGIN)
        assert report.schema_created
        assert report.schema_upgraded
        assert db.conn.execute(
            "SELECT schema_version FROM fleet_sync_state"
        ).fetchone()[0] == CATALOG_SCHEMA_VERSION
        assert "fleet_sync_peer_state" in _fleet_objects(db.conn)
    finally:
        db.close()


def test_reconcile_backfills_untracked_rows_with_triggers_active(
    tmp_path: Path,
) -> None:
    """A catalog left incomplete by a buggy bootstrap is repaired in place.

    Reproduces the production failure: after writers are activated (capture
    triggers installed), some live rows are missing winner metadata -- as the
    SQLite < 3.38 RETURNING-on-upsert gap left them -- so a serve fails
    closed and migration can no longer help (it early-skips / rolls back once
    triggers exist). reconcile_catalog() backfills exactly the missing rows,
    additively and with triggers intact.
    """
    path = tmp_path / "personal.db"
    db = GraphDB(path)
    try:
        for index in range(5):
            _insert_source(db.conn, f"src-{index}")
        db.conn.commit()
        db.migrate_fleet_sync_catalog(ORIGIN)
        db.activate_fleet_sync_writers(ORIGIN)

        assert db.conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'fleet_sync_%'"
        ).fetchone()[0] > 0

        full = db.conn.execute(
            "SELECT COUNT(*) FROM fleet_sync_catalog WHERE tombstone=0"
        ).fetchone()[0]
        # Simulate the buggy bootstrap silently dropping live-row metadata:
        # remove two catalog rows whose live rows still exist.
        db.conn.execute(
            "DELETE FROM fleet_sync_catalog WHERE address IN "
            "(SELECT address FROM fleet_sync_catalog WHERE tombstone=0 LIMIT 2)"
        )
        db.conn.commit()
        assert db.conn.execute(
            "SELECT COUNT(*) FROM fleet_sync_catalog WHERE tombstone=0"
        ).fetchone()[0] == full - 2

        # Migration cannot repair a triggered catalog: it rolls back and raises.
        with pytest.raises(WatermarkError):
            db.migrate_fleet_sync_catalog(ORIGIN)

        report = db.reconcile_fleet_sync_catalog(ORIGIN)
        assert report.bootstrapped_rows == 2
        assert report.live_rows == report.catalog_rows
        assert report.triggers_active
        assert db.conn.execute(
            "SELECT COUNT(*) FROM fleet_sync_catalog WHERE tombstone=0"
        ).fetchone()[0] == full

        # Idempotent: a second pass finds nothing untracked.
        again = db.reconcile_fleet_sync_catalog(ORIGIN)
        assert again.bootstrapped_rows == 0
        assert again.live_rows == again.catalog_rows
        assert again.triggers_active
    finally:
        db.close()


def test_snapshot_skips_deprecated_duplicate_settings_base_rows(
    tmp_path: Path,
) -> None:
    """Deprecated superseded base rows are not streamed as separate records.

    A settings store that accumulated duplicate base rows keeps only the newest
    live per natural key; the platform DEPRECATES the rest (GraphDB open heal +
    partial unique indexes gated on ``deprecated = 0``). Those deprecated rows
    share the single 'base' catalog address with the live winner, so the base
    snapshot must skip them -- otherwise base.total_records exceeds the
    catalog's one-per-address count and the serve fails closed
    (untracked logical rows). Override and
    exclusion rows keep their own per-id addresses and all stream.
    """
    from tools.network.fleet_sync.streaming import (
        iter_indexed_snapshot_mutations,
    )

    db = GraphDB(tmp_path / "personal.db")
    try:
        def _setting(ident: str, created_at: str, payload: str, *,
                     supersedes: str | None = None, deprecated: int = 0) -> None:
            db.conn.execute(
                "INSERT INTO settings(id,set_id,schema_revision,key,payload,"
                "publication_state,supersedes,deprecated,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (ident, "app.flags", 1, "voice", payload, "canonical",
                 supersedes, deprecated, created_at, created_at),
            )

        # One live base row plus two DEPRECATED superseded base rows for the same
        # natural key -- exactly the shape the open heal leaves behind. The
        # partial unique index (WHERE deprecated = 0) permits the deprecated
        # siblings; they share the live winner's 'base' address.
        _setting("base-live", "2026-01-02T00:00:00Z", '{"on": true}')
        _setting("base-old-a", "2026-01-01T00:00:00Z", '{"on": false}', deprecated=1)
        _setting("base-old-c", "2026-01-01T12:00:00Z", '{"on": null}', deprecated=1)
        # An override patch for the same key keeps its own per-id address.
        _setting("ovr-1", "2026-01-03T00:00:00Z", '{"on": true}',
                 supersedes="base-live")
        db.conn.commit()

        settings_muts = [
            m for m in iter_indexed_snapshot_mutations(db.conn)
            if m.table == "settings"
        ]

        # Only the live base + the override stream: 2, not 4.
        assert len(settings_muts) == 2
        # Every emitted address is unique -- the exact invariant the serve
        # asserts (base.total_records == one-per-address catalog count).
        addresses = [m.address for m in settings_muts]
        assert len(set(addresses)) == len(addresses)
        assert sorted(a[-1] for a in addresses) == [
            "base", "supersedes:base-live:ovr-1"
        ]
        # The surviving base row is the live one, not a deprecated sibling.
        base = next(m for m in settings_muts if m.address[-1] == "base")
        assert dict(base.values)["payload"] == {"on": True}
    finally:
        db.close()
