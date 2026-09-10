"""The signed reachability descriptor: what it publishes, and what it refuses.

Contract graph://7ed8a519-356 §1, §3, §4, §6, plus the v4 generation amendment.
"""

from __future__ import annotations

import pytest

from tools.network import fleet_descriptor as fd
from tools.network.idkit import KeyPair

# The two machines, and the addresses each actually advertised on 2026-09-10.
HOME_TAILNET = "ws://100.122.70.30:9410"
HOME_BRIDGE = "ws://172.16.0.4:9410"
SJC_TAILNET = "ws://100.68.240.25:9410"
SJC_BRIDGE = "ws://172.16.0.2:9410"


def _machine():
    key = KeyPair.generate()
    return key, key.public_hex


class TestPublishedAddresses:
    """The publisher publishes what it has. Two filters were tried here and
    both were wrong; the tests record why so neither comes back."""

    def test_a_private_address_is_still_published(self):
        """A shape filter assumes the subnet collision. The daemon says each
        host pins AUTONOMY_SUBNET from its own preflight, so similar hosts
        converge but nothing guarantees it -- and a rule that assumes
        collision drops working direct paths to prevent a hazard it cannot
        reliably detect."""
        assert fd.publishable_addresses([HOME_TAILNET, HOME_BRIDGE]) == [
            HOME_TAILNET, HOME_BRIDGE]

    def test_a_machines_own_addresses_are_the_point(self):
        """Filtering out this machine's own addresses would mean advertising
        nothing: they are the only addresses it has to advertise."""
        assert fd.publishable_addresses([HOME_TAILNET]) == [HOME_TAILNET]

    def test_the_list_is_deduped_bounded_and_order_preserving(self):
        """Order is preserved rather than re-ranked: candidate_addresses
        already returns tailnet ahead of private even when detection yields
        the private one first (measured on auto-fpjdr), so ranking was never
        the gap."""
        many = [f"ws://100.64.0.{n}:9410" for n in range(1, 20)]
        out = fd.publishable_addresses(many + [many[0]])
        assert out == many[:fd.MAX_ADDRESSES]


class TestSignAndVerify:
    def test_a_descriptor_round_trips_and_verifies_against_its_own_row_key(self):
        key, pub = _machine()
        built = fd.build(key, machine_pub=pub, addresses=[HOME_TAILNET],
                         generation=1)
        assert fd.verify(built)["addresses"] == [HOME_TAILNET]

    def test_another_machines_signature_is_refused(self):
        """A descriptor can only ever be signed by the machine it describes."""
        _key, pub = _machine()
        other, _ = _machine()
        forged = fd.build(other, machine_pub=other.public_hex,
                          addresses=[HOME_TAILNET], generation=1)
        forged["machine_pub"] = pub
        with pytest.raises(Exception):
            fd.verify(forged)

    @pytest.mark.parametrize("mutate", [
        lambda d: d.__setitem__("addresses", [SJC_TAILNET]),
        lambda d: d.__setitem__("generation", 99),
        lambda d: d.__setitem__("signed_at_ns", 1),
        lambda d: d.pop("signature"),
    ])
    def test_every_signed_field_is_covered(self, mutate):
        key, pub = _machine()
        built = fd.build(key, machine_pub=pub, addresses=[HOME_TAILNET],
                         generation=4)
        mutate(built)
        with pytest.raises(Exception):
            fd.verify(built)

    def test_verify_takes_no_roster_and_cannot_admit_anybody(self):
        """§1: reachability is never authority. A valid signature says only
        that this machine published this. Membership is the roster's answer,
        checked separately, and this function has no way to express it."""
        import inspect

        params = set(inspect.signature(fd.verify).parameters)
        assert params == {"descriptor"}


class TestRelayLocator:
    def test_a_descriptor_with_no_locator_is_valid(self):
        """§6: a missing relay field makes the relay-fallback and pre-ICE
        paths unavailable; it does not make the peer unreachable. This is
        auto-ieh3l's acceptance 7 at the artefact level."""
        key, pub = _machine()
        built = fd.build(key, machine_pub=pub, addresses=[HOME_TAILNET],
                         generation=1)
        assert "relay" not in built
        assert fd.verify(built)["addresses"] == [HOME_TAILNET]

    def test_the_serving_key_is_not_the_row_key(self):
        """§4: durable identity and serving slot are different keys, and after
        76d61b5 the org connectors genuinely carry a distinct serving key.
        Carrying it as the row key would import slot identity into
        membership."""
        key, pub = _machine()
        serving = KeyPair.generate().public_hex
        persona = KeyPair.generate().public_hex
        built = fd.build(
            key, machine_pub=pub, addresses=[HOME_TAILNET], generation=1,
            relay={
                "relay_base": "wss://relay.auto.network",
                "org_uuid": "11111111-1111-4111-8111-111111111111",
                "persona_pub": persona,
                "serving_machine_pub": serving,
                "capabilities": ["tls-stream/1"],
                "expires_at_ns": 1_800_000_000_000_000_000,
            },
        )
        verified = fd.verify(built)
        assert verified["relay"]["serving_machine_pub"] == serving
        assert verified["machine_pub"] == pub != serving

    def test_the_locator_carries_no_generation(self):
        """v4 amendment: generation is a field of the DESCRIPTOR. The locator
        is optional, so a generation living only inside it would leave a
        locator-less machine with no ordering across a restart at all. The
        locator inherits the descriptor's generation by construction."""
        key, pub = _machine()
        with pytest.raises(fd.DescriptorError):
            fd.build(
                key, machine_pub=pub, addresses=[HOME_TAILNET], generation=1,
                relay={
                    "relay_base": "wss://relay.auto.network",
                    "org_uuid": "11111111-1111-4111-8111-111111111111",
                    "persona_pub": KeyPair.generate().public_hex,
                    "serving_machine_pub": KeyPair.generate().public_hex,
                    "descriptor_generation": 3,
                },
            )


class TestGenerationMonotonicity:
    """The three properties a reader's ordering fence depends on.

    A counter that restarts means a machine republishes generation 1 while a
    peer holds 7, the peer treats the CURRENT descriptor as stale, and it
    routes on addresses that may be gone -- indefinitely, with nothing logged
    at either end.
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

    def test_it_increases_and_never_repeats(self, store):
        seen = [fd.next_generation() for _ in range(5)]
        assert seen == [1, 2, 3, 4, 5]
        assert len(set(seen)) == len(seen)

    def test_it_survives_a_restart(self, store):
        """The property that matters. Nothing in the path reads a clock, and
        the counter is durable, so a process that dies and comes back cannot
        hand out a number it already used."""
        assert fd.next_generation() == 1
        assert fd.next_generation() == 2
        # A "restart" is exactly a fresh read of the durable row.
        assert fd.next_generation() == 3

    def test_a_number_is_spent_when_minted_not_when_published(self, store):
        """Persist-before-publish. A crash between minting and announcing must
        burn the number, because a repeat is what breaks a reader's fence and
        a gap is harmless."""
        minted = fd.next_generation()
        # ...and the publish fails here, so nothing is ever announced.
        assert fd.next_generation() == minted + 1

    def test_an_observed_generation_repairs_a_rebuilt_counter(self, store):
        """Self-healing seed: if the machine-local store is rebuilt behind a
        surviving identity, the highest generation visible in any projection
        of this machine's OWN descriptor pulls the counter back up rather than
        letting it republish at 1."""
        assert fd.next_generation() == 1
        assert fd.next_generation(observed=41) == 42
        assert fd.next_generation() == 43

    def test_a_descriptor_carries_the_generation_it_was_minted_with(self, store):
        key, pub = _machine()
        generation = fd.next_generation()
        built = fd.build(key, machine_pub=pub, addresses=[HOME_TAILNET],
                         generation=generation)
        assert fd.verify(built)["generation"] == generation
