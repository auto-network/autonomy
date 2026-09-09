"""Controls for the receiving half: grouping, durable state, and the ACK gate."""

from pathlib import Path
import sqlite3

import pytest

from tools.graph.db import GraphDB
from tools.network.fleet_sync.authored_sweep import read_live_authored_page
from tools.network.fleet_sync.catalog import MutationCatalog
from tools.network.fleet_sync.sweep_receive import (
    BootstrapAbsent,
    BootstrapPhaseError,
    Phase,
    apply_live_page,
    begin_bootstrap,
    may_advertise_frontier,
    read_bootstrap,
    record_pull_complete,
    record_sweep_complete,
    resume_cursor,
)

SOURCE_ORIGIN = "machine-source"
TARGET_ORIGIN = "machine-target"
WIDE = 1 << 40


def _insert_source(conn: sqlite3.Connection, identity: str, title: str) -> None:
    conn.execute(
        "INSERT INTO sources(id,type,title,metadata,created_at,ingested_at) "
        "VALUES(?,?,?,?,?,?)",
        (identity, "note", title, "{}", "2026-08-19T00:00:00Z",
         "2026-08-19T00:00:00Z"),
    )


def _store(path: Path, origin: str) -> tuple[GraphDB, MutationCatalog]:
    db = GraphDB(path)
    catalog = MutationCatalog(db.conn, origin)
    catalog.install()
    return db, catalog


def _seeded_source(path: Path, count: int = 5) -> tuple[GraphDB, MutationCatalog]:
    db, catalog = _store(path, SOURCE_ORIGIN)
    for index in range(count):
        with catalog.transaction(10 + index, f"tx-{index}"):
            _insert_source(db.conn, f"s-{index:02d}", f"title {index}")
    return db, catalog


# ── round trip ───────────────────────────────────────────────────────────

def test_swept_page_applies_through_the_existing_merge(tmp_path: Path) -> None:
    """No new admission path: swept records are ordinary AuthoredMutations and
    the existing merge accepts them, provenance intact."""
    source, _ = _seeded_source(tmp_path / "source.db")
    target, target_catalog = _store(tmp_path / "target.db", TARGET_ORIGIN)
    try:
        page = read_live_authored_page(
            source.conn, frontier={SOURCE_ORIGIN: WIDE},
            max_records=100, max_bytes=1 << 20,
        )
        assert len(page.records) == 5

        result = apply_live_page(target_catalog, page.records)
        assert result.applied == 5
        assert result.groups == 5, "five distinct transactions, five batches"

        landed = [
            row[0] for row in target.conn.execute(
                "SELECT id FROM sources ORDER BY id"
            )
        ]
        assert landed == [f"s-{i:02d}" for i in range(5)]

        origins = {
            row[0] for row in target.conn.execute(
                "SELECT incarnation FROM fleet_sync_origins"
            )
        }
        assert SOURCE_ORIGIN in origins, "the source's authorship was preserved"
        assert TARGET_ORIGIN not in origins, "no local authorship was invented"
    finally:
        source.close()
        target.close()


def test_reapplying_a_page_is_inert(tmp_path: Path) -> None:
    """Resume re-delivers rows across a restart; they must merge inert."""
    source, _ = _seeded_source(tmp_path / "source.db", count=3)
    target, target_catalog = _store(tmp_path / "target.db", TARGET_ORIGIN)
    try:
        page = read_live_authored_page(
            source.conn, frontier={SOURCE_ORIGIN: WIDE},
            max_records=100, max_bytes=1 << 20,
        )
        first = apply_live_page(target_catalog, page.records)
        second = apply_live_page(target_catalog, page.records)
        assert first.applied == 3
        assert second.applied == 0 and second.ignored == 3
        assert target.conn.execute(
            "SELECT COUNT(*) FROM sources"
        ).fetchone()[0] == 3
    finally:
        source.close()
        target.close()


def test_one_transaction_spanning_pages_groups_per_page(tmp_path: Path) -> None:
    """A transaction straddling a page boundary is applied in parts. That is
    permitted; what is forbidden is advertising a frontier while it is partial."""
    source, source_catalog = _store(tmp_path / "source.db", SOURCE_ORIGIN)
    target, target_catalog = _store(tmp_path / "target.db", TARGET_ORIGIN)
    try:
        with source_catalog.transaction(10, "tx-wide"):
            for index in range(6):
                _insert_source(source.conn, f"s-{index}", "x")

        begin_bootstrap(target.conn, {SOURCE_ORIGIN: WIDE})
        first = read_live_authored_page(
            source.conn, frontier={SOURCE_ORIGIN: WIDE},
            max_records=2, max_bytes=1 << 20,
        )
        applied = apply_live_page(target_catalog, first.records)
        assert applied.groups == 1 and applied.applied == 2

        # The transaction is now PARTIALLY present, so the frontier this store
        # would report does not satisfy the write-floor promise.
        assert may_advertise_frontier(target.conn) is False
        assert target.conn.execute(
            "SELECT COUNT(*) FROM sources"
        ).fetchone()[0] == 2
    finally:
        source.close()
        target.close()


# ── durable state and the ACK gate ───────────────────────────────────────

def test_frontier_and_phase_survive_a_restart(tmp_path: Path) -> None:
    """F is not derivable from the database, so it is persisted; a crash
    mid-sweep must not lose which frontier the sweep was anchored to."""
    path = tmp_path / "target.db"
    db, _ = _store(path, TARGET_ORIGIN)
    try:
        begin_bootstrap(db.conn, {SOURCE_ORIGIN: 42, "other": 7})
    finally:
        db.close()

    reopened = GraphDB(path)
    try:
        state = read_bootstrap(reopened.conn)
        assert state is not None
        assert state.phase is Phase.SWEEPING
        assert state.frontier == {SOURCE_ORIGIN: 42, "other": 7}
        assert may_advertise_frontier(reopened.conn) is False, (
            "a crash mid-sweep must still refuse to advertise after restart"
        )
    finally:
        reopened.close()


def test_advertisement_opens_only_after_the_pull_half(tmp_path: Path) -> None:
    db, _ = _store(tmp_path / "target.db", TARGET_ORIGIN)
    try:
        assert may_advertise_frontier(db.conn) is True, (
            "a store that never bootstrapped this way is unaffected"
        )
        begin_bootstrap(db.conn, {SOURCE_ORIGIN: 10})
        assert may_advertise_frontier(db.conn) is False

        record_sweep_complete(db.conn)
        assert read_bootstrap(db.conn).phase is Phase.PULLING
        assert may_advertise_frontier(db.conn) is False, (
            "sweep-complete is NOT bootstrap-complete"
        )

        record_pull_complete(db.conn)
        assert read_bootstrap(db.conn).phase is Phase.COMPLETE
        assert may_advertise_frontier(db.conn) is True
    finally:
        db.close()


def test_phase_cannot_skip_the_pull_half(tmp_path: Path) -> None:
    db, _ = _store(tmp_path / "target.db", TARGET_ORIGIN)
    try:
        begin_bootstrap(db.conn, {SOURCE_ORIGIN: 10})
        with pytest.raises(BootstrapPhaseError):
            record_pull_complete(db.conn)
        assert may_advertise_frontier(db.conn) is False
    finally:
        db.close()


def test_completion_without_a_bootstrap_is_typed(tmp_path: Path) -> None:
    db, _ = _store(tmp_path / "target.db", TARGET_ORIGIN)
    try:
        with pytest.raises(BootstrapAbsent):
            record_sweep_complete(db.conn)
    finally:
        db.close()


def test_frontier_is_captured_once_and_never_advanced(tmp_path: Path) -> None:
    """Re-anchoring mid-bootstrap would move the partition boundary and strand
    every key between the old and new F."""
    db, _ = _store(tmp_path / "target.db", TARGET_ORIGIN)
    try:
        begin_bootstrap(db.conn, {SOURCE_ORIGIN: 10})
        again = begin_bootstrap(db.conn, {SOURCE_ORIGIN: 10})
        assert again.phase is Phase.SWEEPING, "idempotent for the same F"
        with pytest.raises(BootstrapPhaseError):
            begin_bootstrap(db.conn, {SOURCE_ORIGIN: 99})
    finally:
        db.close()


# ── the database as the cursor ───────────────────────────────────────────

def test_resume_cursor_is_derived_from_the_store(tmp_path: Path) -> None:
    """Sole writer plus canonical order means the store's own furthest row is
    the position; nothing separate is persisted, so nothing can disagree."""
    db, catalog = _store(tmp_path / "target.db", TARGET_ORIGIN)
    try:
        assert resume_cursor(db.conn) is None, "empty store resumes from the start"
        for index in range(4):
            with catalog.transaction(10 + index, f"tx-{index}"):
                _insert_source(db.conn, f"s-{index:02d}", "x")
        cursor = resume_cursor(db.conn)
        assert cursor is not None
        table, address = cursor
        assert table == "sources"
        assert address == ("s-03",), "the furthest key in canonical order"
    finally:
        db.close()


def test_resume_cursor_round_trips_into_the_reader(tmp_path: Path) -> None:
    """The derived cursor must be exactly what the producer accepts, so a
    restart continues without duplicating or skipping a row."""
    source, _ = _seeded_source(tmp_path / "source.db", count=6)
    target, target_catalog = _store(tmp_path / "target.db", TARGET_ORIGIN)
    try:
        frontier = {SOURCE_ORIGIN: WIDE}
        begin_bootstrap(target.conn, frontier)

        first = read_live_authored_page(
            source.conn, frontier=frontier, max_records=2, max_bytes=1 << 20,
        )
        apply_live_page(target_catalog, first.records)

        # Simulate a restart: the cursor is read back out of the store.
        cursor = resume_cursor(target.conn)
        rest = read_live_authored_page(
            source.conn, frontier=frontier, start_after=cursor,
            max_records=100, max_bytes=1 << 20,
        )
        apply_live_page(target_catalog, rest.records)

        landed = [
            row[0] for row in target.conn.execute(
                "SELECT id FROM sources ORDER BY id"
            )
        ]
        assert landed == [f"s-{i:02d}" for i in range(6)]
        assert len(landed) == len(set(landed)), "resume duplicated a row"
    finally:
        source.close()
        target.close()


def test_commits_are_contiguous_address_prefixes_not_transaction_major(
    tmp_path: Path,
) -> None:
    """Regrouping a page by transaction breaks the database-as-cursor
    invariant. With addresses a(tx1) b(tx2) c(tx1), a transaction-major split
    commits a and c before b; a crash there leaves c as the furthest row, so
    resume continues past b and b is lost forever. Every commit must instead be
    a contiguous prefix, so the furthest row implies all earlier ones."""
    source, source_catalog = _store(tmp_path / "source.db", SOURCE_ORIGIN)
    target, target_catalog = _store(tmp_path / "target.db", TARGET_ORIGIN)
    try:
        # Interleave two transactions across canonical address order: tx1
        # owns the outer addresses, tx2 the middle one. Local writes must
        # advance in time, so tx1 writes both of its rows at once.
        with source_catalog.transaction(10, "tx1"):
            _insert_source(source.conn, "s-a", "a")
            _insert_source(source.conn, "s-c", "c")
        with source_catalog.transaction(11, "tx2"):
            _insert_source(source.conn, "s-b", "b")

        page = read_live_authored_page(
            source.conn, frontier={SOURCE_ORIGIN: WIDE},
            max_records=100, max_bytes=1 << 20,
        )
        assert [i.mutation.address[0] for i in page.records] == [
            "s-a", "s-b", "s-c"
        ], "producer must emit canonical address order"

        result = apply_live_page(target_catalog, page.records)
        assert result.groups == 3, (
            "three identity changes across the address order means three "
            "prefix commits, not two transaction-major batches"
        )

        landed = [
            row[0] for row in target.conn.execute(
                "SELECT id FROM sources ORDER BY id"
            )
        ]
        assert landed == ["s-a", "s-b", "s-c"]
        assert resume_cursor(target.conn) == ("sources", ("s-c",))
    finally:
        source.close()
        target.close()


def test_crash_mid_page_leaves_a_resumable_prefix(tmp_path: Path) -> None:
    """Hand apply_live_page the WHOLE interleaved page and fail the second real
    commit, so the prefix property is proven by the implementation's own commit
    ordering rather than by a pre-sliced input.

    With a(tx1) b(tx2) c(tx1), the transaction-major split this replaced would
    have committed a and c first; the crash would then leave c as the furthest
    row and resume would skip b forever.
    """
    source, source_catalog = _store(tmp_path / "source.db", SOURCE_ORIGIN)
    target, target_catalog = _store(tmp_path / "target.db", TARGET_ORIGIN)
    try:
        # Interleave two transactions across canonical address order: tx1
        # owns the outer addresses, tx2 the middle one. Local writes must
        # advance in time, so tx1 writes both of its rows at once.
        with source_catalog.transaction(10, "tx1"):
            _insert_source(source.conn, "s-a", "a")
            _insert_source(source.conn, "s-c", "c")
        with source_catalog.transaction(11, "tx2"):
            _insert_source(source.conn, "s-b", "b")

        page = read_live_authored_page(
            source.conn, frontier={SOURCE_ORIGIN: WIDE},
            max_records=100, max_bytes=1 << 20,
        )
        assert [i.mutation.address[0] for i in page.records] == [
            "s-a", "s-b", "s-c"
        ]

        real_apply = target_catalog.apply_remote_batch
        calls = {"n": 0}

        def failing(batch):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("simulated crash after the first commit")
            return real_apply(batch)

        target_catalog.apply_remote_batch = failing
        with pytest.raises(RuntimeError, match="simulated crash"):
            apply_live_page(target_catalog, page.records)
        target_catalog.apply_remote_batch = real_apply

        landed = [
            row[0] for row in target.conn.execute(
                "SELECT id FROM sources ORDER BY id"
            )
        ]
        assert landed == ["s-a"], (
            "the crash must leave a contiguous prefix; a transaction-major "
            f"split would have left ['s-a', 's-c'], got {landed}"
        )

        cursor = resume_cursor(target.conn)
        assert cursor == ("sources", ("s-a",))
        rest = read_live_authored_page(
            source.conn, frontier={SOURCE_ORIGIN: WIDE},
            start_after=cursor, max_records=100, max_bytes=1 << 20,
        )
        assert [i.mutation.address[0] for i in rest.records] == ["s-b", "s-c"], (
            "resume must recover s-b, the row a transaction-major commit "
            "would have stranded"
        )
        apply_live_page(target_catalog, rest.records)
        assert [
            row[0] for row in target.conn.execute(
                "SELECT id FROM sources ORDER BY id"
            )
        ] == ["s-a", "s-b", "s-c"]
    finally:
        source.close()
        target.close()


def test_state_writers_refuse_an_active_caller_transaction(tmp_path: Path) -> None:
    """These writers commit, so an open caller transaction would be stolen.
    Refusal must leave the caller's pending work neither committed nor rolled
    back."""
    from tools.network.fleet_sync.sweep_receive import CallerTransactionActive

    db, catalog = _store(tmp_path / "target.db", TARGET_ORIGIN)
    try:
        db.conn.execute("BEGIN")
        db.conn.execute(
            "CREATE TABLE IF NOT EXISTS caller_scratch(x INTEGER)"
        )
        db.conn.execute("INSERT INTO caller_scratch(x) VALUES(1)")

        with pytest.raises(CallerTransactionActive):
            begin_bootstrap(db.conn, {SOURCE_ORIGIN: 10})
        assert db.conn.in_transaction, "the caller's transaction was disturbed"
        assert db.conn.execute(
            "SELECT COUNT(*) FROM caller_scratch"
        ).fetchone()[0] == 1, "pending write was neither kept nor discarded"
        db.conn.rollback()

        # And the refusal wrote nothing.
        assert read_bootstrap(db.conn) is None
    finally:
        db.close()


def test_oversize_page_applies_nothing(tmp_path: Path) -> None:
    """Bounds run while collecting, so an over-limit iterable cannot commit a
    prefix and then fail."""
    from tools.network.fleet_sync.catalog import MAX_TRANSACTION_OPERATIONS
    from tools.network.fleet_sync.sweep_receive import PageTooLarge

    source, _ = _seeded_source(tmp_path / "source.db", count=3)
    target, target_catalog = _store(tmp_path / "target.db", TARGET_ORIGIN)
    try:
        page = read_live_authored_page(
            source.conn, frontier={SOURCE_ORIGIN: WIDE},
            max_records=100, max_bytes=1 << 20,
        )
        oversize = list(page.records) * (MAX_TRANSACTION_OPERATIONS // 3 + 2)
        with pytest.raises(PageTooLarge):
            apply_live_page(target_catalog, oversize)
        assert target.conn.execute(
            "SELECT COUNT(*) FROM sources"
        ).fetchone()[0] == 0, "an over-limit page applied a partial prefix"
    finally:
        source.close()
        target.close()


def test_resume_cursor_returns_the_composite_maximum(tmp_path: Path) -> None:
    """`ORDER BY a,b,c DESC` reverses only the last term and would return a row
    that is not the composite maximum. Every key expression needs its own DESC."""
    db, catalog = _store(tmp_path / "target.db", TARGET_ORIGIN)
    try:
        # note_versions has a composite logical key (source_id, created_at, hash).
        with catalog.transaction(10, "tx-1"):
            _insert_source(db.conn, "s-a", "a")
            _insert_source(db.conn, "s-b", "b")
            db.conn.execute(
                "INSERT INTO note_versions(source_id,version,content,created_at)"
                " VALUES(?,?,?,?)", ("s-b", 1, "later", "2026-01-02T00:00:00Z"),
            )
            db.conn.execute(
                "INSERT INTO note_versions(source_id,version,content,created_at)"
                " VALUES(?,?,?,?)", ("s-a", 1, "earlier", "2026-01-03T00:00:00Z"),
            )
        table, address = resume_cursor(db.conn)
        assert table == "note_versions"
        assert address[0] == "s-b", (
            "composite maximum is the greatest source_id, not the greatest "
            "created_at -- a single trailing DESC returns the wrong row"
        )
    finally:
        db.close()


def test_bootstrap_table_is_classified_local_and_survives_reopen(
    tmp_path: Path,
) -> None:
    """An unclassified fleet_sync_* table fails the schema audit, and the
    catalog must reopen cleanly over a store that has begun bootstrapping."""
    from tools.network.fleet_sync.policies import (
        LOCAL_SYNC_TABLES, PolicyKind, audit_schema, classify_table,
    )

    assert "fleet_sync_bootstrap" in LOCAL_SYNC_TABLES
    assert classify_table("fleet_sync_bootstrap") is PolicyKind.LOCAL

    path = tmp_path / "target.db"
    db, _ = _store(path, TARGET_ORIGIN)
    try:
        begin_bootstrap(db.conn, {SOURCE_ORIGIN: 10})
        audit_schema(db.conn)
    finally:
        db.close()

    reopened = GraphDB(path)
    try:
        reopened_catalog = MutationCatalog(reopened.conn, TARGET_ORIGIN)
        reopened_catalog.install()
        audit_schema(reopened.conn)
        assert read_bootstrap(reopened.conn).phase is Phase.SWEEPING
    finally:
        reopened.close()
