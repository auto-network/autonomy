"""Fleet performance telemetry stays machine-local and cumulative."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from tools.graph.db import GraphDB, _org_db_path
from tools.graph.schemas.fleet_sync_telemetry import (
    FLEET_SYNC_TELEMETRY_SET_ID,
)
from tools.network import fleet_sync_telemetry
from tools.network.idkit import KeyPair


@pytest.fixture
def local_stores(tmp_path, monkeypatch):
    orgs = tmp_path / "orgs"
    orgs.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    GraphDB.close_all_pooled()
    personal = GraphDB.create_org_db("personal", type_="personal")
    personal.activate_fleet_sync_writers(KeyPair.generate().public_hex)
    personal.close()
    yield _org_db_path("personal"), _org_db_path("machine")
    GraphDB.close_all_pooled()


def _journal_rows(path) -> int:
    """Authored work on the personal store: catalog rows (the journal is gone)."""
    with sqlite3.connect(path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM fleet_sync_catalog").fetchone()[0])


def test_recording_sync_telemetry_never_authors_personal_fleet_work(local_stores):
    personal, machine = local_stores
    peer = "bb" * 32
    before = _journal_rows(personal)
    assert fleet_sync_telemetry.read_acknowledged_transaction_ref(peer) == 0

    first = fleet_sync_telemetry.record_iteration(
        peer,
        channel="relay",
        direction="pull",
        mode="delta",
        outcome="success",
        started_at_ns=100,
        duration_ms=410_000,
        bytes_sent=512,
        bytes_received=61_460_671,
        mutation_frames=347_760,
        transactions=64_579,
        acknowledged_transaction_ref=64_579,
    )
    second = fleet_sync_telemetry.record_iteration(
        peer,
        channel="relay",
        direction="pull",
        mode="delta",
        outcome="failed",
        started_at_ns=200,
        duration_ms=1_250,
        bytes_sent=256,
        bytes_received=4096,
        mutation_frames=12,
        transactions=2,
        error_code="relay_close_1000",
        acknowledged_transaction_ref=99_999,
    )

    assert _journal_rows(personal) == before
    assert machine.exists()
    assert first["iterations"] == 1
    assert second["iterations"] == 2
    assert second["successful_iterations"] == 1
    assert second["failed_iterations"] == 1
    assert second["total_duration_ms"] == 411_250
    assert second["total_bytes_received"] == 61_464_767
    assert second["total_mutation_frames"] == 347_772
    assert second["acknowledged_transaction_ref"] == 64_579
    assert second["last_success_at_ns"] == first["last_success_at_ns"]

    totals = fleet_sync_telemetry.read_peer_totals()
    assert totals[peer]["iterations"] == 2
    assert totals[peer]["bytes_received"] == 61_464_767
    assert fleet_sync_telemetry.read_acknowledged_transaction_ref(peer) == 64_579

    with sqlite3.connect(machine) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM settings WHERE set_id=?",
            (FLEET_SYNC_TELEMETRY_SET_ID,),
        ).fetchone()
    assert row == (1,)


def test_breadcrumb_trail_replaces_position_and_thins_exponentially(local_stores):
    """A verified stream identity REPLACES the acknowledged position (a
    restored source legitimately reports a smaller one), and the stored
    trail keeps the recent positions contiguously plus an exponentially
    thinned history, bounded regardless of how many pulls ever happened."""
    _personal, _machine = local_stores
    peer = "cc" * 32

    def ack(sequence: int, ref: int):
        return fleet_sync_telemetry.record_iteration(
            peer,
            channel="direct",
            direction="pull",
            mode="delta",
            outcome="success",
            started_at_ns=sequence,
            duration_ms=1,
            acknowledged_transaction_ref=ref,
            acknowledged_breadcrumb={
                "origin": "aa" * 32,
                "transaction": f"local:{sequence:08d}",
                "timestamp": sequence,
            },
        )

    payload = None
    for sequence in range(1, 101):
        payload = ack(sequence, ref=sequence * 10)
    assert payload["acknowledged_transaction_ref"] == 1000

    trail = fleet_sync_telemetry.read_resume_breadcrumbs(peer)
    assert trail[0] == ("aa" * 32, "local:00000100", 100)
    # Newest eight contiguous; older survivors one per power-of-two age
    # bucket; the very first acknowledged position persists forever.
    recent = [identity[2] for identity in trail[:8]]
    assert recent == [100, 99, 98, 97, 96, 95, 94, 93]
    older_ages = [100 - identity[2] for identity in trail[8:]]
    assert all(age >= 8 for age in older_ages)
    assert len({age.bit_length() for age in older_ages}) == len(older_ages)
    assert ("aa" * 32, "local:00000001", 1) in trail
    assert len(trail) <= fleet_sync_telemetry.MAX_RESUME_BREADCRUMBS

    # A post-restore acknowledgement moves the position BACKWARDS honestly.
    smaller = ack(101, ref=40)
    assert smaller["acknowledged_transaction_ref"] == 40
    assert fleet_sync_telemetry.read_resume_breadcrumbs(peer)[0][2] == 101


def test_peer_totals_keep_the_transport_breakdown(tmp_path, monkeypatch):
    """THE ONE THAT MATTERS for "direct or relay?".

    `read_peer_totals` parses `channel` out of the key and used to discard it,
    summing every transport into one bucket per peer. So a caller could see
    that a peer moved 30 GB and never whether it went over the direct listener
    or the relay — a distinction the writer records and the key encodes. On
    2026-09-09 answering that question needed a raw settings read.
    """
    import tools.network.fleet_sync_telemetry as tel

    peer = "aa" * 32
    rows = [
        SimpleNamespace(
            key=f"direct:pull:{peer}:personal",
            payload={"total_bytes_sent": 10, "total_bytes_received": 100,
                     "iterations": 2, "total_transactions": 5},
        ),
        SimpleNamespace(
            key=f"relay:pull:{peer}:personal",
            payload={"total_bytes_sent": 1, "total_bytes_received": 7,
                     "iterations": 1, "total_transactions": 2},
        ),
    ]
    monkeypatch.setattr(
        tel.settings_ops, "read_owned_set",
        lambda *a, **k: SimpleNamespace(members=rows))

    totals = tel.read_peer_totals()[peer]

    # The aggregate is unchanged — existing callers keep working.
    assert totals["bytes_received"] == 107
    # And the split is now available.
    assert totals["by_transport"]["direct"]["bytes_received"] == 100
    assert totals["by_transport"]["relay"]["bytes_received"] == 7
    assert totals["by_transport"]["direct"]["transactions"] == 5


def test_a_single_transport_still_reports_only_itself(tmp_path, monkeypatch):
    """NEGATIVE CONTROL: a peer reached only one way must not appear to have
    used both. An empty bucket for the unused transport would read as "we
    tried direct and moved nothing", which is a different claim."""
    import tools.network.fleet_sync_telemetry as tel

    peer = "bb" * 32
    monkeypatch.setattr(
        tel.settings_ops, "read_owned_set",
        lambda *a, **k: SimpleNamespace(members=[SimpleNamespace(
            key=f"relay:serve:{peer}:personal",
            payload={"total_bytes_sent": 9, "total_bytes_received": 0,
                     "iterations": 1, "total_transactions": 1},
        )]))

    totals = tel.read_peer_totals()[peer]

    assert set(totals["by_transport"]) == {"relay"}
