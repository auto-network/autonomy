"""Served-acknowledgement floor: journal pruning is bounded and safe."""

from pathlib import Path

import pytest

from tools.graph.db import GraphDB
from tools.network.fleet_sync.catalog import MutationCatalog, WatermarkError


EPOCH = "epoch-1"
PEER = "b" * 64


def _insert_source(conn, identity: str, title: str) -> None:
    conn.execute(
        "INSERT INTO sources(id,type,title,metadata,created_at,ingested_at) "
        "VALUES(?,?,?,?,?,?)",
        (identity, "note", title, "{}", "2026-08-19T00:00:00Z",
         "2026-08-19T00:00:00Z"),
    )


def _pull_all(server: MutationCatalog, client: MutationCatalog,
              cursor: int) -> int:
    """Drain the server's retained journal into the client, like one pull."""
    while True:
        page = server.next_journal_transaction_ref(cursor)
        if page is None:
            return cursor
        cursor, items = page
        client.apply_remote_batch(items)


def test_ack_floor_bounds_journal_growth(tmp_path: Path) -> None:
    source = GraphDB(tmp_path / "source.db")
    target = GraphDB(tmp_path / "target.db")
    try:
        server = MutationCatalog(source.conn, "a" * 64)
        client = MutationCatalog(target.conn, "b" * 64)
        server.install()
        client.install()

        cursor = 0
        for round_index in range(5):
            for write in range(10):
                stamp = 1_000 + round_index * 100 + write
                with server.transaction(stamp, f"r{round_index}w{write:02d}"):
                    _insert_source(
                        source.conn,
                        f"s-{round_index}-{write}",
                        f"title-{round_index}-{write}",
                    )
            cursor = _pull_all(server, client, cursor)
            server.record_served_ack(PEER, EPOCH, cursor)
            journal_rows, _transaction_rows = server.prune_acknowledged(
                [PEER], EPOCH
            )
            assert journal_rows == 10
            retained = source.conn.execute(
                "SELECT COUNT(*) FROM fleet_sync_journal"
            ).fetchone()[0]
            assert retained == 0
            # Transactions whose writes still win stay pinned as winner
            # provenance; nothing else survives.
            pinned = source.conn.execute(
                "SELECT COUNT(*) FROM fleet_sync_transactions"
            ).fetchone()[0]
            assert pinned == (round_index + 1) * 10

        # Convergence was never compromised by pruning.
        assert target.conn.execute(
            "SELECT COUNT(*) FROM sources"
        ).fetchone()[0] == 50

        # Superseding every winner releases the old transactions: retained
        # rows track live addresses, not write history.
        with server.transaction(9_000, "rewrite-all"):
            source.conn.execute("UPDATE sources SET title='rewritten'")
        cursor = _pull_all(server, client, cursor)
        server.record_served_ack(PEER, EPOCH, cursor)
        server.prune_acknowledged([PEER], EPOCH)
        assert source.conn.execute(
            "SELECT COUNT(*) FROM fleet_sync_transactions"
        ).fetchone()[0] == 1
        assert source.conn.execute(
            "SELECT COUNT(*) FROM fleet_sync_journal"
        ).fetchone()[0] == 0

        # The client's newest breadcrumb still resolves after pruning, so
        # the next pull resumes rather than replaying from zero.
        breadcrumb = server.journal_breadcrumb(cursor)
        assert breadcrumb is not None
        assert server.journal_resume_ref([breadcrumb]) == cursor
    finally:
        source.close()
        target.close()


def test_floor_unavailable_prunes_nothing(tmp_path: Path) -> None:
    db = GraphDB(tmp_path / "personal.db")
    try:
        catalog = MutationCatalog(db.conn, "a" * 64)
        catalog.install()
        with catalog.transaction(1_000, "t1"):
            _insert_source(db.conn, "s1", "one")

        # Empty roster: a solo machine never retires frames.
        assert catalog.prune_acknowledged([], EPOCH) == (0, 0)
        # A peer with no acknowledgement this epoch blocks the floor.
        assert catalog.prune_acknowledged([PEER], EPOCH) == (0, 0)
        # A partial roster acknowledgement still blocks the floor.
        catalog.record_served_ack(PEER, EPOCH, 1)
        assert catalog.prune_acknowledged([PEER, "c" * 64], EPOCH) == (0, 0)
        assert db.conn.execute(
            "SELECT COUNT(*) FROM fleet_sync_journal"
        ).fetchone()[0] == 1
    finally:
        db.close()


def test_floor_is_min_over_peers_and_acks_are_monotonic(
    tmp_path: Path,
) -> None:
    db = GraphDB(tmp_path / "personal.db")
    try:
        catalog = MutationCatalog(db.conn, "a" * 64)
        catalog.install()
        for index in range(3):
            with catalog.transaction(1_000 + index, f"t{index}"):
                _insert_source(db.conn, f"s{index}", f"title-{index}")

        slow = "c" * 64
        catalog.record_served_ack(PEER, EPOCH, 3)
        catalog.record_served_ack(slow, EPOCH, 2)
        # A stale, lower acknowledgement never regresses the recorded one.
        catalog.record_served_ack(PEER, EPOCH, 1)
        assert catalog.acknowledged_journal_floor([PEER, slow], EPOCH) == 2

        journal_rows, _transactions = catalog.prune_acknowledged(
            [PEER, slow], EPOCH
        )
        assert journal_rows == 2
        remaining = db.conn.execute(
            "SELECT transaction_ref FROM fleet_sync_journal"
        ).fetchall()
        assert [int(row[0]) for row in remaining] == [3]
    finally:
        db.close()


def test_prune_drops_other_epoch_peer_state(tmp_path: Path) -> None:
    db = GraphDB(tmp_path / "personal.db")
    try:
        catalog = MutationCatalog(db.conn, "a" * 64)
        catalog.install()
        with catalog.transaction(1_000, "t1"):
            _insert_source(db.conn, "s1", "one")
        catalog.record_served_ack(PEER, "epoch-0", 1)
        catalog.record_served_ack(PEER, EPOCH, 1)
        catalog.prune_acknowledged([PEER], EPOCH)
        epochs = db.conn.execute(
            "SELECT DISTINCT roster_epoch FROM fleet_sync_peer_state"
        ).fetchall()
        assert [row[0] for row in epochs] == [EPOCH]
    finally:
        db.close()


def test_malformed_ack_is_refused(tmp_path: Path) -> None:
    db = GraphDB(tmp_path / "personal.db")
    try:
        catalog = MutationCatalog(db.conn, "a" * 64)
        catalog.install()
        for bad in (0, -1, True, "7"):
            with pytest.raises(WatermarkError):
                catalog.record_served_ack(PEER, EPOCH, bad)
    finally:
        db.close()
