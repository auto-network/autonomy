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

from .catalog import (
    MAX_TRANSACTION_FRAME_BYTES,
    MAX_TRANSACTION_OPERATIONS,
    MutationCatalog,
)
from .delta import encode_authored_frame
from .compaction import AuthoredMutation
from .policies import PolicyKind, TABLE_POLICIES
from .streaming import BASE_TABLE_ORDER, _key_expressions

#: Protocol version at which a peer is asking for, and can handle, a
#: keyspace sweep. A v3/v4 peer neither requests nor receives one: their
#: decoder rejects unknown fields outright, so "additive and ignored" is
#: not available and silence is the only safe treatment.
SWEEP_PROTOCOL_VERSION = 5


class BootstrapError(Exception):
    """Base for typed bootstrap-state failures."""


class BootstrapPhaseError(BootstrapError):
    """A phase transition was requested out of order."""


class BootstrapAbsent(BootstrapError):
    """An operation needs a bootstrap that was never begun."""


class CallerTransactionActive(BootstrapError):
    """The caller owns a transaction; these writers commit and must not steal it."""


class PageTooLarge(BootstrapError):
    """A page exceeds the record or encoded-byte bound, so none of it is applied."""


class SweepBeginInvalid(BootstrapError):
    """A sweep.begin control record was absent, malformed, or not this peer's."""


class BootstrapNotRecorded(BootstrapError):
    """Swept rows were offered to a store that has not recorded where they came from.

    ``F`` is not derivable from the receiving database. A store holding swept
    rows without it cannot know, after a restart, which frontier the partial
    copy was taken against -- and choosing a new one strands every key between
    the old frontier and the new. Refusing is what makes that state
    unreachable rather than merely discouraged.
    """


def _require_no_caller_transaction(conn: sqlite3.Connection) -> None:
    """These functions commit, so a caller's open transaction would be stolen.

    Refusing leaves it exactly as found -- neither committed nor rolled back.
    """
    if conn.in_transaction:
        raise CallerTransactionActive(
            "bootstrap state writers require a connection with no open "
            "transaction; the caller's transaction was left untouched"
        )


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
    #: Contiguous address-prefix runs this page was committed as. A
    #: transaction whose rows are not adjacent yields one run per stretch.
    groups: int


#: Wire kind carrying the serving store's frontier, once, before any page.
SWEEP_BEGIN_KIND = "sweep.begin"
#: Wire kind closing the sweep. Not completion -- the PULL half still owes.
SWEEP_END_KIND = "sweep.end"

#: A frontier names one entry per origin the serving store knows. Bounded so a
#: malformed or hostile record cannot make the receiver allocate without limit.
MAX_FRONTIER_ORIGINS = 4096

_TABLE = """
    CREATE TABLE IF NOT EXISTS fleet_sync_bootstrap(
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        phase TEXT NOT NULL,
        frontier TEXT NOT NULL
    )
"""


def _ensure_table(conn: sqlite3.Connection) -> None:
    """Create the table. Only the write/init path may call this."""
    conn.execute(_TABLE)


def _table_present(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='fleet_sync_bootstrap'"
    ).fetchone() is not None


def begin_bootstrap(
    conn: sqlite3.Connection, frontier: Mapping[str, int]
) -> BootstrapState:
    """Record ``F`` and enter SWEEPING. Idempotent for the same frontier.

    ``F`` is the SERVING store's frontier at sweep start, not this store's own
    (which is empty for a new joiner, and would make the PULL half unbounded).
    """
    _require_no_caller_transaction(conn)
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
    """Read the bootstrap row, or None. Genuinely READ-ONLY.

    This is reached by diagnostics and by the frontier gate on every outward
    request, so it must not mutate a healthy store. An absent table means no
    bootstrap, not an invitation to create one.
    """
    if not _table_present(conn):
        return None
    row = conn.execute(
        "SELECT phase,frontier FROM fleet_sync_bootstrap WHERE singleton=1"
    ).fetchone()
    if row is None:
        return None
    return BootstrapState(Phase(str(row[0])), json.loads(str(row[1])))


def _advance(conn: sqlite3.Connection, expected: Phase, target: Phase) -> BootstrapState:
    _require_no_caller_transaction(conn)
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
    """The caller certifies the ``> F`` half is durably applied.

    This records an assertion; it does not verify one. Nothing here proves the
    delta covered every address the sweep assigned to PULL -- that coverage
    proof belongs to the caller that ran the delta, and calling this without it
    advertises a frontier the store has not earned.
    """
    return _advance(conn, Phase.PULLING, Phase.COMPLETE)


def may_advertise_frontier(conn: sqlite3.Connection) -> bool:
    """Whether this store's watermarks may be published to anyone.

    Consumed by ``SQLiteFleetSyncStore.advertisable_origin_watermarks``, which
    both the direct and relay request builders call, so an incomplete bootstrap
    publishes nothing on either transport.

    Scope, so this is not read as more than it is: it gates the OUTWARD claim.
    Internal and repair reads of ``origin_watermarks`` are deliberately
    ungated and still see true state, and a store that never bootstrapped this
    way is unaffected.

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
    """Apply one swept page as CONTIGUOUS ADDRESS-PREFIX commits.

    ``apply_remote_batch`` requires every item of a call to share
    ``(origin, transaction_id, timestamp)``, and a page ordered by address
    spans many transactions -- so the page must be split. The split must
    preserve address order, NOT gather a transaction's scattered rows.

    Regrouping by transaction breaks the database-as-cursor invariant. Given
    addresses ``a(tx1) b(tx2) c(tx1)``, a transaction-major split commits
    ``a, c`` before ``b``; a crash there leaves ``c`` as the furthest row, so
    resume continues past ``b`` and ``b`` is never fetched again. A permanent
    hole, and exactly the failure the cursor exists to prevent.

    Splitting instead on each change of transaction identity keeps every commit
    a contiguous prefix of the page, so the furthest row present always implies
    every earlier address is present too. A transaction whose rows are not
    adjacent is applied in several calls, which is already permitted -- partial
    transactions are why the frontier gate exists.
    """
    if read_bootstrap(catalog.conn) is None:
        raise BootstrapNotRecorded(
            "cannot apply a swept page before begin_bootstrap has recorded "
            "the frontier this sweep is anchored to"
        )

    runs: list[list[AuthoredMutation]] = []
    identity: tuple[str, str, int] | None = None
    count = 0
    encoded = 0
    for item in records:
        # Bound the input WHILE collecting, before anything is applied: an
        # arbitrary iterable must not be able to commit a partial page and
        # then fail. Same ceilings the merge itself enforces per call.
        count += 1
        encoded += len(encode_authored_frame(item))
        if count > MAX_TRANSACTION_OPERATIONS or encoded > MAX_TRANSACTION_FRAME_BYTES:
            raise PageTooLarge(
                f"page exceeds the apply bounds at record {count} "
                f"({encoded} encoded bytes); nothing was applied"
            )
        key = (
            item.origin_incarnation,
            item.transaction_id,
            item.mutation.timestamp_ns,
        )
        if key != identity:
            runs.append([])
            identity = key
        runs[-1].append(item)

    applied = 0
    ignored = 0
    for run in runs:
        run_applied, run_ignored = catalog.apply_remote_batch(run)
        applied += run_applied
        ignored += run_ignored
    return AppliedPage(applied, ignored, len(runs))


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
        # DESC binds to ONE term: `ORDER BY a,b,c DESC` reverses only c and
        # would return a row that is not the composite maximum. Every key
        # expression must carry its own DESC.
        descending = ",".join(f"{expression} DESC" for expression in expressions)
        row = conn.execute(
            f'SELECT {",".join(expressions)} FROM "{table}"{where} '
            f'ORDER BY {descending} LIMIT 1'
        ).fetchone()
        if row is not None:
            return table, tuple(row)
    return None


def _hex64(value: object) -> bool:
    return (
        isinstance(value, str) and len(value) == 64
        and all(ch in "0123456789abcdef" for ch in value)
    )


def handle_sweep_begin(
    conn: sqlite3.Connection,
    record: Mapping[str, Any],
    *,
    expected_scope: str,
    expected_source_pub: str,
) -> BootstrapState:
    """Validate one ``sweep.begin`` and persist its frontier. ONE implementation.

    Both the direct and relay receivers call this. Parsing a control record
    that decides what a store believes about its own coverage is not something
    to write twice -- two copies drift, and the drift is silent until a store
    holds rows under a frontier it never agreed to.

    Everything is checked before anything is written:

    * the record is this kind, at the sweep-capable protocol version;
    * its source is the peer the transport actually authenticated, and its
      scope is the scope that was asked for -- a record from elsewhere never
      anchors this store;
    * the frontier is a bounded map of origin incarnation to a non-negative
      timestamp, so a malformed or hostile record cannot allocate without
      limit or poison the partition boundary with a negative or non-integer.

    Persistence is delegated to ``begin_bootstrap``, which already refuses a
    second, different frontier. A resume therefore keeps the ``F`` it started
    with: re-anchoring mid-sweep would move the partition boundary and strand
    every key between the old frontier and the new.
    """
    if not isinstance(record, Mapping):
        raise SweepBeginInvalid("sweep.begin must be a mapping")
    if record.get("kind") != SWEEP_BEGIN_KIND:
        raise SweepBeginInvalid(
            f"expected {SWEEP_BEGIN_KIND!r}, got {record.get('kind')!r}"
        )
    if record.get("v") != SWEEP_PROTOCOL_VERSION:
        raise SweepBeginInvalid(
            f"sweep.begin requires protocol v{SWEEP_PROTOCOL_VERSION}, "
            f"got {record.get('v')!r}"
        )
    source = record.get("source_machine_pub")
    if source != expected_source_pub:
        raise SweepBeginInvalid(
            "sweep.begin source is not the authenticated peer"
        )
    scope = record.get("scope")
    if scope != expected_scope:
        raise SweepBeginInvalid(
            f"sweep.begin scope {scope!r} is not the requested {expected_scope!r}"
        )
    frontier = record.get("frontier")
    if not isinstance(frontier, Mapping):
        raise SweepBeginInvalid("sweep.begin frontier must be a mapping")
    if len(frontier) > MAX_FRONTIER_ORIGINS:
        raise SweepBeginInvalid(
            f"sweep.begin frontier names {len(frontier)} origins, "
            f"bound is {MAX_FRONTIER_ORIGINS}"
        )
    captured: dict[str, int] = {}
    for origin, timestamp in frontier.items():
        if not _hex64(origin):
            raise SweepBeginInvalid("sweep.begin frontier origin is malformed")
        if isinstance(timestamp, bool) or not isinstance(timestamp, int):
            raise SweepBeginInvalid(
                "sweep.begin frontier timestamp must be an integer"
            )
        if timestamp < 0:
            raise SweepBeginInvalid(
                "sweep.begin frontier timestamp must not be negative"
            )
        captured[origin] = timestamp
    return begin_bootstrap(conn, captured)
