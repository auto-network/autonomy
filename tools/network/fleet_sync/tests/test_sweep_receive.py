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
