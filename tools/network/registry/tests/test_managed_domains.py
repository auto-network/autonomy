"""Administrator-assigned platform domains, never self-allocated by clients."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from tools.network.registry import domains, relay
from tools.network.registry.store import RegistryStore
from .conftest import ORG, register
from .test_dns01_ops import _ctrl, _tunnel
from .test_serve_zones import _present_zone_args, _state
from .test_dns_responder import ask, RELAY_IP

ZONE = "anchore.serve.auto.network"
OTHER = "22222222-2222-4222-8222-222222222222"


@pytest.mark.parametrize("name", ["serve.auto.network", "x.auto.network", "x.y.serve.auto.network",
    "-x.serve.auto.network"])
def test_invalid_names(name):
    with pytest.raises(ValueError):
        domains.validate_managed_domain(name)


def test_reservation_checks_registration_and_is_idempotent(app, client, root, clock):
    store = app.state.store
    assert domains.show_domain(store, ZONE)["available"]
    with pytest.raises(ValueError, match="registered"):
        domains.reserve_domain(store, ZONE, ORG, now=clock.now)
    register(client, clock, root, org_uuid=ORG)
    row = domains.reserve_domain(store, ZONE, ORG, now=clock.now)
    assert row["binding_kind"] == "registry" and row["org_uuid"] == ORG
    assert not domains.show_domain(store, ZONE)["available"]
    assert domains.reserve_domain(store, ZONE, ORG, now=clock.now + 1) == row
    with pytest.raises(ValueError):
        domains.reserve_domain(store, ZONE, OTHER, now=clock.now)
    store.set_serve_zone_state(ZONE, "revoked", now=clock.now)
    assert domains.reserve_domain(store, ZONE, ORG, now=clock.now)["state"] == "active"


def test_two_connections_cannot_allocate_to_different_owners(tmp_path):
    path = str(tmp_path / "registry.db")
    stores = [RegistryStore(path), RegistryStore(path)]
    for org in (ORG, OTHER):
        stores[0].create_org(org, "aa" * 32, "none", None, now=10, expires_at=100)
    barrier = Barrier(2)

    def reserve(pair):
        store, org = pair
        barrier.wait()
        try:
            return domains.reserve_domain(store, ZONE, org, now=20)["org_uuid"]
        except ValueError:
            return "refused"
    try:
        with ThreadPoolExecutor(2) as pool:
            result = list(pool.map(reserve, zip(stores, (ORG, OTHER))))
        assert result.count("refused") == 1
        assert stores[0].get_serve_zone(ZONE)["org_uuid"] in result
        assert domains.reserve_domain(stores[0], "expired.serve.auto.network", ORG, now=101)["org_uuid"] == ORG
    finally:
        for store in stores:
            store.close()


def test_ordinary_claim_cannot_mint_assignment(app, client, root, clock):
    register(client, clock, root, org_uuid=ORG)
    with _tunnel(client, clock, root) as ws:
        for kind in ("registry", "ns-token", "parent-txt"):
            reply = _ctrl(ws, "serve.zone.claim", {"zone": ZONE, "binding_kind": kind})
            assert not reply["ok"]
            assert app.state.store.get_serve_zone(ZONE) is None
        row = domains.reserve_domain(app.state.store, ZONE, ORG, now=clock.now)
        assert _ctrl(ws, "serve.zone.claim", {"zone": ZONE, "binding_kind": "registry"})["ok"]
        for kind in ("ns-token", "parent-txt"):
            assert not _ctrl(ws, "serve.zone.claim", {"zone": ZONE, "binding_kind": kind})["ok"]
        assert app.state.store.get_serve_zone(ZONE) == row
        # Existing custom-domain removal deactivates use but keeps ownership.
        assert _ctrl(ws, "serve.zone.release", {"zone": ZONE})["ok"]
        assert app.state.store.get_serve_zone(ZONE)["org_uuid"] == ORG
        assert not _ctrl(ws, "serve.zone.claim", {"zone": ZONE, "binding_kind": "registry"})["ok"]


def test_existing_member_address_is_not_available(app, client, root, clock):
    register(client, clock, root)
    store = app.state.store
    label = "member-" + "a" * 20
    store.bind_persona_label("aa" * 32, label, now=clock.now)
    domain = label + ".serve.auto.network"
    assert not domains.show_domain(store, domain)["available"]
    with pytest.raises(ValueError, match="assigned to a member"):
        domains.reserve_domain(store, domain, ORG, now=clock.now)
    assert domains.validate_managed_domain("www.serve.auto.network") == "www.serve.auto.network"


def test_owner_host_dns01_and_foreign_refusal(app, client, root, clock):
    register(client, clock, root, org_uuid=ORG)
    store = app.state.store
    domains.reserve_domain(store, ZONE, ORG, now=clock.now)
    with _tunnel(client, clock, root) as ws:
        host = {"host": f"hello.{ZONE}", "reservation": relay.zone_reservation_id(ZONE, "hello")}
        assert _ctrl(ws, "host-register", host)["ok"]
        reply = _ctrl(ws, "serve.dns01.present", _present_zone_args(root, clock, ZONE))
        assert reply["ok"] and reply["name"] == f"_acme-challenge.{ZONE}"
        # Another org owns this distinct assignment: no identity in request can override it.
        foreign = "foreign.serve.auto.network"
        store.upsert_serve_zone(foreign, org=OTHER, binding_kind="registry", binding_value=OTHER,
                               state="active", now=clock.now, verified_at=clock.now)
        assert not _ctrl(ws, "serve.zone.claim", {"zone": foreign, "binding_kind": "registry"})["ok"]
        assert not _ctrl(ws, "host-register", {"host": f"hello.{foreign}",
            "reservation": relay.zone_reservation_id(foreign, "hello")})["ok"]
        assert not _ctrl(ws, "serve.dns01.present", _present_zone_args(root, clock, foreign))["ok"]
    feed = client.get("/v1/dns/zone-state").json()
    state = _state(["serve.auto.network", *feed["zones"]])
    assert ask(state, f"hello.{ZONE}", "A")["answers"][0][3] == RELAY_IP
    assert ask(state, ZONE, "SOA")["answers"][0][0] == ZONE + "."
