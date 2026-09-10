"""The first-contact projection: announce carries the descriptor, lookup verifies it.

Contract graph://7ed8a519-356 §3. The registry stores and returns the SAME
signed descriptor and does not re-sign it, so the caller is the only thing
between a cache and a forged hint.
"""

from __future__ import annotations

import json

import pytest

from tools.network import fleet_descriptor as fd
from tools.network import fleet_reachability as fr
from tools.network.idkit import KeyPair, Subject, issue_cert

ORG = "11111111-1111-4111-8111-111111111111"
TAILNET = "ws://100.68.240.25:9410"
BRIDGE = "ws://172.16.0.2:9410"


class _Resp:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


class _Client:
    """Stands in for the registry: records what was announced, replays it on
    lookup WITHOUT re-signing, which is the behaviour §3 specifies."""

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


def test_announce_carries_the_descriptor_verbatim(machine):
    key, cert = machine
    descriptor = fd.build(key, machine_pub=key.public_hex,
                          addresses=[TAILNET, BRIDGE], generation=3)
    client = _Client()
    fr.announce(
        "http://registry.invalid", ORG, key, cert, [TAILNET],
        descriptor=descriptor, client=client,
    )
    sent = client.announced[0]["descriptor"]
    assert sent == descriptor
    # BOTH addresses are published, including the bridge one. A machine
    # advertises what it has; deciding an address is meaningless elsewhere
    # would assume a subnet collision that the daemon says is expected and not
    # guaranteed. The peer's dialer drops what reaches ITSELF, which is exact.
    assert sent["addresses"] == [TAILNET, BRIDGE]


def test_lookup_returns_a_descriptor_the_caller_verified_itself(machine):
    key, cert = machine
    descriptor = fd.build(key, machine_pub=key.public_hex,
                          addresses=[TAILNET], generation=7)
    client = _Client()
    client.stored[key.public_hex] = {"addrs": [TAILNET], "descriptor": descriptor}

    hints = fr.lookup_hints("http://registry.invalid", ORG, key, cert,
                            [key.public_hex], client=client)

    assert hints[key.public_hex]["descriptor"]["generation"] == 7


def test_a_descriptor_signed_by_another_machine_is_discarded(machine):
    """A registry answering a lookup for A with a validly-signed descriptor
    for B is the attack the name check exists for. The signature is real; the
    binding to the peer we asked about is what fails."""
    key, cert = machine
    other = KeyPair.generate()
    forged = fd.build(other, machine_pub=other.public_hex,
                      addresses=["ws://100.64.9.9:9410"], generation=99)
    client = _Client()
    client.stored[key.public_hex] = {"addrs": [TAILNET], "descriptor": forged}

    hints = fr.lookup_hints("http://registry.invalid", ORG, key, cert,
                            [key.public_hex], client=client)

    assert hints[key.public_hex]["descriptor"] is None
    # ...and the direct addresses still resolve. One bad cached entry must not
    # deny discovery of a peer whose addresses are fine.
    assert hints[key.public_hex]["addrs"] == [TAILNET]


def test_a_tampered_descriptor_is_discarded_without_denying_the_peer(machine):
    key, cert = machine
    descriptor = fd.build(key, machine_pub=key.public_hex,
                          addresses=[TAILNET], generation=2)
    descriptor["addresses"] = ["ws://100.64.6.6:9410"]
    client = _Client()
    client.stored[key.public_hex] = {"addrs": [TAILNET], "descriptor": descriptor}

    hints = fr.lookup_hints("http://registry.invalid", ORG, key, cert,
                            [key.public_hex], client=client)

    assert hints[key.public_hex]["descriptor"] is None
    assert hints[key.public_hex]["addrs"] == [TAILNET]


def test_a_peer_with_no_descriptor_is_unchanged(machine):
    """The pre-descriptor announce still works: this rolls out one machine at
    a time and a peer that has not published one is not thereby unreachable."""
    key, cert = machine
    client = _Client()
    client.stored[key.public_hex] = {"addrs": [TAILNET]}

    hints = fr.lookup_hints("http://registry.invalid", ORG, key, cert,
                            [key.public_hex], client=client)

    assert hints[key.public_hex]["addrs"] == [TAILNET]
    assert hints[key.public_hex]["descriptor"] is None


def test_a_locator_less_descriptor_resolves(machine):
    """auto-ieh3l acceptance 7, at the first-contact projection. §6: a missing
    relay field makes the relay-fallback and pre-ICE paths unavailable; it
    does not make the peer unreachable."""
    key, cert = machine
    descriptor = fd.build(key, machine_pub=key.public_hex,
                          addresses=[TAILNET], generation=1)
    assert "relay" not in descriptor
    client = _Client()
    client.stored[key.public_hex] = {"addrs": [], "descriptor": descriptor}

    hints = fr.lookup_hints("http://registry.invalid", ORG, key, cert,
                            [key.public_hex], client=client)

    resolved = hints[key.public_hex]["descriptor"]
    assert resolved["addresses"] == [TAILNET]
    assert "relay" not in resolved


class TestPublisherCadence:
    """A keepalive must not mint a generation.

    The announce cadence is TTL/2, so minting per announce would burn a
    generation every couple of minutes forever on a machine whose reachability
    had not moved -- readers would see a stream of "newer" descriptors carrying
    identical addresses, and the ordering fence would be churning on noise.
    """

    @pytest.fixture
    def store(self, tmp_path, monkeypatch):
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

    def _cache(self, key, cert, addrs):
        return fr.ReachabilityCache(
            binding_getter=lambda: {
                "registry_url": "http://registry.invalid", "org_uuid": ORG},
            machine_key_getter=lambda: key,
            cert_getter=lambda: cert,
            roster_getter=lambda: [],
            advertise_addrs=lambda: list(addrs),
            client=_Client(),
        )

    def test_the_same_reachability_re_announces_the_same_descriptor(
        self, machine, store
    ):
        key, cert = machine
        cache = self._cache(key, cert, [TAILNET])
        wanted = ((TAILNET,), None)

        first = cache._descriptor_for(key, [TAILNET], wanted)
        again = cache._descriptor_for(key, [TAILNET], wanted)

        assert first is again
        assert first["generation"] == 1

    def test_changed_reachability_mints_a_new_generation(self, machine, store):
        key, cert = machine
        cache = self._cache(key, cert, [TAILNET])

        first = cache._descriptor_for(key, [TAILNET], ((TAILNET,), None))
        moved = cache._descriptor_for(
            key, ["ws://100.64.1.1:9410"], (("ws://100.64.1.1:9410",), None))

        assert moved["generation"] > first["generation"]
        assert moved["addresses"] == ["ws://100.64.1.1:9410"]

    def test_a_build_failure_leaves_the_announce_without_a_descriptor(
        self, machine, store, monkeypatch
    ):
        """A peer that cannot publish a descriptor must not thereby become
        unannounced: the pre-descriptor announce still works."""
        key, cert = machine
        cache = self._cache(key, cert, [TAILNET])
        monkeypatch.setattr(
            fd, "next_generation",
            lambda **kw: (_ for _ in ()).throw(RuntimeError("store down")))

        assert cache._descriptor_for(key, [TAILNET], ((TAILNET,), None)) is None


class TestSelfReachableCandidates:
    """The gap no bead owned: a peer hint that reaches the dialer itself.

    Observed 2026-09-09 -- sjc-2 dialled toward home, reached itself, and the
    fleet handshake refused it 24 times between 19:35:47Z and 20:00:21Z:
    'expected 3996513b6b233afd, got 571d62ab69c59f83', the second being
    sjc-2's own key. The refusal is correct and it is not free.
    """

    def test_a_candidate_on_our_own_host_is_not_dialed(self):
        own = ["ws://100.122.70.30:9410", "ws://172.16.0.4:9410"]
        peer = ["ws://100.68.240.25:9410", "ws://172.16.0.4:9410"]
        assert fr.drop_self_reachable(peer, own) == ["ws://100.68.240.25:9410"]

    def test_a_different_port_on_our_own_host_still_reaches_us(self):
        """HOST, not host:port. Our host answers on whatever port it answers
        on; reaching this machine at all is the wrong outcome."""
        own = ["ws://172.16.0.4:9410"]
        assert fr.drop_self_reachable(["ws://172.16.0.4:8443"], own) == []

    def test_knowing_none_of_our_own_addresses_filters_nothing(self):
        """A machine that does not know its own addresses must not start
        discarding a peer's."""
        peer = ["ws://100.68.240.25:9410", "ws://172.16.0.4:9410"]
        assert fr.drop_self_reachable(peer, []) == peer

    def test_a_peer_whose_only_address_reaches_us_is_dropped_not_emptied(self):
        """The peer disappears from the resolved map rather than appearing
        with an empty candidate list, so a caller cannot read 'known, with
        nowhere to dial' as 'reachable'."""
        own = ["ws://172.16.0.4:9410"]
        assert fr.drop_self_reachable(["ws://172.16.0.4:9410"], own) == []

    def test_it_assumes_nothing_about_subnets(self):
        """The compose subnet is pinned per host by a deterministic preflight,
        so a collision is the expected case and NOT an invariant. A peer on a
        genuinely different private range keeps its candidate."""
        own = ["ws://172.16.0.4:9410"]
        peer = ["ws://192.168.7.7:9410", "ws://10.1.2.3:9410"]
        assert fr.drop_self_reachable(peer, own) == peer
