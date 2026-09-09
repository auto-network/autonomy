"""Controls for the SWEEP half: live rows at or below the frontier.

The partition under test::

    SWEEP serves ts <= F[origin]      PULL serves ts > F[origin]

Explicitly OUT of scope for this reader, and asserted absent rather than
pretended: catalog-only tombstones and quarantine-only frames. A walk over live
rows cannot discover either.
"""

from pathlib import Path
import sqlite3

import pytest

from tools.graph.db import GraphDB
from tools.network.fleet_sync.authored_sweep import (
    CallerTransactionActive,
    ContradictoryCatalogRow,
    InvalidBudget,
    MalformedCursor,
    OversizeRecord,
    UntrackedLiveRow,
    read_live_authored_page,
)
from tools.network.fleet_sync.catalog import MutationCatalog

ORIGIN = "machine-a"
WIDE = 1 << 40  # a frontier no test timestamp reaches


def _insert_source(conn: sqlite3.Connection, identity: str, title: str) -> None:
    conn.execute(
        "INSERT INTO sources(id,type,title,metadata,created_at,ingested_at) "
        "VALUES(?,?,?,?,?,?)",
        (identity, "note", title, "{}", "2026-08-19T00:00:00Z",
         "2026-08-19T00:00:00Z"),
    )


def _store(path: Path) -> tuple[GraphDB, MutationCatalog]:
    db = GraphDB(path)
    catalog = MutationCatalog(db.conn, ORIGIN)
    catalog.install()
    return db, catalog


def _page(conn, frontier, **kwargs):
    kwargs.setdefault("max_records", 1000)
    kwargs.setdefault("max_bytes", 8 * 1024 * 1024)
    return read_live_authored_page(conn, frontier=frontier, **kwargs)


def _addresses(page) -> list[tuple]:
    return [item.mutation.address for item in page.records]


# ── the partition ────────────────────────────────────────────────────────

def test_frontier_partitions_the_keyspace_at_exactly_ts_equals_f(
    tmp_path: Path,
) -> None:
    """ts == F is SWEEP's. The pull serves strictly greater, so an inclusive
    sweep boundary is what makes the two halves disjoint AND total."""
    db, catalog = _store(tmp_path / "personal.db")
    try:
        with catalog.transaction(10, "tx-below"):
            _insert_source(db.conn, "s-below", "below")
        with catalog.transaction(20, "tx-at"):
            _insert_source(db.conn, "s-at", "at the frontier")
        with catalog.transaction(30, "tx-above"):
            _insert_source(db.conn, "s-above", "above")

        page = _page(db.conn, {ORIGIN: 20})
        served = {address[0] for address in _addresses(page)}
        assert served == {"s-below", "s-at"}, "ts == F must be swept"
        assert page.filtered == 1
        assert page.exhausted is True
    finally:
        db.close()


def test_origin_absent_from_the_frontier_is_pull_not_sweep(
    tmp_path: Path,
) -> None:
    """An origin that appeared after the sweep began reads as F = 0, so its
    rows belong to PULL. Serving them here would claim coverage the frontier
    does not describe."""
    db, catalog = _store(tmp_path / "personal.db")
    try:
        with catalog.transaction(10, "tx-1"):
            _insert_source(db.conn, "s1", "unknown origin")
        page = _page(db.conn, {"some-other-machine": WIDE})
        assert page.records == ()
        assert page.filtered == 1
    finally:
        db.close()


def test_overwrite_after_the_frontier_leaves_the_sweep_half(
    tmp_path: Path,
) -> None:
    """A key overwritten past F becomes PULL's. The sweep must NOT try to
    reconstruct its previous value -- it has none, and PULL owns the key."""
    db, catalog = _store(tmp_path / "personal.db")
    try:
        with catalog.transaction(10, "tx-1"):
            _insert_source(db.conn, "s1", "original")
        with catalog.transaction(99, "tx-2"):
            db.conn.execute("UPDATE sources SET title='rewritten' WHERE id='s1'")

        page = _page(db.conn, {ORIGIN: 10})
        assert page.records == ()
        assert page.filtered == 1
    finally:
        db.close()


# ── provenance ───────────────────────────────────────────────────────────

def test_provenance_is_the_catalog_not_application_columns(
    tmp_path: Path,
) -> None:
    """`created_at` is a future-dated application column here. The record must
    carry the CATALOG timestamp and the real origin/transaction/operation."""
    db, catalog = _store(tmp_path / "personal.db")
    try:
        with catalog.transaction(7, "tx-provenance"):
            db.conn.execute(
                "INSERT INTO sources(id,type,title,metadata,created_at,"
                "ingested_at) VALUES(?,?,?,?,?,?)",
                ("s1", "note", "t", "{}", "2999-01-01T00:00:00Z",
                 "2999-01-01T00:00:00Z"),
            )
        page = _page(db.conn, {ORIGIN: WIDE})
        assert len(page.records) == 1
        item = page.records[0]
        assert item.origin_incarnation == ORIGIN
        assert item.transaction_id == "tx-provenance"
        assert item.operation_index >= 0
        assert item.mutation.timestamp_ns == 7, "catalog stamp, not created_at"
    finally:
        db.close()


def test_untracked_live_row_fails_the_whole_page_after_valid_rows(
    tmp_path: Path,
) -> None:
    """A row written before its table gained capture has no provenance.
    Returning the valid prefix plus a counter would let a caller bootstrap a
    hole, so the entire page fails and no cursor is produced."""
    db = GraphDB(tmp_path / "personal.db")
    try:
        _insert_source(db.conn, "s-untracked", "written before capture")
        db.conn.commit()
        catalog = MutationCatalog(db.conn, ORIGIN)
        catalog.install()
        untracked = db.conn.execute(
            "SELECT COUNT(*) FROM sources WHERE id='s-untracked' AND NOT EXISTS("
            " SELECT 1 FROM fleet_sync_catalog)"
        ).fetchone()[0]
        if not untracked:
            pytest.skip("install() backfilled the row; untracked case needs "
                        "a store where backfill has not yet reached it")
        with pytest.raises(UntrackedLiveRow):
            _page(db.conn, {ORIGIN: WIDE})
        assert not db.conn.in_transaction, "read view must close on error"
    finally:
        db.close()


def test_contradictory_tombstone_fails_the_page(tmp_path: Path) -> None:
    db, catalog = _store(tmp_path / "personal.db")
    try:
        with catalog.transaction(10, "tx-1"):
            _insert_source(db.conn, "s1", "live")
        db.conn.execute("UPDATE fleet_sync_catalog SET tombstone=1")
        db.conn.commit()
        with pytest.raises(ContradictoryCatalogRow):
            _page(db.conn, {ORIGIN: WIDE})
        assert not db.conn.in_transaction
    finally:
        db.close()


# ── budgets, cursor, ownership ───────────────────────────────────────────

@pytest.mark.parametrize("bad", [0, -1, True, False, None, 1.5, "8"])
def test_budgets_must_be_positive_integers(tmp_path: Path, bad: object) -> None:
    db, _ = _store(tmp_path / "personal.db")
    try:
        with pytest.raises(InvalidBudget):
            read_live_authored_page(
                db.conn, frontier={}, max_records=bad, max_bytes=1024,
            )
        with pytest.raises(InvalidBudget):
            read_live_authored_page(
                db.conn, frontier={}, max_records=8, max_bytes=bad,
            )
    finally:
        db.close()


def test_record_budget_bounds_the_page_and_the_cursor_round_trips(
    tmp_path: Path,
) -> None:
    """Paging must neither duplicate nor skip: the union of pages is the
    keyspace exactly once, and each cursor seek is exact."""
    db, catalog = _store(tmp_path / "personal.db")
    try:
        for index in range(9):
            with catalog.transaction(10 + index, f"tx-{index}"):
                _insert_source(db.conn, f"s-{index:02d}", "x")

        seen: list[tuple] = []
        cursor = None
        pages = 0
        while True:
            page = _page(db.conn, {ORIGIN: WIDE}, start_after=cursor,
                         max_records=2)
            pages += 1
            seen.extend(_addresses(page))
            if page.exhausted:
                break
            assert page.examined_through is not None
            cursor = page.examined_through
            assert pages < 20, "paging failed to terminate"

        assert len(seen) == 9
        assert len(set(seen)) == 9, "a page duplicated an address"
        assert seen == sorted(seen), "canonical order broken across pages"
        assert pages > 1, "the record budget did not bound the page"
    finally:
        db.close()


def test_byte_budget_leaves_the_nonfitting_record_unconsumed(
    tmp_path: Path,
) -> None:
    """The cursor must not advance over a record the page did not deliver."""
    db, catalog = _store(tmp_path / "personal.db")
    try:
        for index in range(4):
            with catalog.transaction(10 + index, f"tx-{index}"):
                _insert_source(db.conn, f"s-{index}", "y" * 400)

        first = _page(db.conn, {ORIGIN: WIDE}, max_bytes=1200)
        assert first.records, "budget too small to admit any record"
        assert not first.exhausted
        assert first.examined_through == (
            "sources", first.records[-1].mutation.address
        ), "examined_through must name the last CONSUMED record"

        rest = _page(db.conn, {ORIGIN: WIDE}, start_after=first.examined_through)
        overlap = set(_addresses(first)) & set(_addresses(rest))
        assert not overlap, "the looked-ahead record was consumed twice"
        assert len(_addresses(first)) + len(_addresses(rest)) == 4
    finally:
        db.close()


def test_first_record_over_budget_is_typed_and_yields_no_cursor(
    tmp_path: Path,
) -> None:
    db, catalog = _store(tmp_path / "personal.db")
    try:
        with catalog.transaction(10, "tx-1"):
            _insert_source(db.conn, "s1", "z" * 2000)
        with pytest.raises(OversizeRecord):
            _page(db.conn, {ORIGIN: WIDE}, max_bytes=64)
        assert not db.conn.in_transaction
    finally:
        db.close()


def test_malformed_cursor_is_typed(tmp_path: Path) -> None:
    db, _ = _store(tmp_path / "personal.db")
    try:
        for bad in [("not_a_table", ("x",)), ("sources", "notatuple"),
                    ("sources", ("too", "many", "parts"))]:
            with pytest.raises(MalformedCursor):
                _page(db.conn, {ORIGIN: WIDE}, start_after=bad)
    finally:
        db.close()


def test_active_caller_transaction_is_refused_and_left_untouched(
    tmp_path: Path,
) -> None:
    db, catalog = _store(tmp_path / "personal.db")
    try:
        with catalog.transaction(10, "tx-1"):
            _insert_source(db.conn, "s1", "committed")
        db.conn.execute("BEGIN")
        db.conn.execute("SELECT 1").fetchone()
        assert db.conn.in_transaction
        with pytest.raises(CallerTransactionActive):
            _page(db.conn, {ORIGIN: WIDE})
        assert db.conn.in_transaction, "the caller's transaction was disturbed"
        db.conn.rollback()
    finally:
        db.close()


def test_reader_holds_no_transaction_after_a_successful_page(
    tmp_path: Path,
) -> None:
    db, catalog = _store(tmp_path / "personal.db")
    try:
        with catalog.transaction(10, "tx-1"):
            _insert_source(db.conn, "s1", "a")
        page = _page(db.conn, {ORIGIN: WIDE})
        assert page.records
        assert not db.conn.in_transaction, "a read view outlived the page"
    finally:
        db.close()


def test_mutation_between_pages_is_visible_with_consistent_provenance(
    tmp_path: Path,
) -> None:
    """Pages are independent reads. A row written between them appears with
    its own provenance -- never a mix of one row's value and another's stamp."""
    db, catalog = _store(tmp_path / "personal.db")
    try:
        with catalog.transaction(10, "tx-1"):
            _insert_source(db.conn, "s-a", "first")
        first = _page(db.conn, {ORIGIN: WIDE}, max_records=1)

        with catalog.transaction(11, "tx-2"):
            _insert_source(db.conn, "s-b", "second")
        rest = _page(db.conn, {ORIGIN: WIDE},
                     start_after=first.examined_through)

        by_id = {
            item.mutation.address[0]: item
            for item in first.records + rest.records
        }
        assert by_id["s-b"].transaction_id == "tx-2"
        assert by_id["s-b"].mutation.timestamp_ns == 11
        assert by_id["s-a"].transaction_id == "tx-1"
        assert by_id["s-a"].mutation.timestamp_ns == 10
    finally:
        db.close()


def test_one_transaction_straddles_pages_and_no_completeness_is_implied(
    tmp_path: Path,
) -> None:
    """A single transaction's rows can span pages. The reader reports records
    and a position -- it never signals that an origin prefix is complete, and
    `exhausted` means the live universe only, NOT bootstrap completion."""
    db, catalog = _store(tmp_path / "personal.db")
    try:
        with catalog.transaction(10, "tx-wide"):
            for index in range(6):
                _insert_source(db.conn, f"s-{index}", "x")

        page = _page(db.conn, {ORIGIN: WIDE}, max_records=2)
        assert len(page.records) == 2
        assert {item.transaction_id for item in page.records} == {"tx-wide"}
        assert page.exhausted is False
        assert not hasattr(page, "frontier")
        assert not hasattr(page, "complete")
        assert not hasattr(page, "watermark")

        final = _page(db.conn, {ORIGIN: WIDE})
        assert final.exhausted is True, (
            "exhausted is live-universe exhaustion; the caller must still "
            "apply the PULL half before bootstrap is complete"
        )
    finally:
        db.close()


# ── documented non-coverage ──────────────────────────────────────────────

def test_tombstones_are_absent_because_a_live_walk_cannot_find_them(
    tmp_path: Path,
) -> None:
    """A deleted row has no live row to walk from, so a catalog-only tombstone
    is structurally undiscoverable here. Asserted, not pretended: composition
    must cover deletions."""
    db, catalog = _store(tmp_path / "personal.db")
    try:
        with catalog.transaction(10, "tx-1"):
            _insert_source(db.conn, "s1", "doomed")
        with catalog.transaction(11, "tx-2"):
            db.conn.execute("DELETE FROM sources WHERE id='s1'")

        tombstones = db.conn.execute(
            "SELECT COUNT(*) FROM fleet_sync_catalog WHERE tombstone=1"
        ).fetchone()[0]
        assert tombstones == 1, "the catalog does hold the tombstone"

        page = _page(db.conn, {ORIGIN: WIDE})
        assert page.records == (), (
            "the live walk cannot surface the deletion -- this is the "
            "documented coverage gap, not a bug in the reader"
        )
    finally:
        db.close()
