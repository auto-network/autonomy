"""Tier-used readout (auto-wryex): every pull records WHICH path carried it."""

from __future__ import annotations

import pytest

from tools.graph.db import GraphDB
from tools.network import fleet_direct_config as fdc
from tools.network import fleet_sync_telemetry as tel


@pytest.fixture
def machine(tmp_path, monkeypatch):
    orgs = tmp_path / "orgs"
    orgs.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    GraphDB.close_all_pooled()
    GraphDB.create_org_db("personal", type_="personal").close()
    GraphDB.create_org_db("machine", type_="personal", path=orgs.parent / "machine.db").close()
    yield tmp_path
    GraphDB.close_all_pooled()


@pytest.mark.parametrize("addr,expected", [
    ("ws://100.122.70.30:9410", "tailnet"),
    ("ws://192.168.1.20:9410", "private"),
    ("ws://172.16.0.2:9410", "private"),
    ("ws://8.8.8.8:9410", "public"),
    ("ws://127.0.0.1:9410", "loopback"),
    ("wss://relay.auto.network/l/abc", "named"),
    ("100.89.80.126", "tailnet"),
    ("", None), (None, None), ("ws://[not-a-host", None),
])
def test_path_class(addr, expected):
    assert fdc.path_class(addr) == expected


def test_telemetry_records_address_and_accumulates_bytes_by_path(machine):
    peer = "ab" * 32
    common = dict(channel="direct", direction="pull", mode="delta",
                  started_at_ns=1, duration_ms=10)
    tel.record_iteration(peer, outcome="failed", error_code="x",
                         address="ws://100.1.2.3:9410", path_class="tailnet",
                         bytes_received=500, **common)
    tel.record_iteration(peer, outcome="success",
                         address="ws://100.1.2.3:9410", path_class="tailnet",
                         bytes_sent=100, bytes_received=900, **common)
    tel.record_iteration(peer, outcome="success",
                         address="ws://203.0.113.7:9410", path_class="public",
                         bytes_received=50, **common)
    rows = {f"{r['channel']}/{r['direction']}": r["payload"] for r in tel.read_channel_rows()}
    payload = rows["direct/pull"]
    assert payload["last_path_class"] == "public"
    assert payload["last_address"] == "ws://203.0.113.7:9410"
    assert payload["last_success_path_class"] == "public"
    # a failed attempt names its path but does not add to the per-class bytes
    assert payload["bytes_by_path_class"] == {"tailnet": 1000, "public": 50}

    # relay rows carry the relay host, never a token
    tel.record_iteration(peer, outcome="success", address="https://relay.example",
                         path_class="relay", bytes_received=7,
                         channel="relay", direction="pull", mode="delta",
                         started_at_ns=1, duration_ms=1)
    relay = {f"{r['channel']}": r["payload"] for r in tel.read_channel_rows()}["relay"]
    assert relay["bytes_by_path_class"] == {"relay": 7}
    assert "token" not in relay["last_address"]


def test_verdict_paths_section_reads_the_rows(machine):
    from tools.network.fleet_verdict import _paths_check

    peer = "cd" * 32
    tel.record_iteration(peer, outcome="success", address="ws://100.9.9.9:9410",
                         path_class="tailnet", bytes_received=10,
                         channel="direct", direction="pull", mode="delta",
                         started_at_ns=1, duration_ms=1)
    paths = _paths_check()
    entry = paths[peer[:12]]["direct/pull/personal"]
    assert entry["last_success_path_class"] == "tailnet"
    assert entry["bytes_by_path_class"] == {"tailnet": 10}
