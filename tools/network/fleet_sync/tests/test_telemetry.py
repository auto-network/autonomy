"""Fleet performance telemetry stays machine-local and cumulative."""

from __future__ import annotations

import sqlite3

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
    with sqlite3.connect(path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM fleet_sync_journal").fetchone()[0])


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
