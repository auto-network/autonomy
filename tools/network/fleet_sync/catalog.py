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
import json
import logging
from pathlib import Path
import re
import sqlite3
import time
from typing import Iterable, Iterator, Sequence
from uuid import uuid4
import zlib

from .codec import (
    MAX_FRAME_BYTES, Mutation, decode_mutation_frame, decode_value, encode_mutation_frame,
    encode_value,
)
from .compaction import AuthoredMutation, WatermarkError
from .materialize import ContentAddressedBlobStore, materialize
from .merge import mutation_wins
from .policies import (
    RETIRED_LOGICAL_TABLES, PolicyKind, TABLE_POLICIES, audit_schema,
)
from .snapshot import _logical_address, _logical_values, _row_timestamp
from .streaming import ensure_streaming_indexes, iter_indexed_snapshot_mutations


CATALOG_SCHEMA_VERSION = 3
logger = logging.getLogger(__name__)

#: Local objects earlier versions created and this version removes on open.
RETIRED_SCHEMA_OBJECTS: frozenset[str] = frozenset({"fleet_sync_journal"})


def _address_prefix(table: str) -> bytes:
    """The exact byte prefix every catalog/quarantine address of *table* has.

    An address is ``encode_value([table, [key...]])``, and that encoding is
    frozen: a two-item list header, then the table name as a length-prefixed
    string. So a half-open range over the prefix is an exact, index-driven
    scan of the WITHOUT ROWID primary key -- not a LIKE over blobs.
    """
    return encode_value([table, []])[: 1 + 4 + 1 + 4 + len(table.encode("utf-8"))]


def purge_retired_catalog_addresses(
    conn: sqlite3.Connection, tables: Sequence[str], *, batch: int = 20_000,
) -> int:
    """Delete winner-catalog and quarantine rows addressing dropped tables.

    Dropping a replicated table's SQL definition without this leaves the
    catalog citing rows that no longer exist: _verify_catalog_integrity
    raises on the first one ("catalog address names non-replicated table").

    BOUNDED BY CONSTRUCTION. A live store carries hundreds of thousands of
    these addresses, and deleting them in one statement would hold the single
    SQLite write lock far longer than a concurrent reader's busy timeout.
    Each batch is its own transaction and the lock is released between them,
    so a busy database keeps serving while the purge makes progress.
    Interrupting it is safe: the next call resumes.

    (A precaution, not a post-mortem: the 2026-09-08 graph-API outage was NOT
    caused by this delete. See GraphDB.drop_retired_entity_tables for what
    actually happened.)

    Refuses any table that is not declared retired, so a live table can never
    be silently unwound by a typo.
    """
    removed = 0
    for table in tables:
        if table not in RETIRED_LOGICAL_TABLES:
            raise WatermarkError(f"table is not retired: {table!r}")
        low = _address_prefix(table)
        high = low[:-1] + bytes([low[-1] + 1])
        for name in ("fleet_sync_catalog", "fleet_sync_quarantine"):
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (name,),
            ).fetchone() is None:
                continue
            while True:
                cursor = conn.execute(
                    f"DELETE FROM {name} WHERE address IN ("
                    f"  SELECT address FROM {name} "
                    "   WHERE address >= ? AND address < ? LIMIT ?)",
                    (low, high, int(batch)),
                )
                deleted = cursor.rowcount if cursor.rowcount > 0 else 0
                conn.commit()
                removed += deleted
                if deleted < int(batch):
                    break
    return removed


MAX_TRANSACTION_OPERATIONS = 16_384
MAX_TRANSACTION_FRAME_BYTES = 128 * 1024 * 1024

#: prune_acknowledged() lock-hold bounds: stop after this much wall time
#: (the scheduler calls it again next pass), delete at most this many
#: transaction ids per write transaction, and yield between them so a
#: waiting dashboard writer can take personal.db.
PRUNE_BUDGET_S = 1.0
PRUNE_BATCH_REFS = 2_000
PRUNE_YIELD_S = 0.005

#: Computed once per process by MutationCatalog.expected_schema_object_names.
_EXPECTED_SCHEMA_OBJECTS: frozenset[str] | None = None


class _SchemaScratch:
    """Duck-typed stand-in exposing only ``.conn`` for running the schema
    statements against a scratch connection (see expected_schema_object_names)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
_table_columns: dict[str, tuple[str, ...]] = {}
_MUTATING_TABLE = re.compile(
    r'^\s*(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE\s+INTO|'
    r'UPDATE(?:\s+OR\s+\w+)?|DELETE\s+FROM)\s+["`\[]?([A-Za-z_]\w*)',
    re.IGNORECASE,
)


def journal_has_gap(conn: sqlite3.Connection) -> bool:
    """Retired: deltas are served from catalog + live rows, so a store can
    always replay from any watermark. Kept as a name for callers that still
    import it; always False."""
    return False


def ensure_quarantine_table(conn: sqlite3.Connection) -> None:
    """The unrealized-row backlog, upgraded in place when newer columns are
    missing (the table predates them and is created lazily)."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS fleet_sync_quarantine("
        "address BLOB PRIMARY KEY,"
        "table_name TEXT NOT NULL,"
        "logical_address TEXT NOT NULL,"
        "reason TEXT NOT NULL,"
        "watermark INTEGER NOT NULL,"
        "quarantined_at_ns INTEGER NOT NULL,"
        "frame BLOB,"
        "origin TEXT,"
        "transaction_id TEXT,"
        "operation_index INTEGER)"
    )
    columns = {
        row[1] for row in conn.execute(
            "PRAGMA table_info(fleet_sync_quarantine)"
        )
    }
    upgrades = {
        "frame": "BLOB",
        "origin": "TEXT",
        "transaction_id": "TEXT",
        "operation_index": "INTEGER",
    }
    for name, kind in upgrades.items():
        if name not in columns:
            conn.execute(
                f"ALTER TABLE fleet_sync_quarantine ADD COLUMN {name} {kind}"
            )


def quarantine_unrealized(
    conn: sqlite3.Connection,
    rows: list[tuple[str, tuple, str]],
    *,
    watermark: int,
    replay: dict[bytes, tuple[bytes, str, str, int]] | None = None,
    commit: bool = True,
) -> None:
    """Retain rows the receiver could not realize, for observability and repair.

    Each entry is ``(table, address, reason)``. ``reason`` is ``fk_orphan`` (a
    NOT-NULL parent absent from the transfer — the origin keeps its own copy;
    repair re-materializes it once the parent is recovered),
    ``attachment_bytes_unavailable`` (external content-addressed bytes not yet
    fetched — the row lands once blob transfer backfills it), or
    ``secondary_identity_conflict`` (the row is new under its replication key
    but collides with a local row on some OTHER unique column, so this store
    can never realize it — it is still forwarded, because a downstream peer
    holding no such local row can). Recording them keeps a durable, decodable
    backlog so a later repair or fetch can drain it.
    ``replay`` optionally maps address blobs to the deferred mutation's
    replay identity ``(frame, origin, transaction_id, operation_index)``: a
    delta-deferred row is never re-served (the peer's trail advances past
    it), so the drain rebuilds the originated mutation from the stored frame
    and re-applies it through ordinary last-writer-wins; swept entries
    store NULLs because a later sweep carries the row again.
    Rewritable per address: a later transfer that finally realizes the row
    makes the entry stale, and the drain clears it.
    """
    ensure_quarantine_table(conn)
    now = time.time_ns()
    for table, address, reason in rows:
        address_blob = encode_value([table, list(address)])
        frame, origin, transaction_id, operation_index = (
            (replay or {}).get(address_blob) or (None, None, None, None)
        )
        conn.execute(
            "INSERT OR REPLACE INTO fleet_sync_quarantine "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                address_blob,
                table,
                json.dumps(list(address)),
                reason,
                int(watermark),
                now,
                frame,
                origin,
                transaction_id,
                operation_index,
            ),
        )
    if commit:
        conn.commit()


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _trigger_key_arguments(table: str) -> tuple[str, ...]:
    policy = TABLE_POLICIES[table]
    if table == "note_versions":
        return ("source_id", "created_at", "content")
    if table == "settings":
        return (
            "id", "set_id", "schema_revision", "key", "publication_state",
            "supersedes", "excludes", "terminal_persona",
        )
    return policy.key


def _sql_key(prefix: str, table: str) -> str:
    arguments = ",".join(
        f"{prefix}.{_quote(column)}" for column in _trigger_key_arguments(table)
    )
    return f"fleet_sync_key('{table}',{arguments})"


def _settings_row_is_live(prefix: str) -> str:
    """Match the base-row visibility rule used by snapshot and lookup.

    Deprecated base Settings are retained as local history, but they are not a
    live logical row.  Override and exclusion rows have per-row logical
    addresses and remain replicated even when deprecated.
    """
    return (
        f"({prefix}.supersedes IS NOT NULL OR {prefix}.excludes IS NOT NULL "
        f"OR {prefix}.deprecated = 0)"
    )


def _winner_predicate() -> str:
    return """
        excluded.timestamp_ns > fleet_sync_catalog.timestamp_ns OR (
            excluded.timestamp_ns = fleet_sync_catalog.timestamp_ns AND
            excluded.transaction_ref = fleet_sync_catalog.transaction_ref AND
            excluded.operation_index > fleet_sync_catalog.operation_index
        )
    """


def _capture_statement(
    table: str, columns: tuple[str, ...], prefix: str, tombstone: int, *,
    condition: str | None = None,
) -> str:
    if condition is None:
        values_open, values_close = "VALUES(", ")"
    else:
        values_open, values_close = "SELECT ", f" WHERE {condition}"
    # The catalog row is the whole capture: address, timestamp, tombstone
    # and the (transaction, operation) that wrote it. Wire frames are
    # rebuilt from this row plus the live row when served
    # (transaction_items) -- exactly how a swept page is built. The
    # per-row frame callback that used to fill fleet_sync_journal was the
    # write path's largest cost (tracked ingestion 0.41x of untracked).
    del columns  # the column list still shapes the trigger's NEW/OLD refs
    return f"""
        INSERT INTO fleet_sync_catalog(
            address,timestamp_ns,tombstone,transaction_ref,operation_index
        ) {values_open}
            {_sql_key(prefix, table)},fleet_sync_timestamp(),{tombstone},
            fleet_sync_transaction_ref(),fleet_sync_next_operation()
        {values_close}
        ON CONFLICT(address) DO UPDATE SET
            timestamp_ns=excluded.timestamp_ns,
            tombstone=excluded.tombstone,
            transaction_ref=excluded.transaction_ref,
            operation_index=excluded.operation_index
        WHERE {_winner_predicate()};
    """


def _trigger_sql(table: str, columns: tuple[str, ...]) -> tuple[str, str, str]:
    capture = "fleet_sync_capture_enabled()=1"
    insert = f"""
        CREATE TRIGGER fleet_sync_{table}_insert AFTER INSERT ON {_quote(table)}
        WHEN {capture} BEGIN {_capture_statement(table, columns, 'NEW', 0)} END
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
            column for column in columns if column != "wire"
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
                {_capture_statement(table, columns, 'NEW', 0, condition=capture)}
            END
        """
        delete = f"""
            CREATE TRIGGER fleet_sync_{table}_delete BEFORE DELETE ON {_quote(table)}
            BEGIN SELECT RAISE(ABORT, 'fleet-sync immutable row cannot delete'); END
        """
        return insert, update, delete
    if table == "settings":
        old_live = _settings_row_is_live("OLD")
        new_live = _settings_row_is_live("NEW")
        insert = f"""
            CREATE TRIGGER fleet_sync_{table}_insert AFTER INSERT ON {_quote(table)}
            WHEN {capture} AND {new_live} BEGIN
                {_capture_statement(table, columns, 'NEW', 0)}
            END
        """
        update = f"""
            CREATE TRIGGER fleet_sync_{table}_update AFTER UPDATE ON {_quote(table)}
            WHEN {capture} BEGIN
                {_capture_statement(table, columns, 'NEW', 0, condition=new_live)}
                {_capture_statement(table, columns, 'OLD', 1,
                    condition=(
                        f"{old_live} AND (NOT {new_live} OR "
                        f"{_sql_key('OLD', table)} != {_sql_key('NEW', table)})"
                    ),
                )}
            END
        """
        delete = f"""
            CREATE TRIGGER fleet_sync_{table}_delete AFTER DELETE ON {_quote(table)}
            WHEN {capture} AND {old_live} BEGIN
                {_capture_statement(table, columns, 'OLD', 1)}
            END
        """
        return insert, update, delete
    delete = f"""
        CREATE TRIGGER fleet_sync_{table}_delete AFTER DELETE ON {_quote(table)}
        WHEN {capture} BEGIN {_capture_statement(table, columns, 'OLD', 1)} END
    """
    update_body = f"""
        {_capture_statement(table, columns, 'NEW', 0)}
        {_capture_statement(table, columns, 'OLD', 1,
            condition=f"{_sql_key('OLD', table)} != {_sql_key('NEW', table)}",
        )}
    """
    update = f"""
        CREATE TRIGGER fleet_sync_{table}_update AFTER UPDATE ON {_quote(table)}
        WHEN {capture} BEGIN {update_body} END
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
    """Install and operate the fleet-sync fail-closed write adapter."""

    def __init__(self, conn: sqlite3.Connection, origin_incarnation: str) -> None:
        self.conn = conn
        self.origin_incarnation = origin_incarnation
        self._context: _WriteContext | None = None
        # Optional attachment store consulted when applying remote batches;
        # set by the owning store/scheduler after attach. Absent, attachment
        # rows defer to quarantine instead of realizing.
        self.blob_store: ContentAddressedBlobStore | None = None
        # Per store, never module-global: the previous global was rewritten by
        # every open, so a process serving two stores regenerated each one's
        # trigger SQL from whichever store it opened LAST.
        self.newly_captured_tables: frozenset[str] = frozenset()
        self._table_columns: dict[str, tuple[str, ...]] = {
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

    def before_statement(self, sql: object) -> bool:
        """Enter one automatic originated context at the first replicated DML.

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
            # A statement that did not reach a replication trigger leaves no
            # transaction worth retaining.
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
        # One captured row invokes this deterministic UDF several times from
        # one trigger body (journal insert, catalog upsert, frame). A
        # single-entry memo collapses those consecutive identical calls
        # without unbounded retention.
        memo = MutationCatalog._key_memo
        if memo is not None and memo[0] == table and memo[1] == values:
            return memo[2]
        encoded = MutationCatalog._encode_key(table, *values)
        MutationCatalog._key_memo = (table, values, encoded)
        return encoded

    _key_memo: tuple[object, tuple[object, ...], bytes] | None = None

    @staticmethod
    def _encode_key(table: object, *values: object) -> bytes:
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
            (row_id, set_id, revision, key, publication, supersedes, excludes,
             persona) = values
            if supersedes is not None:
                role = f"supersedes:{supersedes}:{row_id}"
            elif excludes is not None:
                role = f"excludes:{excludes}:{row_id}"
            else:
                role = "base"
            address = (set_id, revision, key, publication, role)
            if persona is not None:
                address = address + (persona,)  # one slot per signer
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

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone() is not None

    @staticmethod
    def expected_schema_object_names() -> frozenset[str]:
        """Names of every local table/index install() creates, derived by
        running the very same statements against a scratch in-memory
        connection — so a rollforward can never disagree with install().

        Deliberately NOT a scratch MutationCatalog: __init__ rewrites the
        module-level ``_table_columns`` from its connection's tables, and a
        scratch database would blank the live catalog's column knowledge.
        """
        global _EXPECTED_SCHEMA_OBJECTS
        if _EXPECTED_SCHEMA_OBJECTS is None:
            scratch = sqlite3.connect(":memory:")
            try:
                MutationCatalog._create_schema_objects(
                    _SchemaScratch(scratch))  # type: ignore[arg-type]
                _EXPECTED_SCHEMA_OBJECTS = frozenset(
                    str(r[0]) for r in scratch.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type IN ('table','index') "
                        "AND name NOT LIKE 'sqlite_%'"
                    )
                )
            finally:
                scratch.close()
        return _EXPECTED_SCHEMA_OBJECTS

    def ensure_schema_objects(self) -> bool:
        """Roll an already-activated store forward to the current set of
        local schema objects. Read-only when nothing is missing (no write
        lock taken); otherwise runs install()'s idempotent statements in one
        short IMMEDIATE transaction. Returns True iff anything was created.

        install() runs once at activation and never again; every later open
        attaches without it, so an object added to _create_schema_objects
        afterwards never reached production stores (live 2026-09-06: the
        prune index shipped in code while personal.db kept only its
        autoindexes and the quadratic prune went on starving the dashboard).
        """
        expected = self.expected_schema_object_names()
        present = {
            str(r[0]) for r in self.conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type IN ('table','index') AND name NOT LIKE 'sqlite_%'"
            )
        }
        retired = present & RETIRED_SCHEMA_OBJECTS
        if expected <= present and not retired:
            return False
        if retired and expected <= present:
            # Nothing to create, only something to drop. A DROP inside
            # _create_schema_objects never ran on an activated store,
            # because this check saw nothing missing (fleet_sync_journal
            # stayed on every live store after 6fdec619 and broke the
            # schema audit, 2026-09-07). Drop it here, unconditionally.
            if self.conn.in_transaction:
                self.conn.commit()
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                for name in sorted(retired):
                    self.conn.execute(f'DROP TABLE IF EXISTS "{name}"')
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
            return True
        if self.conn.in_transaction:
            self.conn.commit()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self._create_schema_objects()
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return True

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
            # fleet_sync_journal (per-change wire frames) is retired: every
            # frame is derivable from fleet_sync_catalog + the live row, and
            # it doubled the store (1,024 bytes per mutation on 400-byte rows,
            # 149-157% overhead vs a plain indexed store, perf baseline
            # 2026-09-02 / 2026-09-07). Dropped on the next install.
            """DROP TABLE IF EXISTS fleet_sync_journal""",
            # Where the served-ack prune's sweep resumes on the next pass.
            # Without it every pass restarted from id 0 and re-probed every
            # still-cited transaction below the floor: 160-169 s per pass
            # retiring nothing on a 580k-row store (anchore, 2026-09-07).
            """CREATE TABLE IF NOT EXISTS fleet_sync_prune_cursor(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                cursor INTEGER NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS fleet_sync_peer_state(
                machine_public_key TEXT NOT NULL,
                roster_epoch TEXT NOT NULL,
                online INTEGER NOT NULL DEFAULT 0 CHECK(online IN (0,1)),
                last_success_ns INTEGER,
                peer_watermark INTEGER,
                local_watermark INTEGER,
                bytes_sent INTEGER NOT NULL DEFAULT 0 CHECK(bytes_sent>=0),
                bytes_received INTEGER NOT NULL DEFAULT 0 CHECK(bytes_received>=0),
                deltas_sent INTEGER NOT NULL DEFAULT 0 CHECK(deltas_sent>=0),
                deltas_received INTEGER NOT NULL DEFAULT 0 CHECK(deltas_received>=0),
                transactions_applied INTEGER NOT NULL DEFAULT 0
                    CHECK(transactions_applied>=0),
                acknowledgements INTEGER NOT NULL DEFAULT 0
                    CHECK(acknowledgements>=0),
                retries INTEGER NOT NULL DEFAULT 0 CHECK(retries>=0),
                lag_ns INTEGER,
                last_error_code TEXT,
                peer_built_at TEXT,
                updated_at_ns INTEGER NOT NULL DEFAULT 0 CHECK(updated_at_ns>=0),
                PRIMARY KEY(machine_public_key,roster_epoch)
            ) WITHOUT ROWID""",
            # The former order index (timestamp_ns,transaction_ref,
            # operation_index,address) is retired: every consumer's ORDER BY
            # needs joined-table columns and sorts in a temp B-tree anyway,
            # its range predicate spans the whole catalog at serve
            # time, and — this being a WITHOUT ROWID table — it duplicated
            # the full address blob per row on every captured write.
            """DROP INDEX IF EXISTS idx_fleet_sync_catalog_order""",
            """CREATE INDEX IF NOT EXISTS idx_fleet_sync_peer_state_online
                ON fleet_sync_peer_state(roster_epoch,online,machine_public_key)""",
            # prune_acknowledged retires a transaction only when no winner
            # still cites it: without this index that check is a full
            # catalog scan per candidate transaction — 83k × 709k rows inside
            # one IMMEDIATE transaction, holding personal.db >45s per attempt
            # and starving every dashboard writer (live 2026-09-06). Single
            # integer column plus the address key per row; not part of the
            # compatibility digest (table_info only).
            """CREATE INDEX IF NOT EXISTS idx_fleet_sync_catalog_transaction_ref
                ON fleet_sync_catalog(transaction_ref)""",
            # Per-origin order: the watermark pager (next_transactions_for_origin),
            # origin_watermarks and the prune's newest-per-origin keeper set all
            # walk transactions by origin in timestamp order.
            """CREATE INDEX IF NOT EXISTS idx_fleet_sync_transactions_origin_ts
                ON fleet_sync_transactions(origin_id,timestamp_ns,transaction_id)""",
        )
        for statement in statements:
            self.conn.execute(statement)
        # Migrate an existing peer-state table to carry the peer's build
        # timestamp (learned from a schema-refusal), so a version mismatch can
        # name WHICH build the incompatible peer runs. Local, non-replicated
        # column; a plain additive ALTER, guarded so it runs once.
        peer_columns = {
            str(row[1])
            for row in self.conn.execute(
                "PRAGMA table_info(fleet_sync_peer_state)"
            )
        }
        if "peer_built_at" not in peer_columns:
            self.conn.execute(
                "ALTER TABLE fleet_sync_peer_state ADD COLUMN peer_built_at TEXT"
            )

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
            # Drop first, unconditionally, INCLUDING for tables this pass
            # will not re-create. Reclassifying a live table to DERIVED or
            # LOCAL otherwise leaves its capture triggers behind, and
            # _triggers_match_current_schema then never converges: the
            # refresh raises "trigger refresh incomplete" on every open.
            for operation in ("insert", "update", "delete"):
                self.conn.execute(
                    f"DROP TRIGGER IF EXISTS fleet_sync_{table}_{operation}"
                )
            if policy.kind in {PolicyKind.LOCAL, PolicyKind.DERIVED}:
                continue
            columns = self._table_columns.get(table, ())
            if not columns:
                continue  # policy table absent from THIS store: no trigger
            for sql in _trigger_sql(table, columns):
                self.conn.execute(sql)

    def _trigger_count(self) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'fleet_sync_%'"
        ).fetchone()[0])

    @staticmethod
    def _normalized_trigger_sql(sql: str) -> str:
        """Canonical comparison form for generated versus stored DDL."""
        return " ".join(sql.split()).rstrip(";")

    def _triggers_match_current_schema(self) -> bool:
        """Whether every capture trigger carries the current table columns.

        Trigger bodies freeze their ``NEW``/``OLD`` argument list when they
        are created.  Adding a replicated column therefore requires replacing
        the trigger even though its name and the catalog schema are unchanged.
        """
        expected: dict[str, str] = {}
        for table, policy in TABLE_POLICIES.items():
            if policy.kind in {PolicyKind.LOCAL, PolicyKind.DERIVED}:
                continue
            columns = self._table_columns.get(table, ())
            if not columns:
                continue  # absent from this store: nothing expected
            for operation, sql in zip(
                ("insert", "update", "delete"), _trigger_sql(table, columns),
                strict=True,
            ):
                expected[f"fleet_sync_{table}_{operation}"] = (
                    self._normalized_trigger_sql(sql)
                )
        actual = {
            str(row[0]): self._normalized_trigger_sql(str(row[1]))
            for row in self.conn.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
                "AND name LIKE 'fleet_sync_%'"
            )
        }
        return actual == expected

    def refresh_triggers_for_current_schema(self) -> bool:
        """Atomically replace stale capture DDL after a graph schema upgrade.

        No replicated row or winner metadata changes here.  The operation
        only recompiles trigger argument lists against the columns already
        installed by ``GraphDB._init_schema``.  A second opener rechecks after
        taking the writer lock so concurrent repair is idempotent.
        """
        if self._triggers_match_current_schema():
            return False
        if self.conn.in_transaction:
            raise WatermarkError("trigger refresh requires an idle connection")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            if self._triggers_match_current_schema():
                self.conn.commit()
                return False
            # This is maintenance for an ALREADY-ACTIVATED policy inventory,
            # not a new activation decision.  Do not run the whole durable-
            # table audit here: unrelated product tables may still be waiting
            # for an explicit replicate/local policy, and their separate gap
            # must not prevent existing classified tables from recompiling
            # after a column addition.  Migration and first activation retain
            # the fail-closed full audit.
            ensure_streaming_indexes(
                self.conn, manage_transaction=False, audit=False,
            )
            before = {
                str(r[0]).removeprefix("fleet_sync_").rsplit("_", 1)[0]
                for r in self.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger' "
                    "AND name LIKE 'fleet_sync_%'"
                )
            }
            self._install_triggers()
            if not self._triggers_match_current_schema():
                raise WatermarkError("fleet-sync trigger refresh incomplete")
            after = {
                table for table, policy in TABLE_POLICIES.items()
                if policy.kind not in {PolicyKind.LOCAL, PolicyKind.DERIVED}
                and self._table_columns.get(table)
            }
            # Tables that had NO capture before this refresh: rows written
            # to them so far are untracked and need a backfill. A table
            # whose trigger text merely changed needs none.
            self.newly_captured_tables = frozenset(after - before)
            self.conn.commit()
            return True
        except Exception:
            self.conn.rollback()
            raise

    def triggers_active(self) -> bool:
        replicated = sum(
            policy.kind not in {PolicyKind.LOCAL, PolicyKind.DERIVED}
            and bool(self._table_columns.get(table))
            for table, policy in TABLE_POLICIES.items()
        )
        return self._trigger_count() == replicated * 3

    def install(self) -> None:
        """Install the Alpha schema and immediately activate capture hooks.

        Production uses :meth:`migrate_existing` first and activates hooks only
        when every personal-store writer has moved to the originated adapter.
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

    def _bootstrap_existing_rows(self, *, audit: bool = True) -> int:
        """Give each untracked live row deterministic legacy winner metadata.

        For a dry run, wrap the caller's transaction so this can commit or
        roll back atomically — this function's own progress bookkeeping
        (the temp table, transaction-batch counters) depends on its writes
        actually landing as it iterates, so it cannot safely simulate
        without writing; rollback-after-run is the only safe dry-run shape.
        See ``fleet_doctor.py``'s ``--repair-catalog`` for the pattern:
        ``BEGIN IMMEDIATE``, run this, inspect the still-open transaction,
        then ``COMMIT`` or ``ROLLBACK``.
        """
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
            for mutation in iter_indexed_snapshot_mutations(
                self.conn, audit=audit,
            ):
                address_blob = encode_value([
                    mutation.table, list(mutation.address)
                ])
                existing = self.conn.execute(
                    "SELECT tombstone,timestamp_ns FROM fleet_sync_catalog "
                    "WHERE address=?",
                    (address_blob,),
                ).fetchone()
                if existing is not None:
                    if bool(existing[0]):
                        # The row exists and the catalog says it was deleted.
                        # A tombstone older than the row's own timestamp is
                        # stale (the row was re-created or restored without
                        # capture): the live row wins and is re-bootstrapped
                        # below. A tombstone NEWER than the row is a genuine
                        # contradiction and stops the reconcile, naming the row
                        # (two ~3-minute startups were spent on an anonymous
                        # version of this error, 2026-09-07).
                        if int(existing[1]) > mutation.timestamp_ns:
                            raise WatermarkError(
                                "live bootstrap row conflicts with a newer "
                                f"catalog tombstone: table={mutation.table!r} "
                                f"address={mutation.address!r} "
                                f"row_ts={mutation.timestamp_ns} "
                                f"tombstone_ts={int(existing[1])}"
                            )
                        self.conn.execute(
                            "DELETE FROM fleet_sync_catalog WHERE address=?",
                            (address_blob,),
                        )
                    else:
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

    def _repair_deprecated_settings_tombstones(self) -> int:
        """Repair the pre-fix encoding of a retired base Setting.

        Older settings triggers recorded ``deprecated: 0 -> 1`` as a live
        update even though snapshots and ``_live_row`` both define that base
        address as absent. Rewrite only the exact self-inconsistent case: the
        catalog winner is a non-tombstone and the physical row at that
        address is deprecated with the same originated timestamp. Provenance
        and operation ordering are preserved (the winner keeps its
        transaction and operation; only the tombstone flag flips).
        """
        policy = TABLE_POLICIES["settings"]
        repaired = 0
        retired = self.conn.execute(
            "SELECT * FROM settings WHERE supersedes IS NULL "
            "AND excludes IS NULL AND deprecated=1"
        ).fetchall()
        for raw in retired:
            row = dict(raw)
            address = _logical_address(policy, row)
            address_blob = encode_value(["settings", list(address)])
            winner = self.conn.execute(
                "SELECT timestamp_ns FROM fleet_sync_catalog "
                "WHERE address=? AND tombstone=0",
                (address_blob,),
            ).fetchone()
            if winner is None:
                continue
            if _row_timestamp(policy, row) != int(winner[0]):
                continue
            # Base rows collapse to one address per natural key. If a LIVE
            # base row (deprecated=0) still exists at this address, the
            # address is live and the retired row is mere history: never
            # tombstone it, or the next bootstrap finds a live row under a
            # tombstone and the reconcile stops.
            live = self.conn.execute(
                "SELECT 1 FROM settings WHERE set_id=? AND schema_revision=? "
                'AND "key"=? AND publication_state=? AND supersedes IS NULL '
                "AND excludes IS NULL AND deprecated=0 "
                "AND terminal_persona IS ? LIMIT 1",
                (row["set_id"], row["schema_revision"], row["key"],
                 row["publication_state"], row.get("terminal_persona")),
            ).fetchone()
            if live is not None:
                continue
            self.conn.execute(
                "UPDATE fleet_sync_catalog SET tombstone=1 WHERE address=?",
                (address_blob,),
            )
            repaired += 1
        return repaired

    def _verify_catalog_integrity(
        self, *, audit: bool = True,
    ) -> tuple[int, int]:
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
        for mutation in iter_indexed_snapshot_mutations(
            self.conn, audit=audit,
        ):
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
        locked current rows before installing triggers. Imported or originated
        history is never silently reinterpreted as bootstrap state.
        """
        non_bootstrap = self.conn.execute(
            "SELECT transaction_id FROM fleet_sync_transactions "
            "WHERE transaction_id NOT LIKE 'bootstrap-v1:%' LIMIT 1"
        ).fetchone()
        if non_bootstrap is not None:
            raise WatermarkError(
                "triggerless fleet-sync catalog contains originated history"
            )
        self.conn.execute("DELETE FROM fleet_sync_catalog")
        self.conn.execute("DELETE FROM fleet_sync_transactions")
        self.conn.execute("DELETE FROM fleet_sync_origins")
        return self._bootstrap_existing_rows()

    def migrate_existing(self) -> CatalogMigrationReport:
        """Atomically prepare an existing personal store for writer activation.

        This migration intentionally does *not* create capture triggers.  The
        next rollout must first route GraphDB, vault, and key-control writers
        through originated transactions, then activate the hooks in the same
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
            "fleet_sync_transactions", "fleet_sync_peer_state",
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

    def reconcile_catalog(self, *, audit: bool = True) -> CatalogMigrationReport:
        """Backfill untracked live rows into an ALREADY-ACTIVATED catalog.

        Unlike :meth:`migrate_existing`, this tolerates capture triggers. It
        repairs the one legacy Settings encoding where a retired base row was
        journaled as live, then inserts winner metadata only for live rows the
        catalog is currently missing via the idempotent skip-if-present path in
        :meth:`_bootstrap_existing_rows`.

        It repairs a production catalog that an earlier buggy bootstrap left
        incomplete -- e.g. the SQLite < 3.38 RETURNING-on-upsert gap that
        silently dropped colliding-timestamp rows during the original
        activation -- which otherwise leaves rows the catalog never tracks,
        invisible to every reader that goes through it (a bootstrap sweep
        included), with no self-healing path, because migration early-skips
        once triggers exist.

        Only synchronization metadata is written, never a replicated table,
        so no capture trigger fires. The legacy repair preserves the original
        transaction identity, timestamp, address, and operation index while
        correcting its live/tombstone bit. The final integrity scan proves
        every live row now has winner metadata before the single transaction
        commits; any shortfall rolls back.
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
        if audit:
            audit_schema(self.conn)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self._ensure_state()
            self._repair_deprecated_settings_tombstones()
            bootstrapped = self._bootstrap_existing_rows(audit=audit)
            live_rows, catalog_rows = self._verify_catalog_integrity(audit=audit)
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
            # Signed rows are one slot per signer: the address names the
            # persona. An unsigned address must never resolve to a signed
            # row at the same natural key, hence the explicit IS NULL.
            if len(address) > 5:
                clauses.append("terminal_persona=?")
                params.append(address[5])
            else:
                clauses.append("terminal_persona IS NULL")
            if role == "base":
                # The winner for a base address is the sole ``deprecated = 0``
                # base row for this natural key; the deprecated siblings are
                # superseded history the platform keeps but the base snapshot
                # (iter_indexed_snapshot_mutations) already filters out with the
                # same predicate. Without ``deprecated = 0`` here, a store that
                # accumulated duplicate base rows makes this ``.fetchone()`` pick
                # an arbitrary sibling, so the winner catalog's candidate hash
                # (built through this resolver) disagrees with the materialized
                # base row and the transfer fails with a winner/base
                # hash mismatch.
                clauses.extend([
                    "supersedes IS NULL", "excludes IS NULL", "deprecated = 0",
                ])
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
        foreign-key orphans whose parent is absent from the transfer (see
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
        """Every row change with a transaction timestamp in
        (after_watermark, through], as wire mutations, in
        (timestamp_ns, origin, transaction_id, operation_index) order --
        built from the catalog row and the live row. A row overwritten by
        a later transaction appears once, under that later transaction."""
        conn = cut.reader if cut is not None else self.conn
        through = cut.watermark if cut is not None else (1 << 63) - 1
        rows = conn.execute(
            "SELECT o.incarnation,t.transaction_id,c.operation_index,"
            "c.address,c.timestamp_ns,c.tombstone "
            "FROM fleet_sync_catalog c "
            "JOIN fleet_sync_transactions t ON t.id=c.transaction_ref "
            "JOIN fleet_sync_origins o ON o.id=t.origin_id "
            "WHERE t.timestamp_ns>? AND t.timestamp_ns<=? "
            "ORDER BY t.timestamp_ns,o.incarnation,t.transaction_id,c.operation_index",
            (after_watermark, through),
        ).fetchall()
        for origin, transaction, operation, address_blob, timestamp, tombstone in rows:
            table, address = self._decode_address(bytes(address_blob))
            if bool(tombstone):
                mutation = Mutation(table, address, int(timestamp), True)
            else:
                policy = TABLE_POLICIES[table]
                live = self._live_row(conn, table, address)
                mutation = Mutation(
                    table, address, int(timestamp), False,
                    _logical_values(policy, live),
                )
            yield AuthoredMutation(
                str(origin), str(transaction), int(operation), mutation,
            )

    def origin_watermarks(self) -> dict[str, int]:
        """``{origin incarnation: max timestamp_ns held}`` over every
        transaction this database has learned. Under the per-origin
        write-floor promise this is W[origin]: every origin-originated
        transaction at or below it is held."""
        clause = ""
        params: tuple = ()
        return {
            str(row[0]): int(row[1])
            for row in self.conn.execute(
                "SELECT o.incarnation, MAX(t.timestamp_ns) "
                "FROM fleet_sync_transactions t "
                "JOIN fleet_sync_origins o ON o.id=t.origin_id "
                + clause + "GROUP BY o.incarnation",
                params,
            )
        }
    def origin_list(self) -> list[str]:
        return [
            str(row[0]) for row in self.conn.execute(
                "SELECT incarnation FROM fleet_sync_origins ORDER BY incarnation"
            )
        ]

    def iter_transaction_items(
        self, transaction_ref: int, incarnation: str, transaction_id: str
    ) -> Iterator[AuthoredMutation]:
        """The row changes of one transaction, as wire mutations, built
        lazily in operation order from the rows that still cite the
        transaction in fleet_sync_catalog (plus rows parked in the
        quarantine with their frame) and their live rows -- exactly how a
        swept page is built. A row overwritten by a later transaction is
        absent and arrives under that later transaction. Lazy so that a
        transaction with tens of thousands of surviving rows can be served
        in bounded groups while it is built, instead of after minutes of
        silence (SJC-2's autonomy scope, 2026-09-07)."""
        ensure_quarantine_table(self.conn)
        parked = [
            (int(operation), bytes(frame))
            for frame, operation in self.conn.execute(
                "SELECT frame,operation_index FROM fleet_sync_quarantine "
                "WHERE origin=? AND transaction_id=? AND frame IS NOT NULL "
                "AND operation_index IS NOT NULL "
                "AND reason!='settings_signature_invalid'",
                (incarnation, transaction_id),
            ).fetchall()
        ]
        cited = self.conn.execute(
            "SELECT address,timestamp_ns,tombstone,operation_index "
            "FROM fleet_sync_catalog WHERE transaction_ref=? "
            "ORDER BY operation_index",
            (int(transaction_ref),),
        ).fetchall()
        # Merge the two operation-ordered sequences without materializing
        # the mutations up front; the catalog list holds small tuples only.
        parked.sort(key=lambda p: p[0])
        p_index = 0
        for raw in cited:
            operation = int(raw[3])
            while p_index < len(parked) and parked[p_index][0] < operation:
                yield AuthoredMutation(
                    incarnation, transaction_id, parked[p_index][0],
                    decode_mutation_frame(parked[p_index][1]),
                )
                p_index += 1
            table, address = self._decode_address(bytes(raw[0]))
            if not self._serveable(table, address, transaction_id):
                continue
            if bool(raw[2]):
                mutation = Mutation(table, address, int(raw[1]), True)
            else:
                policy = TABLE_POLICIES[table]
                row = self._live_row(self.conn, table, address)
                mutation = Mutation(
                    table, address, int(raw[1]), False,
                    _logical_values(policy, row),
                )
            yield AuthoredMutation(incarnation, transaction_id, operation, mutation)
        while p_index < len(parked):
            yield AuthoredMutation(
                incarnation, transaction_id, parked[p_index][0],
                decode_mutation_frame(parked[p_index][1]),
            )
            p_index += 1

    def _serveable(self, table: str, address: tuple, transaction_id: str) -> bool:
        """Whether a cited address still names something this store may send.

        Scoped deliberately to RETIRED tables, not to every non-replicating
        one. A retired table keeps its catalog addresses until the
        housekeeping purge runs, and across a fleet the code deploy and that
        purge are not ordered with respect to each other. Constructing a
        Mutation for a non-replicating table raises CodecError in
        __post_init__, so without this the FIRST leftover address ends every
        serve of the scope -- the sender-side twin of the receive-side abort
        that stalled the autonomy scope for a day.

        A LOCAL table appearing in a catalog is a different thing entirely: it
        has no capture triggers, so an address for one should be impossible.
        Swallowing that quietly would hide a real defect, so it is left to
        raise.
        """
        if table not in RETIRED_LOGICAL_TABLES:
            return True
        logger.warning(
            "fleet sync: catalog cites the retired table %s; skipping it in "
            "the serve (run drop_retired_entity_tables to purge it): "
            "address=%r transaction=%s",
            table, address, transaction_id,
        )
        return False

    def transaction_group(
        self, transaction_ref: int, incarnation: str, transaction_id: str,
        *, offset: int, limit: int,
    ) -> tuple[list[AuthoredMutation], bool]:
        """One bounded slice of a transaction's items, built from rows on
        THIS connection: the cited catalog rows ``[offset, offset+limit)``
        in operation order, plus (in the first slice only) the rows parked
        in the quarantine with their frame. Returns ``(items, more)``.
        Stateless per call so a serve can fetch each slice on whatever
        worker thread it runs on (sqlite connections are thread-bound)."""
        items: list[AuthoredMutation] = []
        if offset == 0:
            ensure_quarantine_table(self.conn)
            for frame, operation in self.conn.execute(
                "SELECT frame,operation_index FROM fleet_sync_quarantine "
                "WHERE origin=? AND transaction_id=? AND frame IS NOT NULL "
                "AND operation_index IS NOT NULL "
                "AND reason!='settings_signature_invalid'",
                (incarnation, transaction_id),
            ).fetchall():
                items.append(AuthoredMutation(
                    incarnation, transaction_id, int(operation),
                    decode_mutation_frame(bytes(frame)),
                ))
        rows = self.conn.execute(
            "SELECT address,timestamp_ns,tombstone,operation_index "
            "FROM fleet_sync_catalog WHERE transaction_ref=? "
            "ORDER BY operation_index LIMIT ? OFFSET ?",
            (int(transaction_ref), int(limit), int(offset)),
        ).fetchall()
        for raw in rows:
            table, address = self._decode_address(bytes(raw[0]))
            if not self._serveable(table, address, transaction_id):
                continue
            if bool(raw[2]):
                mutation = Mutation(table, address, int(raw[1]), True)
            else:
                policy = TABLE_POLICIES[table]
                try:
                    row = self._live_row(self.conn, table, address)
                except WatermarkError as exc:
                    # The catalog cites a live row this store cannot
                    # resolve (a settings base row deprecated in place
                    # without a tombstone, or a row lost without capture).
                    # One such row must not end every serve of the scope
                    # at this transaction forever. A settings base address
                    # whose only rows are deprecated IS absent by the
                    # platform's rule, so it is served as a tombstone at
                    # the catalog's timestamp; anything else is skipped and
                    # named, for the reconcile to repair.
                    if table == "settings" and str(address[4]) == "base":
                        logger.warning(
                            "fleet sync: catalog cites a settings base with "
                            "no live row; serving a tombstone: address=%r "
                            "transaction=%s (%s)", address, transaction_id, exc,
                        )
                        mutation = Mutation(table, address, int(raw[1]), True)
                    else:
                        logger.warning(
                            "fleet sync: catalog cites a live row this store "
                            "cannot resolve; skipping it in the serve: "
                            "table=%s address=%r transaction=%s (%s)",
                            table, address, transaction_id, exc,
                        )
                        continue
                else:
                    mutation = Mutation(
                        table, address, int(raw[1]), False,
                        _logical_values(policy, row),
                    )
            items.append(AuthoredMutation(
                incarnation, transaction_id, int(raw[3]), mutation,
            ))
        items.sort(key=lambda item: item.operation_index)
        return items, len(rows) == int(limit)

    def transaction_items(
        self, transaction_ref: int, incarnation: str, transaction_id: str
    ) -> list[AuthoredMutation]:
        """All of ``iter_transaction_items`` as a list."""
        return list(self.iter_transaction_items(
            transaction_ref, incarnation, transaction_id,
        ))

    def next_transaction_heads_for_origin(
        self,
        incarnation: str,
        after_timestamp_ns: int,
        after_transaction_id: str | None = None,
        *,
        limit: int = 200,
    ) -> list[tuple[int, int, str]]:
        """Up to *limit* ``(ref, timestamp_ns, transaction_id)`` of
        *incarnation* after the position, in (timestamp_ns, transaction_id)
        order, without building any items."""
        if after_transaction_id is None:
            where = "t.timestamp_ns>?"
            params: tuple = (incarnation, int(after_timestamp_ns), int(limit))
        else:
            where = "(t.timestamp_ns>? OR (t.timestamp_ns=? AND t.transaction_id>?))"
            params = (incarnation, int(after_timestamp_ns), int(after_timestamp_ns),
                      after_transaction_id, int(limit))
        return [
            (int(r[0]), int(r[1]), str(r[2]))
            for r in self.conn.execute(
                "SELECT t.id,t.timestamp_ns,t.transaction_id "
                "FROM fleet_sync_transactions t "
                "JOIN fleet_sync_origins o ON o.id=t.origin_id "
                f"WHERE o.incarnation=? AND {where} "
                "ORDER BY t.timestamp_ns, t.transaction_id LIMIT ?",
                params,
            ).fetchall()
        ]

    def next_transaction_after_ref(
        self, after_transaction_ref: int = 0,
    ) -> tuple[int, list[AuthoredMutation]] | None:
        """The next transaction this database learned after one local row
        id, with its items built from rows (``transaction_items``). Row ids
        are local and never wire authority; this is the walk a test or a
        repair tool uses to drain a store in learned order."""
        row = self.conn.execute(
            "SELECT t.id,o.incarnation,t.transaction_id "
            "FROM fleet_sync_transactions t "
            "JOIN fleet_sync_origins o ON o.id=t.origin_id "
            "WHERE t.id>? ORDER BY t.id LIMIT 1",
            (int(after_transaction_ref),),
        ).fetchone()
        if row is None:
            return None
        ref = int(row[0])
        return ref, self.transaction_items(ref, str(row[1]), str(row[2]))

    def next_transaction_for_origin(
        self,
        incarnation: str,
        after_timestamp_ns: int,
        after_transaction_id: str | None = None,
    ) -> tuple[int, int, str, list[AuthoredMutation]] | None:
        """The next transaction by *incarnation* strictly after the given
        position in (timestamp_ns, transaction_id) order, or None."""
        page = self.next_transactions_for_origin(
            incarnation, after_timestamp_ns, after_transaction_id, limit=1,
        )
        return page[0] if page else None

    def next_transactions_for_origin(
        self,
        incarnation: str,
        after_timestamp_ns: int,
        after_transaction_id: str | None = None,
        *,
        limit: int = 200,
    ) -> list[tuple[int, int, str, list[AuthoredMutation]]]:
        """Up to *limit* retained transactions by *incarnation* after the
        position, in (timestamp_ns, transaction_id) order, on ONE
        connection. Serving one transaction per connection open made the
        busiest server take ~180 s per puller at N=20 (2026-09-07)."""
        if after_transaction_id is None:
            where = "t.timestamp_ns>?"
            params: tuple = (incarnation, int(after_timestamp_ns), int(limit))
        else:
            where = "(t.timestamp_ns>? OR (t.timestamp_ns=? AND t.transaction_id>?))"
            params = (incarnation, int(after_timestamp_ns), int(after_timestamp_ns),
                      after_transaction_id, int(limit))
        heads = self.conn.execute(
            "SELECT t.id,t.timestamp_ns,t.transaction_id "
            "FROM fleet_sync_transactions t "
            "JOIN fleet_sync_origins o ON o.id=t.origin_id "
            f"WHERE o.incarnation=? AND {where} "
            "ORDER BY t.timestamp_ns, t.transaction_id LIMIT ?",
            params,
        ).fetchall()
        # Every transaction after the position is served, retained journal
        # or not: transaction_items rebuilds retired frames from the
        # catalog and live rows, and a transaction with nothing surviving
        # comes back empty so the puller still advances its watermark.
        return [
            (int(head[0]), int(head[1]), str(head[2]),
             self.transaction_items(int(head[0]), incarnation, str(head[2])))
            for head in heads
        ]

    def drain_pending_signatures(self) -> int:
        """Re-apply signed settings rows parked as ``settings_signature_pending``
        (this store had no organization genesis to verify against when they
        arrived). Each entry goes back through ordinary apply: it lands, or
        it is inert against a newer winner, or it is re-parked (still
        pending, or now provably invalid). Returns the entries cleared."""
        ensure_quarantine_table(self.conn)
        rows = self.conn.execute(
            "SELECT address,frame,origin,transaction_id,operation_index "
            "FROM fleet_sync_quarantine WHERE reason='settings_signature_pending' "
            "AND frame IS NOT NULL AND origin IS NOT NULL "
            "AND transaction_id IS NOT NULL AND operation_index IS NOT NULL"
        ).fetchall()
        cleared = 0
        for address, frame, origin, transaction_id, operation_index in rows:
            applied, ignored = self.apply_remote_batch([AuthoredMutation(
                str(origin), str(transaction_id), int(operation_index),
                decode_mutation_frame(bytes(frame)),
            )])
            if not applied and not ignored:
                continue
            with self.conn:
                self.conn.execute(
                    "DELETE FROM fleet_sync_quarantine WHERE address=?",
                    (bytes(address),),
                )
            cleared += 1
        return cleared

    def record_transactions(
        self, entries: Sequence[tuple[str, str, int]]
    ) -> int:
        """Record transactions served with no surviving rows, so this
        machine's watermark for their origin advances past them. One
        commit; a transaction already known is left alone."""
        if not entries:
            return 0
        if self._context is not None or self.conn.in_transaction:
            raise WatermarkError("cannot record transactions inside another transaction")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for origin, transaction_id, timestamp_ns in entries:
                self._ensure_transaction(
                    str(origin), str(transaction_id), int(timestamp_ns)
                )
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        return len(entries)

    def implied_ack_ref(self, watermarks: dict[str, int]) -> int:
        """The journal position a per-origin watermark map proves consumed:
        the largest local transaction id such that every transaction at or
        below it has timestamp_ns <= W[its origin] (0 for an origin absent
        from the map). Feeds the existing served-ack prune floor unchanged,
        so retention keeps its proven invariant."""
        first_uncovered: int | None = None
        for origin in self.origin_list():
            row = self.conn.execute(
                "SELECT MIN(t.id) FROM fleet_sync_transactions t "
                "JOIN fleet_sync_origins o ON o.id=t.origin_id "
                "WHERE o.incarnation=? AND t.timestamp_ns>?",
                (origin, int(watermarks.get(origin, 0))),
            ).fetchone()
            if row is not None and row[0] is not None:
                candidate = int(row[0])
                if first_uncovered is None or candidate < first_uncovered:
                    first_uncovered = candidate
        if first_uncovered is None:
            newest = self.conn.execute(
                "SELECT COALESCE(MAX(id),0) FROM fleet_sync_transactions"
            ).fetchone()
            return int(newest[0]) if newest else 0
        return max(0, first_uncovered - 1)

    def journal_breadcrumb(
        self, transaction_ref: int
    ) -> tuple[str, str, int] | None:
        """The breadcrumb naming one locally learned transaction, or None."""
        if (
            isinstance(transaction_ref, bool)
            or not isinstance(transaction_ref, int)
            or transaction_ref <= 0
        ):
            return None
        row = self.conn.execute(
            "SELECT o.incarnation,t.transaction_id,t.timestamp_ns "
            "FROM fleet_sync_transactions t "
            "JOIN fleet_sync_origins o ON o.id=t.origin_id WHERE t.id=?",
            (transaction_ref,),
        ).fetchone()
        if row is None:
            return None
        return str(row[0]), str(row[1]), int(row[2])

    def newest_transaction_ref(self) -> int:
        """The newest transaction row id, journal-backed or not.

        A served bootstrap delivers content through the frontier, so its
        done-summary may acknowledge through this row even when the journal
        is empty (fully pruned, or a fresh joiner): resume trails
        resolve against transaction rows, not journal frames, so the next
        pull becomes a delta from here instead of another bootstrap.
        """
        row = self.conn.execute(
            "SELECT MAX(id) FROM fleet_sync_transactions"
        ).fetchone()
        return int(row[0] or 0)

    def journal_resume_ref(
        self, breadcrumbs: Iterable[tuple[str, str, int]]
    ) -> int:
        """Resolve a peer's content-addressed resume trail to a local position.

        Journal row ids are never authority a peer may present: restoring
        this database from a backup renumbers them, and a stale numeric
        cursor silently skips everything originated afterwards.  The peer
        instead presents breadcrumbs naming transactions it has already
        verified — each one a durable promise that it consumed this
        journal's complete prefix through that transaction — and the resume
        position is recomputed here, from this journal's current rows, on
        every pull.

        The result is the newest presented transaction this journal still
        knows.  Pre-restore rows keep their ids (they came back with the
        backup), so a trail that reaches behind the backup point bounds the
        replay to the divergence window; an empty or entirely unknown trail
        replays the retained journal, which deterministic merge makes inert
        on the peer.
        """
        best = 0
        for origin, transaction_id, timestamp_ns in breadcrumbs:
            row = self.conn.execute(
                "SELECT t.id,t.timestamp_ns FROM fleet_sync_transactions t "
                "JOIN fleet_sync_origins o ON o.id=t.origin_id "
                "WHERE o.incarnation=? AND t.transaction_id=?",
                (origin, transaction_id),
            ).fetchone()
            if row is None or int(row[1]) != timestamp_ns:
                continue
            best = max(best, int(row[0]))
        return best

    def record_served_ack(
        self, machine_public_key: str, roster_epoch: str,
        acked_transaction_ref: int,
    ) -> None:
        """Record a peer's implicit acknowledgement of this journal's prefix.

        The acknowledgement is the peer's own resume trail: presenting a
        breadcrumb that resolves to local transaction row ``N`` is a durable
        promise that the peer consumed this journal's complete prefix through
        ``N`` (see ``journal_resume_ref``).  The ref is a *local row id*, not
        a timestamp — serving order is id order, and remote-imported
        transactions may carry timestamps far behind their ids, so a
        timestamp floor could retire frames a slow peer never consumed.  Row
        ids and this table restore together from the same backup, so the
        stored refs rewind with the journal they describe.
        """
        if (
            isinstance(acked_transaction_ref, bool)
            or not isinstance(acked_transaction_ref, int)
            or acked_transaction_ref <= 0
        ):
            raise WatermarkError("served acknowledgement ref is malformed")
        with self.conn:
            self.conn.execute(
                "INSERT INTO fleet_sync_peer_state("
                "machine_public_key,roster_epoch,online,local_watermark,"
                "updated_at_ns) VALUES(?,?,1,?,?) "
                "ON CONFLICT(machine_public_key,roster_epoch) DO UPDATE SET "
                "local_watermark=CASE "
                "WHEN fleet_sync_peer_state.local_watermark IS NULL OR "
                "excluded.local_watermark>fleet_sync_peer_state.local_watermark "
                "THEN excluded.local_watermark "
                "ELSE fleet_sync_peer_state.local_watermark END,"
                "updated_at_ns=excluded.updated_at_ns",
                (
                    machine_public_key, roster_epoch,
                    acked_transaction_ref, time.time_ns(),
                ),
            )

    def acknowledged_journal_floor(
        self, machine_public_keys: Sequence[str], roster_epoch: str
    ) -> int | None:
        """The journal prefix every listed peer has acknowledged, or None.

        None means the floor is unavailable — some listed machine has never
        presented a resolvable resume trail this epoch — and nothing may be
        retired.  An empty machine list is also None: a solo machine keeps
        its journal, because a concurrently enrolling peer may already be
        mid-pull against a roster snapshot this process has not seen yet.
        """
        keys = list(machine_public_keys)
        if not keys:
            return None
        rows = dict(self.conn.execute(
            "SELECT machine_public_key,local_watermark "
            "FROM fleet_sync_peer_state WHERE roster_epoch=? "
            "AND machine_public_key IN ("
            + ",".join("?" for _ in keys) + ")",
            (roster_epoch, *keys),
        ).fetchall())
        floors = [rows.get(key) for key in keys]
        if any(floor is None for floor in floors):
            return None
        return min(int(floor) for floor in floors)

    def prune_acknowledged(
        self, machine_public_keys: Sequence[str], roster_epoch: str,
        *, budget_s: float = PRUNE_BUDGET_S,
        batch: int = PRUNE_BATCH_REFS,
    ) -> tuple[int, int]:
        """Retire journal frames every active peer has acknowledged.

        Deletes journal rows with ``transaction_ref <= floor`` and
        transaction rows strictly below the floor that no longer own journal
        rows.  The floor transaction row itself is kept: it is the slowest
        peer's newest breadcrumb, and ``journal_resume_ref`` must keep
        resolving it.  Rows from other roster epochs in
        ``fleet_sync_peer_state`` are dropped in the same pass — every
        reader keys on the current epoch, so they are dead weight.

        INCREMENTAL and lock-bounded: work proceeds in ``batch``-sized id
        ranges, each its own short write transaction, and stops once
        ``budget_s`` has elapsed — the caller's next pass continues where
        this one left off.  This database is the live personal graph; the
        first prune after a fleet member's first acknowledgement retires
        the ENTIRE journal, and doing that in one transaction held
        personal.db for >45s per attempt and starved every dashboard writer
        (live 2026-09-06).  Returns ``(journal_rows_deleted,
        transaction_rows_deleted)`` for THIS pass.
        """
        if self._context is not None or self.conn.in_transaction:
            raise WatermarkError("cannot prune journal inside a transaction")
        floor = self.acknowledged_journal_floor(
            machine_public_keys, roster_epoch
        )
        if floor is None or floor <= 0:
            return (0, 0)
        deadline = time.monotonic() + budget_s
        journal_rows = 0  # no journal any more; kept in the return shape
        transaction_rows = 0

        def within_budget() -> bool:
            return time.monotonic() < deadline

        # An origin's newest transaction is its watermark on this machine;
        # deleting it would make the map ask for (and be re-served) what it
        # already holds. Compute the keepers ONCE per pass (one row per
        # origin, index-backed) instead of a correlated sort per candidate:
        # the correlated form scanned every transaction of the origin for
        # every candidate inside one statement the budget could not
        # interrupt, holding the store's write lock for far longer than
        # PRUNE_BUDGET_S on a large scope (home, 2026-09-07 04:45Z).
        # One grouped scan over the origin index: the previous correlated
        # NOT EXISTS used the index on origin_id only and rescanned every
        # transaction of the origin for every row (73k x 73k on anchore:
        # 152-171 s per pass retiring nothing, measured 2026-09-07 08:00Z).
        # Ties at the newest timestamp keep every tied id; extra keepers
        # are always safe.
        keepers = [
            int(r[0]) for r in self.conn.execute(
                "SELECT t.id FROM fleet_sync_transactions t JOIN ("
                "SELECT origin_id, MAX(timestamp_ns) AS newest "
                "FROM fleet_sync_transactions GROUP BY origin_id"
                ") m ON m.origin_id=t.origin_id AND m.newest=t.timestamp_ns"
            )
        ]
        keeper_clause = ""
        if keepers:
            keeper_clause = " AND id NOT IN (" + ",".join("?" * len(keepers)) + ")"
        retirable = (
            "NOT EXISTS(SELECT 1 FROM fleet_sync_catalog c "
            "WHERE c.transaction_ref=fleet_sync_transactions.id)"
            + keeper_clause
        )
        # Sweep in fixed id windows from where the previous pass stopped,
        # and wrap to 0 once the floor is reached, so a row that becomes
        # retirable later (its winner overwritten) is reached on the next
        # sweep. Every statement touches at most one window, so a pass
        # costs about its budget however large the store. The old
        # "jump to the next retirable id" MIN(id) query probed every
        # still-cited row below the floor on every pass.
        cursor = self.prune_cursor()
        if cursor >= floor:
            cursor = 0
        swept_all = False
        while cursor < floor:
            high = min(cursor + batch, floor)
            with self.conn:
                transaction_rows += int(self.conn.execute(
                    "DELETE FROM fleet_sync_transactions "
                    f"WHERE id>=? AND id<? AND {retirable}",
                    (cursor, high, *keepers),
                ).rowcount)
            cursor = high
            if cursor >= floor:
                swept_all = True
            time.sleep(PRUNE_YIELD_S)
            if not within_budget():
                break
        with self.conn:
            self.conn.execute(
                "INSERT INTO fleet_sync_prune_cursor(singleton,cursor) VALUES(1,?) "
                "ON CONFLICT(singleton) DO UPDATE SET cursor=excluded.cursor",
                (0 if swept_all else cursor,),
            )
        with self.conn:
            self.conn.execute(
                "DELETE FROM fleet_sync_peer_state WHERE roster_epoch<>?",
                (roster_epoch,),
            )
        return (journal_rows, transaction_rows)

    def prune_cursor(self) -> int:
        """Where the served-ack prune's sweep resumes (0 = a fresh sweep)."""
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS fleet_sync_prune_cursor("
            "singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
            "cursor INTEGER NOT NULL)"
        )
        row = self.conn.execute(
            "SELECT cursor FROM fleet_sync_prune_cursor WHERE singleton=1"
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def apply_remote(self, originated: AuthoredMutation) -> bool:
        """Merge one trusted remote mutation atomically; return winner status."""
        applied, _ = self.apply_remote_batch([originated])
        return applied == 1

    def apply_remote_batch(
        self, authored_items: Iterable[AuthoredMutation]
    ) -> tuple[int, int]:
        """Atomically merge one originated transaction in dependency-safe order."""
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
            raise WatermarkError("remote batch crosses an originated transaction")
        if len({item.operation_index for item in items}) != len(items):
            raise WatermarkError("remote transaction repeats an operation index")
        if self._context is not None or self.conn.in_transaction:
            raise WatermarkError("cannot apply remote inside another transaction")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            winners: list[tuple[AuthoredMutation, bytes]] = []
            ignored = 0
            for originated in items:
                mutation = originated.mutation
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
                        winners.append((originated, address_blob))
                        continue
                    if current_timestamp > mutation.timestamp_ns:
                        ignored += 1
                        continue
                    if current_timestamp == mutation.timestamp_ns:
                        if (str(current[1]), str(current[2])) == identity:
                            if int(current[3]) >= originated.operation_index:
                                ignored += 1
                                continue
                            winners.append((originated, address_blob))
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
                winners.append((originated, address_blob))
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
            report = materialize(
                self.conn, [item.mutation for item, _ in winners],
                manage_transaction=False,
                blob_store=self.blob_store,
            )
            # Rows materialize could not realize — attachments whose bytes no
            # local file satisfies, and foreign-key orphans — are deferred:
            # journaled (a downstream peer may realize them) but excluded
            # from the winner catalog and the hash verification, and
            # quarantined with their canonical frame so the attachment
            # transport can drain them without a re-pull. Raising here would
            # poison the batch on every retry, since the peer's resume trail
            # advances past this transaction regardless.
            deferred = {
                encode_value(["attachments", [attachment_id]])
                for attachment_id in report.pending_attachments
            }
            deferred.update(
                encode_value([table, list(address)])
                for table, address in report.skipped_orphans
            )
            # Signed settings rows whose envelope did not verify here: an
            # invalid signature is never stored and never forwarded; a
            # row this store cannot verify yet (no organization genesis
            # known) is parked with its frame and re-applied by the drain
            # once the genesis has arrived.
            rejected_reasons = {
                encode_value([table, list(address)]): reason
                for table, address, reason in report.rejected_signatures
            }
            deferred.update(rejected_reasons)
            final_winners = {
                address_blob: originated for originated, address_blob in winners
            }
            for address_blob, originated in final_winners.items():
                mutation = originated.mutation
                if mutation.tombstone or address_blob in deferred:
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
                        "materialized row does not preserve canonical mutation bytes: "
                        f"table={mutation.table!r} address={mutation.address!r} "
                        f"expected={mutation.candidate_hash.hex()[:16]} "
                        f"installed={installed.candidate_hash.hex()[:16]}"
                    )
            deferred_count = 0
            for originated, address_blob in winners:
                mutation = originated.mutation
                if address_blob in deferred:
                    deferred_count += 1
                else:
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
                            originated.operation_index,
                        ),
                    )
            if deferred:
                deferred_rows = []
                replay: dict[bytes, tuple[bytes, str, str, int]] = {}
                for originated, address_blob in winners:
                    if address_blob not in deferred:
                        continue
                    mutation = originated.mutation
                    reason = rejected_reasons.get(address_blob) or (
                        "attachment_bytes_unavailable"
                        if mutation.table == "attachments" else "fk_orphan"
                    )
                    deferred_rows.append(
                        (mutation.table, tuple(mutation.address), reason)
                    )
                    replay[address_blob] = (
                        encode_mutation_frame(mutation),
                        originated.origin_incarnation,
                        originated.transaction_id,
                        originated.operation_index,
                    )
                quarantine_unrealized(
                    self.conn, deferred_rows,
                    watermark=timestamp, replay=replay, commit=False,
                )
            self.conn.execute(
                "UPDATE fleet_sync_state SET last_timestamp="
                "MAX(last_timestamp,?) WHERE singleton=1",
                (mutation.timestamp_ns,),
            )
            self.conn.commit()
            return len(winners) - deferred_count, ignored
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
    # Schema rollforward for already-activated stores: install() runs once
    # at activation and never again, so a local object added to
    # _create_schema_objects later (an index, a state table) otherwise never
    # reaches production — live 2026-09-06, the prune index shipped but the
    # personal store still had only autoindexes.
    catalog.ensure_schema_objects()
    refreshed = catalog.refresh_triggers_for_current_schema()
    if not catalog.triggers_active():
        raise WatermarkError("fleet-sync capture triggers are only partially active")
    if refreshed:
        # Recompiling trigger text changes no data. The only case that
        # needs a backfill is a table that gained capture for the first
        # time (a package upgrade classified it) and already holds rows.
        # Running the full reconcile on every refresh cost ~165 s per org
        # store inside dashboard startup, twice on 2026-09-07 (auto-boa0j).
        # NEVER backfill a retired table. Its triggers can reappear whenever a
        # process still running pre-retirement code re-attaches: that build's
        # TABLE_POLICIES still calls the table replicated, so it computes the
        # table as newly captured and installs capture again. On sjc-2 that
        # cost a 39.5s backfill of 325k junk winner rows (2026-09-08). Filter
        # here so a current build never adds to the mess a stale one makes,
        # and the state settles as soon as every process is on this code.
        untracked = {
            table: int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in sorted(getattr(catalog, "newly_captured_tables", ()))
            if table not in RETIRED_LOGICAL_TABLES
        }
        untracked = {t: n for t, n in untracked.items() if n}
        if untracked:
            started = time.monotonic()
            logger.warning(
                "fleet sync: capture newly installed on %s with existing rows; "
                "backfilling winner metadata", untracked,
            )
            catalog.reconcile_catalog(audit=False)
            logger.warning(
                "fleet sync: backfill finished in %.1fs", time.monotonic() - started,
            )
        else:
            logger.info(
                "fleet sync: capture triggers recompiled; no backfill needed"
            )
    conn.install_fleet_sync_hook(catalog)
    return catalog
