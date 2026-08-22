"""Crash-safe local spool for immutable official usage batches.

The spool is deliberately service-neutral.  It owns persistence, retry order,
exact acknowledgement, sequence continuity, bounded capacity, and health; it
does not know how a batch was measured or where it will be delivered.

SQLite transactions run with a rollback journal and ``synchronous=FULL``.  A
successful :meth:`UsageSpool.append` has therefore crossed the local durable
commit boundary before the caller may attempt network delivery.  One process
owns a spool database at a time; a non-blocking advisory lock rejects a second
producer instead of allowing ambiguous sequence allocation.
"""

from __future__ import annotations

import fcntl
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .batch import UsageBatch, UsageBatchError

SPOOL_SCHEMA_VERSION = 1
DEFAULT_MAX_RECORDS = 100_000
DEFAULT_MAX_BYTES = 1024 * 1024 * 1024


class UsageSpoolError(RuntimeError):
    """Base class for local spool failures."""


class UsageSpoolLocked(UsageSpoolError):
    """Another process already owns the spool."""


class UsageSpoolCorrupt(UsageSpoolError):
    """Stored state or batch bytes failed integrity validation."""


class UsageSpoolConflict(UsageSpoolError):
    """A known logical batch identity was presented with different content."""


class UsageSpoolCapacity(UsageSpoolError):
    """Appending would exceed the configured durable backlog bound."""


class UsageSpoolSequenceError(UsageSpoolError):
    """A producer/organization stream skipped or replayed a sequence."""


class UsageSpoolAckError(UsageSpoolError):
    """An acknowledgement did not exactly name a stored batch and checksum."""


class UsageSpoolIOError(UsageSpoolError):
    """The local durable store could not complete an operation."""


@dataclass(frozen=True)
class SpoolRecord:
    ordinal: int
    batch: UsageBatch
    wire: bytes
    acknowledged: bool


@dataclass(frozen=True)
class SpoolHealth:
    pending_records: int
    pending_bytes: int
    acknowledged_records: int
    acknowledged_bytes: int
    retained_bytes: int
    stream_count: int
    oldest_interval_end: int | None
    newest_interval_end: int | None
    max_records: int
    max_bytes: int


def _raise_sqlite(exc: sqlite3.Error) -> None:
    message = str(exc).lower()
    if "malformed" in message or "corrupt" in message:
        raise UsageSpoolCorrupt(f"usage spool database is corrupt: {exc}") from exc
    raise UsageSpoolIOError(f"usage spool storage operation failed: {exc}") from exc


class UsageSpool:
    """Exclusive, bounded, durable queue of canonical usage-batch bytes."""

    def __init__(
        self,
        path: str | Path,
        *,
        max_records: int = DEFAULT_MAX_RECORDS,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        if (
            isinstance(max_records, bool)
            or not isinstance(max_records, int)
            or max_records < 1
        ):
            raise ValueError("max_records must be a positive integer")
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes < 1
        ):
            raise ValueError("max_bytes must be a positive integer")
        self.path = Path(path)
        self.max_records = max_records
        self.max_bytes = max_bytes
        self._mutex = threading.RLock()
        self._closed = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.path.with_name(self.path.name + ".lock")
        self._lock_file = self._lock_path.open("a+b")
        os.chmod(self._lock_path, 0o600)
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock_file.close()
            raise UsageSpoolLocked(f"usage spool is already open: {self.path}") from exc

        try:
            self._db = sqlite3.connect(
                self.path,
                timeout=10,
                isolation_level=None,
                check_same_thread=False,
            )
            self._db.execute("PRAGMA journal_mode=DELETE")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._initialize()
            check = self._db.execute("PRAGMA quick_check").fetchone()
            if check != ("ok",):
                raise UsageSpoolCorrupt(f"usage spool quick-check failed: {check!r}")
        except UsageSpoolError:
            self._release_failed_open()
            raise
        except sqlite3.Error as exc:
            self._release_failed_open()
            _raise_sqlite(exc)

    def _release_failed_open(self) -> None:
        db = getattr(self, "_db", None)
        if db is not None:
            db.close()
        fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
        self._lock_file.close()
        self._closed = True

    def _initialize(self) -> None:
        with self._transaction():
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS spool_meta ("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS streams ("
                "producer TEXT NOT NULL, organization_id TEXT NOT NULL, "
                "high_sequence INTEGER NOT NULL, "
                "PRIMARY KEY (producer, organization_id))"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS batches ("
                "ordinal INTEGER PRIMARY KEY AUTOINCREMENT, "
                "batch_id TEXT NOT NULL UNIQUE, checksum TEXT NOT NULL, "
                "producer TEXT NOT NULL, organization_id TEXT NOT NULL, "
                "sequence INTEGER NOT NULL, interval_end INTEGER NOT NULL, "
                "wire BLOB, wire_bytes INTEGER NOT NULL, "
                "state INTEGER NOT NULL CHECK (state IN (0, 1)), "
                "appended_at INTEGER NOT NULL, acknowledged_at INTEGER)"
            )
            row = self._db.execute(
                "SELECT value FROM spool_meta WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                self._db.execute(
                    "INSERT INTO spool_meta(key, value) VALUES('schema_version', ?)",
                    (str(SPOOL_SCHEMA_VERSION),),
                )
            elif row != (str(SPOOL_SCHEMA_VERSION),):
                raise UsageSpoolCorrupt(
                    f"unsupported usage spool schema version: {row[0]!r}"
                )

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        try:
            self._db.execute("BEGIN IMMEDIATE")
            yield
            self._db.execute("COMMIT")
        except UsageSpoolError:
            self._rollback()
            raise
        except sqlite3.Error as exc:
            self._rollback()
            _raise_sqlite(exc)
        except BaseException:
            self._rollback()
            raise

    def _rollback(self) -> None:
        try:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    def _require_open(self) -> None:
        if self._closed:
            raise UsageSpoolError("usage spool is closed")

    def append(self, wire: bytes | str) -> bool:
        """Durably append canonical bytes; return false for an exact known retry."""
        try:
            batch = UsageBatch.from_json(wire)
        except UsageBatchError as exc:
            raise UsageSpoolCorrupt("refusing to spool an invalid usage batch") from exc
        encoded = batch.to_json()
        with self._mutex:
            self._require_open()
            with self._transaction():
                known = self._db.execute(
                    "SELECT checksum, wire, state FROM batches WHERE batch_id=?",
                    (batch.batch_id,),
                ).fetchone()
                if known is not None:
                    known_checksum, known_wire, state = known
                    exact_pending = state == 0 and bytes(known_wire) == encoded
                    exact_acked = state == 1 and known_checksum == batch.checksum
                    if known_checksum != batch.checksum or not (
                        exact_pending or exact_acked
                    ):
                        raise UsageSpoolConflict(
                            "logical usage batch identity already has different content"
                        )
                    return False

                stream = self._db.execute(
                    "SELECT high_sequence FROM streams "
                    "WHERE producer=? AND organization_id=?",
                    (batch.producer, batch.organization_id),
                ).fetchone()
                expected = 1 if stream is None else int(stream[0]) + 1
                if batch.sequence != expected:
                    direction = "gap" if batch.sequence > expected else "stale replay"
                    raise UsageSpoolSequenceError(
                        f"usage sequence {direction}: expected {expected}, got {batch.sequence}"
                    )

                count, byte_count = self._db.execute(
                    "SELECT COUNT(*), COALESCE(SUM(wire_bytes), 0) FROM batches"
                ).fetchone()
                if (
                    count >= self.max_records
                    or byte_count + len(encoded) > self.max_bytes
                ):
                    raise UsageSpoolCapacity(
                        "usage spool capacity reached; preserve current records and backpressure"
                    )

                self._db.execute(
                    "INSERT INTO batches("
                    "batch_id, checksum, producer, organization_id, sequence, "
                    "interval_end, wire, wire_bytes, state, appended_at"
                    ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
                    (
                        batch.batch_id,
                        batch.checksum,
                        batch.producer,
                        batch.organization_id,
                        batch.sequence,
                        batch.interval_end,
                        encoded,
                        len(encoded),
                        int(time.time()),
                    ),
                )
                if stream is None:
                    self._db.execute(
                        "INSERT INTO streams(producer, organization_id, high_sequence) "
                        "VALUES(?, ?, ?)",
                        (batch.producer, batch.organization_id, batch.sequence),
                    )
                else:
                    self._db.execute(
                        "UPDATE streams SET high_sequence=? "
                        "WHERE producer=? AND organization_id=?",
                        (batch.sequence, batch.producer, batch.organization_id),
                    )
        return True

    def pending(self, *, limit: int = 100) -> Iterator[SpoolRecord]:
        """Yield pending records in durable append order, validating every row."""
        yield from self._records(limit=limit, include_acknowledged=False)

    def recoverable(self, *, limit: int = 100) -> Iterator[SpoolRecord]:
        """Yield pending and retained acknowledged bytes for sink recovery replay."""
        yield from self._records(limit=limit, include_acknowledged=True)

    def _records(
        self, *, limit: int, include_acknowledged: bool
    ) -> Iterator[SpoolRecord]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        with self._mutex:
            self._require_open()
            try:
                where = "" if include_acknowledged else "WHERE state=0 "
                rows = self._db.execute(
                    "SELECT ordinal, batch_id, checksum, producer, organization_id, "
                    "sequence, interval_end, wire, state FROM batches "
                    f"{where}ORDER BY ordinal LIMIT ?",
                    (limit,),
                ).fetchall()
            except sqlite3.Error as exc:
                _raise_sqlite(exc)
        for row in rows:
            (
                ordinal,
                batch_id,
                checksum,
                producer,
                org,
                sequence,
                interval_end,
                wire,
                state,
            ) = row
            if not isinstance(wire, bytes):
                raise UsageSpoolCorrupt(f"retained spool row {ordinal} has no wire bytes")
            try:
                batch = UsageBatch.from_json(wire)
            except UsageBatchError as exc:
                raise UsageSpoolCorrupt(
                    f"retained spool row {ordinal} contains an invalid batch"
                ) from exc
            stored = (batch_id, checksum, producer, org, sequence, interval_end)
            parsed = (
                batch.batch_id,
                batch.checksum,
                batch.producer,
                batch.organization_id,
                batch.sequence,
                batch.interval_end,
            )
            if stored != parsed or batch.to_json() != wire:
                raise UsageSpoolCorrupt(
                    f"retained spool row {ordinal} metadata does not match its batch"
                )
            yield SpoolRecord(
                ordinal=ordinal,
                batch=batch,
                wire=wire,
                acknowledged=state == 1,
            )

    def acknowledge(self, batch_id: str, checksum: str) -> bool:
        """Mark only an exact accepted id/checksum durable; false means already acked."""
        with self._mutex:
            self._require_open()
            with self._transaction():
                row = self._db.execute(
                    "SELECT checksum, state FROM batches WHERE batch_id=?", (batch_id,)
                ).fetchone()
                if row is None:
                    raise UsageSpoolAckError("acknowledgement names an unknown batch")
                if row[0] != checksum:
                    raise UsageSpoolAckError("acknowledgement checksum does not match")
                if row[1] == 1:
                    return False
                self._db.execute(
                    "UPDATE batches SET state=1, "
                    "acknowledged_at=? WHERE batch_id=?",
                    (int(time.time()), batch_id),
                )
        return True

    def prune_acked(
        self,
        *,
        producer: str,
        organization_id: str,
        through_sequence: int,
        limit: int = 1_000,
    ) -> int:
        """Delete bytes covered by one stream's sink recovery watermark.

        An ingest acknowledgement is not proof of an off-host backup.  The
        caller supplies the exact producer/organization sequence known to be
        covered by the sink's durable recovery watermark.  Until then,
        acknowledged wire remains locally recoverable and counts against spool
        capacity.  A sequence watermark avoids unsafe wall-clock comparisons.
        """
        if (
            isinstance(through_sequence, bool)
            or not isinstance(through_sequence, int)
            or through_sequence < 1
        ):
            raise ValueError("through_sequence must be a positive integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        with self._mutex:
            self._require_open()
            with self._transaction():
                cursor = self._db.execute(
                    "DELETE FROM batches WHERE ordinal IN ("
                    "SELECT ordinal FROM batches WHERE state=1 "
                    "AND producer=? AND organization_id=? AND sequence<=? "
                    "ORDER BY ordinal LIMIT ?)",
                    (producer, organization_id, through_sequence, limit),
                )
                return cursor.rowcount

    def next_sequence(self, producer: str, organization_id: str) -> int:
        """Return the next sequence allocated by this durable stream state."""
        with self._mutex:
            self._require_open()
            try:
                row = self._db.execute(
                    "SELECT high_sequence FROM streams "
                    "WHERE producer=? AND organization_id=?",
                    (producer, organization_id),
                ).fetchone()
            except sqlite3.Error as exc:
                _raise_sqlite(exc)
        return 1 if row is None else int(row[0]) + 1

    def health(self) -> SpoolHealth:
        with self._mutex:
            self._require_open()
            try:
                pending = self._db.execute(
                    "SELECT COUNT(*), COALESCE(SUM(wire_bytes), 0), "
                    "MIN(interval_end), MAX(interval_end) FROM batches WHERE state=0"
                ).fetchone()
                acked = self._db.execute(
                    "SELECT COUNT(*), COALESCE(SUM(wire_bytes), 0) "
                    "FROM batches WHERE state=1"
                ).fetchone()
                streams = self._db.execute("SELECT COUNT(*) FROM streams").fetchone()[0]
            except sqlite3.Error as exc:
                _raise_sqlite(exc)
        return SpoolHealth(
            pending_records=int(pending[0]),
            pending_bytes=int(pending[1]),
            acknowledged_records=int(acked[0]),
            acknowledged_bytes=int(acked[1]),
            retained_bytes=int(pending[1]) + int(acked[1]),
            stream_count=int(streams),
            oldest_interval_end=pending[2],
            newest_interval_end=pending[3],
            max_records=self.max_records,
            max_bytes=self.max_bytes,
        )

    def close(self) -> None:
        with self._mutex:
            if self._closed:
                return
            self._db.close()
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            self._lock_file.close()
            self._closed = True

    def __enter__(self) -> "UsageSpool":
        self._require_open()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()
