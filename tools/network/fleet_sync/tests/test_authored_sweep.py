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
from tools.network.fleet_sync.codec import encode_value

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
    """A live row with no catalog provenance fails the ENTIRE page, including
    the valid prefix already accumulated.

    The store is fully tracked and then exactly one later canonical catalog
    address is removed, so the untracked row is reached after a valid record
    has been collected. Nothing here can skip, and the assertion is about the
    SELECTED address rather than the catalog being globally empty.
    """
    db, catalog = _store(tmp_path / "personal.db")
    try:
        with catalog.transaction(10, "tx-1"):
            _insert_source(db.conn, "s-00", "tracked, emitted first")
            _insert_source(db.conn, "s-01", "provenance about to be removed")

        target = encode_value(["sources", ["s-01"]])
        removed = db.conn.execute(
            "DELETE FROM fleet_sync_catalog WHERE address=?", (target,)
        ).rowcount
        db.conn.commit()
        assert removed == 1, "the specific address was not the one removed"
        assert db.conn.execute(
            "SELECT COUNT(*) FROM fleet_sync_catalog WHERE address=?", (target,)
        ).fetchone()[0] == 0
        assert db.conn.execute(
            "SELECT COUNT(*) FROM fleet_sync_catalog"
        ).fetchone()[0] > 0, "the rest of the catalog must remain tracked"

        with pytest.raises(UntrackedLiveRow, match="s-01"):
            _page(db.conn, {ORIGIN: WIDE})
        assert not db.conn.in_transaction, "read view must close on error"
    finally:
        db.close()


def test_schema_prevents_a_broken_provenance_join(tmp_path: Path) -> None:
    """The reader is not the only thing standing between a catalog row and an
    unresolvable transaction -- the foreign key is, and it is the stronger
    guarantee. Attempting to orphan ``transaction_ref`` is refused outright.
    """
    db, catalog = _store(tmp_path / "personal.db")
    try:
        with catalog.transaction(10, "tx-1"):
            _insert_source(db.conn, "s-00", "tracked")

        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            db.conn.execute(
                "UPDATE fleet_sync_catalog SET transaction_ref="
                "(SELECT COALESCE(MAX(id),0)+1000 FROM fleet_sync_transactions)"
            )
        db.conn.rollback()
    finally:
        db.close()


def test_unresolvable_provenance_still_fails_whole_page_without_fk_enforcement(
    tmp_path: Path,
) -> None:
    """Defence in depth. SQLite enforces foreign keys only when the pragma is
    on, so a store opened without it could hold a catalog row whose transaction
    does not resolve. The reader must still refuse the whole page rather than
    emit a record with no honest origin.
    """
    db, catalog = _store(tmp_path / "personal.db")
    try:
        with catalog.transaction(10, "tx-1"):
            _insert_source(db.conn, "s-00", "tracked, emitted first")
            _insert_source(db.conn, "s-01", "provenance about to dangle")

        target = encode_value(["sources", ["s-01"]])
        db.conn.execute("PRAGMA foreign_keys=off")
        try:
            changed = db.conn.execute(
                "UPDATE fleet_sync_catalog SET transaction_ref="
                "(SELECT COALESCE(MAX(id),0)+1000 FROM fleet_sync_transactions) "
                "WHERE address=?",
                (target,),
            ).rowcount
            db.conn.commit()
        finally:
            db.conn.execute("PRAGMA foreign_keys=on")
        assert changed == 1, "the specific address was not the one dangled"

        with pytest.raises(UntrackedLiveRow, match="s-01"):
            _page(db.conn, {ORIGIN: WIDE})
        assert not db.conn.in_transaction
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


def test_all_filtered_store_still_pages_and_makes_progress(
    tmp_path: Path,
) -> None:
    """Filtered rows emit nothing, so `max_records` alone never trips on a
    store whose keyspace is entirely newer than F -- it would be walked end to
    end under one read view. `max_examined` bounds rows READ, and an
    all-filtered page still returns a position so the caller advances."""
    db, catalog = _store(tmp_path / "personal.db")
    try:
        for index in range(9):
            with catalog.transaction(100 + index, f"tx-{index}"):
                _insert_source(db.conn, f"s-{index:02d}", "newer than F")

        seen = 0
        pages = 0
        cursor = None
        while True:
            page = _page(db.conn, {ORIGIN: 5}, start_after=cursor,
                         max_records=100, max_examined=2)
            pages += 1
            seen += page.examined
            assert page.records == (), "every row here is PULL's"
            if page.exhausted:
                break
            assert page.examined_through is not None, (
                "an all-filtered page must still report a position"
            )
            cursor = page.examined_through
            assert pages < 20, "all-filtered paging failed to terminate"

        assert seen == 9
        assert pages > 1, "max_examined did not bound the scan"
    finally:
        db.close()


def test_unsigned_settings_cursor_round_trips(tmp_path: Path) -> None:
    """An unsigned settings row's logical address carries no persona, so a
    cursor derived from it is one part short of the key arity. That shape must
    be accepted, not rejected before the reader pads it."""
    db, catalog = _store(tmp_path / "personal.db")
    try:
        with catalog.transaction(10, "tx-1"):
            db.conn.execute(
                "INSERT INTO settings(id,set_id,schema_revision,key,payload,"
                "publication_state,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                ("one", "example", 1, "k1", '{"v":1}', "raw",
                 "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
            )
        page = _page(db.conn, {ORIGIN: WIDE})
        settings = [
            item for item in page.records if item.mutation.table == "settings"
        ]
        assert settings, "the settings row must be swept"
        full = settings[0].mutation.address
        short = full[:-1] if full[-1] == "" else full
        resumed = _page(db.conn, {ORIGIN: WIDE},
                        start_after=("settings", tuple(short)))
        assert all(
            item.mutation.address != full
            for item in resumed.records
        ), "the resumed page re-delivered the cursor row"
    finally:
        db.close()
