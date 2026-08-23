"""Transactional current-winner/tombstone catalog for fleet-sync alpha.

The catalog is deliberately skinny.  Live payload remains in the authoritative
graph table; the catalog contains only the canonical logical address and
replication metadata.  Deletes remain as tombstones.  SQLite triggers make a
local graph mutation and its catalog transition one transaction, while a
frozen WAL snapshot gives the serializer an immutable cut as writers continue.
"""

from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import sqlite3
import time
from typing import Iterable, Iterator
from uuid import uuid4
import zlib

from .codec import (
    MAX_FRAME_BYTES, Mutation, decode_mutation_frame, decode_value, encode_mutation_frame,
    encode_value,
)
from .compaction import AuthoredMutation, WatermarkError
from .materialize import materialize
from .merge import mutation_wins
from .policies import (
    EXCLUDED_SETTING_SET_IDS,
    PolicyKind,
    TABLE_POLICIES,
    audit_schema,
)
from .snapshot import _logical_address, _logical_values
from .streaming import ensure_streaming_indexes, iter_indexed_snapshot_mutations


CATALOG_SCHEMA_VERSION = 3
JOURNAL_STORAGE_VERSION = 1
MAX_TRANSACTION_OPERATIONS = 16_384
MAX_TRANSACTION_FRAME_BYTES = 128 * 1024 * 1024
_table_columns: dict[str, tuple[str, ...]] = {}
_MUTATING_TABLE = re.compile(
    r'^\s*(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE\s+INTO|'
    r'UPDATE(?:\s+OR\s+\w+)?|DELETE\s+FROM)\s+["`\[]?([A-Za-z_]\w*)',
    re.IGNORECASE,
)


def _pack_journal(frame: bytes) -> bytes:
    return bytes([JOURNAL_STORAGE_VERSION]) + zlib.compress(frame, level=1)


def _unpack_journal(stored: bytes) -> bytes:
    if not stored or stored[0] != JOURNAL_STORAGE_VERSION:
        raise WatermarkError("unsupported journal storage frame")
    decompressor = zlib.decompressobj()
    frame = decompressor.decompress(stored[1:], MAX_FRAME_BYTES + 1)
    if len(frame) > MAX_FRAME_BYTES or not decompressor.eof:
        raise WatermarkError("journal storage frame violates size bounds")
    if decompressor.unused_data or decompressor.unconsumed_tail:
        raise WatermarkError("journal storage frame has trailing data")
    return frame


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _trigger_key_arguments(table: str) -> tuple[str, ...]:
    policy = TABLE_POLICIES[table]
    if table == "note_versions":
        return ("source_id", "created_at", "content")
    if table == "settings":
        return (
            "id", "set_id", "schema_revision", "key", "publication_state",
            "supersedes", "excludes",
        )
    return policy.key


def _sql_key(prefix: str, table: str) -> str:
    arguments = ",".join(
        f"{prefix}.{_quote(column)}" for column in _trigger_key_arguments(table)
    )
    return f"fleet_sync_key('{table}',{arguments})"


def _settings_when(prefix: str) -> str:
    values = ",".join(f"'{value}'" for value in sorted(EXCLUDED_SETTING_SET_IDS))
    return f"{prefix}.set_id NOT IN ({values})"


def _winner_predicate() -> str:
    return """
        excluded.timestamp_ns > fleet_sync_catalog.timestamp_ns OR (
            excluded.timestamp_ns = fleet_sync_catalog.timestamp_ns AND
            excluded.transaction_ref = fleet_sync_catalog.transaction_ref AND
            excluded.operation_index > fleet_sync_catalog.operation_index
        )
    """


def _capture_statement(
    table: str, prefix: str, tombstone: int, *, condition: str | None = None
) -> str:
    if condition is None:
        values_open, values_close = "VALUES(", ")"
    else:
        values_open, values_close = "SELECT ", f" WHERE {condition}"
    frame_arguments = ",".join(
        f"{prefix}.{_quote(column)}" for column in _table_columns[table]
    )
    return f"""
        INSERT INTO fleet_sync_journal(transaction_ref,operation_index,frame)
        {values_open}
            fleet_sync_transaction_ref(),fleet_sync_next_operation(),
            fleet_sync_frame_{table}({tombstone},{frame_arguments})
        {values_close};
        INSERT INTO fleet_sync_catalog(
            address,timestamp_ns,tombstone,transaction_ref,operation_index
        ) {values_open}
            {_sql_key(prefix, table)},fleet_sync_timestamp(),{tombstone},
            fleet_sync_transaction_ref(),fleet_sync_current_operation()
        {values_close}
        ON CONFLICT(address) DO UPDATE SET
            timestamp_ns=excluded.timestamp_ns,
            tombstone=excluded.tombstone,
            transaction_ref=excluded.transaction_ref,
            operation_index=excluded.operation_index
        WHERE {_winner_predicate()};
    """


def _trigger_sql(table: str) -> tuple[str, str, str]:
    capture = "fleet_sync_capture_enabled()=1"
    when_new = capture
    when_old = capture
    if table == "settings":
        # Put the exclusion first.  An identity-only write never asks for a
        # replication context; a transition into/out of the excluded sets
        # still records the replicated side.
        when_new = _settings_when("NEW") + " AND " + capture
        when_old = _settings_when("OLD") + " AND " + capture
    insert = f"""
        CREATE TRIGGER fleet_sync_{table}_insert AFTER INSERT ON {_quote(table)}
        WHEN {when_new} BEGIN {_capture_statement(table, 'NEW', 0)} END
    """
    policy = TABLE_POLICIES[table]
    if policy.kind is PolicyKind.IMMUTABLE:
        update = f"""
            CREATE TRIGGER fleet_sync_{table}_update BEFORE UPDATE ON {_quote(table)}
            BEGIN SELECT RAISE(ABORT, 'fleet-sync immutable row cannot update'); END
        """
        delete = f"""
            CREATE TRIGGER fleet_sync_{table}_delete BEFORE DELETE ON {_quote(table)}
            BEGIN SELECT RAISE(ABORT, 'fleet-sync immutable row cannot delete'); END
        """
        return insert, update, delete
    if policy.kind is PolicyKind.IMMUTABLE_PRUNABLE:
        stable_columns = [
            column for column in _table_columns[table] if column != "wire"
        ]
        stable = " AND ".join(
            f"OLD.{_quote(column)} IS NEW.{_quote(column)}"
            for column in stable_columns
        )
        wire_transition = (
            "((OLD.wire IS NULL AND NEW.wire IS NOT NULL) OR "
            "(OLD.wire IS NOT NULL AND NEW.wire IS NULL))"
        )
        update = f"""
            CREATE TRIGGER fleet_sync_{table}_update BEFORE UPDATE ON {_quote(table)}
            BEGIN
                SELECT CASE WHEN NOT ({stable} AND {wire_transition})
                    THEN RAISE(ABORT, 'fleet-sync immutable row has invalid update') END;
                {_capture_statement(table, 'NEW', 0, condition=capture)}
            END
        """
        delete = f"""
            CREATE TRIGGER fleet_sync_{table}_delete BEFORE DELETE ON {_quote(table)}
            BEGIN SELECT RAISE(ABORT, 'fleet-sync immutable row cannot delete'); END
        """
        return insert, update, delete
    delete = f"""
        CREATE TRIGGER fleet_sync_{table}_delete AFTER DELETE ON {_quote(table)}
        WHEN {when_old} BEGIN {_capture_statement(table, 'OLD', 1)} END
    """
    if table == "settings":
        update_body = f"""
            {_capture_statement(table, 'NEW', 0, condition=_settings_when('NEW'))}
            {_capture_statement(
                table, 'OLD', 1,
                condition=f"{_settings_when('OLD')} AND {_sql_key('OLD', table)} != {_sql_key('NEW', table)}",
            )}
        """
        update_when = (
            f"({_settings_when('OLD')} OR {_settings_when('NEW')}) AND {capture}"
        )
    else:
        update_body = f"""
            {_capture_statement(table, 'NEW', 0)}
            {_capture_statement(
                table, 'OLD', 1,
                condition=f"{_sql_key('OLD', table)} != {_sql_key('NEW', table)}",
            )}
        """
        update_when = capture
    update = f"""
        CREATE TRIGGER fleet_sync_{table}_update AFTER UPDATE ON {_quote(table)}
        WHEN {update_when} BEGIN {update_body} END
    """
    return insert, update, delete


@dataclass
class _WriteContext:
    timestamp_ns: int
    origin: str
    transaction_id: str
    transaction_ref: int
    operation_index: int = 0
    current_operation: int = -1
    frame_bytes: int = 0
    capture: bool = True
    automatic: bool = False


@dataclass
class FrozenCatalogCut:
    watermark: int
    reader: sqlite3.Connection

    def close(self) -> None:
        self.reader.rollback()
        self.reader.close()

    def __enter__(self) -> "FrozenCatalogCut":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


@dataclass(frozen=True)
class WinnerMetadata:
    origin_incarnation: str
    transaction_id: str
    operation_index: int
    table: str
    address: tuple[object, ...]
    timestamp_ns: int
    tombstone: bool
    candidate_hash: bytes


@dataclass(frozen=True)
class CatalogMigrationReport:
    """Observable result of one production personal-store migration."""

    schema_created: bool
    schema_upgraded: bool
    bootstrapped_rows: int
    live_rows: int
    catalog_rows: int
    triggers_active: bool


class MutationCatalog:
    """Install and operate the simulation's fail-closed write adapter."""

    def __init__(self, conn: sqlite3.Connection, origin_incarnation: str) -> None:
        self.conn = conn
        self.origin_incarnation = origin_incarnation
        self._context: _WriteContext | None = None
        global _table_columns
        _table_columns = {
            table: tuple(str(row[1]) for row in self.conn.execute(
                f"PRAGMA table_info({_quote(table)})"
            ))
            for table in TABLE_POLICIES
        }
        self._register_functions()

    def _register_functions(self) -> None:
        self.conn.create_function(
            "fleet_sync_key", -1, self._key_function, deterministic=True
        )
        self.conn.create_function("fleet_sync_timestamp", 0, self._timestamp)
        self.conn.create_function(
            "fleet_sync_transaction_ref", 0, self._transaction_ref
        )
        self.conn.create_function("fleet_sync_next_operation", 0, self._next_operation)
        self.conn.create_function(
            "fleet_sync_current_operation", 0, self._current_operation
        )
        self.conn.create_function(
            "fleet_sync_capture_enabled", 0,
            self._capture_enabled,
        )
        for table, columns in _table_columns.items():
            self.conn.create_function(
                f"fleet_sync_frame_{table}", -1,
                lambda tombstone, *values, table=table, columns=columns:
                    self._frame(table, bool(tombstone), columns, values),
            )

    def before_statement(self, sql: object) -> bool:
        """Enter one automatic authored context at the first replicated DML.

        The returned flag tells :class:`FleetSyncConnection` that this hook
        opened the SQLite transaction and therefore owns rollback if the
        application statement itself fails.  A caller-owned transaction stays
        caller-owned.
        """
        if self._context is not None or not isinstance(sql, str):
            return False
        match = _MUTATING_TABLE.match(sql)
        if match is None:
            return False
        policy = TABLE_POLICIES.get(match.group(1).lower())
        if policy is None or policy.kind in {PolicyKind.LOCAL, PolicyKind.DERIVED}:
            return False

        opened = not self.conn.in_transaction
        if opened:
            self.conn.execute("BEGIN IMMEDIATE")
        try:
            timestamp_ns = time.time_ns()
            floor, last = self.conn.execute(
                "SELECT write_floor,last_timestamp FROM fleet_sync_state "
                "WHERE singleton=1"
            ).fetchone()
            required = max(int(floor), int(last))
            if timestamp_ns <= required:
                raise WatermarkError(f"write refused before time {required + 1}")
            transaction_id = f"local:{uuid4().hex}"
            transaction_ref = self._ensure_transaction(
                self.origin_incarnation, transaction_id, timestamp_ns
            )
            self._context = _WriteContext(
                timestamp_ns,
                self.origin_incarnation,
                transaction_id,
                transaction_ref,
                automatic=True,
            )
            return opened
        except Exception:
            if opened:
                self.conn.rollback()
            raise

    def before_commit(self) -> None:
        """Finalize an automatic context inside the application transaction."""
        context = self._context
        if context is None or not context.automatic:
            return
        if context.operation_index == 0:
            # An identity-only Settings statement is permitted but its trigger
            # intentionally emits nothing. Do not retain an empty transaction.
            self.conn.execute(
                "DELETE FROM fleet_sync_transactions WHERE id=?",
                (context.transaction_ref,),
            )
            return
        self.conn.execute(
            "UPDATE fleet_sync_state SET last_timestamp=? WHERE singleton=1",
            (context.timestamp_ns,),
        )

    def after_transaction(self) -> None:
        """Drop process-local authorship state after commit or rollback."""
        self._context = None

    def _capture_enabled(self) -> int:
        return int(self._require_context().capture)

    @staticmethod
    def _key_function(table: object, *values: object) -> bytes:
        if not isinstance(table, str) or table not in TABLE_POLICIES:
            raise ValueError("unknown fleet-sync table")
        if table == "note_versions":
            source_id, created_at, content = values
            if not isinstance(content, str):
                raise ValueError("note version content must be text")
            address = (
                source_id, created_at,
                hashlib.sha256(content.encode("utf-8")).hexdigest(),
            )
        elif table == "settings":
            row_id, set_id, revision, key, publication, supersedes, excludes = values
            if supersedes is not None:
                role = f"supersedes:{supersedes}:{row_id}"
            elif excludes is not None:
                role = f"excludes:{excludes}:{row_id}"
            else:
                role = "base"
            address = (set_id, revision, key, publication, role)
        else:
            address = values
        return encode_value([table, list(address)])

    def _require_context(self) -> _WriteContext:
        if self._context is None:
            raise sqlite3.IntegrityError("fleet-sync mutation context is required")
        return self._context

    def _timestamp(self) -> int:
        return self._require_context().timestamp_ns

    def _transaction_ref(self) -> int:
        return self._require_context().transaction_ref

    def _next_operation(self) -> int:
        context = self._require_context()
        if context.operation_index >= MAX_TRANSACTION_OPERATIONS:
            raise sqlite3.IntegrityError(
                "fleet-sync transaction exceeds operation bound"
            )
        value = context.operation_index
        context.operation_index += 1
        context.current_operation = value
        return value

    def _current_operation(self) -> int:
        context = self._require_context()
        if context.current_operation < 0:
            raise sqlite3.IntegrityError("fleet-sync operation was not allocated")
        return context.current_operation

    def _frame(
        self, table: str, tombstone: bool,
        columns: tuple[str, ...], values: tuple[object, ...],
    ) -> bytes:
        context = self._require_context()
        row = dict(zip(columns, values, strict=True))
        policy = TABLE_POLICIES[table]
        address = _logical_address(policy, row)
        logical_values = () if tombstone else _logical_values(policy, row)
        frame = encode_mutation_frame(Mutation(
            table, address, context.timestamp_ns, tombstone, logical_values
        ))
        context.frame_bytes += len(frame)
        if context.frame_bytes > MAX_TRANSACTION_FRAME_BYTES:
            raise sqlite3.IntegrityError(
                "fleet-sync transaction exceeds byte bound"
            )
        return _pack_journal(frame)

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone() is not None

    def _create_schema_objects(self) -> None:
        """Create local catalog/state objects inside the caller transaction."""
        statements = (
            """CREATE TABLE IF NOT EXISTS fleet_sync_state(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                schema_version INTEGER NOT NULL,
                origin_incarnation TEXT NOT NULL,
                write_floor INTEGER NOT NULL,
                last_timestamp INTEGER NOT NULL,
                bootstrap_generation INTEGER NOT NULL DEFAULT 0
                    CHECK(bootstrap_generation>=0)
            )""",
            """CREATE TABLE IF NOT EXISTS fleet_sync_catalog(
                address BLOB NOT NULL,
                timestamp_ns INTEGER NOT NULL,
                tombstone INTEGER NOT NULL CHECK(tombstone IN (0,1)),
                transaction_ref INTEGER NOT NULL,
                operation_index INTEGER NOT NULL,
                PRIMARY KEY(address),
                FOREIGN KEY(transaction_ref) REFERENCES fleet_sync_transactions(id)
            ) WITHOUT ROWID""",
            """CREATE TABLE IF NOT EXISTS fleet_sync_origins(
                id INTEGER PRIMARY KEY,
                incarnation TEXT NOT NULL UNIQUE
            )""",
            """CREATE TABLE IF NOT EXISTS fleet_sync_transactions(
                id INTEGER PRIMARY KEY,
                origin_id INTEGER NOT NULL,
                transaction_id TEXT NOT NULL,
                timestamp_ns INTEGER NOT NULL,
                UNIQUE(origin_id,transaction_id),
                FOREIGN KEY(origin_id) REFERENCES fleet_sync_origins(id)
            )""",
            """CREATE TABLE IF NOT EXISTS fleet_sync_journal(
                transaction_ref INTEGER NOT NULL,
                operation_index INTEGER NOT NULL,
                frame BLOB NOT NULL,
                PRIMARY KEY(transaction_ref,operation_index),
                FOREIGN KEY(transaction_ref) REFERENCES fleet_sync_transactions(id)
            ) WITHOUT ROWID""",
            """CREATE TABLE IF NOT EXISTS fleet_sync_peer_state(
                machine_public_key TEXT NOT NULL,
                roster_epoch TEXT NOT NULL,
                online INTEGER NOT NULL DEFAULT 0 CHECK(online IN (0,1)),
                last_success_ns INTEGER,
                peer_watermark INTEGER,
                local_watermark INTEGER,
                bytes_sent INTEGER NOT NULL DEFAULT 0 CHECK(bytes_sent>=0),
                bytes_received INTEGER NOT NULL DEFAULT 0 CHECK(bytes_received>=0),
                checkpoints_sent INTEGER NOT NULL DEFAULT 0 CHECK(checkpoints_sent>=0),
                checkpoints_received INTEGER NOT NULL DEFAULT 0 CHECK(checkpoints_received>=0),
                deltas_sent INTEGER NOT NULL DEFAULT 0 CHECK(deltas_sent>=0),
                deltas_received INTEGER NOT NULL DEFAULT 0 CHECK(deltas_received>=0),
                transactions_applied INTEGER NOT NULL DEFAULT 0
                    CHECK(transactions_applied>=0),
                acknowledgements INTEGER NOT NULL DEFAULT 0
                    CHECK(acknowledgements>=0),
                retries INTEGER NOT NULL DEFAULT 0 CHECK(retries>=0),
                lag_ns INTEGER,
                last_error_code TEXT,
                updated_at_ns INTEGER NOT NULL DEFAULT 0 CHECK(updated_at_ns>=0),
                PRIMARY KEY(machine_public_key,roster_epoch)
            ) WITHOUT ROWID""",
            """CREATE INDEX IF NOT EXISTS idx_fleet_sync_catalog_order
                ON fleet_sync_catalog(
                    timestamp_ns,transaction_ref,operation_index,address
                )""",
            """CREATE INDEX IF NOT EXISTS idx_fleet_sync_peer_state_online
                ON fleet_sync_peer_state(roster_epoch,online,machine_public_key)""",
        )
        for statement in statements:
            self.conn.execute(statement)

    def _ensure_state(self) -> tuple[bool, bool]:
        """Create or safely upgrade the singleton catalog identity row."""
        columns = {
            str(row[1])
            for row in self.conn.execute("PRAGMA table_info(fleet_sync_state)")
        }
        added_generation = "bootstrap_generation" not in columns
        if added_generation:
            self.conn.execute(
                "ALTER TABLE fleet_sync_state ADD COLUMN "
                "bootstrap_generation INTEGER NOT NULL DEFAULT 0 "
                "CHECK(bootstrap_generation>=0)"
            )
        row = self.conn.execute(
            "SELECT schema_version,origin_incarnation FROM fleet_sync_state "
            "WHERE singleton=1"
        ).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO fleet_sync_state("
                "singleton,schema_version,origin_incarnation,write_floor,"
                "last_timestamp,bootstrap_generation) VALUES(1,?,?,0,0,0)",
                (CATALOG_SCHEMA_VERSION, self.origin_incarnation),
            )
            return True, False
        version, origin = int(row[0]), str(row[1])
        if origin != self.origin_incarnation:
            raise WatermarkError("fleet-sync catalog identity/version mismatch")
        if version == CATALOG_SCHEMA_VERSION:
            return False, added_generation
        if version == 2:
            # Version 3 adds only the local peer-state table and index created
            # above.  No replicated/catalog bytes need rewriting.
            self.conn.execute(
                "UPDATE fleet_sync_state SET schema_version=? WHERE singleton=1",
                (CATALOG_SCHEMA_VERSION,),
            )
            return False, True
        raise WatermarkError("fleet-sync catalog identity/version mismatch")

    def _install_triggers(self) -> None:
        for table, policy in TABLE_POLICIES.items():
            if policy.kind in {PolicyKind.LOCAL, PolicyKind.DERIVED}:
                continue
            for operation in ("insert", "update", "delete"):
                self.conn.execute(
                    f"DROP TRIGGER IF EXISTS fleet_sync_{table}_{operation}"
                )
            for sql in _trigger_sql(table):
                self.conn.execute(sql)

    def _trigger_count(self) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'fleet_sync_%'"
        ).fetchone()[0])

    def triggers_active(self) -> bool:
        replicated = sum(
            policy.kind not in {PolicyKind.LOCAL, PolicyKind.DERIVED}
            for policy in TABLE_POLICIES.values()
        )
        return self._trigger_count() == replicated * 3

    def install(self) -> None:
        """Install the Alpha schema and immediately activate capture hooks.

        Production uses :meth:`migrate_existing` first and activates hooks only
        when every personal-store writer has moved to the authored adapter.
        Keeping this method eager preserves the executable Alpha contract.
        """
        if self.conn.in_transaction:
            self.conn.commit()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self._create_schema_objects()
            self._ensure_state()
            self._install_triggers()
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _bootstrap_existing_rows(self) -> int:
        """Give each untracked live row deterministic legacy winner metadata."""
        self.conn.execute(
            "CREATE TEMP TABLE fleet_sync_bootstrap_progress("
            "timestamp_ns INTEGER PRIMARY KEY,next_operation INTEGER NOT NULL)"
        )
        # A small LRU avoids repeated origin/transaction lookups for common
        # timestamps without making memory proportional to timestamp variety.
        transaction_refs: OrderedDict[tuple[int, int], int] = OrderedDict()
        inserted = 0
        maximum = 0
        generation: int | None = None
        try:
            for mutation in iter_indexed_snapshot_mutations(self.conn):
                address_blob = encode_value([
                    mutation.table, list(mutation.address)
                ])
                existing = self.conn.execute(
                    "SELECT tombstone FROM fleet_sync_catalog WHERE address=?",
                    (address_blob,),
                ).fetchone()
                if existing is not None:
                    if bool(existing[0]):
                        raise WatermarkError(
                            "live bootstrap row conflicts with catalog tombstone"
                        )
                    continue

                if generation is None:
                    generation = int(self.conn.execute(
                        "SELECT bootstrap_generation FROM fleet_sync_state "
                        "WHERE singleton=1"
                    ).fetchone()[0]) + 1
                    self.conn.execute(
                        "UPDATE fleet_sync_state SET bootstrap_generation=? "
                        "WHERE singleton=1",
                        (generation,),
                    )
                timestamp = mutation.timestamp_ns
                # SQLite < 3.38 can return no row from RETURNING on an
                # upsert's DO-UPDATE path — split into a plain upsert plus a
                # separate SELECT rather than relying on RETURNING here.
                self.conn.execute(
                    "INSERT INTO fleet_sync_bootstrap_progress VALUES(?,1) "
                    "ON CONFLICT(timestamp_ns) DO UPDATE SET "
                    "next_operation=next_operation+1",
                    (timestamp,),
                )
                ordinal = int(self.conn.execute(
                    "SELECT next_operation-1 FROM fleet_sync_bootstrap_progress "
                    "WHERE timestamp_ns=?",
                    (timestamp,),
                ).fetchone()[0])
                batch = ordinal // MAX_TRANSACTION_OPERATIONS
                operation = ordinal % MAX_TRANSACTION_OPERATIONS
                transaction_key = (timestamp, batch)
                transaction_ref = transaction_refs.get(transaction_key)
                if transaction_ref is None:
                    transaction_ref = self._ensure_transaction(
                        self.origin_incarnation,
                        f"bootstrap-v1:{generation}:{timestamp}:{batch}",
                        timestamp,
                    )
                    transaction_refs[transaction_key] = transaction_ref
                    if len(transaction_refs) > 1024:
                        transaction_refs.popitem(last=False)
                else:
                    transaction_refs.move_to_end(transaction_key)
                self.conn.execute(
                    "INSERT INTO fleet_sync_catalog VALUES(?,?,?,?,?)",
                    (address_blob, timestamp, 0, transaction_ref, operation),
                )
                maximum = max(maximum, timestamp)
                inserted += 1
        finally:
            self.conn.execute("DROP TABLE IF EXISTS fleet_sync_bootstrap_progress")

        self.conn.execute(
            "UPDATE fleet_sync_state SET last_timestamp="
            "MAX(last_timestamp,?) WHERE singleton=1",
            (maximum,),
        )
        return inserted

    def _verify_catalog_integrity(self) -> tuple[int, int]:
        """Validate bounded catalog decoding and complete live-row coverage."""
        catalog_rows = 0
        for raw in self.conn.execute(
            "SELECT address,timestamp_ns,tombstone FROM fleet_sync_catalog"
        ):
            table, address = self._decode_address(bytes(raw[0]))
            policy = TABLE_POLICIES.get(table)
            if policy is None or policy.kind in {PolicyKind.LOCAL, PolicyKind.DERIVED}:
                raise WatermarkError("catalog address names non-replicated table")
            if not bool(raw[2]):
                row = self._live_row(self.conn, table, address)
                # Candidate hashing validates BLOB/JSON/canonical value shape
                # without retaining the payload in migration memory.
                Mutation(
                    table, address, int(raw[1]), False,
                    _logical_values(policy, row),
                ).candidate_hash
            catalog_rows += 1

        live_rows = 0
        for mutation in iter_indexed_snapshot_mutations(self.conn):
            address_blob = encode_value([mutation.table, list(mutation.address)])
            row = self.conn.execute(
                "SELECT tombstone FROM fleet_sync_catalog WHERE address=?",
                (address_blob,),
            ).fetchone()
            if row is None or bool(row[0]):
                raise WatermarkError("live personal row lacks winner metadata")
            live_rows += 1
        return live_rows, catalog_rows

    def _rebuild_prepared_catalog(self) -> int:
        """Re-snapshot a triggerless preparation under the activation lock.

        Preparation intentionally permits legacy writers to continue. Even a
        complete address inventory cannot detect an in-place value change, so
        activation discards only bootstrap provenance and rebuilds it from the
        locked current rows before installing triggers. Imported or authored
        history is never silently reinterpreted as bootstrap state.
        """
        journal_rows = int(self.conn.execute(
            "SELECT COUNT(*) FROM fleet_sync_journal"
        ).fetchone()[0])
        non_bootstrap = self.conn.execute(
            "SELECT transaction_id FROM fleet_sync_transactions "
            "WHERE transaction_id NOT LIKE 'bootstrap-v1:%' LIMIT 1"
        ).fetchone()
        if journal_rows or non_bootstrap is not None:
            raise WatermarkError(
                "triggerless fleet-sync catalog contains authored history"
            )
        self.conn.execute("DELETE FROM fleet_sync_catalog")
        self.conn.execute("DELETE FROM fleet_sync_transactions")
        self.conn.execute("DELETE FROM fleet_sync_origins")
        return self._bootstrap_existing_rows()

    def migrate_existing(self) -> CatalogMigrationReport:
        """Atomically prepare an existing personal store for writer activation.

        This migration intentionally does *not* create capture triggers.  The
        next rollout must first route GraphDB, vault, and key-control writers
        through authored transactions, then activate the hooks in the same
        deployment.  Until then ordinary writes remain valid and a repeated
        migration bootstraps only rows that appeared since the previous pass.
        """
        if self.conn.in_transaction:
            raise WatermarkError("catalog migration requires an idle connection")
        if (
            len(self.origin_incarnation) != 64
            or any(ch not in "0123456789abcdef" for ch in self.origin_incarnation)
        ):
            raise WatermarkError(
                "production origin must be a 64-character lowercase machine public key"
            )
        # Fail before the first DDL statement when a schema addition has no
        # explicit replicate/rebuild/local policy.
        audit_schema(self.conn)
        required = {
            "fleet_sync_state", "fleet_sync_catalog", "fleet_sync_origins",
            "fleet_sync_transactions", "fleet_sync_journal",
            "fleet_sync_peer_state",
        }
        schema_created = not all(
            self._table_exists(self.conn, table) for table in required
        )
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self._create_schema_objects()
            _, schema_upgraded = self._ensure_state()
            ensure_streaming_indexes(self.conn, manage_transaction=False)
            bootstrapped = self._bootstrap_existing_rows()
            live_rows, catalog_rows = self._verify_catalog_integrity()
            if self._trigger_count():
                raise WatermarkError(
                    "production catalog migration found capture triggers present"
                )
            self.conn.commit()
            return CatalogMigrationReport(
                schema_created=schema_created,
                schema_upgraded=schema_upgraded,
                bootstrapped_rows=bootstrapped,
                live_rows=live_rows,
                catalog_rows=catalog_rows,
                triggers_active=False,
            )
        except Exception:
            self.conn.rollback()
            raise

    def reconcile_catalog(self) -> CatalogMigrationReport:
        """Backfill untracked live rows into an ALREADY-ACTIVATED catalog.

        Unlike :meth:`migrate_existing`, this tolerates capture triggers and
        never rebuilds or deletes: it inserts winner metadata only for live
        rows the catalog is currently missing, via the idempotent
        skip-if-present path in :meth:`_bootstrap_existing_rows`.

        It repairs a production catalog that an earlier buggy bootstrap left
        incomplete -- e.g. the SQLite < 3.38 RETURNING-on-upsert gap that
        silently dropped colliding-timestamp rows during the original
        activation -- which otherwise fails closed forever at checkpoint time
        (``AlphaError: checkpoint contains untracked logical rows``) with no
        self-healing path, because migration early-skips once triggers exist.

        Only ``fleet_sync_catalog``/``fleet_sync_transactions``/
        ``fleet_sync_origins`` are written, never a replicated table, so no
        capture trigger fires and authored history is left untouched. The
        final integrity scan proves every live row now has winner metadata
        before the single transaction commits; any shortfall rolls back.
        """
        if self.conn.in_transaction:
            raise WatermarkError("catalog reconcile requires an idle connection")
        if (
            len(self.origin_incarnation) != 64
            or any(ch not in "0123456789abcdef" for ch in self.origin_incarnation)
        ):
            raise WatermarkError(
                "production origin must be a 64-character lowercase machine public key"
            )
        # Same fail-closed guard as migration: a schema addition with no
        # explicit replicate/rebuild/local policy stops the repair up front.
        audit_schema(self.conn)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self._ensure_state()
            bootstrapped = self._bootstrap_existing_rows()
            live_rows, catalog_rows = self._verify_catalog_integrity()
            self.conn.commit()
            return CatalogMigrationReport(
                schema_created=False,
                schema_upgraded=False,
                bootstrapped_rows=bootstrapped,
                live_rows=live_rows,
                catalog_rows=catalog_rows,
                triggers_active=self.triggers_active(),
            )
        except Exception:
            self.conn.rollback()
            raise

    def activate_production_writers(self) -> bool:
        """Enable fail-closed triggers and automatic transaction authorship.

        Returns ``True`` only when this call installs the triggers.  Catalog
        preparation and writer activation stay separate so an older writer can
        never be trapped between those rollout steps.
        """
        from tools.network.fleet_sync_connection import FleetSyncConnection

        if not isinstance(self.conn, FleetSyncConnection):
            raise TypeError(
                "production writer activation requires FleetSyncConnection"
            )
        if self.conn.in_transaction:
            raise WatermarkError("writer activation requires an idle connection")
        count = self._trigger_count()
        if count and not self.triggers_active():
            raise WatermarkError("fleet-sync capture triggers are only partially active")
        if count:
            identity = self.conn.execute(
                "SELECT origin_incarnation FROM fleet_sync_state WHERE singleton=1"
            ).fetchone()
            if identity is None or str(identity[0]) != self.origin_incarnation:
                raise WatermarkError("fleet-sync catalog identity/version mismatch")
        if not count:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                audit_schema(self.conn)
                self._create_schema_objects()
                self._ensure_state()
                ensure_streaming_indexes(self.conn, manage_transaction=False)
                self._rebuild_prepared_catalog()
                self._verify_catalog_integrity()
                self._install_triggers()
                if not self.triggers_active():
                    raise WatermarkError("fleet-sync capture trigger install incomplete")
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        self.conn.install_fleet_sync_hook(self)
        return count == 0

    @contextmanager
    def transaction(self, timestamp_ns: int, transaction_id: str) -> Iterator[None]:
        if self._context is not None or self.conn.in_transaction:
            raise WatermarkError("nested fleet-sync transaction")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            floor, last = self.conn.execute(
                "SELECT write_floor,last_timestamp FROM fleet_sync_state WHERE singleton=1"
            ).fetchone()
            required = max(int(floor), int(last))
            if timestamp_ns <= required:
                raise WatermarkError(f"write refused before time {required + 1}")
            transaction_ref = self._ensure_transaction(
                self.origin_incarnation, transaction_id, timestamp_ns
            )
            self._context = _WriteContext(
                timestamp_ns, self.origin_incarnation, transaction_id,
                transaction_ref,
            )
            yield
            self.conn.execute(
                "UPDATE fleet_sync_state SET last_timestamp=? WHERE singleton=1",
                (timestamp_ns,),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        finally:
            self._context = None

    def freeze_cut(self) -> FrozenCatalogCut:
        if self._context is not None or self.conn.in_transaction:
            raise WatermarkError("cannot freeze during a write transaction")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            last = int(self.conn.execute(
                "SELECT last_timestamp FROM fleet_sync_state WHERE singleton=1"
            ).fetchone()[0])
            self.conn.execute(
                "UPDATE fleet_sync_state SET write_floor=? WHERE singleton=1", (last,)
            )
            path = Path(self.conn.execute("PRAGMA database_list").fetchone()[2])
            reader = sqlite3.connect(path)
            reader.row_factory = sqlite3.Row
            reader.execute("BEGIN")
            # Establish the snapshot before releasing the local writer barrier.
            reader.execute("SELECT COUNT(*) FROM fleet_sync_catalog").fetchone()
            self.conn.commit()
            return FrozenCatalogCut(last, reader)
        except Exception:
            self.conn.rollback()
            raise

    @staticmethod
    def _decode_address(blob: bytes) -> tuple[str, tuple[object, ...]]:
        value = decode_value(blob)
        if not isinstance(value, list) or len(value) != 2:
            raise WatermarkError("catalog address has wrong shape")
        table, address = value
        if not isinstance(table, str) or not isinstance(address, list):
            raise WatermarkError("catalog address has wrong types")
        return table, tuple(address)

    def _ensure_transaction(
        self, origin: str, transaction_id: str, timestamp_ns: int
    ) -> int:
        self.conn.execute(
            "INSERT OR IGNORE INTO fleet_sync_origins(incarnation) VALUES(?)",
            (origin,),
        )
        origin_id = int(self.conn.execute(
            "SELECT id FROM fleet_sync_origins WHERE incarnation=?", (origin,)
        ).fetchone()[0])
        self.conn.execute(
            "INSERT OR IGNORE INTO fleet_sync_transactions("
            "origin_id,transaction_id,timestamp_ns) VALUES(?,?,?)",
            (origin_id, transaction_id, timestamp_ns),
        )
        row = self.conn.execute(
            "SELECT id,timestamp_ns FROM fleet_sync_transactions "
            "WHERE origin_id=? AND transaction_id=?",
            (origin_id, transaction_id),
        ).fetchone()
        if int(row[1]) != timestamp_ns:
            raise WatermarkError("transaction id reused at another timestamp")
        return int(row[0])

    @staticmethod
    def _live_row(
        conn: sqlite3.Connection, table: str, address: tuple[object, ...]
    ) -> dict[str, object]:
        policy = TABLE_POLICIES[table]
        conn.row_factory = sqlite3.Row
        if table == "note_versions":
            rows = conn.execute(
                "SELECT * FROM note_versions WHERE source_id=? AND created_at=?",
                address[:2],
            )
            for raw in rows:
                row = dict(raw)
                if _logical_address(policy, row) == address:
                    return row
            raise WatermarkError("catalog points to missing note version")
        if table == "settings":
            clauses = ["set_id=?", "schema_revision=?", '"key"=?',
                       "publication_state=?"]
            params = list(address[:4])
            role = str(address[4])
            if role == "base":
                clauses.extend(["supersedes IS NULL", "excludes IS NULL"])
            else:
                row_id = role.rsplit(":", 1)[-1]
                clauses.append("id=?")
                params.append(row_id)
            raw = conn.execute(
                "SELECT * FROM settings WHERE " + " AND ".join(clauses), params
            ).fetchone()
        else:
            clauses = [f"{_quote(column)} IS ?" for column in policy.key]
            raw = conn.execute(
                f"SELECT * FROM {_quote(table)} WHERE " + " AND ".join(clauses),
                address,
            ).fetchone()
        if raw is None:
            raise WatermarkError(f"catalog points to missing live row: {table}")
        return dict(raw)

    def iter_mutations(
        self, cut: FrozenCatalogCut | None = None
    ) -> Iterator[AuthoredMutation]:
        conn = cut.reader if cut is not None else self.conn
        watermark = cut.watermark if cut is not None else (1 << 63) - 1
        rows = conn.execute(
            "SELECT c.address,c.timestamp_ns,c.tombstone,o.incarnation,"
            "t.transaction_id,c.operation_index FROM fleet_sync_catalog c "
            "JOIN fleet_sync_transactions t ON t.id=c.transaction_ref "
            "JOIN fleet_sync_origins o ON o.id=t.origin_id "
            "WHERE c.timestamp_ns<=? ORDER BY c.timestamp_ns,o.incarnation,"
            "t.transaction_id,c.operation_index,c.address",
            (watermark,),
        )
        for raw in rows:
            table, address = self._decode_address(bytes(raw[0]))
            timestamp = int(raw[1])
            tombstone = bool(raw[2])
            values = ()
            if not tombstone:
                row = self._live_row(conn, table, address)
                values = _logical_values(TABLE_POLICIES[table], row)
            mutation = Mutation(table, address, timestamp, tombstone, values)
            yield AuthoredMutation(
                str(raw[3]), str(raw[4]), int(raw[5]), mutation
            )

    def iter_winner_metadata(
        self, cut: FrozenCatalogCut | None = None
    ) -> Iterator[WinnerMetadata]:
        for item in self.iter_mutations(cut):
            mutation = item.mutation
            yield WinnerMetadata(
                item.origin_incarnation, item.transaction_id,
                item.operation_index, mutation.table, mutation.address,
                mutation.timestamp_ns, mutation.tombstone,
                mutation.candidate_hash,
            )

    def install_winner_metadata(
        self, entries: Iterable[WinnerMetadata],
        *, skip_addresses: frozenset[bytes] = frozenset(),
    ) -> int:
        """Verify a realized exact base and install its skinny winner state.

        ``skip_addresses`` names rows the materializer could not represent —
        foreign-key orphans whose parent is absent from the checkpoint (see
        ``ForeignKeyOrphanError``). Their winner metadata is not verified,
        installed, or counted, so the catalog stays exactly consistent with the
        rows that actually landed. The count this returns therefore excludes
        them, and the caller subtracts the same skip count from its base/winner
        invariants."""
        if self._context is not None or self.conn.in_transaction:
            raise WatermarkError("cannot install winners inside another transaction")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            maximum = 0
            count = 0
            last_transaction: tuple[str, str, int] | None = None
            transaction_ref = -1
            for item in entries:
                if item.table not in TABLE_POLICIES:
                    raise WatermarkError("winner metadata names unknown table")
                address_blob = encode_value([item.table, list(item.address)])
                if address_blob in skip_addresses:
                    continue
                if item.tombstone:
                    mutation = Mutation(
                        item.table, item.address, item.timestamp_ns, True
                    )
                    try:
                        self._live_row(self.conn, item.table, item.address)
                    except WatermarkError:
                        pass
                    else:
                        raise WatermarkError(
                            "tombstoned winner is present in exact base"
                        )
                else:
                    row = self._live_row(self.conn, item.table, item.address)
                    mutation = Mutation(
                        item.table, item.address, item.timestamp_ns, False,
                        _logical_values(TABLE_POLICIES[item.table], row),
                    )
                if mutation.candidate_hash != item.candidate_hash:
                    raise WatermarkError(
                        "winner metadata/base hash mismatch at "
                        f"{item.table} {tuple(item.address)!r}: "
                        f"winner_hash={item.candidate_hash.hex()} "
                        f"base_hash={mutation.candidate_hash.hex()}"
                    )
                transaction_key = (
                    item.origin_incarnation, item.transaction_id,
                    item.timestamp_ns,
                )
                if transaction_key != last_transaction:
                    transaction_ref = self._ensure_transaction(*transaction_key)
                    last_transaction = transaction_key
                self.conn.execute(
                    "INSERT INTO fleet_sync_catalog VALUES(?,?,?,?,?)",
                    (address_blob, item.timestamp_ns, int(item.tombstone),
                     transaction_ref, item.operation_index),
                )
                maximum = max(maximum, item.timestamp_ns)
                count += 1
            self.conn.execute(
                "UPDATE fleet_sync_state SET last_timestamp="
                "MAX(last_timestamp,?) WHERE singleton=1", (maximum,),
            )
            self.conn.commit()
            return count
        except Exception:
            self.conn.rollback()
            raise

    def iter_journal(
        self,
        cut: FrozenCatalogCut | None = None,
        *,
        after_watermark: int = -1,
    ) -> Iterator[AuthoredMutation]:
        conn = cut.reader if cut is not None else self.conn
        through = cut.watermark if cut is not None else (1 << 63) - 1
        rows = conn.execute(
            "SELECT o.incarnation,t.transaction_id,j.operation_index,j.frame "
            "FROM fleet_sync_journal j "
            "JOIN fleet_sync_transactions t ON t.id=j.transaction_ref "
            "JOIN fleet_sync_origins o ON o.id=t.origin_id "
            "WHERE t.timestamp_ns>? AND t.timestamp_ns<=? "
            "ORDER BY t.timestamp_ns,o.incarnation,t.transaction_id,j.operation_index",
            (after_watermark, through),
        )
        for origin, transaction, operation, frame in rows:
            yield AuthoredMutation(
                str(origin), str(transaction), int(operation),
                decode_mutation_frame(_unpack_journal(bytes(frame))),
            )

    def next_journal_transaction(
        self,
        after: tuple[int, str, str] | None = None,
    ) -> tuple[tuple[int, str, str], list[AuthoredMutation]] | None:
        """Read the next retained authored transaction in bounded memory.

        ``after`` is the exact total-order key returned by the prior call, not
        a scalar watermark. The connection is short-lived at the scheduler
        layer, so this gives a response iterator one transaction at a time
        without holding a SQLite snapshot or collecting the journal.
        """
        where = ""
        params: tuple[object, ...] = ()
        if after is not None:
            where = (
                "AND (t.timestamp_ns,o.incarnation,t.transaction_id)>(?,?,?) "
            )
            params = after
        row = self.conn.execute(
            "SELECT t.id,t.timestamp_ns,o.incarnation,t.transaction_id "
            "FROM fleet_sync_transactions t "
            "JOIN fleet_sync_origins o ON o.id=t.origin_id "
            "WHERE EXISTS(SELECT 1 FROM fleet_sync_journal j "
            "WHERE j.transaction_ref=t.id) "
            + where
            + "ORDER BY t.timestamp_ns,o.incarnation,t.transaction_id LIMIT 1",
            params,
        ).fetchone()
        if row is None:
            return None
        transaction_ref = int(row[0])
        key = (int(row[1]), str(row[2]), str(row[3]))
        items = [
            AuthoredMutation(
                key[1], key[2], int(operation),
                decode_mutation_frame(_unpack_journal(bytes(frame))),
            )
            for operation, frame in self.conn.execute(
                "SELECT operation_index,frame FROM fleet_sync_journal "
                "WHERE transaction_ref=? ORDER BY operation_index",
                (transaction_ref,),
            )
        ]
        return key, items

    def prune_journal(self, through_watermark: int) -> int:
        """Retire transaction frames only after an exact base ACK covers them."""
        if self._context is not None or self.conn.in_transaction:
            raise WatermarkError("cannot prune journal inside a transaction")
        with self.conn:
            cursor = self.conn.execute(
                "DELETE FROM fleet_sync_journal WHERE transaction_ref IN ("
                "SELECT id FROM fleet_sync_transactions WHERE timestamp_ns<=?)",
                (through_watermark,),
            )
        return int(cursor.rowcount)

    def apply_remote(self, authored: AuthoredMutation) -> bool:
        """Merge one trusted remote mutation atomically; return winner status."""
        applied, _ = self.apply_remote_batch([authored])
        return applied == 1

    def apply_remote_batch(
        self, authored_items: Iterable[AuthoredMutation]
    ) -> tuple[int, int]:
        """Atomically merge one authored transaction in dependency-safe order."""
        items: list[AuthoredMutation] = []
        total_bytes = 0
        for item in authored_items:
            if len(items) >= MAX_TRANSACTION_OPERATIONS:
                raise WatermarkError("remote transaction exceeds operation bound")
            total_bytes += len(encode_mutation_frame(item.mutation))
            if total_bytes > MAX_TRANSACTION_FRAME_BYTES:
                raise WatermarkError("remote transaction exceeds byte bound")
            items.append(item)
        items.sort(key=lambda item: item.operation_index)
        if not items:
            return 0, 0
        identity = (items[0].origin_incarnation, items[0].transaction_id)
        timestamp = items[0].mutation.timestamp_ns
        if any(
            (item.origin_incarnation, item.transaction_id) != identity
            or item.mutation.timestamp_ns != timestamp
            for item in items
        ):
            raise WatermarkError("remote batch crosses an authored transaction")
        if len({item.operation_index for item in items}) != len(items):
            raise WatermarkError("remote transaction repeats an operation index")
        if self._context is not None or self.conn.in_transaction:
            raise WatermarkError("cannot apply remote inside another transaction")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            winners: list[tuple[AuthoredMutation, bytes]] = []
            ignored = 0
            for authored in items:
                mutation = authored.mutation
                address_blob = encode_value([
                    mutation.table, list(mutation.address)
                ])
                current = self.conn.execute(
                    "SELECT c.timestamp_ns,o.incarnation,t.transaction_id,"
                    "c.operation_index,c.tombstone FROM fleet_sync_catalog c "
                    "JOIN fleet_sync_transactions t ON t.id=c.transaction_ref "
                    "JOIN fleet_sync_origins o ON o.id=t.origin_id "
                    "WHERE c.address=?",
                    (address_blob,),
                ).fetchone()
                if current is not None:
                    policy = TABLE_POLICIES[mutation.table]
                    current_timestamp = int(current[0])
                    if policy.kind in {
                        PolicyKind.IMMUTABLE, PolicyKind.IMMUTABLE_PRUNABLE,
                    }:
                        current_values = _logical_values(
                            policy,
                            self._live_row(
                                self.conn, mutation.table, mutation.address
                            ),
                        )
                        current_mutation = Mutation(
                            mutation.table, mutation.address,
                            current_timestamp, bool(current[4]), current_values,
                        )
                        if not mutation_wins(current_mutation, mutation):
                            ignored += 1
                            continue
                        winners.append((authored, address_blob))
                        continue
                    if current_timestamp > mutation.timestamp_ns:
                        ignored += 1
                        continue
                    if current_timestamp == mutation.timestamp_ns:
                        if (str(current[1]), str(current[2])) == identity:
                            if int(current[3]) >= authored.operation_index:
                                ignored += 1
                                continue
                            winners.append((authored, address_blob))
                            continue
                        current_values = ()
                        if not bool(current[4]):
                            current_values = _logical_values(
                                TABLE_POLICIES[mutation.table],
                                self._live_row(
                                    self.conn, mutation.table, mutation.address
                                ),
                            )
                        current_mutation = Mutation(
                            mutation.table, mutation.address,
                            current_timestamp, bool(current[4]), current_values,
                        )
                        if current_mutation.candidate_hash >= mutation.candidate_hash:
                            ignored += 1
                            continue
                winners.append((authored, address_blob))
            if not winners:
                self.conn.rollback()
                return 0, ignored
            transaction_ref = self._ensure_transaction(
                identity[0], identity[1], timestamp
            )
            self._context = _WriteContext(
                timestamp, identity[0], identity[1], transaction_ref,
                capture=False,
            )
            materialize(
                self.conn, [item.mutation for item, _ in winners],
                manage_transaction=False,
            )
            final_winners = {
                address_blob: authored for authored, address_blob in winners
            }
            for address_blob, authored in final_winners.items():
                mutation = authored.mutation
                if mutation.tombstone:
                    continue
                installed = Mutation(
                    mutation.table, mutation.address, mutation.timestamp_ns, False,
                    _logical_values(
                        TABLE_POLICIES[mutation.table],
                        self._live_row(self.conn, mutation.table, mutation.address),
                    ),
                )
                if installed.candidate_hash != mutation.candidate_hash:
                    raise WatermarkError(
                        "materialized row does not preserve canonical mutation bytes"
                    )
            for authored, address_blob in winners:
                mutation = authored.mutation
                self.conn.execute(
                    "INSERT INTO fleet_sync_catalog VALUES(?,?,?,?,?) "
                    "ON CONFLICT(address) DO UPDATE SET "
                    "timestamp_ns=excluded.timestamp_ns,"
                    "tombstone=excluded.tombstone,"
                    "transaction_ref=excluded.transaction_ref,"
                    "operation_index=excluded.operation_index",
                    (
                        address_blob, mutation.timestamp_ns,
                        int(mutation.tombstone), transaction_ref,
                        authored.operation_index,
                    ),
                )
                self.conn.execute(
                    "INSERT OR IGNORE INTO fleet_sync_journal VALUES(?,?,?)",
                    (
                        transaction_ref, authored.operation_index,
                        _pack_journal(encode_mutation_frame(mutation)),
                    ),
                )
            self.conn.execute(
                "UPDATE fleet_sync_state SET last_timestamp="
                "MAX(last_timestamp,?) WHERE singleton=1",
                (mutation.timestamp_ns,),
            )
            self.conn.commit()
            return len(winners), ignored
        except Exception:
            self.conn.rollback()
            raise
        finally:
            self._context = None


def attach_active_production_catalog(
    conn: sqlite3.Connection,
) -> MutationCatalog | None:
    """Attach authorship to a newly opened connection when triggers are live.

    A prepared-but-not-activated database returns ``None``.  Any partial
    trigger set fails the open instead of leaving one writer able to bypass or
    mis-execute the capture contract.
    """
    from tools.network.fleet_sync_connection import FleetSyncConnection

    if not isinstance(conn, FleetSyncConnection):
        raise TypeError("fleet-sync production stores require FleetSyncConnection")
    trigger_count = int(conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' "
        "AND name LIKE 'fleet_sync_%'"
    ).fetchone()[0])
    if not trigger_count:
        return None
    row = conn.execute(
        "SELECT origin_incarnation FROM fleet_sync_state WHERE singleton=1"
    ).fetchone()
    if row is None:
        raise WatermarkError("fleet-sync triggers exist without catalog identity")
    catalog = MutationCatalog(conn, str(row[0]))
    if not catalog.triggers_active():
        raise WatermarkError("fleet-sync capture triggers are only partially active")
    conn.install_fleet_sync_hook(catalog)
    return catalog
