"""Authoritative SQLite ingest ledger for official usage batches.

This module owns accepted physical usage, producer authorization, exact-once
dedupe, and visible sequence gaps.  It has no pricing, entitlement, balance,
member, session, or packet-path behavior.
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from tools.network.idkit import canonical_json, load_public_key
from tools.network.idkit.errors import IdkitError

from .batch import UsageBatch, UsageBatchError

LEDGER_SCHEMA_VERSION = 1


class UsageLedgerError(RuntimeError):
    """Base class for official ledger rejection and storage failures."""


class UsageLedgerLocked(UsageLedgerError):
    """Another accounting process owns the ledger."""


class UsageLedgerAuthorizationError(UsageLedgerError):
    """The producer is not authorized for the organization or counters."""


class UsageLedgerConflict(UsageLedgerError):
    """An accepted immutable identity was presented with different content."""


class UsageLedgerCorrupt(UsageLedgerError):
    """The ledger or a retained accepted batch failed integrity validation."""


class UsageLedgerIOError(UsageLedgerError):
    """The ledger could not durably complete an operation."""


@dataclass(frozen=True)
class IngestReceipt:
    batch_id: str
    checksum: str
    accepted: bool
    contiguous_sequence: int
    highest_sequence: int
    gap_count: int


@dataclass(frozen=True)
class StreamReconciliation:
    producer: str
    organization_id: str
    contiguous_sequence: int
    highest_sequence: int
    gaps: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class LedgerHealth:
    accepted_batches: int
    authorization_bindings: int
    disabled_bindings: int
    streams: int
    streams_with_gaps: int
    missing_sequences: int


def _raise_sqlite(exc: sqlite3.Error) -> None:
    message = str(exc).lower()
    if "malformed" in message or "corrupt" in message:
        raise UsageLedgerCorrupt(f"usage ledger database is corrupt: {exc}") from exc
    raise UsageLedgerIOError(f"usage ledger storage operation failed: {exc}") from exc


def _canonical_org(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, TypeError, AttributeError) as exc:
        raise UsageLedgerAuthorizationError(
            "organization_id must be a canonical UUID"
        ) from exc
    if parsed.version is None or str(parsed) != value:
        raise UsageLedgerAuthorizationError(
            "organization_id must be a canonical UUID"
        )
    return value


def _producer_key(value: str) -> str:
    try:
        load_public_key(value)
    except IdkitError as exc:
        raise UsageLedgerAuthorizationError(
            "producer must be an Ed25519 public key"
        ) from exc
    return value


def _counter_families(values: set[str] | frozenset[str]) -> tuple[str, ...]:
    if not values or len(values) > 64:
        raise UsageLedgerAuthorizationError(
            "producer authorization requires 1 to 64 counter families"
        )
    result = tuple(sorted(values))
    for value in result:
        if not isinstance(value, str) or not value:
            raise UsageLedgerAuthorizationError("counter families must be strings")
    return result


class UsageLedger:
    """Single-process authoritative ingest ledger."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._mutex = threading.RLock()
        self._closed = False
        self._lock_path = self.path.with_name(self.path.name + ".lock")
        self._lock_file = self._lock_path.open("a+b")
        os.chmod(self._lock_path, 0o600)
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock_file.close()
            raise UsageLedgerLocked(f"usage ledger is already open: {self.path}") from exc
        try:
            self._db = sqlite3.connect(
                self.path,
                timeout=10,
                isolation_level=None,
                check_same_thread=False,
            )
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._initialize()
            check = self._db.execute("PRAGMA quick_check").fetchone()
            if check != ("ok",):
                raise UsageLedgerCorrupt(f"usage ledger quick-check failed: {check!r}")
        except UsageLedgerError:
            self._release_failed_open()
            raise
        except sqlite3.Error as exc:
            self._release_failed_open()
            _raise_sqlite(exc)

    def _initialize(self) -> None:
        with self._transaction():
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS ledger_meta ("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS producer_authorizations ("
                "producer TEXT NOT NULL, organization_id TEXT NOT NULL, "
                "counter_families BLOB NOT NULL, enabled INTEGER NOT NULL "
                "CHECK(enabled IN (0, 1)), updated_at INTEGER NOT NULL, "
                "PRIMARY KEY(producer, organization_id))"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS accepted_batches ("
                "batch_id TEXT PRIMARY KEY, checksum TEXT NOT NULL, "
                "producer TEXT NOT NULL, organization_id TEXT NOT NULL, "
                "sequence INTEGER NOT NULL, interval_start INTEGER NOT NULL, "
                "interval_end INTEGER NOT NULL, created_at INTEGER NOT NULL, "
                "counters BLOB NOT NULL, wire BLOB NOT NULL, accepted_at INTEGER NOT NULL, "
                "UNIQUE(producer, organization_id, sequence))"
            )
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS accepted_org_interval "
                "ON accepted_batches(organization_id, interval_end)"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS stream_state ("
                "producer TEXT NOT NULL, organization_id TEXT NOT NULL, "
                "contiguous_sequence INTEGER NOT NULL, "
                "highest_sequence INTEGER NOT NULL, "
                "PRIMARY KEY(producer, organization_id))"
            )
            row = self._db.execute(
                "SELECT value FROM ledger_meta WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                self._db.execute(
                    "INSERT INTO ledger_meta(key, value) VALUES('schema_version', ?)",
                    (str(LEDGER_SCHEMA_VERSION),),
                )
            elif row != (str(LEDGER_SCHEMA_VERSION),):
                raise UsageLedgerCorrupt(
                    f"unsupported usage ledger schema version: {row[0]!r}"
                )

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        try:
            self._db.execute("BEGIN IMMEDIATE")
            yield
            self._db.execute("COMMIT")
        except UsageLedgerError:
            self._rollback()
            raise
        except sqlite3.IntegrityError as exc:
            self._rollback()
            raise UsageLedgerConflict(f"usage ledger identity conflict: {exc}") from exc
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

    def _release_failed_open(self) -> None:
        db = getattr(self, "_db", None)
        if db is not None:
            db.close()
        fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
        self._lock_file.close()
        self._closed = True

    def _require_open(self) -> None:
        if self._closed:
            raise UsageLedgerError("usage ledger is closed")

    def authorize_producer(
        self,
        *,
        producer: str,
        organization_id: str,
        counter_families: set[str] | frozenset[str],
        enabled: bool = True,
    ) -> None:
        """Create or replace one producer/organization authorization binding."""
        producer = _producer_key(producer)
        organization_id = _canonical_org(organization_id)
        families = _counter_families(counter_families)
        if not isinstance(enabled, bool):
            raise UsageLedgerAuthorizationError("enabled must be boolean")
        encoded = canonical_json(list(families))
        with self._mutex:
            self._require_open()
            with self._transaction():
                self._db.execute(
                    "INSERT INTO producer_authorizations("
                    "producer, organization_id, counter_families, enabled, updated_at"
                    ") VALUES(?, ?, ?, ?, ?) "
                    "ON CONFLICT(producer, organization_id) DO UPDATE SET "
                    "counter_families=excluded.counter_families, "
                    "enabled=excluded.enabled, updated_at=excluded.updated_at",
                    (producer, organization_id, encoded, int(enabled), int(time.time())),
                )

    def ingest(self, wire: bytes | str) -> IngestReceipt:
        """Commit one authorized batch or return its unchanged prior receipt."""
        try:
            batch = UsageBatch.from_json(wire)
        except UsageBatchError as exc:
            raise UsageLedgerCorrupt("invalid official usage batch") from exc
        encoded = batch.to_json()
        with self._mutex:
            self._require_open()
            with self._transaction():
                authorization = self._db.execute(
                    "SELECT organization_id, counter_families, enabled "
                    "FROM producer_authorizations "
                    "WHERE producer=? AND organization_id=?",
                    (batch.producer, batch.organization_id),
                ).fetchone()
                if authorization is None or authorization[2] != 1:
                    raise UsageLedgerAuthorizationError(
                        "producer is not enabled for official accounting"
                    )
                try:
                    allowed = frozenset(json.loads(bytes(authorization[1])))
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise UsageLedgerCorrupt(
                        "producer authorization counter families are corrupt"
                    ) from exc
                unknown = sorted(set(batch.counters) - allowed)
                if unknown:
                    raise UsageLedgerAuthorizationError(
                        f"producer is not authorized for counter families: {unknown}"
                    )

                known = self._db.execute(
                    "SELECT checksum, wire FROM accepted_batches WHERE batch_id=?",
                    (batch.batch_id,),
                ).fetchone()
                if known is not None:
                    if known[0] != batch.checksum or bytes(known[1]) != encoded:
                        raise UsageLedgerConflict(
                            "accepted batch identity has different content"
                        )
                    return self._receipt(batch, accepted=False)

                same_sequence = self._db.execute(
                    "SELECT batch_id, checksum FROM accepted_batches "
                    "WHERE producer=? AND organization_id=? AND sequence=?",
                    (batch.producer, batch.organization_id, batch.sequence),
                ).fetchone()
                if same_sequence is not None:
                    raise UsageLedgerConflict(
                        "producer sequence already names a different batch"
                    )

                self._db.execute(
                    "INSERT INTO accepted_batches("
                    "batch_id, checksum, producer, organization_id, sequence, "
                    "interval_start, interval_end, created_at, counters, wire, accepted_at"
                    ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        batch.batch_id,
                        batch.checksum,
                        batch.producer,
                        batch.organization_id,
                        batch.sequence,
                        batch.interval_start,
                        batch.interval_end,
                        batch.created_at,
                        canonical_json(dict(batch.counters)),
                        encoded,
                        int(time.time()),
                    ),
                )
                self._advance_stream(batch)
                return self._receipt(batch, accepted=True)

    def _advance_stream(self, batch: UsageBatch) -> None:
        state = self._db.execute(
            "SELECT contiguous_sequence, highest_sequence FROM stream_state "
            "WHERE producer=? AND organization_id=?",
            (batch.producer, batch.organization_id),
        ).fetchone()
        contiguous = 0 if state is None else int(state[0])
        highest = batch.sequence if state is None else max(int(state[1]), batch.sequence)
        while self._db.execute(
            "SELECT 1 FROM accepted_batches WHERE producer=? "
            "AND organization_id=? AND sequence=?",
            (batch.producer, batch.organization_id, contiguous + 1),
        ).fetchone() is not None:
            contiguous += 1
        self._db.execute(
            "INSERT INTO stream_state("
            "producer, organization_id, contiguous_sequence, highest_sequence"
            ") VALUES(?, ?, ?, ?) ON CONFLICT(producer, organization_id) DO UPDATE SET "
            "contiguous_sequence=excluded.contiguous_sequence, "
            "highest_sequence=excluded.highest_sequence",
            (batch.producer, batch.organization_id, contiguous, highest),
        )

    def _receipt(self, batch: UsageBatch, *, accepted: bool) -> IngestReceipt:
        reconciliation = self.reconciliation(
            producer=batch.producer,
            organization_id=batch.organization_id,
        )
        gap_count = sum(end - start + 1 for start, end in reconciliation.gaps)
        return IngestReceipt(
            batch_id=batch.batch_id,
            checksum=batch.checksum,
            accepted=accepted,
            contiguous_sequence=reconciliation.contiguous_sequence,
            highest_sequence=reconciliation.highest_sequence,
            gap_count=gap_count,
        )

    def reconciliation(
        self,
        *,
        producer: str,
        organization_id: str,
    ) -> StreamReconciliation:
        """Return exact missing sequence ranges for one producer/org stream."""
        with self._mutex:
            self._require_open()
            try:
                state = self._db.execute(
                    "SELECT contiguous_sequence, highest_sequence FROM stream_state "
                    "WHERE producer=? AND organization_id=?",
                    (producer, organization_id),
                ).fetchone()
                if state is None:
                    return StreamReconciliation(producer, organization_id, 0, 0, ())
                contiguous, highest = map(int, state)
                present = {
                    int(row[0])
                    for row in self._db.execute(
                        "SELECT sequence FROM accepted_batches WHERE producer=? "
                        "AND organization_id=? AND sequence>? ORDER BY sequence",
                        (producer, organization_id, contiguous),
                    ).fetchall()
                }
            except sqlite3.Error as exc:
                _raise_sqlite(exc)
        gaps: list[tuple[int, int]] = []
        expected = contiguous + 1
        for sequence in sorted(present):
            if sequence > expected:
                gaps.append((expected, sequence - 1))
            expected = sequence + 1
        if expected <= highest:
            gaps.append((expected, highest))
        return StreamReconciliation(
            producer, organization_id, contiguous, highest, tuple(gaps)
        )

    def accepted_count(self, organization_id: str) -> int:
        with self._mutex:
            self._require_open()
            try:
                return int(
                    self._db.execute(
                        "SELECT COUNT(*) FROM accepted_batches WHERE organization_id=?",
                        (organization_id,),
                    ).fetchone()[0]
                )
            except sqlite3.Error as exc:
                _raise_sqlite(exc)

    def health(self) -> LedgerHealth:
        """Return bounded, identity-free operational ingest state."""
        with self._mutex:
            self._require_open()
            try:
                accepted = self._db.execute(
                    "SELECT COUNT(*) FROM accepted_batches"
                ).fetchone()[0]
                auth = self._db.execute(
                    "SELECT COUNT(*), COALESCE(SUM(enabled=0), 0) "
                    "FROM producer_authorizations"
                ).fetchone()
                stream = self._db.execute(
                    "SELECT COUNT(*), "
                    "COALESCE(SUM(contiguous_sequence<highest_sequence), 0), "
                    "COALESCE(SUM(highest_sequence), 0) FROM stream_state"
                ).fetchone()
            except sqlite3.Error as exc:
                _raise_sqlite(exc)
        return LedgerHealth(
            accepted_batches=int(accepted),
            authorization_bindings=int(auth[0]),
            disabled_bindings=int(auth[1]),
            streams=int(stream[0]),
            streams_with_gaps=int(stream[1]),
            missing_sequences=int(stream[2]) - int(accepted),
        )

    def audit(self) -> int:
        """Verify SQLite and every retained canonical batch; return row count."""
        with self._mutex:
            self._require_open()
            try:
                check = self._db.execute("PRAGMA quick_check").fetchone()
                if check != ("ok",):
                    raise UsageLedgerCorrupt(
                        f"usage ledger quick-check failed: {check!r}"
                    )
                rows = self._db.execute(
                    "SELECT batch_id, checksum, producer, organization_id, sequence, "
                    "interval_start, interval_end, created_at, counters, wire "
                    "FROM accepted_batches ORDER BY rowid"
                ).fetchall()
            except sqlite3.Error as exc:
                _raise_sqlite(exc)
        for row in rows:
            wire = row[9]
            if not isinstance(wire, bytes):
                raise UsageLedgerCorrupt("accepted usage row has no canonical wire")
            try:
                batch = UsageBatch.from_json(wire)
            except UsageBatchError as exc:
                raise UsageLedgerCorrupt(
                    f"accepted usage batch {row[0]} failed validation"
                ) from exc
            stored_counters = row[8]
            expected = (
                batch.batch_id,
                batch.checksum,
                batch.producer,
                batch.organization_id,
                batch.sequence,
                batch.interval_start,
                batch.interval_end,
                batch.created_at,
                canonical_json(dict(batch.counters)),
            )
            if row[:9] != expected or stored_counters != expected[8]:
                raise UsageLedgerCorrupt(
                    f"accepted usage batch {batch.batch_id} metadata mismatch"
                )
        return len(rows)

    def close(self) -> None:
        with self._mutex:
            if self._closed:
                return
            self._db.close()
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            self._lock_file.close()
            self._closed = True

    def __enter__(self) -> "UsageLedger":
        self._require_open()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()
