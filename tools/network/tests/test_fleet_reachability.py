"""Fleet peer reachability against the real registry app (node:announce/lookup)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tools.network import fleet_reachability as fr
from tools.network.idkit import KeyPair, Subject, issue_cert
from tools.network.registry.app import create_app
from tools.network.registry.signing import sign_request

NOW = 1_800_000_000
DAY = 86_400
ORG = "11111111-1111-4111-8111-111111111111"


class _Clock:
    now = NOW

    def __call__(self):
        return self.now


@pytest.fixture
def env():
    clock = _Clock()
    app = create_app(":memory:", now_fn=clock, secure_cookies=False)
    client = TestClient(app)
    root = KeyPair.generate()
    reg = client.request("POST", "http://testserver/v1/orgs", json=sign_request(
        root, "POST", "/v1/orgs",
        {"org_uuid": ORG, "root_pub": root.public_hex, "recovery_policy": "none"},
        ts=NOW))
    assert reg.status_code == 201, reg.text
    return client, root


def _machine(root, name):
    key = KeyPair.generate()
    cert = issue_cert(root, key.public_hex, scope=["node:announce", "node:lookup"],
                      org=ORG, subject=Subject("agent", name),
                      not_before=NOW - 50, not_after=NOW + 200 * DAY)
    return key, cert


def test_announce_then_lookup_discovers_peers(env):
    client, root = env
    a_key, a_cert = _machine(root, "A")
    b_key, b_cert = _machine(root, "B")

    fr.announce("http://testserver", ORG, a_key, a_cert, ["ws://a.example:8443"],
                ts=NOW, client=client)
    fr.announce("http://testserver", ORG, b_key, b_cert, ["ws://b.example:8443"],
                ts=NOW, client=client)

    peers = fr.lookup("http://testserver", ORG, a_key, a_cert,
                      [a_key.public_hex, b_key.public_hex], ts=NOW, client=client)
    assert peers[b_key.public_hex] == ["ws://b.example:8443"]
    assert peers[a_key.public_hex] == ["ws://a.example:8443"]


def test_lookup_omits_an_unannounced_peer(env):
    client, root = env
    a_key, a_cert = _machine(root, "A")
    ghost = KeyPair.generate()  # in roster conceptually, never announced
    peers = fr.lookup("http://testserver", ORG, a_key, a_cert,
                      [ghost.public_hex], ts=NOW, client=client)
    assert peers == {}


def test_a_cert_without_node_scope_cannot_announce(env):
    import httpx
    client, root = env
    bad = KeyPair.generate()
    bad_cert = issue_cert(root, bad.public_hex, scope=["viewer:identify"],
                          org=ORG, subject=Subject("agent", "X"),
                          not_before=NOW - 50, not_after=NOW + 200 * DAY)
    with pytest.raises(httpx.HTTPStatusError):
        fr.announce("http://testserver", ORG, bad, bad_cert, ["ws://x:1"],
                    ts=NOW, client=client)


def test_lookup_hints_carries_the_peer_standing_relay_route(env):
    """A machine announces its own standing relay route beside (or instead
    of) direct addresses; peers read it through lookup_hints. lookup() stays
    direct-only, so the scheduler never tries to direct-dial a relay URL."""
    client, root = env
    a_key, a_cert = _machine(root, "A")
    b_key, b_cert = _machine(root, "B")
    relay_route = "wss://relay.example/l/" + "ab" * 16
    fr.announce("http://testserver", ORG, b_key, b_cert, [],
                relay_url=relay_route, ts=NOW, client=client)

    hints = fr.lookup_hints("http://testserver", ORG, a_key, a_cert,
                            [b_key.public_hex], ts=NOW, client=client)
    assert hints[b_key.public_hex] == {"addrs": [], "relay_url": relay_route}
    assert fr.lookup("http://testserver", ORG, a_key, a_cert,
                     [b_key.public_hex], ts=NOW, client=client) == {}

    t = [1000.0]
    cache = fr.ReachabilityCache(
        binding_getter=lambda: {"registry_url": "http://testserver", "org_uuid": ORG},
        machine_key_getter=lambda: a_key,
        cert_getter=lambda: a_cert,
        roster_getter=lambda: [a_key.public_hex, b_key.public_hex],
        advertise_addrs=[], relay_url=lambda: "wss://relay.example/l/" + "cd" * 16,
        interval=45.0, ts=NOW, clock=lambda: t[0], client=client)
    assert cache.relay_routes() == {b_key.public_hex: relay_route}
    assert cache.peers() == {}
    # A announced its own standing route (with no direct addresses)
    got = client.request(
        "POST", f"http://testserver/v1/orgs/{ORG}/reachability/query",
        json=sign_request(root, "POST", f"/v1/orgs/{ORG}/reachability/query",
                          {"node": a_key.public_hex}, ts=NOW)).json()["hints"]
    assert got[0]["relay_url"] == "wss://relay.example/l/" + "cd" * 16
    assert got[0]["addrs"] == []


def test_reachability_cache_announces_discovers_and_throttles(env):
    client, root = env
    a_key, a_cert = _machine(root, "A")
    b_key, b_cert = _machine(root, "B")
    fr.announce("http://testserver", ORG, b_key, b_cert, ["ws://b:8443"],
                ts=NOW, client=client)

    t = [1000.0]
    cache = fr.ReachabilityCache(
        binding_getter=lambda: {"registry_url": "http://testserver", "org_uuid": ORG},
        machine_key_getter=lambda: a_key,
        cert_getter=lambda: a_cert,
        roster_getter=lambda: [a_key.public_hex, b_key.public_hex],
        advertise_addrs=["ws://a:8443"], interval=45.0, ts=NOW,
        clock=lambda: t[0], client=client)

    # first call: A announces itself and discovers B
    assert cache.peers()[b_key.public_hex] == ["ws://b:8443"]
    got = client.request(
        "POST", f"http://testserver/v1/orgs/{ORG}/reachability/query",
        json=sign_request(root, "POST", f"/v1/orgs/{ORG}/reachability/query",
                          {"node": a_key.public_hex}, ts=NOW)).json()["hints"]
    assert got[0]["addrs"] == ["ws://a:8443"]  # A announced itself

    # within the interval: B's new address is NOT picked up (served stale)
    fr.announce("http://testserver", ORG, b_key, b_cert, ["ws://b-NEW:8443"],
                ts=NOW, client=client)
    assert cache.peers()[b_key.public_hex] == ["ws://b:8443"]

    # past the interval: refresh picks up the new address
    t[0] += 50.0
    assert cache.peers()[b_key.public_hex] == ["ws://b-NEW:8443"]
    # snapshot() reports without refreshing
    t[0] += 50.0
    assert cache.snapshot()[b_key.public_hex] == ["ws://b-NEW:8443"]
    assert cache.last_announce is not None


def test_reachability_cache_reads_a_callable_advertise_list_each_refresh(env):
    """An operator who sets the advertised URLs after unlock is announced on
    the next refresh -- the runtime is not re-armed for it."""
    client, root = env
    a_key, a_cert = _machine(root, "A")
    advertised = {"addrs": []}
    t = [1000.0]
    cache = fr.ReachabilityCache(
        binding_getter=lambda: {"registry_url": "http://testserver", "org_uuid": ORG},
        machine_key_getter=lambda: a_key,
        cert_getter=lambda: a_cert,
        roster_getter=lambda: [a_key.public_hex],
        advertise_addrs=lambda: list(advertised["addrs"]),
        interval=45.0, ts=NOW, clock=lambda: t[0], client=client)

    def hints():
        return client.request(
            "POST", f"http://testserver/v1/orgs/{ORG}/reachability/query",
            json=sign_request(root, "POST", f"/v1/orgs/{ORG}/reachability/query",
                              {"node": a_key.public_hex}, ts=NOW)).json()["hints"]

    cache.peers()
    assert hints() == []            # nothing advertised, nothing announced
    assert cache.last_announce is None
    advertised["addrs"] = ["wss://a.example:9410"]
    t[0] += 50.0
    cache.peers()
    assert hints()[0]["addrs"] == ["wss://a.example:9410"]
    assert cache.last_announce[1] == ["wss://a.example:9410"]
