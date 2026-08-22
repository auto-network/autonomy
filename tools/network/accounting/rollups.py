"""Deterministic settled UTC usage rollups and invariant verification."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Iterable

from tools.network.idkit import canonical_json

from .batch import BATCH_INTERVAL_SECONDS
from .ledger import (
    UsageLedgerConflict,
    UsageLedgerCorrupt,
    _canonical_org,
    _raise_sqlite,
)

if TYPE_CHECKING:
    from .ledger import UsageLedger

FIVE_MINUTE = "five_minute"
HOUR = "hour"
DAY = "day"
MONTH = "month"
ROLLUP_TIERS = (FIVE_MINUTE, HOUR, DAY, MONTH)
_DIGEST_DOMAIN = b"autonomy.network.usage-rollup-source.v1\n"


class UsageRollupError(RuntimeError):
    """Base class for settled rollup failures."""


class UsageRollupIncomplete(UsageRollupError):
    """A parent interval does not have every settled child row."""


@dataclass(frozen=True)
class RollupRow:
    tier: str
    organization_id: str
    interval_start: int
    interval_end: int
    meters: dict[str, dict[str, int]]
    source_tier: str
    source_rows: int
    source_digest: str


@dataclass(frozen=True)
class RollupRun:
    organization_id: str
    requested_start: int
    settled_through: int
    inserted: int
    unchanged: int


@dataclass(frozen=True)
class PruneRun:
    organization_id: str
    as_of: int
    five_minute_deleted: int
    hour_deleted: int
    day_deleted: int


def steady_state_rows(*, daily_years: int = 3, monthly_years: int = 3) -> int:
    """Return the frozen non-leap sizing baseline used by the mission."""
    if daily_years < 1 or monthly_years < 1:
        raise ValueError("retention years must be positive")
    return 2_016 + 720 + (365 * daily_years) + (12 * monthly_years)


def _month_start(timestamp: int) -> int:
    value = datetime.fromtimestamp(timestamp, UTC)
    return int(datetime(value.year, value.month, 1, tzinfo=UTC).timestamp())


def _next_month(timestamp: int) -> int:
    value = datetime.fromtimestamp(timestamp, UTC)
    year = value.year + (1 if value.month == 12 else 0)
    month = 1 if value.month == 12 else value.month + 1
    return int(datetime(year, month, 1, tzinfo=UTC).timestamp())


def _years_before(timestamp: int, years: int) -> int:
    value = datetime.fromtimestamp(timestamp, UTC)
    try:
        result = value.replace(year=value.year - years)
    except ValueError:
        # A leap-day anniversary expires at the end of February.
        result = value.replace(year=value.year - years, day=28)
    return int(result.timestamp())


def _validated_counters(counters: object) -> dict[str, int]:
    if not isinstance(counters, dict):
        raise UsageLedgerCorrupt("rollup source counters are not an object")
    for name, value in counters.items():
        if (
            not isinstance(name, str)
            or isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
        ):
            raise UsageLedgerCorrupt("rollup source counter is invalid")
    return counters


def _decode_counters(encoded: bytes) -> dict[str, int]:
    try:
        counters = json.loads(bytes(encoded))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise UsageLedgerCorrupt("rollup source counters are corrupt") from exc
    return _validated_counters(counters)


def _sum_meters(encoded_rows: Iterable[bytes]) -> dict[str, dict[str, int]]:
    totals: dict[str, dict[str, int]] = {}
    for encoded in encoded_rows:
        try:
            meters = json.loads(bytes(encoded))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise UsageLedgerCorrupt("rollup source meters are corrupt") from exc
        if not isinstance(meters, dict):
            raise UsageLedgerCorrupt("rollup source meters are not an object")
        for meter_class, encoded_counters in meters.items():
            if not isinstance(meter_class, str) or not isinstance(
                encoded_counters, dict
            ):
                raise UsageLedgerCorrupt("rollup source meter is invalid")
            counters = _validated_counters(encoded_counters)
            target = totals.setdefault(meter_class, {})
            for name, value in counters.items():
                target[name] = target.get(name, 0) + value
    return {
        meter_class: dict(sorted(counters.items()))
        for meter_class, counters in sorted(totals.items())
    }


def _digest(parts: list[list[object]]) -> str:
    return hashlib.sha256(
        _DIGEST_DOMAIN + canonical_json(parts)
    ).hexdigest()


class UsageRollups:
    """Materialize immutable settled aggregates from one authoritative ledger."""

    def __init__(self, ledger: "UsageLedger") -> None:
        self.ledger = ledger

    def materialize(self, organization_id: str, *, start: int) -> RollupRun:
        organization_id = _canonical_org(organization_id)
        if isinstance(start, bool) or not isinstance(start, int) or start < 0:
            raise ValueError("start must be a non-negative integer")
        if start % BATCH_INTERVAL_SECONDS:
            raise ValueError("start must be five-minute aligned")
        with self.ledger._mutex:
            self.ledger._require_open()
            try:
                bindings = self.ledger._db.execute(
                    "SELECT a.producer, a.active_from, a.active_through, "
                    "p.closed_through, a.meter_class "
                    "FROM producer_authorizations a "
                    "LEFT JOIN producer_progress p ON p.producer=a.producer "
                    "AND p.organization_id=a.organization_id "
                    "WHERE a.organization_id=? ORDER BY a.active_from",
                    (organization_id,),
                ).fetchall()
            except sqlite3.Error as exc:
                _raise_sqlite(exc)
            candidate = max(
                (int(row[3]) for row in bindings if row[3] is not None),
                default=start,
            )
            if not bindings or candidate <= start:
                raise UsageRollupIncomplete(
                    "organization has no closed interval after the requested start"
                )
            inserted = 0
            unchanged = 0
            with self.ledger._transaction():
                cursor = start
                while cursor + BATCH_INTERVAL_SECONDS <= candidate:
                    end = cursor + BATCH_INTERVAL_SECONDS
                    relevant = tuple(
                        row
                        for row in bindings
                        if int(row[1]) < end
                        and (row[2] is None or int(row[2]) > cursor)
                    )
                    if not relevant or any(
                        row[3] is None or int(row[3]) < end for row in relevant
                    ):
                        break
                    result = self._five_minute(organization_id, cursor, end)
                    inserted += result
                    unchanged += 1 - result
                    cursor += BATCH_INTERVAL_SECONDS

                settled = cursor
                if settled <= start:
                    raise UsageRollupIncomplete(
                        "the first requested interval is not settled"
                    )

                for tier, source_tier, seconds in (
                    (HOUR, FIVE_MINUTE, 3_600),
                    (DAY, HOUR, 86_400),
                ):
                    cursor = ((start + seconds - 1) // seconds) * seconds
                    while cursor + seconds <= settled:
                        result = self._parent(
                            tier,
                            source_tier,
                            organization_id,
                            cursor,
                            cursor + seconds,
                            seconds // (300 if tier == HOUR else 3_600),
                        )
                        inserted += result
                        unchanged += 1 - result
                        cursor += seconds

                cursor = _month_start(start)
                if cursor < start:
                    cursor = _next_month(cursor)
                while _next_month(cursor) <= settled:
                    end = _next_month(cursor)
                    days = (end - cursor) // 86_400
                    result = self._parent(
                        MONTH, DAY, organization_id, cursor, end, days
                    )
                    inserted += result
                    unchanged += 1 - result
                    cursor = end
            return RollupRun(
                organization_id=organization_id,
                requested_start=start,
                settled_through=settled,
                inserted=inserted,
                unchanged=unchanged,
            )

    def verify_parent(
        self,
        organization_id: str,
        tier: str,
        interval_start: int,
    ) -> bool:
        """Verify one retained parent against every required child row."""
        organization_id = _canonical_org(organization_id)
        if tier not in (HOUR, DAY, MONTH):
            raise ValueError("only parent tiers can be verified")
        with self.ledger._mutex:
            self.ledger._require_open()
            try:
                parent = self.ledger._db.execute(
                    "SELECT interval_end, meters, source_tier, source_rows, "
                    "source_digest FROM usage_rollups WHERE tier=? "
                    "AND organization_id=? AND interval_start=?",
                    (tier, organization_id, interval_start),
                ).fetchone()
                if parent is None:
                    return False
                interval_end = int(parent[0])
                if tier == HOUR:
                    expected_end, expected_source, expected_rows = (
                        interval_start + 3_600,
                        FIVE_MINUTE,
                        12,
                    )
                elif tier == DAY:
                    expected_end, expected_source, expected_rows = (
                        interval_start + 86_400,
                        HOUR,
                        24,
                    )
                else:
                    expected_end = _next_month(interval_start)
                    expected_source = DAY
                    expected_rows = (expected_end - interval_start) // 86_400
                if (
                    interval_end != expected_end
                    or parent[2] != expected_source
                    or int(parent[3]) != expected_rows
                ):
                    return False
                children = self.ledger._db.execute(
                    "SELECT interval_start, meters, source_digest "
                    "FROM usage_rollups WHERE tier=? AND organization_id=? "
                    "AND interval_start>=? AND interval_end<=? "
                    "ORDER BY interval_start",
                    (
                        parent[2],
                        organization_id,
                        interval_start,
                        interval_end,
                    ),
                ).fetchall()
            except sqlite3.Error as exc:
                _raise_sqlite(exc)
        if len(children) != expected_rows:
            return False
        meters = canonical_json(_sum_meters(row[1] for row in children))
        digest = _digest([[row[0], row[2]] for row in children])
        return meters == bytes(parent[1]) and digest == parent[4]

    def prune(self, organization_id: str, *, as_of: int) -> PruneRun:
        """Delete expired child rows only after exact parent verification."""
        organization_id = _canonical_org(organization_id)
        if (
            isinstance(as_of, bool)
            or not isinstance(as_of, int)
            or as_of < 0
            or as_of % BATCH_INTERVAL_SECONDS
        ):
            raise ValueError("as_of must be a non-negative aligned boundary")
        cutoffs = (
            (FIVE_MINUTE, HOUR, as_of - (7 * 86_400)),
            (HOUR, DAY, as_of - (30 * 86_400)),
            (DAY, MONTH, _years_before(as_of, 3)),
        )
        deleted: dict[str, int] = {FIVE_MINUTE: 0, HOUR: 0, DAY: 0}
        with self.ledger._mutex:
            self.ledger._require_open()
            with self.ledger._transaction():
                for child_tier, parent_tier, cutoff in cutoffs:
                    parents = self.ledger._db.execute(
                        "SELECT p.interval_start, p.interval_end "
                        "FROM usage_rollups p "
                        "WHERE p.tier=? AND p.organization_id=? AND p.interval_end<=? "
                        "AND EXISTS (SELECT 1 FROM usage_rollups c "
                        "WHERE c.tier=? AND c.organization_id=p.organization_id "
                        "AND c.interval_start>=p.interval_start "
                        "AND c.interval_end<=p.interval_end) "
                        "ORDER BY interval_start",
                        (parent_tier, organization_id, cutoff, child_tier),
                    ).fetchall()
                    for parent_start, parent_end in parents:
                        if not self.verify_parent(
                            organization_id, parent_tier, int(parent_start)
                        ):
                            raise UsageRollupIncomplete(
                                f"cannot prune unverified {child_tier} source rows"
                            )
                        cursor = self.ledger._db.execute(
                            "DELETE FROM usage_rollups WHERE tier=? "
                            "AND organization_id=? AND interval_start>=? "
                            "AND interval_end<=?",
                            (
                                child_tier,
                                organization_id,
                                int(parent_start),
                                int(parent_end),
                            ),
                        )
                        deleted[child_tier] += cursor.rowcount
        return PruneRun(
            organization_id=organization_id,
            as_of=as_of,
            five_minute_deleted=deleted[FIVE_MINUTE],
            hour_deleted=deleted[HOUR],
            day_deleted=deleted[DAY],
        )

    def _five_minute(self, organization_id: str, start: int, end: int) -> int:
        rows = self.ledger._db.execute(
            "SELECT b.batch_id, b.checksum, b.counters, a.meter_class "
            "FROM accepted_batches b "
            "JOIN producer_authorizations a ON a.producer=b.producer "
            "AND a.organization_id=b.organization_id "
            "WHERE b.organization_id=? AND b.interval_start=? ORDER BY b.batch_id",
            (organization_id, start),
        ).fetchall()
        meters: dict[str, dict[str, int]] = {}
        for row in rows:
            target = meters.setdefault(str(row[3]), {})
            for name, value in _decode_counters(row[2]).items():
                target[name] = target.get(name, 0) + value
        meters = {
            meter_class: dict(sorted(counters.items()))
            for meter_class, counters in sorted(meters.items())
        }
        parts = [[row[0], row[1], row[3]] for row in rows]
        return self._insert_exact(
            RollupRow(
                FIVE_MINUTE,
                organization_id,
                start,
                end,
                meters,
                "batch",
                len(rows),
                _digest(parts),
            )
        )

    def _parent(
        self,
        tier: str,
        source_tier: str,
        organization_id: str,
        start: int,
        end: int,
        expected_rows: int,
    ) -> int:
        rows = self.ledger._db.execute(
            "SELECT interval_start, meters, source_digest FROM usage_rollups "
            "WHERE tier=? AND organization_id=? AND interval_start>=? "
            "AND interval_end<=? ORDER BY interval_start",
            (source_tier, organization_id, start, end),
        ).fetchall()
        if len(rows) != expected_rows:
            raise UsageRollupIncomplete(
                f"{tier} interval requires {expected_rows} settled {source_tier} rows"
            )
        meters = _sum_meters(row[1] for row in rows)
        parts = [[row[0], row[2]] for row in rows]
        return self._insert_exact(
            RollupRow(
                tier,
                organization_id,
                start,
                end,
                meters,
                source_tier,
                len(rows),
                _digest(parts),
            )
        )

    def _insert_exact(self, row: RollupRow) -> int:
        encoded = canonical_json(row.meters)
        existing = self.ledger._db.execute(
            "SELECT interval_end, meters, source_tier, source_rows, source_digest "
            "FROM usage_rollups WHERE tier=? AND organization_id=? "
            "AND interval_start=?",
            (row.tier, row.organization_id, row.interval_start),
        ).fetchone()
        expected = (
            row.interval_end,
            encoded,
            row.source_tier,
            row.source_rows,
            row.source_digest,
        )
        if existing is not None:
            if existing != expected:
                raise UsageLedgerConflict(
                    "settled usage rollup differs from its immutable prior row"
                )
            return 0
        self.ledger._db.execute(
            "INSERT INTO usage_rollups("
            "tier, organization_id, interval_start, interval_end, meters, "
            "source_tier, source_rows, source_digest, settled_at"
            ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row.tier,
                row.organization_id,
                row.interval_start,
                row.interval_end,
                encoded,
                row.source_tier,
                row.source_rows,
                row.source_digest,
                int(time.time()),
            ),
        )
        return 1

    def rows(self, organization_id: str, tier: str) -> tuple[RollupRow, ...]:
        organization_id = _canonical_org(organization_id)
        if tier not in ROLLUP_TIERS:
            raise ValueError(f"unknown rollup tier: {tier}")
        with self.ledger._mutex:
            self.ledger._require_open()
            try:
                query = (
                    "SELECT interval_start, interval_end, meters, source_tier, "
                    "source_rows, source_digest FROM usage_rollups "
                    "WHERE tier=? AND organization_id=?"
                )
                parameters: tuple[object, ...] = (tier, organization_id)
                query += " ORDER BY interval_start"
                rows = self.ledger._db.execute(query, parameters).fetchall()
            except sqlite3.Error as exc:
                _raise_sqlite(exc)
        result: list[RollupRow] = []
        for row in rows:
            try:
                meters = json.loads(bytes(row[2]))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise UsageLedgerCorrupt("rollup meters are corrupt") from exc
            if not isinstance(meters, dict):
                raise UsageLedgerCorrupt("rollup meters are not an object")
            _sum_meters((canonical_json(meters),))
            result.append(
                RollupRow(
                    tier,
                    organization_id,
                    int(row[0]),
                    int(row[1]),
                    dict(meters),
                    row[3],
                    int(row[4]),
                    row[5],
                )
            )
        return tuple(result)
