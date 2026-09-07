from pathlib import Path
import sqlite3

import pytest
import tools.network.fleet_sync.catalog as catalog_module

from tools.graph.db import GraphDB
from tools.network.fleet_sync.catalog import MutationCatalog
from tools.network.fleet_sync.codec import Mutation
from tools.network.fleet_sync.compaction import AuthoredMutation, WatermarkError
from tools.network.fleet_sync.policies import PolicyKind, TABLE_POLICIES


def _insert_source(conn: sqlite3.Connection, identity: str, title: str) -> None:
    conn.execute(
        "INSERT INTO sources(id,type,title,metadata,created_at,ingested_at) "
        "VALUES(?,?,?,?,?,?)",
        (identity, "note", title, "{}", "2026-08-19T00:00:00Z",
         "2026-08-19T00:00:00Z"),
    )


def _source(identity: str, title: str, timestamp: int) -> Mutation:
    return Mutation(
        "sources", (identity,), timestamp, False,
        (("created_at", "2026-08-19T00:00:00Z"), ("deprecated", 0),
         ("id", identity), ("ingested_at", "2026-08-19T00:00:00Z"),
         ("keywords", None), ("last_activity_at", None), ("metadata", {}),
         ("moved_to_org", None), ("persona_id", None),
         ("platform", None),
         ("publication_state", "curated"), ("session_id", None),
         ("short_description", None),
         ("successor_id", None), ("title", title), ("type", "note"),
         ("url", None)),
    )


def _insert_setting(
    conn: sqlite3.Connection, *, identity: str, set_id: str, key: str,
    payload: str, deprecated: int, schema_revision: int = 1,
    publication_state: str = "raw",
) -> None:
    conn.execute(
        "INSERT INTO settings(id,set_id,schema_revision,key,payload,"
        "publication_state,deprecated,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (identity, set_id, schema_revision, key, payload, publication_state,
         deprecated, "2026-08-19T00:00:00Z", "2026-08-19T00:00:00Z"),
    )


def test_live_row_settings_base_resolves_the_deprecated_zero_winner(
    tmp_path: Path,
) -> None:
    """Superseded base rows accumulate as deprecated history but collapse to one
    logical settings address. ``_live_row`` must resolve the sole
    ``deprecated = 0`` winner, not an arbitrary deprecated sibling — the base
    snapshot filters to ``deprecated = 0`` with the same predicate, so if this
    resolver (which builds the winner catalog's candidate hash) picked a
    different physical row the checkpoint would fail install with a winner/base
    hash mismatch.
    """
    db = GraphDB(tmp_path / "personal.db")
    try:
        # Deprecated siblings first (lower rowids), so an unordered fetchone()
        # would return one of them; the live winner is inserted last.
        _insert_setting(db.conn, identity="dep-a", set_id="dashboard.x",
                        key="default", payload='{"n":1}', deprecated=1)
        _insert_setting(db.conn, identity="dep-b", set_id="dashboard.x",
                        key="default", payload='{"n":2}', deprecated=1)
        _insert_setting(db.conn, identity="live", set_id="dashboard.x",
                        key="default", payload='{"n":3}', deprecated=0)
        db.conn.commit()
        address = ("dashboard.x", 1, "default", "raw", "base")
        row = MutationCatalog._live_row(db.conn, "settings", address)
        assert row["id"] == "live"
        assert row["deprecated"] == 0
    finally:
        db.close()


def test_replicating_writes_require_an_authored_transaction(tmp_path: Path) -> None:
    db = GraphDB(tmp_path / "personal.db")
    try:
        catalog = MutationCatalog(db.conn, "machine-a")
        catalog.install()
        with pytest.raises(sqlite3.OperationalError, match="user-defined function"):
            _insert_source(db.conn, "s1", "not attributed")
        db.conn.rollback()
        assert db.conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0

        with pytest.raises(sqlite3.OperationalError, match="user-defined function"):
            db.conn.execute(
                "INSERT INTO settings(id,set_id,schema_revision,key,payload,"
                "publication_state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                ("root", "autonomy.identity.personal", 1, "root", "{}", "raw",
                 "2026-08-19T00:00:00Z", "2026-08-19T00:00:00Z"),
            )
        db.conn.rollback()
        with catalog.transaction(11, "tx-root"):
            db.conn.execute(
                "INSERT INTO settings(id,set_id,schema_revision,key,payload,"
                "publication_state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                ("root", "autonomy.identity.personal", 1, "root", "{}", "raw",
                 "2026-08-19T00:00:00Z", "2026-08-19T00:00:00Z"),
            )
        identity = list(catalog.iter_mutations())
        assert len(identity) == 1
        assert identity[0].mutation.address[0] == "autonomy.identity.personal"
    finally:
        db.close()


def test_catalog_installs_hooks_for_every_replicated_table(tmp_path: Path) -> None:
    db = GraphDB(tmp_path / "personal.db")
    try:
        catalog = MutationCatalog(db.conn, "machine-a")
        catalog.install()
        actual = {
            str(row[0]) for row in db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND name LIKE 'fleet_sync_%'"
            )
        }
        replicated = {
            table for table, policy in TABLE_POLICIES.items()
            if policy.kind not in {PolicyKind.LOCAL, PolicyKind.DERIVED}
        }
        expected = {
            f"fleet_sync_{table}_{operation}"
            for table in replicated for operation in ("insert", "update", "delete")
        }
        assert actual == expected
    finally:
        db.close()


def test_local_write_delete_and_rollback_are_atomic(tmp_path: Path) -> None:
    db = GraphDB(tmp_path / "personal.db")
    try:
        catalog = MutationCatalog(db.conn, "machine-a")
        catalog.install()
        with catalog.transaction(10, "tx-1"):
            _insert_source(db.conn, "s1", "first")
        item = list(catalog.iter_mutations())[0]
        assert (item.origin_incarnation, item.transaction_id) == ("machine-a", "tx-1")
        assert item.mutation.values

        with pytest.raises(RuntimeError, match="abort"):
            with catalog.transaction(11, "tx-abort"):
                db.conn.execute("DELETE FROM sources WHERE id='s1'")
                raise RuntimeError("abort")
        assert db.conn.execute("SELECT title FROM sources").fetchone()[0] == "first"
        assert not list(catalog.iter_mutations())[0].mutation.tombstone

        with catalog.transaction(12, "tx-2"):
            db.conn.execute("DELETE FROM sources WHERE id='s1'")
        deleted = list(catalog.iter_mutations())[0]
        assert deleted.mutation.tombstone
        assert deleted.mutation.timestamp_ns == 12
    finally:
        db.close()


def test_deprecated_settings_base_is_an_idempotent_tombstone(
    tmp_path: Path,
) -> None:
    source = GraphDB(tmp_path / "source.db")
    target = GraphDB(tmp_path / "target.db")
    try:
        source_catalog = MutationCatalog(source.conn, "machine-a")
        target_catalog = MutationCatalog(target.conn, "machine-b")
        source_catalog.install()
        target_catalog.install()
        with source_catalog.transaction(10, "create"):
            _insert_setting(
                source.conn,
                identity="setting-a",
                set_id="dashboard.example",
                key="default",
                payload='{"enabled":true}',
                deprecated=0,
            )
        created = list(source_catalog.iter_journal(after_watermark=-1))
        assert target_catalog.apply_remote_batch(created) == (1, 0)

        with source_catalog.transaction(20, "deprecate"):
            source.conn.execute(
                "UPDATE settings SET deprecated=1 WHERE id='setting-a'"
            )
        retired = list(source_catalog.iter_journal(after_watermark=10))
        assert len(retired) == 1
        assert retired[0].mutation.address == (
            "dashboard.example", 1, "default", "raw", "base",
        )
        assert retired[0].mutation.tombstone

        assert target_catalog.apply_remote_batch(retired) == (1, 0)
        assert target.conn.execute(
            "SELECT COUNT(*) FROM settings WHERE deprecated=0"
        ).fetchone()[0] == 0
        assert target_catalog.apply_remote_batch(retired) == (0, 1)
    finally:
        source.close()
        target.close()


def test_reconcile_repairs_legacy_live_encoding_of_deprecated_base(
    tmp_path: Path,
) -> None:
    db = GraphDB(tmp_path / "personal.db")
    try:
        catalog = MutationCatalog(db.conn, "a" * 64)
        catalog.install()
        with catalog.transaction(10, "create"):
            _insert_setting(
                db.conn,
                identity="retired",
                set_id="dashboard.example",
                key="legacy",
                payload='{"enabled":false}',
                deprecated=0,
            )
        with catalog.transaction(20_000, "deprecate"):
            db.conn.execute(
                "UPDATE settings SET deprecated=1, "
                "updated_at='1970-01-01T00:00:00.000020Z' "
                "WHERE id='retired'"
            )

        address_blob, transaction_ref, operation_index = db.conn.execute(
            "SELECT address,transaction_ref,operation_index "
            "FROM fleet_sync_catalog WHERE tombstone=1"
        ).fetchone()
        row = dict(db.conn.execute(
            "SELECT * FROM settings WHERE id='retired'"
        ).fetchone())
        legacy = Mutation(
            "settings",
            ("dashboard.example", 1, "legacy", "raw", "base"),
            20_000,
            False,
            catalog_module._logical_values(TABLE_POLICIES["settings"], row),
        )
        db.conn.execute(
            "UPDATE fleet_sync_catalog SET tombstone=0 WHERE address=?",
            (address_blob,),
        )
        db.conn.commit()
        del legacy  # the legacy frame lived in the journal, which is gone

        catalog.reconcile_catalog(audit=False)
        repaired = list(catalog.iter_journal(after_watermark=10))
        assert len(repaired) == 1
        assert repaired[0].mutation.tombstone
        assert db.conn.execute(
            "SELECT tombstone FROM fleet_sync_catalog WHERE address=?",
            (address_blob,),
        ).fetchone()[0] == 1
    finally:
        db.close()


def test_frozen_cut_is_a_coherent_wal_snapshot_and_advances_floor(
    tmp_path: Path,
) -> None:
    db = GraphDB(tmp_path / "personal.db")
    try:
        catalog = MutationCatalog(db.conn, "machine-a")
        catalog.install()
        with catalog.transaction(10, "tx-1"):
            _insert_source(db.conn, "s1", "before cut")
        cut = catalog.freeze_cut()
        try:
            with catalog.transaction(20, "tx-2"):
                db.conn.execute("UPDATE sources SET title='after cut' WHERE id='s1'")
            frozen = list(catalog.iter_mutations(cut))[0].mutation
            current = list(catalog.iter_mutations())[0].mutation
            assert dict(frozen.values)["title"] == "before cut"
            assert frozen.timestamp_ns == 10
            assert dict(current.values)["title"] == "after cut"
            assert current.timestamp_ns == 20
        finally:
            cut.close()
        with pytest.raises(WatermarkError, match="write refused"):
            with catalog.transaction(10, "tx-backward"):
                pass
    finally:
        db.close()


def test_logical_key_change_tombstones_old_address(tmp_path: Path) -> None:
    db = GraphDB(tmp_path / "personal.db")
    try:
        catalog = MutationCatalog(db.conn, "machine-a")
        catalog.install()
        with catalog.transaction(10, "tx-1"):
            _insert_source(db.conn, "old", "rename")
        with catalog.transaction(11, "tx-2"):
            db.conn.execute("UPDATE sources SET id='new' WHERE id='old'")
        mutations = {item.mutation.address: item.mutation for item in catalog.iter_mutations()}
        assert mutations[("old",)].tombstone
        assert not mutations[("new",)].tombstone
    finally:
        db.close()


def test_remote_merge_preserves_origin_and_is_store_forwardable(
    tmp_path: Path,
) -> None:
    db = GraphDB(tmp_path / "personal.db")
    try:
        catalog = MutationCatalog(db.conn, "machine-b")
        catalog.install()
        authored = AuthoredMutation("machine-a", "a-1", 0, _source("s1", "A", 10))
        assert catalog.apply_remote(authored)
        assert not catalog.apply_remote(
            AuthoredMutation("machine-c", "c-old", 0, _source("s1", "old", 9))
        )
        relayed = list(catalog.iter_mutations())[0]
        assert relayed.origin_incarnation == "machine-a"
        assert relayed.mutation == authored.mutation

        tombstone = AuthoredMutation(
            "machine-a", "a-2", 0, Mutation("sources", ("s1",), 11, True)
        )
        assert catalog.apply_remote(tombstone)
        assert db.conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0
        assert list(catalog.iter_mutations())[0] == tombstone
    finally:
        db.close()


def test_equal_timestamp_tie_break_is_candidate_hash(tmp_path: Path) -> None:
    db = GraphDB(tmp_path / "personal.db")
    try:
        catalog = MutationCatalog(db.conn, "machine-b")
        catalog.install()
        candidates = [
            AuthoredMutation("machine-a", "a", 0, _source("s1", title, 10))
            for title in ("one", "two")
        ]
        low, high = sorted(candidates, key=lambda item: item.mutation.candidate_hash)
        assert catalog.apply_remote(high)
        assert not catalog.apply_remote(low)
        assert db.conn.execute("SELECT title FROM sources").fetchone()[0] == dict(
            high.mutation.values
        )["title"]
    finally:
        db.close()


def test_remote_multitable_transaction_is_dependency_ordered_and_atomic(
    tmp_path: Path,
) -> None:
    source = GraphDB(tmp_path / "source.db")
    target = GraphDB(tmp_path / "target.db")
    try:
        source_catalog = MutationCatalog(source.conn, "machine-a")
        target_catalog = MutationCatalog(target.conn, "machine-b")
        source_catalog.install()
        target_catalog.install()
        with source_catalog.transaction(20, "parent-child"):
            _insert_source(source.conn, "s1", "parent")
            source.conn.execute(
                "INSERT INTO thoughts(id,source_id,content,tags,metadata,created_at) "
                "VALUES(?,?,?,?,?,?)",
                ("t1", "s1", "child", "[]", "{}", "2026-08-19T00:00:01Z"),
            )
        authored = list(source_catalog.iter_mutations())
        # Deliberately present the child first; the receiver's materializer
        # must recover dependency order inside the transaction.
        authored.reverse()
        assert target_catalog.apply_remote_batch(authored) == (2, 0)
        assert target.conn.execute("SELECT source_id FROM thoughts").fetchone()[0] == "s1"

        bad = AuthoredMutation(
            "machine-a", "different-transaction", 9, authored[0].mutation
        )
        with pytest.raises(WatermarkError, match="crosses"):
            target_catalog.apply_remote_batch([authored[1], bad])
    finally:
        source.close()
        target.close()


def test_iter_journal_is_built_from_rows_and_bounded_by_watermark(
    tmp_path: Path,
) -> None:
    db = GraphDB(tmp_path / "personal.db")
    try:
        catalog = MutationCatalog(db.conn, "machine-a")
        catalog.install()
        with catalog.transaction(10, "first"):
            _insert_source(db.conn, "s1", "one")
        with catalog.transaction(20, "second"):
            _insert_source(db.conn, "s2", "two")
        assert [item.mutation.timestamp_ns for item in catalog.iter_journal(
            after_watermark=10
        )] == [20]
        assert [item.mutation.timestamp_ns for item in catalog.iter_journal()] == [10, 20]
        assert dict(list(catalog.iter_journal())[0].mutation.values)["title"] == "one"
        assert len(list(catalog.iter_mutations())) == 2
    finally:
        db.close()


def test_journal_pages_exact_transactions_without_skips_or_duplicates(
    tmp_path: Path,
) -> None:
    db = GraphDB(tmp_path / "personal.db")
    try:
        catalog = MutationCatalog(db.conn, "machine-a")
        catalog.install()
        with catalog.transaction(10, "first"):
            _insert_source(db.conn, "s1", "one")
            _insert_source(db.conn, "s2", "two")
        with catalog.transaction(20, "second"):
            _insert_source(db.conn, "s3", "three")

        position: tuple[int, str | None] = (0, None)
        pages: list[list[AuthoredMutation]] = []
        while page := catalog.next_transactions_for_origin(
            "machine-a", position[0], position[1], limit=1,
        ):
            _ref, timestamp, transaction_id, items = page[0]
            position = (timestamp, transaction_id)
            pages.append(items)

        assert [[item.transaction_id for item in items] for items in pages] == [
            ["first", "first"],
            ["second"],
        ]
        assert [
            item.operation_index for items in pages for item in items
        ] == [0, 1, 0]
        assert catalog.next_transactions_for_origin(
            "machine-a", position[0], position[1], limit=1,
        ) == []
    finally:
        db.close()


def test_multiple_operations_on_one_address_keep_transaction_final_state(
    tmp_path: Path,
) -> None:
    source = GraphDB(tmp_path / "source.db")
    target = GraphDB(tmp_path / "target.db")
    try:
        left = MutationCatalog(source.conn, "machine-a")
        right = MutationCatalog(target.conn, "machine-b")
        left.install()
        right.install()
        with left.transaction(10, "rewrite"):
            _insert_source(source.conn, "s1", "first")
            source.conn.execute("UPDATE sources SET title='final' WHERE id='s1'")
        # Two operations on one address inside one transaction: the row's
        # final state is what exists, and what is served.
        journal = list(left.iter_journal())
        assert len(journal) == 1
        assert journal[0].operation_index == 1
        assert right.apply_remote_batch(journal) == (1, 0)
        assert target.conn.execute("SELECT title FROM sources").fetchone()[0] == "final"
        assert dict(list(right.iter_mutations())[0].mutation.values)["title"] == "final"
    finally:
        source.close()
        target.close()


def test_authored_transaction_bound_fails_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = GraphDB(tmp_path / "personal.db")
    try:
        catalog = MutationCatalog(db.conn, "machine-a")
        catalog.install()
        monkeypatch.setattr(catalog_module, "MAX_TRANSACTION_OPERATIONS", 1)
        with pytest.raises(sqlite3.OperationalError, match="user-defined function"):
            with catalog.transaction(10, "too-many"):
                _insert_source(db.conn, "s1", "one")
                _insert_source(db.conn, "s2", "two")
        assert db.conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0
        assert db.conn.execute(
            "SELECT COUNT(*) FROM fleet_sync_catalog"
        ).fetchone()[0] == 0
    finally:
        db.close()


def _settings_address_blob(row: dict) -> bytes:
    address = catalog_module._logical_address(TABLE_POLICIES["settings"], row)
    return catalog_module.encode_value(["settings", list(address)])


def test_reconcile_lets_a_live_row_win_over_an_older_stale_tombstone(
    tmp_path: Path,
) -> None:
    """A live row whose catalog address holds an OLDER tombstone is the
    truth (re-created or restored without capture): the reconcile
    re-bootstraps it instead of stopping. A NEWER tombstone is a genuine
    contradiction and stops the reconcile, naming the row (two anonymous
    3-minute startup failures on 2026-09-07)."""
    db = GraphDB(tmp_path / "personal.db")
    try:
        catalog = MutationCatalog(db.conn, "a" * 64)
        catalog.install()
        with catalog.transaction(1_000, "create"):
            _insert_setting(
                db.conn, identity="live", set_id="dashboard.example",
                key="k", payload='{"v":1}', deprecated=0,
            )
        row = dict(db.conn.execute("SELECT * FROM settings WHERE id='live'").fetchone())
        blob = _settings_address_blob(row)
        db.conn.execute(
            "UPDATE fleet_sync_catalog SET tombstone=1, timestamp_ns=500 WHERE address=?",
            (blob,),
        )
        db.conn.commit()
        catalog.reconcile_catalog(audit=False)
        tombstone, = db.conn.execute(
            "SELECT tombstone FROM fleet_sync_catalog WHERE address=?", (blob,)
        ).fetchone()
        assert tombstone == 0

        db.conn.execute(
            "UPDATE fleet_sync_catalog SET tombstone=1, timestamp_ns=? WHERE address=?",
            ((1 << 62), blob),
        )
        db.conn.commit()
        with pytest.raises(WatermarkError, match="table='settings'"):
            catalog.reconcile_catalog(audit=False)
    finally:
        db.close()


def test_deprecated_base_repair_never_tombstones_an_address_with_a_live_base(
    tmp_path: Path,
) -> None:
    """Base rows collapse to one address per natural key. When an old
    deprecated base row and a new live base row share it, the address is
    live; the repair must leave it alone even when the deprecated row's
    own timestamp matches the catalog winner's."""
    db = GraphDB(tmp_path / "personal.db")
    try:
        catalog = MutationCatalog(db.conn, "a" * 64)
        catalog.install()
        with catalog.transaction(1_000, "old"):
            _insert_setting(
                db.conn, identity="old", set_id="dashboard.example",
                key="k", payload='{"v":1}', deprecated=0,
            )
        with catalog.transaction(2_000, "retire-old"):
            db.conn.execute("UPDATE settings SET deprecated=1 WHERE id='old'")
        with catalog.transaction(3_000, "new"):
            _insert_setting(
                db.conn, identity="new", set_id="dashboard.example",
                key="k", payload='{"v":2}', deprecated=0,
            )
        old_row = dict(db.conn.execute("SELECT * FROM settings WHERE id='old'").fetchone())
        blob = _settings_address_blob(old_row)
        # Force the repair's trigger condition: the winner is live and its
        # timestamp equals the deprecated row's own timestamp.
        db.conn.execute(
            "UPDATE fleet_sync_catalog SET tombstone=0, timestamp_ns=? WHERE address=?",
            (catalog_module._row_timestamp(TABLE_POLICIES["settings"], old_row), blob),
        )
        db.conn.commit()
        assert catalog._repair_deprecated_settings_tombstones() == 0
        catalog.reconcile_catalog(audit=False)
        tombstone, = db.conn.execute(
            "SELECT tombstone FROM fleet_sync_catalog WHERE address=?", (blob,)
        ).fetchone()
        assert tombstone == 0
        assert db.conn.execute(
            "SELECT id FROM settings WHERE deprecated=0 AND key='k'"
        ).fetchone()[0] == "new"
    finally:
        db.close()


def test_serve_survives_a_catalog_row_whose_live_row_cannot_be_resolved(
    tmp_path: Path,
) -> None:
    """A settings base row deprecated in place (the pre-fix encoding) leaves
    the catalog citing a live row that no longer resolves. The serve emits a
    tombstone for that address and keeps serving the rest of the transaction
    instead of failing every round at it."""
    db = GraphDB(tmp_path / "personal.db")
    try:
        catalog = MutationCatalog(db.conn, "a" * 64)
        catalog.install()
        with catalog.transaction(1_000, "mixed"):
            _insert_setting(
                db.conn, identity="gone", set_id="dashboard.example",
                key="k1", payload='{"v":1}', deprecated=0,
            )
            _insert_source(db.conn, "s1", "kept")
        # Deprecate the base row bypassing capture (legacy shape): the
        # capture trigger fails closed on this connection, so do it on a raw
        # connection with the update trigger removed.
        db.conn.commit()
        raw = sqlite3.connect(tmp_path / "personal.db")
        raw.execute("DROP TRIGGER IF EXISTS fleet_sync_settings_update")
        raw.execute("UPDATE settings SET deprecated=1 WHERE id='gone'")
        raw.commit()
        raw.close()
        ref, = [r[0] for r in db.conn.execute(
            "SELECT id FROM fleet_sync_transactions WHERE transaction_id='mixed'"
        )]
        items, more = catalog.transaction_group(ref, "a" * 64, "mixed", offset=0, limit=100)
        assert more is False
        by_table = {i.mutation.table: i.mutation for i in items}
        assert by_table["sources"].tombstone is False
        assert by_table["settings"].tombstone is True
        assert by_table["settings"].address[:3] == ("dashboard.example", 1, "k1")
    finally:
        db.close()
