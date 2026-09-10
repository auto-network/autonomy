"""The descriptor's relay locator: filled from the live connector, read per peer.

auto-e38g4. Contract graph://7ed8a519-356 §4 (durable identity and serving slot
are different keys), §5 (publish only after the connector's hello is
acknowledged; a late ack for an older generation must not publish) and §6 (a
missing locator does not make a peer unreachable).
"""

from __future__ import annotations

import pytest

from tools.network import fleet_descriptor as fd
from tools.network import fleet_reachability as fr
from tools.network.idkit import KeyPair, Subject, issue_cert

ORG = "11111111-1111-4111-8111-111111111111"
TAILNET = "ws://100.68.240.25:9410"
RELAY = "wss://auto.network"


def _locator(persona, machine, *, relay_base=RELAY, caps=("fleet-directed-stream/1",)):
    return {
        "relay_base": relay_base,
        "org_uuid": ORG,
        "persona_pub": persona,
        "serving_machine_pub": machine,
        "capabilities": list(caps),
    }


class _Resp:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


class _Client:
    """The registry as §3 specifies it: stores the descriptor verbatim, never
    re-signs, replays it on lookup."""

    def __init__(self):
        self.announced = []
        self.stored = {}

    def request(self, method, url, json=None):
        if url.endswith("/reachability"):
            self.announced.append(json["payload"])
            return _Resp({"ok": True})
        node = json["payload"]["node"]
        hint = self.stored.get(node)
        return _Resp({"hints": [hint] if hint else []})


@pytest.fixture
def machine():
    root = KeyPair.generate()
    key = KeyPair.generate()
    cert = issue_cert(
        root, key.public_hex, scope=("node:announce", "node:lookup"),
        org=ORG, subject=Subject("machine", key.public_hex),
        not_before=0, not_after=2_000_000_000,
    )
    return key, cert


@pytest.fixture
def store(tmp_path, monkeypatch):
    from tools.graph.db import GraphDB

    orgs = tmp_path / "orgs"
    orgs.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    GraphDB.close_all_pooled()
    personal = GraphDB.create_org_db("personal", type_="personal")
    personal.activate_fleet_sync_writers(KeyPair.generate().public_hex)
    personal.close()
    yield
    GraphDB.close_all_pooled()


def _cache(key, cert, addrs, *, locator=None, client=None, roster=()):
    return fr.ReachabilityCache(
        binding_getter=lambda: {
            "registry_url": "http://registry.invalid", "org_uuid": ORG},
        machine_key_getter=lambda: key,
        cert_getter=lambda: cert,
        roster_getter=lambda: list(roster),
        advertise_addrs=lambda: list(addrs),
        relay_locator=locator,
        client=client or _Client(),
    )


class TestPublication:
    def test_the_announced_descriptor_carries_the_live_slot(self, machine, store):
        key, cert = machine
        persona, slot = "ab" * 32, "cd" * 32
        client = _Client()
        cache = _cache(key, cert, [TAILNET],
                       locator=lambda: _locator(persona, slot), client=client)

        cache._maybe_refresh()

        (payload,) = client.announced
        relay = payload["descriptor"]["relay"]
        assert relay["persona_pub"] == persona
        assert relay["serving_machine_pub"] == slot
        assert relay["relay_base"] == RELAY and relay["org_uuid"] == ORG
        # §4: the row key stays the DURABLE identity, never the slot.
        assert payload["descriptor"]["machine_pub"] == key.public_hex

    def test_a_machine_with_only_a_slot_still_announces(self, machine, store):
        """No direct addresses at all. Withholding the announce would make the
        locator -- the only thing a peer could use -- the one thing never
        published."""
        key, cert = machine
        client = _Client()
        cache = _cache(key, cert, [],
                       locator=lambda: _locator("ab" * 32, "cd" * 32), client=client)

        cache._maybe_refresh()

        (payload,) = client.announced
        assert payload["addrs"] == []
        assert payload["descriptor"]["relay"]["serving_machine_pub"] == "cd" * 32

    def test_no_connector_means_no_locator_and_an_unchanged_announce(
        self, machine, store
    ):
        """§6: the relay paths become unavailable; the peer does not."""
        key, cert = machine
        client = _Client()
        cache = _cache(key, cert, [TAILNET], locator=lambda: None, client=client)

        cache._maybe_refresh()

        (payload,) = client.announced
        assert payload["addrs"] == [TAILNET]
        assert "relay" not in payload["descriptor"]

    def test_a_locator_getter_that_raises_is_not_fatal(self, machine, store):
        key, cert = machine
        client = _Client()

        def boom():
            raise RuntimeError("control socket gone")

        cache = _cache(key, cert, [TAILNET], locator=boom, client=client)
        cache._maybe_refresh()

        (payload,) = client.announced
        assert payload["addrs"] == [TAILNET]
        assert "relay" not in payload["descriptor"]


class TestGenerationCadence:
    """The same rule the addresses already follow: a generation is minted when
    the ROUTE moves, and not when noise around it does."""

    def test_a_moved_slot_mints_a_new_generation(self, machine, store):
        key, cert = machine
        cache = _cache(key, cert, [TAILNET])
        first = _locator("ab" * 32, "cd" * 32)
        moved = _locator("ab" * 32, "ee" * 32)

        before = cache._descriptor_for(
            key, [TAILNET], ((TAILNET,), None, fr._locator_key(first)), first)
        after = cache._descriptor_for(
            key, [TAILNET], ((TAILNET,), None, fr._locator_key(moved)), moved)

        assert after["generation"] > before["generation"]
        assert after["relay"]["serving_machine_pub"] == "ee" * 32

    def test_capabilities_are_not_part_of_the_route(self, machine, store):
        """A capability set that reorders is not a route change. Letting it into
        the key would burn a generation on a machine that had not moved."""
        one = _locator("ab" * 32, "cd" * 32, caps=("a/1", "b/1"))
        other = _locator("ab" * 32, "cd" * 32, caps=("b/1", "a/1"))

        assert fr._locator_key(one) == fr._locator_key(other)

    def test_gaining_a_slot_mints_a_generation(self, machine, store):
        key, cert = machine
        cache = _cache(key, cert, [TAILNET])
        gained = _locator("ab" * 32, "cd" * 32)

        before = cache._descriptor_for(key, [TAILNET], ((TAILNET,), None, None))
        after = cache._descriptor_for(
            key, [TAILNET], ((TAILNET,), None, fr._locator_key(gained)), gained)

        assert "relay" not in before
        assert after["generation"] > before["generation"]


class TestPerPeerReadout:
    def test_a_peer_with_no_direct_addresses_still_yields_its_locator(
        self, machine, store
    ):
        """The point of the whole seam. ``_peers`` is the direct-dial map and
        drops a peer with no usable address; that peer is exactly who the relay
        path exists for, so the locator is read from the verified hints."""
        key, cert = machine
        peer = KeyPair.generate()
        peer_descriptor = fd.build(
            peer, machine_pub=peer.public_hex, addresses=[], generation=1,
            relay=_locator("ab" * 32, "cd" * 32),
        )
        client = _Client()
        client.stored[peer.public_hex] = {"addrs": [], "descriptor": peer_descriptor}
        cache = _cache(key, cert, [TAILNET], client=client, roster=[peer.public_hex])

        cache._maybe_refresh()

        assert peer.public_hex not in cache.snapshot()
        assert cache.relay_locators()[peer.public_hex]["serving_machine_pub"] == "cd" * 32

    def test_a_descriptor_another_machine_signed_yields_no_locator(
        self, machine, store
    ):
        """A locator is only ever as good as the descriptor carrying it, and
        that is verified against the machine the row is keyed by."""
        key, cert = machine
        peer, impostor = KeyPair.generate(), KeyPair.generate()
        forged = fd.build(
            impostor, machine_pub=impostor.public_hex, addresses=[], generation=1,
            relay=_locator("ab" * 32, "cd" * 32),
        )
        client = _Client()
        client.stored[peer.public_hex] = {"addrs": [], "descriptor": forged}
        cache = _cache(key, cert, [TAILNET], client=client, roster=[peer.public_hex])

        cache._maybe_refresh()

        assert cache.relay_locators() == {}

    def test_a_locator_less_peer_is_absent_rather_than_empty(self, machine, store):
        key, cert = machine
        peer = KeyPair.generate()
        client = _Client()
        client.stored[peer.public_hex] = {
            "addrs": [TAILNET],
            "descriptor": fd.build(peer, machine_pub=peer.public_hex,
                                   addresses=[TAILNET], generation=1),
        }
        cache = _cache(key, cert, [], client=client, roster=[peer.public_hex])

        cache._maybe_refresh()

        assert cache.relay_locators() == {}
        assert cache.snapshot()[peer.public_hex] == [TAILNET]
