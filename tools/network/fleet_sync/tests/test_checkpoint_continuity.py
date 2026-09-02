"""Checkpoint-vs-delta by breadcrumb continuity, not roster epoch."""

import sqlite3
import time
from pathlib import Path

from tools.graph.db import GraphDB
from tools.network.fleet_sync.catalog import MutationCatalog, journal_has_gap
from tools.network import fleet_relay_sync


def _insert_source(conn, identity: str, title: str) -> None:
    conn.execute(
        "INSERT INTO sources(id,type,title,metadata,created_at,ingested_at) "
        "VALUES(?,?,?,?,?,?)",
        (identity, "note", title, "{}", "2026-08-19T00:00:00Z",
         "2026-08-19T00:00:00Z"),
    )


def test_serve_checkpoint_decision_truth_table() -> None:
    decide = fleet_relay_sync._serve_checkpoint_decision
    # A resolvable trail always means deltas, whatever was requested.
    assert decide(7, True, True) is False
    assert decide(7, False, True) is False
    # An unresolvable trail checkpoints when requested...
    assert decide(0, True, False) is True
    # ...or when replay would omit retired history.
    assert decide(0, False, True) is True
    # Fresh server, established-looking peer with no trail: bounded replay.
    assert decide(0, False, False) is False


def test_journal_gap_appears_only_after_pruning(tmp_path: Path) -> None:
    db = GraphDB(tmp_path / "personal.db")
    try:
        catalog = MutationCatalog(db.conn, "a" * 64)
        catalog.install()
        for index in range(3):
            with catalog.transaction(1_000 + index, f"t{index}"):
                _insert_source(db.conn, f"s{index}", f"title-{index}")
        assert journal_has_gap(db.conn) is False
        catalog.record_served_ack("b" * 64, "epoch-1", 2)
        catalog.prune_acknowledged(["b" * 64], "epoch-1")
        assert journal_has_gap(db.conn) is True
    finally:
        db.close()


def test_local_sync_state_survives_epoch_changes(
    tmp_path: Path, monkeypatch,
) -> None:
    db_path = tmp_path / "personal.db"
    monkeypatch.setattr(
        fleet_relay_sync, "_org_db_path", lambda _slug: db_path
    )

    # No database at all: no state, so a checkpoint is requested.
    assert fleet_relay_sync._has_local_sync_state("m" * 64, "r" * 64) is False

    db = GraphDB(db_path)
    try:
        catalog = MutationCatalog(db.conn, "a" * 64)
        catalog.install()
        # Prepared but empty: still no state.
        assert fleet_relay_sync._has_local_sync_state(
            "m" * 64, "r" * 64
        ) is False

        # A checkpoint receipt recorded under an OLD roster epoch is still
        # state: enrollment or kick must not force a re-checkpoint.
        db.conn.execute(
            "INSERT INTO fleet_sync_peer_state("
            "machine_public_key,roster_epoch,online,checkpoints_received,"
            "updated_at_ns) VALUES(?,?,1,1,?)",
            ("m" * 64, "epoch-that-no-longer-exists", time.time_ns()),
        )
        db.conn.commit()
        assert fleet_relay_sync._has_local_sync_state(
            "m" * 64, "r" * 64
        ) is True

        # Applied/authored transactions alone are also state, receipt or not.
        db.conn.execute("DELETE FROM fleet_sync_peer_state")
        db.conn.commit()
        with catalog.transaction(1_000, "t0"):
            _insert_source(db.conn, "s0", "grown from deltas")
        assert fleet_relay_sync._has_local_sync_state(
            "m" * 64, "r" * 64
        ) is True
    finally:
        db.close()


def test_checkpoint_receiver_journal_counts_as_gap(tmp_path: Path) -> None:
    """A fresh checkpoint receiver has winner transactions with no journal
    rows; serving an unknown trail from it must checkpoint, not replay."""
    db = GraphDB(tmp_path / "personal.db")
    try:
        catalog = MutationCatalog(db.conn, "a" * 64)
        catalog.install()
        with catalog.transaction(1_000, "t0"):
            _insert_source(db.conn, "s0", "one")
        # Simulate the install shape: journal retired, winner rows pinned.
        db.conn.execute("DELETE FROM fleet_sync_journal")
        db.conn.commit()
        assert journal_has_gap(db.conn) is True
    finally:
        db.close()
