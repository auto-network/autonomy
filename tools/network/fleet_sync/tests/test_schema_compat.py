"""Mixed-schema fleet machines must pause synchronization, then resume.

A schema upgrade is always applied locally by each machine's own software —
the migration itself never crosses the wire. While two machines disagree
about the replicated surface (the synchronization policy inventory plus the
live shape of the replicated tables), their delta frames are not mutually
intelligible, so the serving side must refuse the pull with a typed
schema-mismatch refusal instead of streaming frames that would materialize
wrongly. Once the lagging machine upgrades, the digests match again and
synchronization resumes with no operator step.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from tools.graph.db import GraphDB
from tools.graph.models import Source
from tools.network.fleet_roster import enroll
from tools.network.fleet_sync_scheduler import (
    FleetSyncRuntimeConfig,
    FleetSyncScheduler,
    SQLiteFleetSyncStore,
)
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


def _add_column(path: Path, table: str, column: str) -> None:
    """One structural (DDL-phase) migration step, applied locally."""
    with sqlite3.connect(path) as conn:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
        conn.commit()


async def _eventually(predicate, *, timeout: float = 4.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition did not become true")
        await asyncio.sleep(0.02)


def test_compatibility_digest_tracks_the_replicated_shape(tmp_path):
    left = tmp_path / "left.db"
    right = tmp_path / "right.db"
    _prepare(left, KeyPair.generate())
    _prepare(right, KeyPair.generate())
    left_store = SQLiteFleetSyncStore(left)
    right_store = SQLiteFleetSyncStore(right)

    first = left_store.compatibility_digest()
    assert len(first) == 64
    # Identical replicated surfaces digest identically across machines.
    assert first == right_store.compatibility_digest()
    # The digest is stable across reads (cached until DDL actually runs).
    assert first == left_store.compatibility_digest()

    # A structural migration on one machine changes ITS digest only.
    _add_column(left, "thoughts", "compat_probe")
    diverged = left_store.compatibility_digest()
    assert diverged != first
    assert right_store.compatibility_digest() == first

    # The same migration applied locally on the other machine reconverges.
    _add_column(right, "thoughts", "compat_probe")
    assert right_store.compatibility_digest() == diverged


def test_mismatched_schemas_pause_sync_then_resume_after_local_upgrade(tmp_path):
    async def run() -> None:
        telemetry = []

        def record(peer, **values):
            telemetry.append({"peer": peer, **values})

        root = KeyPair.generate()
        left_key = KeyPair.generate()
        right_key = KeyPair.generate()
        left_path = tmp_path / "left.db"
        right_path = tmp_path / "right.db"
        _prepare(left_path, left_key)
        _prepare(right_path, right_key)
        entries = [
            enroll(root, machine_pub=left_key.public_hex),
            enroll(root, machine_pub=right_key.public_hex),
        ]

        # The right machine upgrades first: a structural migration adds a
        # replicated column. The left machine is still on the old schema.
        _add_column(right_path, "thoughts", "compat_probe")
        _insert(right_path, "post-upgrade", "authored on the upgraded machine")
        _insert(left_path, "left-seed-a", "keeps the delta path")

        right = FleetSyncScheduler(FleetSyncRuntimeConfig(
            machine_key=right_key,
            personal_root_pub=root.public_hex,
            roster_entries=lambda: entries,
            peer_addresses=lambda: {},
            personal_db_path=right_path,
            poll_interval=0.03,
        ))
        await right.start()
        left = FleetSyncScheduler(FleetSyncRuntimeConfig(
            machine_key=left_key,
            personal_root_pub=root.public_hex,
            roster_entries=lambda: entries,
            peer_addresses=lambda: {
                right_key.public_hex: [f"ws://127.0.0.1:{right.port}"],
            },
            personal_db_path=left_path,
            poll_interval=0.03,
            min_backoff=0.01,
            max_backoff=0.05,
            telemetry_recorder=record,
        ))
        await left.start()
        try:
            # Synchronization pauses with a typed refusal — the row must NOT
            # cross, and the reason must be legible, not a generic failure.
            await _eventually(lambda: any(
                row["outcome"] == "failed"
                and row["error_code"] == "FleetSyncSchemaMismatch"
                for row in telemetry
            ))
            assert _title(left_path, "post-upgrade") is None

            # The left machine applies the same migration locally. No other
            # intervention: the next poll succeeds and content flows.
            _add_column(left_path, "thoughts", "compat_probe")
            await _eventually(
                lambda: _title(left_path, "post-upgrade")
                == "authored on the upgraded machine"
            )
        finally:
            await left.stop()
            await right.stop()

    asyncio.run(run())


def test_full_upgrade_lifecycle_pauses_then_converges(tmp_path, monkeypatch):
    """The complete mixed-version story through the REAL upgrade machinery.

    A version bump ships a DDL phase (new replicated column) and a captured
    data phase (backfill). One machine upgrades first: synchronization
    pauses with the typed refusal and nothing crosses. The second machine
    upgrades locally: synchronization resumes unaided and everything
    converges — the pre-pause backlog, the data-phase backfill (which
    crossed as ordinary journaled writes), and post-upgrade content.
    """
    import tools.graph.db as db_module
    from tools.graph.db import GraphDB as GraphDBClass

    async def run() -> None:
        telemetry = []

        def record(peer, **values):
            telemetry.append({"peer": peer, **values})

        root = KeyPair.generate()
        left_key = KeyPair.generate()
        right_key = KeyPair.generate()
        left_path = tmp_path / "left.db"
        right_path = tmp_path / "right.db"
        _prepare(left_path, left_key)
        _prepare(right_path, right_key)
        _insert(left_path, "left-seed-b", "keeps the delta path")
        entries = [
            enroll(root, machine_pub=left_key.public_hex),
            enroll(root, machine_pub=right_key.public_hex),
        ]
        right = FleetSyncScheduler(FleetSyncRuntimeConfig(
            machine_key=right_key,
            personal_root_pub=root.public_hex,
            roster_entries=lambda: entries,
            peer_addresses=lambda: {},
            personal_db_path=right_path,
            poll_interval=0.03,
        ))
        await right.start()
        left = FleetSyncScheduler(FleetSyncRuntimeConfig(
            machine_key=left_key,
            personal_root_pub=root.public_hex,
            roster_entries=lambda: entries,
            peer_addresses=lambda: {
                right_key.public_hex: [f"ws://127.0.0.1:{right.port}"],
            },
            personal_db_path=left_path,
            poll_interval=0.03,
            min_backoff=0.01,
            max_backoff=0.05,
            telemetry_recorder=record,
        ))
        await left.start()
        try:
            # Baseline: same versions, content flows.
            _insert(right_path, "pre-upgrade", "authored before the upgrade")
            await _eventually(
                lambda: _title(left_path, "pre-upgrade") is not None
            )

            # Version N+1 ships: DDL adds a replicated column, the data
            # phase backfills it as CAPTURED authored writes.
            original_ddl = GraphDBClass._migrate_message_id_unique

            def ddl_plus_column(self):
                original_ddl(self)
                columns = {
                    str(row[1]) for row in
                    self.conn.execute("PRAGMA table_info(sources)")
                }
                if "compat_probe" not in columns:
                    self.conn.execute(
                        "ALTER TABLE sources ADD COLUMN compat_probe TEXT"
                    )
                    self.conn.commit()

            def backfill(db):
                db.conn.execute(
                    "UPDATE sources SET compat_probe='migrated' "
                    "WHERE compat_probe IS NULL"
                )
                db.conn.commit()

            version = db_module._SCHEMA_USER_VERSION + 1
            monkeypatch.setattr(db_module, "_SCHEMA_USER_VERSION", version)
            monkeypatch.setattr(
                GraphDBClass, "_migrate_message_id_unique", ddl_plus_column
            )
            monkeypatch.setattr(
                db_module, "_DATA_PHASE_MIGRATIONS", ((version, backfill),)
            )

            # The right machine upgrades first and keeps working.
            GraphDBClass(right_path).close()
            _insert(right_path, "post-upgrade", "authored after the upgrade")

            # Mixed versions: the pull pauses with the typed refusal and
            # nothing authored after the upgrade crosses.
            await _eventually(lambda: any(
                row["outcome"] == "failed"
                and row["error_code"] == "FleetSyncSchemaMismatch"
                for row in telemetry
            ))
            assert _title(left_path, "post-upgrade") is None

            # The left machine upgrades locally. Nothing else.
            GraphDBClass(left_path).close()

            # Resume and full convergence: backlog, backfill, new content.
            await _eventually(
                lambda: _title(left_path, "post-upgrade")
                == "authored after the upgrade"
            )

            def probe(path, source_id):
                with sqlite3.connect(path) as conn:
                    row = conn.execute(
                        "SELECT compat_probe FROM sources WHERE id=?",
                        (source_id,),
                    ).fetchone()
                return None if row is None else row[0]

            await _eventually(lambda: (
                probe(left_path, "pre-upgrade") == "migrated"
                and probe(right_path, "pre-upgrade") == "migrated"
            ))
            # Post-upgrade content is the new world: the backfill applied to
            # the pre-upgrade corpus only, identically on both machines.
            assert probe(left_path, "post-upgrade") == probe(
                right_path, "post-upgrade"
            )
        finally:
            await left.stop()
            await right.stop()

    asyncio.run(run())
