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


class TestExpiry:
    """The locator's ``expires_at_ns`` was validated on the wire, written as 0
    by the publisher, and read by NOTHING -- a field promising a guarantee the
    system did not make. Raised by auto-0831-221227 validating auto-e38g4.

    The reader now enforces exactly what a publisher states and nothing more.
    """

    def test_zero_means_no_expiry(self):
        """What every publisher writes today. Honoured as written rather than
        reinterpreted as "expired at the epoch"."""
        assert fr.locator_is_current(_locator("ab" * 32, "cd" * 32), 10**18)

    def test_a_passed_expiry_drops_the_locator(self):
        locator = dict(_locator("ab" * 32, "cd" * 32), expires_at_ns=500)

        assert fr.locator_is_current(locator, 501) is False

    def test_an_unreached_expiry_is_current(self):
        locator = dict(_locator("ab" * 32, "cd" * 32), expires_at_ns=500)

        assert fr.locator_is_current(locator, 499) is True

    def test_the_expiry_instant_itself_is_expired(self):
        locator = dict(_locator("ab" * 32, "cd" * 32), expires_at_ns=500)

        assert fr.locator_is_current(locator, 500) is False

    def test_an_absent_locator_is_not_current(self):
        assert fr.locator_is_current(None, 1) is False

    def test_an_expired_peer_keeps_its_direct_addresses(self, machine, store,
                                                        monkeypatch):
        """Contract §6 in the expiry case: the slot goes, the peer does not."""
        key, cert = machine
        peer = KeyPair.generate()
        expired = fd.build(
            peer, machine_pub=peer.public_hex, addresses=[TAILNET], generation=1,
            relay=dict(_locator("ab" * 32, "cd" * 32), expires_at_ns=500),
        )
        client = _Client()
        client.stored[peer.public_hex] = {
            "addrs": [TAILNET], "descriptor": expired}
        cache = _cache(key, cert, [], client=client, roster=[peer.public_hex])
        monkeypatch.setattr(fr._time, "time_ns", lambda: 10**6)

        cache._maybe_refresh()

        assert cache.relay_locators() == {}
        assert cache.snapshot()[peer.public_hex] == [TAILNET]

    def test_a_live_expiry_is_served(self, machine, store, monkeypatch):
        key, cert = machine
        peer = KeyPair.generate()
        live = fd.build(
            peer, machine_pub=peer.public_hex, addresses=[], generation=1,
            relay=dict(_locator("ab" * 32, "cd" * 32), expires_at_ns=10**9),
        )
        client = _Client()
        client.stored[peer.public_hex] = {"addrs": [], "descriptor": live}
        cache = _cache(key, cert, [], client=client, roster=[peer.public_hex])
        monkeypatch.setattr(fr._time, "time_ns", lambda: 10**6)

        cache._maybe_refresh()

        assert cache.relay_locators()[peer.public_hex]["expires_at_ns"] == 10**9

    def test_an_expired_locator_is_logged_once_not_every_round(
        self, machine, store, monkeypatch, caplog
    ):
        """relay_locators() runs once per scheduler round (~10 s) plus once per
        delegated pull, and an expiry is permanent until the peer republishes.
        Per-call logging would make a peer that has not republished into the
        log. Raised by auto-0831-221227 reviewing the expiry gate.
        """
        key, cert = machine
        peer = KeyPair.generate()
        client = _Client()
        client.stored[peer.public_hex] = {
            "addrs": [TAILNET],
            "descriptor": fd.build(
                peer, machine_pub=peer.public_hex, addresses=[TAILNET],
                generation=1,
                relay=dict(_locator("ab" * 32, "cd" * 32), expires_at_ns=500),
            ),
        }
        cache = _cache(key, cert, [], client=client, roster=[peer.public_hex])
        monkeypatch.setattr(fr._time, "time_ns", lambda: 10**6)
        cache._maybe_refresh()

        with caplog.at_level("INFO", logger=fr.logger.name):
            for _ in range(5):
                assert cache.relay_locators() == {}

        expired = [r for r in caplog.records if "locator expired" in r.message]
        assert len(expired) == 1, [r.message for r in caplog.records]

    def test_a_republished_locator_lets_a_later_expiry_be_heard(
        self, machine, store, monkeypatch, caplog
    ):
        """Suppression must not be permanent: once the peer republishes, a NEW
        expiry is a new fact and has to be reportable again."""
        key, cert = machine
        peer = KeyPair.generate()
        client = _Client()

        def publish(expires):
            client.stored[peer.public_hex] = {
                "addrs": [],
                "descriptor": fd.build(
                    peer, machine_pub=peer.public_hex, addresses=[],
                    generation=1,
                    relay=dict(_locator("ab" * 32, "cd" * 32),
                               expires_at_ns=expires),
                ),
            }

        cache = _cache(key, cert, [], client=client, roster=[peer.public_hex])
        monkeypatch.setattr(fr._time, "time_ns", lambda: 10**6)

        with caplog.at_level("INFO", logger=fr.logger.name):
            publish(500)                      # expired
            cache._maybe_refresh()
            assert cache.relay_locators() == {}

            publish(0)                        # republished, no expiry
            cache._last = None
            cache._maybe_refresh()
            assert peer.public_hex in cache.relay_locators()

            publish(900)                      # expired again, a NEW fact
            cache._last = None
            cache._maybe_refresh()
            assert cache.relay_locators() == {}

        expired = [r for r in caplog.records if "locator expired" in r.message]
        assert len(expired) == 2, [r.message for r in caplog.records]
