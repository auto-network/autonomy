"""Authoritative SQLite ingest ledger for official usage batches.

This module owns accepted physical usage, producer authorization, exact-once
dedupe, and visible sequence gaps.  It has no pricing, entitlement, balance,
member, session, or packet-path behavior.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
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

from .batch import BATCH_INTERVAL_SECONDS, UsageBatch, UsageBatchError
from .progress import UsageProgress, UsageProgressError

LEDGER_SCHEMA_VERSION = 3
_METER_CLASS_RE = re.compile(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*")


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


@dataclass(frozen=True)
class ProgressReceipt:
    progress_id: str
    checksum: str
    accepted: bool
    organization_closed_through: int | None


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


def _meter_class(value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 160
        or _METER_CLASS_RE.fullmatch(value) is None
    ):
        raise UsageLedgerAuthorizationError(
            "meter_class must be a lowercase stable catalogue identifier"
        )
    return value


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
                "active_from INTEGER NOT NULL DEFAULT 0, active_through INTEGER, "
                "meter_class TEXT NOT NULL DEFAULT 'default', "
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
                "CREATE INDEX IF NOT EXISTS accepted_org_interval_start "
                "ON accepted_batches(organization_id, interval_start)"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS stream_state ("
                "producer TEXT NOT NULL, organization_id TEXT NOT NULL, "
                "contiguous_sequence INTEGER NOT NULL, "
                "highest_sequence INTEGER NOT NULL, "
                "PRIMARY KEY(producer, organization_id))"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS producer_progress ("
                "producer TEXT NOT NULL, organization_id TEXT NOT NULL, "
                "sequence INTEGER NOT NULL, closed_through INTEGER NOT NULL, "
                "progress_id TEXT NOT NULL UNIQUE, checksum TEXT NOT NULL, "
                "wire BLOB NOT NULL, accepted_at INTEGER NOT NULL, "
                "PRIMARY KEY(producer, organization_id))"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS usage_rollups ("
                "tier TEXT NOT NULL, organization_id TEXT NOT NULL, "
                "interval_start INTEGER NOT NULL, interval_end INTEGER NOT NULL, "
                "meters BLOB NOT NULL, source_tier TEXT NOT NULL, "
                "source_rows INTEGER NOT NULL, source_digest TEXT NOT NULL, "
                "settled_at INTEGER NOT NULL, "
                "PRIMARY KEY(tier, organization_id, interval_start))"
            )
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS rollup_retention "
                "ON usage_rollups(tier, organization_id, interval_end)"
            )
            authorization_columns = {
                column[1]
                for column in self._db.execute(
                    "PRAGMA table_info(producer_authorizations)"
                ).fetchall()
            }
            if "active_from" not in authorization_columns:
                self._db.execute(
                    "ALTER TABLE producer_authorizations "
                    "ADD COLUMN active_from INTEGER NOT NULL DEFAULT 0"
                )
            if "active_through" not in authorization_columns:
                self._db.execute(
                    "ALTER TABLE producer_authorizations "
                    "ADD COLUMN active_through INTEGER"
                )
            if "meter_class" not in authorization_columns:
                self._db.execute(
                    "ALTER TABLE producer_authorizations "
                    "ADD COLUMN meter_class TEXT NOT NULL DEFAULT 'default'"
                )
            row = self._db.execute(
                "SELECT value FROM ledger_meta WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                self._db.execute(
                    "INSERT INTO ledger_meta(key, value) VALUES('schema_version', ?)",
                    (str(LEDGER_SCHEMA_VERSION),),
                )
            elif row in (("1",), ("2",)):
                self._db.execute(
                    "UPDATE ledger_meta SET value=? WHERE key='schema_version'",
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
        active_from: int = 0,
        active_through: int | None = None,
        meter_class: str = "default",
    ) -> None:
        """Create or update one non-reusable producer/organization binding."""
        producer = _producer_key(producer)
        organization_id = _canonical_org(organization_id)
        families = _counter_families(counter_families)
        meter_class = _meter_class(meter_class)
        if not isinstance(enabled, bool):
            raise UsageLedgerAuthorizationError("enabled must be boolean")
        for value, name in (
            (active_from, "active_from"),
            (active_through, "active_through"),
        ):
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                or value % BATCH_INTERVAL_SECONDS
            ):
                raise UsageLedgerAuthorizationError(
                    f"{name} must be a non-negative aligned interval boundary"
                )
        if active_through is not None and active_through <= active_from:
            raise UsageLedgerAuthorizationError(
                "active_through must be later than active_from"
            )
        if enabled and active_through is not None:
            raise UsageLedgerAuthorizationError(
                "an enabled authorization cannot have active_through"
            )
        if not enabled and active_through is None:
            raise UsageLedgerAuthorizationError(
                "a disabled authorization requires active_through"
            )
        encoded = canonical_json(list(families))
        with self._mutex:
            self._require_open()
            with self._transaction():
                existing = self._db.execute(
                    "SELECT enabled, active_from, active_through, meter_class "
                    "FROM producer_authorizations WHERE producer=? "
                    "AND organization_id=?",
                    (producer, organization_id),
                ).fetchone()
                if existing is not None:
                    prior_enabled = bool(existing[0])
                    prior_from = int(existing[1])
                    prior_through = (
                        None if existing[2] is None else int(existing[2])
                    )
                    if active_from != prior_from:
                        raise UsageLedgerAuthorizationError(
                            "an authorization's active_from boundary is immutable"
                        )
                    if not prior_enabled and enabled:
                        raise UsageLedgerAuthorizationError(
                            "a retired producer binding cannot be re-enabled; "
                            "authorize a new producer key"
                        )
                    if not prior_enabled and active_through != prior_through:
                        raise UsageLedgerAuthorizationError(
                            "an authorization's active_through boundary is immutable"
                        )
                    if meter_class != existing[3]:
                        raise UsageLedgerAuthorizationError(
                            "an authorization's meter_class is immutable"
                        )
                if not enabled:
                    progress = self._db.execute(
                        "SELECT closed_through FROM producer_progress "
                        "WHERE producer=? AND organization_id=?",
                        (producer, organization_id),
                    ).fetchone()
                    if progress is None or int(progress[0]) < active_through:
                        raise UsageLedgerAuthorizationError(
                            "a disabled authorization must first close through "
                            "active_through"
                        )
                self._db.execute(
                    "INSERT INTO producer_authorizations("
                    "producer, organization_id, counter_families, enabled, updated_at, "
                    "active_from, active_through, meter_class"
                    ") VALUES(?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(producer, organization_id) DO UPDATE SET "
                    "counter_families=excluded.counter_families, "
                    "enabled=excluded.enabled, updated_at=excluded.updated_at, "
                    "active_from=excluded.active_from, "
                    "active_through=excluded.active_through, "
                    "meter_class=excluded.meter_class",
                    (
                        producer,
                        organization_id,
                        encoded,
                        int(enabled),
                        int(time.time()),
                        active_from,
                        active_through,
                        meter_class,
                    ),
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

                authorization = self._db.execute(
                    "SELECT organization_id, counter_families, enabled, active_from "
                    "FROM producer_authorizations "
                    "WHERE producer=? AND organization_id=?",
                    (batch.producer, batch.organization_id),
                ).fetchone()
                if authorization is None or authorization[2] != 1:
                    raise UsageLedgerAuthorizationError(
                        "producer is not enabled for official accounting"
                    )
                if batch.interval_start < int(authorization[3]):
                    raise UsageLedgerAuthorizationError(
                        "usage precedes the producer authorization lifetime"
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

                progress = self._db.execute(
                    "SELECT sequence, closed_through FROM producer_progress "
                    "WHERE producer=? AND organization_id=?",
                    (batch.producer, batch.organization_id),
                ).fetchone()
                if progress is not None and batch.interval_end <= int(progress[1]):
                    raise UsageLedgerConflict(
                        "usage arrived at or before the producer's closed watermark"
                    )

                same_sequence = self._db.execute(
                    "SELECT batch_id, checksum FROM accepted_batches "
                    "WHERE producer=? AND organization_id=? AND sequence=?",
                    (batch.producer, batch.organization_id, batch.sequence),
                ).fetchone()
                if same_sequence is not None:
                    raise UsageLedgerConflict(
                        "producer sequence already names a different batch"
                    )

                previous = self._db.execute(
                    "SELECT interval_end FROM accepted_batches WHERE producer=? "
                    "AND organization_id=? AND sequence<? "
                    "ORDER BY sequence DESC LIMIT 1",
                    (batch.producer, batch.organization_id, batch.sequence),
                ).fetchone()
                following = self._db.execute(
                    "SELECT interval_end FROM accepted_batches WHERE producer=? "
                    "AND organization_id=? AND sequence>? "
                    "ORDER BY sequence LIMIT 1",
                    (batch.producer, batch.organization_id, batch.sequence),
                ).fetchone()
                if previous is not None and int(previous[0]) >= batch.interval_end:
                    raise UsageLedgerConflict(
                        "producer sequences must follow increasing intervals"
                    )
                if following is not None and int(following[0]) <= batch.interval_end:
                    raise UsageLedgerConflict(
                        "producer sequences must follow increasing intervals"
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

    def accept_progress(self, wire: bytes | str) -> ProgressReceipt:
        """Commit a signed idle-inclusive producer closure watermark."""
        try:
            progress = UsageProgress.from_json(wire)
        except UsageProgressError as exc:
            raise UsageLedgerCorrupt("invalid signed usage progress") from exc
        encoded = progress.to_json()
        with self._mutex:
            self._require_open()
            with self._transaction():
                current = self._db.execute(
                    "SELECT sequence, closed_through, progress_id, checksum, wire "
                    "FROM producer_progress WHERE producer=? AND organization_id=?",
                    (progress.producer, progress.organization_id),
                ).fetchone()
                if current is not None and current[2] == progress.progress_id:
                    if current[3] != progress.checksum or bytes(current[4]) != encoded:
                        raise UsageLedgerConflict(
                            "usage progress identity has different content"
                        )
                    return self._progress_receipt(progress, accepted=False)

                authorization = self._db.execute(
                    "SELECT enabled, active_from FROM producer_authorizations "
                    "WHERE producer=? AND organization_id=?",
                    (progress.producer, progress.organization_id),
                ).fetchone()
                if authorization is None or authorization[0] != 1:
                    raise UsageLedgerAuthorizationError(
                        "producer is not enabled for this organization"
                    )
                if progress.closed_through < int(authorization[1]):
                    raise UsageLedgerAuthorizationError(
                        "usage progress precedes the producer authorization lifetime"
                    )
                if current is not None and (
                    progress.sequence < int(current[0])
                    or progress.closed_through <= int(current[1])
                ):
                    raise UsageLedgerConflict("usage progress must advance monotonically")

                state = self._db.execute(
                    "SELECT contiguous_sequence FROM stream_state "
                    "WHERE producer=? AND organization_id=?",
                    (progress.producer, progress.organization_id),
                ).fetchone()
                contiguous = 0 if state is None else int(state[0])
                if progress.sequence > contiguous:
                    raise UsageLedgerConflict(
                        "usage progress cannot pass an unaccepted producer sequence"
                    )
                outside = self._db.execute(
                    "SELECT 1 FROM accepted_batches WHERE producer=? "
                    "AND organization_id=? AND ((sequence<=? AND interval_end>?) "
                    "OR (sequence>? AND interval_end<=?)) LIMIT 1",
                    (
                        progress.producer,
                        progress.organization_id,
                        progress.sequence,
                        progress.closed_through,
                        progress.sequence,
                        progress.closed_through,
                    ),
                ).fetchone()
                if outside is not None:
                    raise UsageLedgerConflict(
                        "usage progress sequence and interval closure disagree"
                    )
                self._db.execute(
                    "INSERT INTO producer_progress("
                    "producer, organization_id, sequence, closed_through, "
                    "progress_id, checksum, wire, accepted_at"
                    ") VALUES(?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(producer, organization_id) DO UPDATE SET "
                    "sequence=excluded.sequence, closed_through=excluded.closed_through, "
                    "progress_id=excluded.progress_id, checksum=excluded.checksum, "
                    "wire=excluded.wire, accepted_at=excluded.accepted_at",
                    (
                        progress.producer,
                        progress.organization_id,
                        progress.sequence,
                        progress.closed_through,
                        progress.progress_id,
                        progress.checksum,
                        encoded,
                        int(time.time()),
                    ),
                )
                return self._progress_receipt(progress, accepted=True)

    def _progress_receipt(
        self, progress: UsageProgress, *, accepted: bool
    ) -> ProgressReceipt:
        return ProgressReceipt(
            progress_id=progress.progress_id,
            checksum=progress.checksum,
            accepted=accepted,
            organization_closed_through=self.organization_closed_through(
                progress.organization_id
            ),
        )

    def organization_closed_through(self, organization_id: str) -> int | None:
        """Return the minimum closure across every currently enabled binding."""
        with self._mutex:
            self._require_open()
            try:
                row = self._db.execute(
                    "SELECT COUNT(*), COUNT(p.closed_through), MIN(p.closed_through) "
                    "FROM producer_authorizations a LEFT JOIN producer_progress p "
                    "ON p.producer=a.producer "
                    "AND p.organization_id=a.organization_id "
                    "WHERE a.organization_id=? AND a.enabled=1",
                    (organization_id,),
                ).fetchone()
            except sqlite3.Error as exc:
                _raise_sqlite(exc)
        if row[0] == 0 or row[0] != row[1]:
            return None
        return int(row[2])

    def interval_is_settled(
        self, organization_id: str, interval_start: int, interval_end: int
    ) -> bool:
        """Return whether every producer active in an interval has closed it."""
        organization_id = _canonical_org(organization_id)
        if (
            isinstance(interval_start, bool)
            or not isinstance(interval_start, int)
            or isinstance(interval_end, bool)
            or not isinstance(interval_end, int)
            or interval_start < 0
            or interval_end != interval_start + BATCH_INTERVAL_SECONDS
            or interval_start % BATCH_INTERVAL_SECONDS
        ):
            raise ValueError("interval must be one aligned five-minute window")
        with self._mutex:
            self._require_open()
            try:
                row = self._db.execute(
                    "SELECT COUNT(*), "
                    "COALESCE(SUM(p.closed_through>=?), 0) "
                    "FROM producer_authorizations a "
                    "LEFT JOIN producer_progress p ON p.producer=a.producer "
                    "AND p.organization_id=a.organization_id "
                    "WHERE a.organization_id=? AND a.active_from<? "
                    "AND (a.active_through IS NULL OR a.active_through>?)",
                    (
                        interval_end,
                        organization_id,
                        interval_end,
                        interval_start,
                    ),
                ).fetchone()
            except sqlite3.Error as exc:
                _raise_sqlite(exc)
        return int(row[0]) > 0 and int(row[0]) == int(row[1])

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
