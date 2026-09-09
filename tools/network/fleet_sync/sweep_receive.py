"""Apply swept pages, and hold the durable bootstrap state that gates the ACK.

The receiving half of the partition::

    SWEEP delivers ts <= F[origin]      PULL delivers ts > F[origin]

Two facts do the work here.

**No new merge path.** A swept record already carries the serving store's real
origin, transaction and operation index, so it is an ordinary
``AuthoredMutation`` and ``MutationCatalog.apply_remote_batch`` accepts it
unchanged -- with the materialization, blob deferral, foreign-key quarantine,
signed-settings verification and canonical hash round-trip that come with it.
That function applies exactly one originated transaction per call, while a
swept page is ordered by ADDRESS and so spans many; grouping is therefore the
whole of the adaptation. Nothing is synthesized and no authorship is invented.

**Sweep completion is not bootstrap completion.** Applying part of a
transaction publishes its origin/frontier entry before its siblings arrive, so
a mid-bootstrap store's watermarks do not satisfy the per-origin write-floor
promise and must not be served to anyone. ``may_advertise_frontier`` is that
gate, and it is durable: it answers correctly after a crash, not merely inside
one request. The joiner reaches ``COMPLETE`` only once the PULL half is applied.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import sqlite3
from typing import Any, Iterable, Mapping

from .catalog import MutationCatalog
from .compaction import AuthoredMutation
from .policies import PolicyKind, TABLE_POLICIES
from .streaming import BASE_TABLE_ORDER, _key_expressions


class BootstrapError(Exception):
    """Base for typed bootstrap-state failures."""


class BootstrapPhaseError(BootstrapError):
    """A phase transition was requested out of order."""


class BootstrapAbsent(BootstrapError):
    """An operation needs a bootstrap that was never begun."""


class Phase(str, Enum):
    #: Filling the ``<= F`` half. Frontier must not be advertised.
    SWEEPING = "sweeping"
    #: Keyspace walked; applying the ``> F`` half. Still must not advertise.
    PULLING = "pulling"
    #: Both halves durably applied. The store may serve its frontier.
    COMPLETE = "complete"


@dataclass(frozen=True)
class BootstrapState:
    phase: Phase
    #: The serving store's per-origin frontier, captured once at sweep start
    #: and NEVER advanced. Not derivable from the database, so it is persisted.
    frontier: dict[str, int]


@dataclass(frozen=True)
class AppliedPage:
    applied: int
    ignored: int
    #: Distinct (origin, transaction) groups this page was split into. A single
    #: transaction straddling pages yields a group in each.
    groups: int


_TABLE = """
    CREATE TABLE IF NOT EXISTS fleet_sync_bootstrap(
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        phase TEXT NOT NULL,
        frontier TEXT NOT NULL
    )
"""


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(_TABLE)


def begin_bootstrap(
    conn: sqlite3.Connection, frontier: Mapping[str, int]
) -> BootstrapState:
    """Record ``F`` and enter SWEEPING. Idempotent for the same frontier.

    ``F`` is the SERVING store's frontier at sweep start, not this store's own
    (which is empty for a new joiner, and would make the PULL half unbounded).
    """
    _ensure_table(conn)
    captured = {str(k): int(v) for k, v in frontier.items()}
    existing = read_bootstrap(conn)
    if existing is not None:
        if existing.frontier != captured:
            raise BootstrapPhaseError(
                "a bootstrap is already in progress under a different "
                "frontier; F is captured once and never advanced"
            )
        return existing
    conn.execute(
        "INSERT INTO fleet_sync_bootstrap(singleton,phase,frontier) "
        "VALUES(1,?,?)",
        (Phase.SWEEPING.value, json.dumps(captured, sort_keys=True)),
    )
    conn.commit()
    return BootstrapState(Phase.SWEEPING, captured)


def read_bootstrap(conn: sqlite3.Connection) -> BootstrapState | None:
    _ensure_table(conn)
    row = conn.execute(
        "SELECT phase,frontier FROM fleet_sync_bootstrap WHERE singleton=1"
    ).fetchone()
    if row is None:
        return None
    return BootstrapState(Phase(str(row[0])), json.loads(str(row[1])))


def _advance(conn: sqlite3.Connection, expected: Phase, target: Phase) -> BootstrapState:
    state = read_bootstrap(conn)
    if state is None:
        raise BootstrapAbsent(f"cannot enter {target.value} with no bootstrap")
    if state.phase is target:
        return state
    if state.phase is not expected:
        raise BootstrapPhaseError(
            f"cannot enter {target.value} from {state.phase.value}; "
            f"expected {expected.value}"
        )
    conn.execute(
        "UPDATE fleet_sync_bootstrap SET phase=? WHERE singleton=1",
        (target.value,),
    )
    conn.commit()
    return BootstrapState(target, state.frontier)


def record_sweep_complete(conn: sqlite3.Connection) -> BootstrapState:
    """The live universe is exhausted. This is NOT bootstrap completion."""
    return _advance(conn, Phase.SWEEPING, Phase.PULLING)


def record_pull_complete(conn: sqlite3.Connection) -> BootstrapState:
    """The ``> F`` half is durably applied. Only now may the frontier be served."""
    return _advance(conn, Phase.PULLING, Phase.COMPLETE)


def may_advertise_frontier(conn: sqlite3.Connection) -> bool:
    """Whether this store's watermarks may be published to anyone.

    False for every incomplete bootstrap, and durably so -- a crash mid-sweep
    leaves the row in SWEEPING, so the answer survives restart rather than
    depending on one request's in-memory state. A store that never bootstrapped
    this way is unaffected.
    """
    state = read_bootstrap(conn)
    return state is None or state.phase is Phase.COMPLETE


def apply_live_page(
    catalog: MutationCatalog, records: Iterable[AuthoredMutation]
) -> AppliedPage:
    """Apply one swept page by splitting it into originated transactions.

    ``apply_remote_batch`` requires every item of a call to share
    ``(origin, transaction_id, timestamp)``; a page ordered by address spans
    many, so it is grouped. Order within a group is preserved -- the callee
    sorts by operation index and rejects a repeated one.
    """
    grouped: dict[tuple[str, str, int], list[AuthoredMutation]] = {}
    for item in records:
        key = (
            item.origin_incarnation,
            item.transaction_id,
            item.mutation.timestamp_ns,
        )
        grouped.setdefault(key, []).append(item)

    applied = 0
    ignored = 0
    for group in grouped.values():
        group_applied, group_ignored = catalog.apply_remote_batch(group)
        applied += group_applied
        ignored += group_ignored
    return AppliedPage(applied, ignored, len(grouped))


def resume_cursor(
    conn: sqlite3.Connection,
) -> tuple[str, tuple[Any, ...]] | None:
    """Where to resume a sweep, derived from the store itself.

    The joiner is the sole writer during bootstrap and writes in canonical
    order, so its own furthest-along row IS the cursor; there is no separate
    position to persist and therefore no window in which a saved cursor and the
    applied data disagree.

    Known limitation, deliberately not hidden: a table that is empty at the
    SOURCE is indistinguishable from one this store has not reached, so resume
    returns the last table holding rows and the sweep re-walks any empty tables
    after it. That is wasted work, never a hole -- re-delivered rows merge
    inert. Rows deferred to quarantine are likewise not live, so they do not
    move this cursor; the quarantine drain, not the sweep, owns them.
    """
    for table in reversed([
        name for name in BASE_TABLE_ORDER
        if TABLE_POLICIES[name].kind not in {PolicyKind.LOCAL, PolicyKind.DERIVED}
    ]):
        expressions = _key_expressions(TABLE_POLICIES[table])
        where = ""
        if table == "settings":
            where = (
                " WHERE (supersedes IS NOT NULL OR excludes IS NOT NULL"
                " OR deprecated = 0)"
            )
        row = conn.execute(
            f'SELECT {",".join(expressions)} FROM "{table}"{where} '
            f'ORDER BY {",".join(expressions)} DESC LIMIT 1'
        ).fetchone()
        if row is not None:
            return table, tuple(row)
    return None
