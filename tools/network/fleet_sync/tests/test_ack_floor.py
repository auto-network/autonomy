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
    """Serve the client everything above its per-origin watermarks, like
    one pull; returns the newest server transaction row id served."""
    held = client.origin_watermarks()
    for origin in server.origin_list():
        position: tuple[int, str | None] = (held.get(origin, 0), None)
        while True:
            page = server.next_transactions_for_origin(
                origin, position[0], position[1], limit=50,
            )
            if not page:
                break
            for ref, timestamp, transaction_id, items in page:
                client.apply_remote_batch(items)
                cursor = max(cursor, ref)
                position = (timestamp, transaction_id)
    return cursor


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
            assert journal_rows == 0  # nothing but rows is stored now
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
            "SELECT COUNT(*) FROM fleet_sync_transactions"
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

        # Every transaction is still cited by a live row: nothing retires.
        assert catalog.prune_acknowledged([PEER, slow], EPOCH) == (0, 0)
        remaining = db.conn.execute(
            "SELECT id FROM fleet_sync_transactions ORDER BY id"
        ).fetchall()
        assert [int(row[0]) for row in remaining] == [1, 2, 3]
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


def _write_history(catalog: MutationCatalog, conn) -> None:
    """20 inserts (their transactions stay cited by current winners) plus
    40 updates of one row (each supersedes the previous winner, so those
    transactions become retirable)."""
    for i in range(20):
        with catalog.transaction(1_000 + i, f"ins{i:02d}"):
            _insert_source(conn, f"s-{i}", f"t-{i}")
    for i in range(40):
        with catalog.transaction(2_000 + i, f"upd{i:02d}"):
            conn.execute("UPDATE sources SET title=? WHERE id='s-0'", (f"v{i}",))


def _remaining(conn) -> tuple[list, int]:
    ids = [int(r[0]) for r in conn.execute(
        "SELECT id FROM fleet_sync_transactions ORDER BY id")]
    return ids, 0


def test_incremental_prune_converges_to_the_one_shot_result(tmp_path: Path, monkeypatch) -> None:
    """Small budget + small batches, called repeatedly, must retire exactly
    what one unbounded prune retires — nothing more, nothing less — while
    each call stays short (the live-DB lock-hold fix, 2026-09-06)."""
    import tools.network.fleet_sync.catalog as catalog_module
    monkeypatch.setattr(catalog_module, "PRUNE_YIELD_S", 0.0)
    dbs = {}
    for name in ("oneshot", "incremental"):
        graph = GraphDB(tmp_path / f"{name}.db")
        client = GraphDB(tmp_path / f"{name}-client.db")
        server = MutationCatalog(graph.conn, "a" * 64)
        peer = MutationCatalog(client.conn, "b" * 64)
        server.install(); peer.install()
        _write_history(server, graph.conn)
        cursor = _pull_all(server, peer, 0)
        server.record_served_ack(PEER, EPOCH, cursor)
        dbs[name] = (graph, client, server)
    try:
        one_graph, _, one_server = dbs["oneshot"]
        one_journal, one_tx = one_server.prune_acknowledged([PEER], EPOCH)
        # 40 updates of one row: the row's insert and 39 superseded updates
        # retire; the newest update stays cited by the row (and is the
        # origin's frontier). Nothing else is stored per transaction.
        assert (one_journal, one_tx) == (0, 40)

        inc_graph, _, inc_server = dbs["incremental"]
        totals = [0, 0]
        calls = 0
        # A budget that admits exactly one batch per call: the first
        # within_budget() check passes, the post-batch yield ends it.
        while True:
            j, t = inc_server.prune_acknowledged(
                [PEER], EPOCH, budget_s=1e-9, batch=7,
            )
            calls += 1
            totals[0] += j; totals[1] += t
            # A pass sweeps one window and remembers where it stopped; a
            # window with nothing retirable is not the end. The sweep is
            # complete when the cursor has wrapped to 0.
            if inc_server.prune_cursor() == 0 or calls > 200:
                break
        assert calls > 3, "the budget must have split the work across calls"
        assert tuple(totals) == (one_journal, one_tx)
        assert _remaining(inc_graph.conn) == _remaining(one_graph.conn)
    finally:
        for graph, client, _ in dbs.values():
            graph.close(); client.close()


def test_prune_transaction_check_probes_an_index_not_the_catalog(tmp_path: Path) -> None:
    graph = GraphDB(tmp_path / "plan.db")
    try:
        MutationCatalog(graph.conn, "a" * 64).install()
        plan = " | ".join(str(tuple(r)) for r in graph.conn.execute(
            "EXPLAIN QUERY PLAN SELECT MIN(id) FROM fleet_sync_transactions "
            "WHERE id>=? AND id<? "
            "AND NOT EXISTS(SELECT 1 FROM fleet_sync_catalog c "
            "WHERE c.transaction_ref=fleet_sync_transactions.id)", (0, 10)))
        assert "SCAN c" not in plan and "SCAN fleet_sync_catalog" not in plan, plan
        assert "idx_fleet_sync_catalog_transaction_ref" in plan, plan
    finally:
        graph.close()
