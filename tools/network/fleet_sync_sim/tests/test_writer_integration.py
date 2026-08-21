from pathlib import Path
import hashlib
import sqlite3

import pytest

from tools.graph.db import GraphDB
from tools.graph.models import Derivation, Source, Thought
from tools.network.idkit import KeyPair
from tools.network.storagekit import object_header, suites
from tools.network.storagekit.credentials import build as build_credential
from tools.network.storagekit.keycontrol import KeyControlStore
from tools.network.storagekit.tests.conftest import World
from tools.network.fleet_sync_sim.catalog import MutationCatalog
from tools.network.fleet_sync_sim.compaction import WatermarkError
from tools.vault.db_content_store import DbContentStore


ORIGIN = "b" * 64


def _local_operation_groups(conn: sqlite3.Connection) -> list[int]:
    return [
        int(row[0])
        for row in conn.execute(
            "SELECT COUNT(*) FROM fleet_sync_transactions AS tx "
            "JOIN fleet_sync_journal AS journal "
            "ON journal.transaction_ref=tx.id "
            "WHERE tx.transaction_id LIKE 'local:%' GROUP BY tx.id ORDER BY tx.id"
        )
    ]


def test_graph_writers_auto_author_group_commit_and_rollback(tmp_path: Path) -> None:
    path = tmp_path / "personal.db"
    db = GraphDB(path)
    assert db.activate_fleet_sync_writers(ORIGIN)
    assert not db.activate_fleet_sync_writers(ORIGIN)
    assert [
        str(row[1]) for row in db.conn.execute(
            "PRAGMA table_info(fleet_sync_catalog)"
        )
    ] == [
        "address", "timestamp_ns", "tombstone",
        "transaction_ref", "operation_index",
    ]

    source = Source(id="source-1", type="note", title="authored")
    db.insert_source(source)
    assert _local_operation_groups(db.conn) == [1]

    db.insert_note_version(source.id, 1, "the synchronized note body")
    db.commit()
    assert _local_operation_groups(db.conn) == [1, 1]
    assert [
        item.mutation.table for item in db._fleet_catalog.iter_mutations()
        if item.transaction_id.startswith("local:")
    ] == ["sources", "note_versions"]

    # These APIs intentionally leave commit ownership to their caller.  They
    # must retain one transaction identity and stable operation order.
    db.conn.execute("BEGIN IMMEDIATE")
    db.insert_thought(Thought(id="thought-1", source_id=source.id, content="one"))
    db.insert_derivation(Derivation(
        id="derivation-1", source_id=source.id, thought_id="thought-1",
        content="two",
    ))
    db.commit()
    assert _local_operation_groups(db.conn) == [1, 1, 2]

    before = db.conn.execute(
        "SELECT COUNT(*) FROM fleet_sync_transactions "
        "WHERE transaction_id LIKE 'local:%'"
    ).fetchone()[0]
    db.conn.execute("BEGIN IMMEDIATE")
    db.insert_thought(Thought(
        id="rolled-back", source_id=source.id, content="never committed"
    ))
    db.conn.rollback()
    assert db.conn.execute(
        "SELECT 1 FROM thoughts WHERE id='rolled-back'"
    ).fetchone() is None
    assert db.conn.execute(
        "SELECT COUNT(*) FROM fleet_sync_transactions "
        "WHERE transaction_id LIKE 'local:%'"
    ).fetchone()[0] == before
    db.close()

    # A normal production reopen discovers active triggers and reattaches the
    # authored connection boundary without an activation call.
    reopened = GraphDB(path)
    try:
        reopened.insert_source(Source(id="source-2", type="note", title="reopened"))
        assert _local_operation_groups(reopened.conn) == [1, 1, 2, 1]

        reopened.conn.cursor().execute(
            "INSERT INTO sources(id,type,metadata,created_at,ingested_at) "
            "VALUES('cursor-write','note','{}','2026-08-21','2026-08-21')"
        )
        reopened.conn.commit()
        assert _local_operation_groups(reopened.conn) == [1, 1, 2, 1, 1]

        # The adapter intentionally recognizes the production statement
        # shapes rather than pretending to parse arbitrary SQL. A mutating
        # CTE it does not recognize still cannot slip past the trigger.
        with pytest.raises(sqlite3.OperationalError, match="user-defined function"):
            reopened.conn.execute(
                "WITH value(id) AS (VALUES('unrecognized')) "
                "INSERT INTO sources(id,type,metadata,created_at,ingested_at) "
                "SELECT id,'note','{}','2026-08-21','2026-08-21' FROM value"
            )
        reopened.conn.rollback()
    finally:
        reopened.close()

    # A completely ordinary connection has neither the authoring lifecycle
    # nor the registered capture functions. Active triggers reject its write.
    bypass = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.OperationalError, match="no such function"):
            bypass.execute(
                "INSERT INTO sources(id,type,metadata,created_at,ingested_at) "
                "VALUES('bypass','note','{}','2026-08-21','2026-08-21')"
            )
        bypass.rollback()
    finally:
        bypass.close()


def test_graph_writer_rejects_clock_rollback_before_data_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = GraphDB(tmp_path / "personal.db")
    try:
        db.activate_fleet_sync_writers(ORIGIN)
        db.conn.execute(
            "UPDATE fleet_sync_state SET last_timestamp=10 WHERE singleton=1"
        )
        db.conn.commit()
        monkeypatch.setattr(
            "tools.network.fleet_sync_sim.catalog.time.time_ns", lambda: 5
        )
        with pytest.raises(WatermarkError, match="write refused before time 11"):
            db.insert_source(Source(id="too-early", type="note"))
        assert db.conn.execute(
            "SELECT 1 FROM sources WHERE id='too-early'"
        ).fetchone() is None
        assert not db.conn.in_transaction
    finally:
        db.close()


def test_writer_activation_failure_rolls_back_every_trigger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = GraphDB(tmp_path / "personal.db")
    try:
        db.migrate_fleet_sync_catalog(ORIGIN)

        def fail_during_trigger_install(catalog: MutationCatalog) -> None:
            catalog.conn.execute(
                "CREATE TRIGGER fleet_sync_partial AFTER INSERT ON sources "
                "BEGIN SELECT 1; END"
            )
            raise RuntimeError("injected activation failure")

        monkeypatch.setattr(
            MutationCatalog, "_install_triggers", fail_during_trigger_install
        )
        with pytest.raises(RuntimeError, match="injected activation failure"):
            db.activate_fleet_sync_writers(ORIGIN)
        assert db.conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'fleet_sync_%'"
        ).fetchone()[0] == 0
        assert db.conn.execute(
            "SELECT schema_version FROM fleet_sync_state"
        ).fetchone()[0] > 0
    finally:
        db.close()


def test_activation_resnapshots_prepared_rows_under_writer_lock(
    tmp_path: Path,
) -> None:
    db = GraphDB(tmp_path / "personal.db")
    try:
        db.insert_source(Source(id="prepared", type="note", title="before"))
        db.migrate_fleet_sync_catalog(ORIGIN)
        before = db.conn.execute(
            "SELECT tx.transaction_id FROM fleet_sync_catalog AS catalog "
            "JOIN fleet_sync_transactions AS tx "
            "ON tx.id=catalog.transaction_ref"
        ).fetchone()[0]

        # Legacy writers are still allowed after preparation. This same-key
        # rewrite is invisible to an address-only completeness audit.
        db.conn.execute(
            "UPDATE sources SET title='after' WHERE id='prepared'"
        )
        db.conn.commit()

        assert db.activate_fleet_sync_writers(ORIGIN)
        after = db.conn.execute(
            "SELECT tx.transaction_id FROM fleet_sync_catalog AS catalog "
            "JOIN fleet_sync_transactions AS tx "
            "ON tx.id=catalog.transaction_ref"
        ).fetchone()[0]
        assert before != after
        assert db.conn.execute(
            "SELECT title FROM sources WHERE id='prepared'"
        ).fetchone()[0] == "after"
    finally:
        db.close()


def test_recorder_failure_aborts_originating_graph_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = GraphDB(tmp_path / "personal.db")
    try:
        db.activate_fleet_sync_writers(ORIGIN)

        def fail_frame(*args, **kwargs):
            raise RuntimeError("injected recorder failure")

        monkeypatch.setattr(db._fleet_catalog, "_frame", fail_frame)
        with pytest.raises(sqlite3.OperationalError, match="user-defined function"):
            db.insert_source(Source(id="must-rollback", type="note"))
        assert db.conn.execute(
            "SELECT 1 FROM sources WHERE id='must-rollback'"
        ).fetchone() is None
        assert db.conn.execute(
            "SELECT COUNT(*) FROM fleet_sync_transactions "
            "WHERE transaction_id LIKE 'local:%'"
        ).fetchone()[0] == 0
        assert not db.conn.in_transaction
    finally:
        db.close()


def test_authorship_setup_failure_releases_automatic_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = GraphDB(tmp_path / "personal.db")
    try:
        db.activate_fleet_sync_writers(ORIGIN)

        def fail_transaction(*args, **kwargs):
            raise sqlite3.OperationalError("injected transaction setup failure")

        monkeypatch.setattr(
            db._fleet_catalog, "_ensure_transaction", fail_transaction
        )
        with pytest.raises(
            sqlite3.OperationalError, match="injected transaction setup failure"
        ):
            db.insert_source(Source(id="never-started", type="note"))
        assert db.conn.execute(
            "SELECT 1 FROM sources WHERE id='never-started'"
        ).fetchone() is None
        assert not db.conn.in_transaction
    finally:
        db.close()


def test_vault_and_keycontrol_connections_join_authored_boundary(
    tmp_path: Path,
) -> None:
    path = tmp_path / "personal.db"
    with GraphDB(path) as db:
        db.activate_fleet_sync_writers(ORIGIN)

    world = World()
    credential, _ = build_credential(
        world.member(0), world.gen, bytes(range(32)), [world.gen],
        (1_800_000_000_000, 0),
    )
    with KeyControlStore(path) as key_control:
        key_control.accept_credential(credential)

    body = b"encrypted body bytes"
    digest = hashlib.sha256(body).hexdigest()
    header = object_header.build(
        KeyPair.generate(),
        b"s" * 32,
        b"c" * 32,
        genesis_id="1" * 64,
        domain_id="2" * 64,
        object_id="3" * 64,
        revision_id="4" * 64,
        storage_state_id="5" * 64,
        writer_authority_heads=("6" * 64,),
        body_suite_id=suites.BODY_SUITE_DEFAULT,
        body_nonce=b"n" * 12,
        wrap_nonce=b"w" * 12,
        ciphertext_hash=digest,
    )
    with DbContentStore(path) as content:
        assert content.put_object(header, body) == digest

    with GraphDB(path) as db:
        assert _local_operation_groups(db.conn) == [1, 2]
        tables = [
            item.mutation.table for item in db._fleet_catalog.iter_mutations()
            if item.transaction_id.startswith("local:")
        ]
        assert tables == [
            "keycontrol_credential",
            "vault_content_bodies",
            "vault_content_objects",
        ]
