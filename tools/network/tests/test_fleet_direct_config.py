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


NO_IFACES = lambda: []  # noqa: E731 -- detection stub: nothing detected


def test_defaults_are_the_historical_loopback_ephemeral_listener(machine):
    cfg = fdc.load(detect=NO_IFACES)
    assert cfg == fdc.FleetDirectConfig("127.0.0.1", 0, (), True)
    assert cfg.enabled is False


def test_store_then_load_round_trips_and_enables_the_tier(machine):
    fdc.store(fdc.FleetDirectConfig(
        "0.0.0.0", 9410, ("wss://home.example:9410",), advertise_auto=False,
    ))
    cfg = fdc.load(detect=NO_IFACES)
    assert cfg.listen_host == "0.0.0.0"
    assert cfg.listen_port == 9410
    assert cfg.advertise_addrs == ("wss://home.example:9410",)
    assert cfg.advertise_auto is False
    assert cfg.enabled is True


def test_env_addresses_are_unioned_not_replaced(machine, monkeypatch):
    fdc.store(fdc.FleetDirectConfig("0.0.0.0", 9410, ("wss://a:1",), False))
    monkeypatch.setenv(fdc.ADVERTISE_ENV, "wss://a:1, ws://b:2")
    assert fdc.load(detect=NO_IFACES).advertise_addrs == ("wss://a:1", "ws://b:2")


def test_auto_candidates_are_every_usable_interface_tailnet_first(machine):
    """A multi-homed machine offers all its interfaces; the peer tries them
    in order. Tailnet first, then private LAN, then public; loopback,
    link-local, duplicates, and garbage dropped; configured URLs lead."""
    detected = lambda: [  # noqa: E731
        "192.168.1.20", "127.0.0.1", "100.101.102.103", "169.254.9.9",
        "203.0.113.7", "192.168.1.20", "not-an-ip", "::1", "0.0.0.0",
    ]
    assert fdc.candidate_addresses(9410, detect=detected) == [
        "ws://100.101.102.103:9410",
        "ws://192.168.1.20:9410",
        "ws://203.0.113.7:9410",
    ]
    assert fdc.candidate_addresses(0, detect=detected) == []

    fdc.store(fdc.FleetDirectConfig("0.0.0.0", 9410, ("ws://sjc.tail.example:9410",)))
    cfg = fdc.load(detect=detected)
    assert cfg.advertise_addrs == (
        "ws://sjc.tail.example:9410",
        "ws://100.101.102.103:9410",
        "ws://192.168.1.20:9410",
        "ws://203.0.113.7:9410",
    )


def test_auto_candidates_need_a_dialable_bind(machine):
    detected = lambda: ["100.101.102.103"]  # noqa: E731
    fdc.store(fdc.FleetDirectConfig("127.0.0.1", 9410, ()))
    assert fdc.load(detect=detected).advertise_addrs == ()
    fdc.store(fdc.FleetDirectConfig("0.0.0.0", 0, ()))
    assert fdc.load(detect=detected).advertise_addrs == ()
    fdc.store(fdc.FleetDirectConfig("0.0.0.0", 9410, (), advertise_auto=False))
    assert fdc.load(detect=detected).advertise_addrs == ()


def test_live_detection_never_raises_and_yields_only_ipv4():
    import ipaddress
    for addr in fdc._detect_ipv4_addresses():
        assert ipaddress.ip_address(addr).version == 4


def test_a_fixed_port_on_loopback_is_not_enabled(machine):
    fdc.store(fdc.FleetDirectConfig("127.0.0.1", 9410, ()))
    assert fdc.load().enabled is False


@pytest.mark.parametrize("payload", [
    {"listen_port": 70000},
    {"listen_port": "9410"},
    {"listen_host": ""},
    {"advertise_addrs": ["https://not-a-socket"]},
    {"advertise_addrs": ["ws://x:1"] * 9},
    {"advertise_auto": "yes"},
])
def test_schema_rejects_malformed_rows(payload):
    with pytest.raises(SchemaValidationError):
        FleetDirectV1.validate(payload)
