"""The direct tier's machine-local configuration row."""

from __future__ import annotations

import pytest

from tools.graph.db import GraphDB
from tools.graph.schemas.fleet_direct import FleetDirectV1
from tools.graph.schemas.registry import SchemaValidationError
from tools.network import fleet_direct_config as fdc


@pytest.fixture
def machine(tmp_path, monkeypatch):
    orgs = tmp_path / "orgs"
    orgs.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    monkeypatch.delenv(fdc.ADVERTISE_ENV, raising=False)
    GraphDB.close_all_pooled()
    GraphDB.create_org_db("personal", type_="personal").close()
    GraphDB.create_org_db(
        "machine", type_="personal", path=orgs.parent / "machine.db"
    ).close()
    yield tmp_path
    GraphDB.close_all_pooled()


def test_defaults_are_the_historical_loopback_ephemeral_listener(machine):
    cfg = fdc.load()
    assert cfg == fdc.FleetDirectConfig("127.0.0.1", 0, ())
    assert cfg.enabled is False


def test_store_then_load_round_trips_and_enables_the_tier(machine):
    fdc.store(fdc.FleetDirectConfig(
        "0.0.0.0", 9410, ("wss://home.example:9410",)
    ))
    cfg = fdc.load()
    assert cfg.listen_host == "0.0.0.0"
    assert cfg.listen_port == 9410
    assert cfg.advertise_addrs == ("wss://home.example:9410",)
    assert cfg.enabled is True


def test_env_addresses_are_unioned_not_replaced(machine, monkeypatch):
    fdc.store(fdc.FleetDirectConfig("0.0.0.0", 9410, ("wss://a:1",)))
    monkeypatch.setenv(fdc.ADVERTISE_ENV, "wss://a:1, ws://b:2")
    assert fdc.load().advertise_addrs == ("wss://a:1", "ws://b:2")
    assert fdc.advertise_addrs() == ["wss://a:1", "ws://b:2"]


def test_a_fixed_port_on_loopback_is_not_enabled(machine):
    fdc.store(fdc.FleetDirectConfig("127.0.0.1", 9410, ()))
    assert fdc.load().enabled is False


@pytest.mark.parametrize("payload", [
    {"listen_port": 70000},
    {"listen_port": "9410"},
    {"listen_host": ""},
    {"advertise_addrs": ["https://not-a-socket"]},
    {"advertise_addrs": ["ws://x:1"] * 9},
])
def test_schema_rejects_malformed_rows(payload):
    with pytest.raises(SchemaValidationError):
        FleetDirectV1.validate(payload)
