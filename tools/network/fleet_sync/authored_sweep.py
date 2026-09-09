"""Bounded pages of live rows, carrying the provenance the catalog already holds.

This is the SWEEP half of bootstrap. The partition it implements is::

    SWEEP serves every key whose winning transaction is  ts <= F[origin]
    PULL  serves every key whose winning transaction is  ts >  F[origin]

``F`` is the serving machine's per-origin frontier, captured once at sweep
start and never advanced. Every key sits on exactly one side, so coverage is a
property of the partition rather than of overlap or of a heal pass. A key
overwritten after ``F`` was captured leaves this half and becomes PULL's --
which is why this reader never needs, and never attempts, to reconstruct a
previous value. There is no cut here and no artifact.

Deliberately NOT covered, and asserted absent by the tests rather than
pretended: catalog-only tombstones (a deleted row has no live row to walk from)
and quarantine-only frames. A walk over live rows structurally cannot discover
either; composition must.

Reading only. Nothing here creates a transaction row, advances a watermark, or
writes to the store.
"""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from typing import Any, Mapping

from .catalog import MAX_TRANSACTION_FRAME_BYTES, MAX_TRANSACTION_OPERATIONS
from .codec import CanonicalValue, Mutation, encode_value
from .compaction import AuthoredMutation
from .delta import MAX_DELTA_FRAME_BYTES, encode_authored_frame
from .policies import PolicyKind, TABLE_POLICIES, audit_schema
from .snapshot import _logical_address, _logical_values
from .streaming import (
    BASE_TABLE_ORDER,
    _key_expressions,
    register_streaming_functions,
)


class SweepPageError(Exception):
    """Base for every typed failure of a page read."""


class CallerTransactionActive(SweepPageError):
    """The caller already owns a transaction on this connection."""


class InvalidBudget(SweepPageError):
    """A record or byte budget was absent, non-integer, boolean or non-positive."""


class MalformedCursor(SweepPageError):
    """``start_after`` does not name a table and address this policy set orders."""


class UntrackedLiveRow(SweepPageError):
    """A live row has no catalog provenance. The whole page fails.

    A row written before its table gained capture is untracked until the
    catalog backfill reaches it. Emitting it would require inventing an origin,
    a transaction and an operation index; skipping it would bootstrap a hole
    that nothing later repairs. Both are worse than refusing.
    """


class ContradictoryCatalogRow(SweepPageError):
    """The catalog marks an address a tombstone while its live row exists."""


class OversizeRecord(SweepPageError):
    """One record alone exceeds the byte budget, so no page can contain it."""


@dataclass(frozen=True)
class LivePage:
    """One bounded page. ``records``/``frames`` are index-aligned."""

    records: tuple[AuthoredMutation, ...]
    frames: tuple[bytes, ...]
    #: Last (table, address) this page finished with -- INCLUDING addresses
    #: filtered out as PULL's. Excludes a looked-ahead record left unconsumed
    #: for the next page. ``None`` when the page consumed nothing.
    examined_through: tuple[str, tuple[CanonicalValue, ...]] | None
    #: The live universe is exhausted. NOT bootstrap completion -- the PULL
    #: half must still be applied before this store is caught up.
    exhausted: bool
    #: Addresses this page examined and assigned to PULL.
    filtered: int
    #: Rows READ from the store this page -- emitted, filtered, and a
    #: non-fitting lookahead if one was reached. It is a scan-cost measure,
    #: NOT cursor progress: a looked-ahead record is counted here but
    #: deliberately not covered by ``examined_through``, so the two can
    #: disagree by one and that is correct. Bounded independently of
    #: ``max_records`` so an all-newer store cannot be walked end to end
    #: inside one read view.
    examined: int


def _positive(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise InvalidBudget(f"{label} must be a positive integer, got {value!r}")
    return int(value)


def _sweepable_tables() -> tuple[str, ...]:
    return tuple(
        table for table in BASE_TABLE_ORDER
        if TABLE_POLICIES[table].kind not in {PolicyKind.LOCAL, PolicyKind.DERIVED}
    )


def read_live_authored_page(
    conn: sqlite3.Connection,
    *,
    frontier: Mapping[str, int],
    start_after: tuple[str, tuple[Any, ...]] | None = None,
    max_records: int,
    max_bytes: int,
    max_examined: int | None = None,
) -> LivePage:
    """Read one bounded page of live rows at or below ``frontier``.

    ``frontier`` maps origin incarnation to the newest timestamp this server
    had absorbed when the sweep began. An origin absent from the map is read as
    ``0``, so rows from an origin that appeared after the sweep started are
    assigned to PULL, which is correct.

    The whole page is built under one owned read view, which is closed before
    returning; nothing is yielded while the view is open. An already-active
    caller transaction is refused and left untouched.
    """
    max_records = min(
        _positive(max_records, "max_records"), MAX_TRANSACTION_OPERATIONS
    )
    max_bytes = min(
        _positive(max_bytes, "max_bytes"), MAX_TRANSACTION_FRAME_BYTES
    )
    # Emitting is bounded by max_records, but FILTERED rows emit nothing, so a
    # store whose keyspace is entirely newer than F would be walked end to end
    # under one read view. Bound rows READ as well, and return the position so
    # an all-filtered page still makes progress.
    max_examined = min(
        _positive(
            max_records if max_examined is None else max_examined, "max_examined"
        ),
        MAX_TRANSACTION_OPERATIONS,
    )
    if conn.in_transaction:
        raise CallerTransactionActive(
            "read_live_authored_page requires a connection with no open "
            "transaction; the caller's transaction was left untouched"
        )

    register_streaming_functions(conn)
    audit_schema(conn)
    tables = _sweepable_tables()
    ranks = {table: rank for rank, table in enumerate(tables)}

    start_table: str | None = None
    start_address: tuple[Any, ...] = ()
    if start_after is not None:
        start_table, raw_address = start_after
        if start_table not in ranks:
            raise MalformedCursor(f"resume table {start_table!r} is not swept")
        if not isinstance(raw_address, (tuple, list)):
            raise MalformedCursor("resume address must be a tuple")
        start_address = tuple(raw_address)
        expected = len(_key_expressions(TABLE_POLICIES[start_table]))
        # An unsigned settings row's logical address carries no persona, so a
        # cursor derived from it is one part short. Normalize that supported
        # arity here -- validating first would reject a cursor this reader
        # goes on to pad anyway.
        if (
            start_table == "settings"
            and len(start_address) == expected - 1
        ):
            start_address += ("",)
        if len(start_address) != expected:
            raise MalformedCursor(
                f"resume address for {start_table!r} needs {expected} parts, "
                f"got {len(start_address)}"
            )

    records: list[AuthoredMutation] = []
    frames: list[bytes] = []
    examined: tuple[str, tuple[CanonicalValue, ...]] | None = None
    used = 0
    filtered = 0
    examined_rows = 0
    exhausted = True

    previous_row_factory = conn.row_factory
    conn.execute("BEGIN")
    try:
        conn.row_factory = sqlite3.Row
        for table in tables:
            if start_table is not None and ranks[table] < ranks[start_table]:
                continue
            policy = TABLE_POLICIES[table]
            expressions = _key_expressions(policy)
            where = ""
            params: tuple[Any, ...] = ()
            if table == "settings":
                # Matches the existing snapshot rule: a logical setting has one
                # live base row, and superseded bases are retained only as local
                # history. Override and exclusion rows carry their own addresses.
                where = (
                    ' WHERE (supersedes IS NOT NULL OR excludes IS NOT NULL'
                    ' OR deprecated = 0)'
                )
            if start_table == table:
                comparison = (
                    f"({','.join(expressions)}) > "
                    f"({','.join('?' for _ in expressions)})"
                )
                where += (" AND " if where else " WHERE ") + comparison
                # Arity was normalized at validation, including the
                # unsigned-settings persona.
                params += tuple(start_address)
            query = (
                f'SELECT * FROM "{table}"{where} '
                f'ORDER BY {",".join(expressions)}'
            )
            # Iterating the cursor keeps SQLite's page cache the only row
            # buffer. There is deliberately no fetchall of the keyspace.
            for raw in conn.execute(query, params):
                row = dict(raw)
                address = _logical_address(policy, row)
                blob = encode_value([table, list(address)])
                entry = conn.execute(
                    "SELECT c.timestamp_ns, c.tombstone, c.operation_index,"
                    " o.incarnation, t.transaction_id"
                    " FROM fleet_sync_catalog c"
                    " JOIN fleet_sync_transactions t ON t.id = c.transaction_ref"
                    " JOIN fleet_sync_origins o ON o.id = t.origin_id"
                    " WHERE c.address = ?",
                    (blob,),
                ).fetchone()
                if entry is None:
                    raise UntrackedLiveRow(
                        f"live row has no catalog provenance: table={table!r} "
                        f"address={address!r}"
                    )
                timestamp = int(entry["timestamp_ns"])
                if int(entry["tombstone"]):
                    raise ContradictoryCatalogRow(
                        f"catalog marks a live row as a tombstone: "
                        f"table={table!r} address={address!r}"
                    )
                origin = str(entry["incarnation"])
                # THE PARTITION. ts <= F[origin] is SWEEP's; anything newer
                # belongs to PULL and is deliberately not served here.
                examined_rows += 1
                if timestamp > int(frontier.get(origin, 0)):
                    filtered += 1
                    examined = (table, address)
                    if examined_rows >= max_examined:
                        return LivePage(
                            tuple(records), tuple(frames), examined, False,
                            filtered, examined_rows,
                        )
                    continue
                item = AuthoredMutation(
                    origin,
                    str(entry["transaction_id"]),
                    int(entry["operation_index"]),
                    Mutation(
                        table, address, timestamp, False,
                        _logical_values(policy, row),
                    ),
                )
                frame = encode_authored_frame(item)
                if len(frame) > MAX_DELTA_FRAME_BYTES:
                    raise OversizeRecord(
                        f"authored record exceeds the canonical frame bound: "
                        f"table={table!r} address={address!r}"
                    )
                if used + len(frame) > max_bytes:
                    if not records:
                        raise OversizeRecord(
                            f"record of {len(frame)} bytes cannot fit a page "
                            f"budget of {max_bytes}: table={table!r} "
                            f"address={address!r}"
                        )
                    # Left unconsumed for the next page; the cursor must NOT
                    # advance over a record this page did not deliver.
                    return LivePage(
                        tuple(records), tuple(frames), examined, False,
                        filtered, examined_rows,
                    )
                records.append(item)
                frames.append(frame)
                used += len(frame)
                examined = (table, address)
                if len(records) >= max_records or examined_rows >= max_examined:
                    return LivePage(
                        tuple(records), tuple(frames), examined, False,
                        filtered, examined_rows,
                    )
            start_table = None
            start_address = ()
    finally:
        # A read view is never held across a return. Rollback, not commit:
        # this reader writes nothing and must not disturb the caller.
        conn.rollback()
        conn.row_factory = previous_row_factory

    return LivePage(
        tuple(records), tuple(frames), examined, exhausted, filtered,
        examined_rows,
    )
