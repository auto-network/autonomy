"""The relay loop defers to a fresh direct path; bytes stay visible per channel."""

from __future__ import annotations

import time

import pytest

from tools.graph.db import GraphDB, _org_db_path
from tools.network import fleet_sync_telemetry
from tools.network.fleet_relay_sync import DIRECT_FRESHNESS_WINDOW_S
from tools.network.idkit import KeyPair

PEER = "bb" * 32


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


def _record(channel: str, outcome: str, **extra) -> None:
    fleet_sync_telemetry.record_iteration(
        PEER,
        channel=channel,
        direction="pull",
        mode="delta",
        outcome=outcome,
        started_at_ns=time.time_ns(),
        duration_ms=5,
        **extra,
    )


def test_direct_freshness_gates_the_relay(local_stores) -> None:
    fresh = fleet_sync_telemetry.direct_pull_fresh

    # No telemetry at all: relay proceeds.
    assert fresh(PEER, window_s=DIRECT_FRESHNESS_WINDOW_S) is False
    # A relay success is not direct evidence.
    _record("relay", "success")
    assert fresh(PEER, window_s=DIRECT_FRESHNESS_WINDOW_S) is False
    # A fresh direct success defers the relay.
    _record("direct", "success")
    assert fresh(PEER, window_s=DIRECT_FRESHNESS_WINDOW_S) is True
    # A direct failure ends the deferral immediately.
    _record("direct", "failed", error_code="ConnectionError")
    assert fresh(PEER, window_s=DIRECT_FRESHNESS_WINDOW_S) is False
    # Recovery re-establishes it; a zero window treats it as already stale.
    _record("direct", "success")
    assert fresh(PEER, window_s=DIRECT_FRESHNESS_WINDOW_S) is True
    assert fresh(PEER, window_s=0.0) is False
    # Scoped telemetry is independent: an org-scope success says nothing
    # about the personal scope's freshness and vice versa.
    assert fresh(PEER, window_s=DIRECT_FRESHNESS_WINDOW_S, scope="alpha") is False
    _record("direct", "success", scope="alpha")
    assert fresh(PEER, window_s=DIRECT_FRESHNESS_WINDOW_S, scope="alpha") is True


def test_channel_rows_expose_per_channel_bytes(local_stores) -> None:
    _record("direct", "success", bytes_sent=100, bytes_received=2_000)
    _record("relay", "success", bytes_sent=50, bytes_received=10)
    _record("direct", "success", bytes_sent=100, bytes_received=3_000,
            scope="alpha")

    rows = fleet_sync_telemetry.read_channel_rows()
    pulls = {
        (row["channel"], row["scope"]): row["payload"]
        for row in rows if row["direction"] == "pull"
    }
    assert pulls[("direct", "personal")]["total_bytes_received"] == 2_000
    assert pulls[("relay", "personal")]["total_bytes_received"] == 10
    assert pulls[("direct", "alpha")]["total_bytes_received"] == 3_000
    # Aggregated totals absorb scoped rows instead of dropping them.
    totals = fleet_sync_telemetry.read_peer_totals()
    assert totals[PEER]["bytes_received"] == 5_010
