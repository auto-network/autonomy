"""Schema migrations on an ACTIVATED database follow the two-phase rule.

DDL phase: pre-attach, uncaptured, structural only. Data phase: any
replicated-row rewrite, executed AFTER fleet-sync attach on the ordinary
authored connection so it journals, timestamps, and synchronizes like any
other write.

Why the split is load-bearing: an uncaptured rewrite bypasses the merge
algebra entirely. It never journals, so peers never hear it — and the
retained pre-migration journal frames replay the OLD values into databases
that already migrated. Silent, permanent divergence (and an uncaptured
DELETE leaves a live winner, so the row resurrects from any peer). With
the rewrite captured, convergence is upgrade-order-independent: every
machine applies the same deterministic rewrite as ordinary authored
writes, producing byte-identical values, so whichever machine's timestamp
wins, the content is the same.

These tests pin both halves: a replicated-table write during the DDL
phase of a fleet-activated database fails closed instead of silently
diverging, and a declared data-phase migration lands in the journal.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import tools.graph.db as db_module
from tools.graph.db import GraphDB
from tools.graph.models import Source
from tools.network.fleet_sync_scheduler import SQLiteFleetSyncStore
from tools.network.idkit import KeyPair


def _prepare(path: Path, machine: KeyPair) -> None:
    db = GraphDB(path)
    try:
        db.activate_fleet_sync_writers(machine.public_hex)
    finally:
        db.close()


def _insert(path: Path, source_id: str, title: str) -> None:
    db = GraphDB(path)
    try:
        db.insert_source(Source(id=source_id, type="note", title=title))
    finally:
        db.close()


def _title(path: Path, source_id: str) -> str | None:
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT title FROM sources WHERE id=?", (source_id,)
        ).fetchone()
    return None if row is None else str(row[0])


def _drain(store: SQLiteFleetSyncStore, cursor: int):
    items = []
    while (page := store.next_transaction(cursor)) is not None:
        cursor, batch = page
        items.extend(batch)
    return cursor, items


def test_uncaptured_replicated_rewrite_in_ddl_phase_fails_closed(
    tmp_path, monkeypatch
):
    machine = KeyPair.generate()
    path = tmp_path / "personal.db"
    _prepare(path, machine)
    _insert(path, "src-1", "original")

    def sneaky_rewrite(self):
        self.conn.execute(
            "UPDATE sources SET title='rewritten' WHERE id='src-1'"
        )
        self.conn.commit()

    # A future version bump whose migration rewrites a replicated table in
    # the pre-attach (DDL) phase — exactly the mistake that diverges a
    # fleet. It must explode at the author's desk, not ship.
    monkeypatch.setattr(
        db_module, "_SCHEMA_USER_VERSION", db_module._SCHEMA_USER_VERSION + 1
    )
    monkeypatch.setattr(GraphDB, "_seed_tags", sneaky_rewrite)

    with pytest.raises(sqlite3.IntegrityError):
        GraphDB(path).close()

    assert _title(path, "src-1") == "original"


def test_data_phase_migration_is_captured_and_journaled(tmp_path, monkeypatch):
    machine = KeyPair.generate()
    path = tmp_path / "personal.db"
    _prepare(path, machine)
    _insert(path, "src-1", "original")
    store = SQLiteFleetSyncStore(path)
    baseline, _ = _drain(store, 0)

    def rewrite(db):
        # Deterministic on row content and idempotent: a peer's copy of the
        # same rewrite may arrive through sync before this machine runs it.
        db.conn.execute(
            "UPDATE sources SET title='rewritten' "
            "WHERE id='src-1' AND title!='rewritten'"
        )
        db.conn.commit()

    version = db_module._SCHEMA_USER_VERSION + 1
    monkeypatch.setattr(db_module, "_SCHEMA_USER_VERSION", version)
    monkeypatch.setattr(
        db_module, "_DATA_PHASE_MIGRATIONS", ((version, rewrite),)
    )

    GraphDB(path).close()
    assert _title(path, "src-1") == "rewritten"

    # The rewrite crossed the capture boundary: it is in the journal with
    # ordinary authored provenance, so peers receive it like any write.
    cursor, items = _drain(store, baseline)
    rewritten = [
        item for item in items
        if item.mutation.table == "sources"
        and item.mutation.address == ("src-1",)
        and "rewritten" in str(item.mutation.values)
    ]
    assert rewritten

    # Stamped complete: a further open neither re-runs the data phase nor
    # authors anything new.
    GraphDB(path).close()
    assert _drain(store, cursor)[1] == []
