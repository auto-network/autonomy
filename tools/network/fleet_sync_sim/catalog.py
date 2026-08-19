"""Transactional current-winner/tombstone catalog for fleet-sync alpha.

The catalog is deliberately skinny.  Live payload remains in the authoritative
graph table; the catalog contains only the canonical logical address and
replication metadata.  Deletes remain as tombstones.  SQLite triggers make a
local graph mutation and its catalog transition one transaction, while a
frozen WAL snapshot gives the serializer an immutable cut as writers continue.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
from pathlib import Path
import sqlite3
from typing import Iterable, Iterator
import zlib

from .codec import (
    MAX_FRAME_BYTES, Mutation, decode_mutation_frame, decode_value, encode_mutation_frame,
    encode_value,
)
from .compaction import AuthoredMutation, WatermarkError
from .materialize import materialize
from .merge import mutation_wins
from .policies import EXCLUDED_SETTING_SET_IDS, PolicyKind, TABLE_POLICIES
from .snapshot import _logical_address, _logical_values


CATALOG_SCHEMA_VERSION = 2
JOURNAL_STORAGE_VERSION = 1
MAX_TRANSACTION_OPERATIONS = 16_384
MAX_TRANSACTION_FRAME_BYTES = 128 * 1024 * 1024
_table_columns: dict[str, tuple[str, ...]] = {}


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

    def install(self) -> None:
        if self.conn.in_transaction:
            self.conn.commit()
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS fleet_sync_state(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                schema_version INTEGER NOT NULL,
                origin_incarnation TEXT NOT NULL,
                write_floor INTEGER NOT NULL,
                last_timestamp INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS fleet_sync_catalog(
                address BLOB NOT NULL,
                timestamp_ns INTEGER NOT NULL,
                tombstone INTEGER NOT NULL CHECK(tombstone IN (0,1)),
                transaction_ref INTEGER NOT NULL,
                operation_index INTEGER NOT NULL,
                PRIMARY KEY(address),
                FOREIGN KEY(transaction_ref) REFERENCES fleet_sync_transactions(id)
            ) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS fleet_sync_origins(
                id INTEGER PRIMARY KEY,
                incarnation TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS fleet_sync_transactions(
                id INTEGER PRIMARY KEY,
                origin_id INTEGER NOT NULL,
                transaction_id TEXT NOT NULL,
                timestamp_ns INTEGER NOT NULL,
                UNIQUE(origin_id,transaction_id),
                FOREIGN KEY(origin_id) REFERENCES fleet_sync_origins(id)
            );
            CREATE TABLE IF NOT EXISTS fleet_sync_journal(
                transaction_ref INTEGER NOT NULL,
                operation_index INTEGER NOT NULL,
                frame BLOB NOT NULL,
                PRIMARY KEY(transaction_ref,operation_index),
                FOREIGN KEY(transaction_ref) REFERENCES fleet_sync_transactions(id)
            ) WITHOUT ROWID;
            CREATE INDEX IF NOT EXISTS idx_fleet_sync_catalog_order
                ON fleet_sync_catalog(timestamp_ns,transaction_ref,operation_index,address);
        """)
        row = self.conn.execute(
            "SELECT schema_version,origin_incarnation FROM fleet_sync_state "
            "WHERE singleton=1"
        ).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO fleet_sync_state VALUES(1,?,?,0,0)",
                (CATALOG_SCHEMA_VERSION, self.origin_incarnation),
            )
        elif tuple(row) != (CATALOG_SCHEMA_VERSION, self.origin_incarnation):
            raise WatermarkError("fleet-sync catalog identity/version mismatch")
        for table, policy in TABLE_POLICIES.items():
            if policy.kind in {PolicyKind.LOCAL, PolicyKind.DERIVED}:
                continue
            for operation in ("insert", "update", "delete"):
                self.conn.execute(
                    f"DROP TRIGGER IF EXISTS fleet_sync_{table}_{operation}"
                )
            for sql in _trigger_sql(table):
                self.conn.execute(sql)
        self.conn.commit()

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
        self, entries: Iterable[WinnerMetadata]
    ) -> int:
        """Verify a realized exact base and install its skinny winner state."""
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
                    raise WatermarkError("winner metadata/base hash mismatch")
                transaction_key = (
                    item.origin_incarnation, item.transaction_id,
                    item.timestamp_ns,
                )
                if transaction_key != last_transaction:
                    transaction_ref = self._ensure_transaction(*transaction_key)
                    last_transaction = transaction_key
                address_blob = encode_value([item.table, list(item.address)])
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
