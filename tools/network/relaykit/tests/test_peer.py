"""G1 peer relay + fallback chain — in-process acceptance.

Pinned properties (spec ``eb245082-b76`` §8, bead ``auto-57hav``):

- ``relay:serve`` is a *delegation*: a relay proves it with a signed,
  nonce-fresh chain to the org root, and BOTH client roles — dialers and
  parking nodes — refuse a relay whose chain lacks the scope. Chain
  verification, not configuration.
- The relaying node's process sees ciphertext only (I5 extended to the
  peer rung), proven against an instrumented relay and an actively
  tampering one.
- The fallback chain degrades direct → peer relay and routes around
  unauthorized or dead relays without stranding the dial.
- Discovery is ledger ∩ hints: the authority ledger (a real fold, not a
  fixture dict) names who may relay; hints say where they are.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets

import socket

import pytest
import websockets

from tools.network.idkit import KeyPair, Subject, canonical_json, issue_cert
from tools.network.ledger.projections import build_live_keys
from tools.network.ledger.testkit import OrgSim
from tools.network.relaykit.channel import HandshakeError
from tools.network.relaykit.dialer import (
    PATH_DIRECT,
    PATH_PEER_RELAY,
    DialError,
    dial_peer,
    dial_via_peer_relay,
    relay_candidates,
)
from tools.network.relaykit.direct import DirectChannelServer, new_session_id
from tools.network.relaykit.peer import (
    PeerParkConnector,
    PeerRelay,
    RelayVerifyError,
    build_relay_hello,
    verify_relay_hello,
)

from .conftest import ORG

CANARY = b"AUTONOMY_PLAINTEXT_CANARY_7f3a9c" * 2


@pytest.fixture
def unused_tcp_port():
    """A port with nothing behind it — the simulated-NAT black hole."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def relay_key():
    return KeyPair.generate()


@pytest.fixture(scope="module")
def relay_cert(root, relay_key, now):
    """The relay:serve delegation — what makes a member node a relay."""
    return issue_cert(
        root,
        relay_key.public_hex,
        scope=("node:announce", "relay:serve"),
        org=ORG,
        subject=Subject("agent", "relay-node-1"),
        not_before=now - 300,
        not_after=now + 7 * 86_400,
    )


@pytest.fixture(scope="module")
def imposter_cert(root, relay_key, now):
    """Same key, real chain to root — but NO relay:serve scope."""
    return issue_cert(
        root,
        relay_key.public_hex,
        scope=("node:announce", "tunnel:serve"),
        org=ORG,
        subject=Subject("agent", "relay-node-1"),
        not_before=now - 300,
        not_after=now + 7 * 86_400,
    )


# ── the relay:serve proof itself ─────────────────────────────────────


class TestRelayHello:
    def test_park_and_dial_roundtrip(self, root, relay_key, relay_cert, now):
        nonce = secrets.token_hex(16)
        hello = build_relay_hello(relay_key, relay_cert, org=ORG, purpose="park",
                                  nonce=nonce, ts=now)
        assert verify_relay_hello(hello, root_pub=root.public_hex, org=ORG,
                                  purpose="park", nonce=nonce, now=now) \
            == relay_key.public_hex

        hello = build_relay_hello(relay_key, relay_cert, org=ORG, purpose="dial",
                                  nonce=nonce, ts=now, target="ab" * 32,
                                  session="cd" * 16)
        verify_relay_hello(hello, root_pub=root.public_hex, org=ORG,
                           purpose="dial", nonce=nonce, target="ab" * 32,
                           session="cd" * 16, now=now)

    def test_missing_scope_is_refused(self, root, relay_key, imposter_cert, now):
        """The acceptance property: no relay:serve in the chain → refused.

        The imposter's chain is otherwise VALID (real signature, chains
        to the real root) — the scope alone is what fails it."""
        nonce = secrets.token_hex(16)
        hello = build_relay_hello(relay_key, imposter_cert, org=ORG,
                                  purpose="park", nonce=nonce, ts=now)
        with pytest.raises(RelayVerifyError, match="Scope"):
            verify_relay_hello(hello, root_pub=root.public_hex, org=ORG,
                               purpose="park", nonce=nonce, now=now)

    def test_freshness_and_binding(self, root, relay_key, relay_cert, now):
        nonce = secrets.token_hex(16)
        hello = build_relay_hello(relay_key, relay_cert, org=ORG, purpose="dial",
                                  nonce=nonce, ts=now, target="ab" * 32,
                                  session="cd" * 16)
        kwargs = dict(root_pub=root.public_hex, org=ORG, purpose="dial",
                      nonce=nonce, target="ab" * 32, session="cd" * 16)

        verify_relay_hello(hello, now=now, **kwargs)
        with pytest.raises(RelayVerifyError, match="ts outside"):
            verify_relay_hello(hello, now=now + 3600, **kwargs)  # replayed later
        with pytest.raises(RelayVerifyError):  # someone else's challenge
            verify_relay_hello(hello, now=now, **{**kwargs, "nonce": "0" * 32})
        with pytest.raises(RelayVerifyError):  # spliced onto another dial
            verify_relay_hello(hello, now=now, **{**kwargs, "session": "ee" * 16})
        with pytest.raises(RelayVerifyError):  # different target
            verify_relay_hello(hello, now=now, **{**kwargs, "target": "ee" * 32})
        with pytest.raises(RelayVerifyError):  # purpose confusion park≠dial
            verify_relay_hello(hello, now=now, **{**kwargs, "purpose": "park"})

    def test_foreign_root_is_refused(self, relay_key, relay_cert, now):
        nonce = secrets.token_hex(16)
        hello = build_relay_hello(relay_key, relay_cert, org=ORG, purpose="park",
                                  nonce=nonce, ts=now)
        with pytest.raises(RelayVerifyError):
            verify_relay_hello(hello, root_pub=KeyPair.generate().public_hex,
                               org=ORG, purpose="park", nonce=nonce, now=now)


# ── live bridge: park, dial, refuse, observe ─────────────────────────


class RecordingPeerRelay(PeerRelay):
    """Sees exactly what the relaying node's process sees."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.observed = bytearray()

    def _observe_data(self, payload: bytes) -> None:
        self.observed.extend(payload)


class EvilPeerRelay(PeerRelay):
    """A peer relay that substitutes its own ECDH key into the first
    node→dialer message (the SERVER_HELLO) — the classic MITM. It holds
    a REAL relay:serve delegation: authorization to relay must not imply
    ability to read or forge (I5)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._attacked: set = set()

    async def _bridge_to_dialer(self, dialer, channel_id, payload):
        if channel_id not in self._attacked:
            self._attacked.add(channel_id)
            with contextlib.suppress(ValueError, TypeError):
                hello = json.loads(payload)
                from cryptography.hazmat.primitives.asymmetric.x25519 import (
                    X25519PrivateKey,
                )
                hello["eph_pub"] = (
                    X25519PrivateKey.generate().public_key()
                    .public_bytes_raw().hex()
                )
                payload = canonical_json(hello)
        await super()._bridge_to_dialer(dialer, channel_id, payload)


async def _park_node(relay_port, root, session_key, session_cert,
                     handler=None) -> tuple:
    """Park the test node at a relay and wait until it is serving."""
    kwargs = {"handler": handler} if handler else {}
    parker = PeerParkConnector(
        f"ws://127.0.0.1:{relay_port}", ORG, session_key, session_cert,
        root_pub=root.public_hex, min_backoff=0.05, max_backoff=0.2, **kwargs
    )
    task = asyncio.create_task(parker.run())
    await asyncio.wait_for(parker.connected.wait(), timeout=5)
    return parker, task


async def _stop_parker(parker, task) -> None:
    parker.stop()
    task.cancel()
    with contextlib.suppress(BaseException):
        await task


class TestPeerRelayBridge:
    def test_park_dial_echo(self, root, session_key, session_cert,
                            relay_key, relay_cert):
        async def run():
            relay = PeerRelay(ORG, root.public_hex, relay_key, relay_cert)
            port = await relay.start()
            parker, task = await _park_node(port, root, session_key, session_cert)
            try:
                assert relay.parked_nodes() == [session_key.public_hex]
                channel = await dial_via_peer_relay(
                    f"ws://127.0.0.1:{port}", org=ORG, root_pub=root.public_hex,
                    target_pub=session_key.public_hex, session=new_session_id(),
                )
                async with channel:
                    await channel.send_message(b"through a peer")
                    assert await channel.recv_message() == b"through a peer"
            finally:
                await _stop_parker(parker, task)
                await relay.stop()
        asyncio.run(run())

    def test_dial_unparked_target_closes_4404(self, root, relay_key, relay_cert):
        async def run():
            relay = PeerRelay(ORG, root.public_hex, relay_key, relay_cert)
            port = await relay.start()
            try:
                with pytest.raises(websockets.exceptions.ConnectionClosed) as info:
                    await dial_via_peer_relay(
                        f"ws://127.0.0.1:{port}", org=ORG,
                        root_pub=root.public_hex, target_pub="ab" * 32,
                        session=new_session_id(),
                    )
                assert info.value.rcvd.code == 4404
            finally:
                await relay.stop()
        asyncio.run(run())

    def test_dialer_refuses_undelegated_relay(self, root, session_key,
                                              session_cert, relay_key,
                                              imposter_cert):
        """A live relay whose chain lacks relay:serve: the DIALER walks
        away during the hello — before any channel byte flows."""
        async def run():
            relay = PeerRelay(ORG, root.public_hex, relay_key, imposter_cert)
            port = await relay.start()
            try:
                with pytest.raises(RelayVerifyError, match="Scope"):
                    await dial_via_peer_relay(
                        f"ws://127.0.0.1:{port}", org=ORG,
                        root_pub=root.public_hex,
                        target_pub=session_key.public_hex,
                        session=new_session_id(),
                    )
            finally:
                await relay.stop()
        asyncio.run(run())

    def test_parker_refuses_undelegated_relay(self, root, session_key,
                                              session_cert, relay_key,
                                              imposter_cert):
        """...and so does a would-be PARKER: the node never presents its
        tunnel hello, so the imposter relay gains no parked tunnel."""
        async def run():
            relay = PeerRelay(ORG, root.public_hex, relay_key, imposter_cert)
            port = await relay.start()
            parker = PeerParkConnector(
                f"ws://127.0.0.1:{port}", ORG, session_key, session_cert,
                root_pub=root.public_hex, min_backoff=0.05, max_backoff=0.1,
            )
            task = asyncio.create_task(parker.run())
            try:
                await asyncio.sleep(0.5)  # several connect+refuse cycles
                assert not parker.connected.is_set()
                assert relay.parked_nodes() == []
            finally:
                await _stop_parker(parker, task)
                await relay.stop()
        asyncio.run(run())

    def test_relay_process_sees_ciphertext_only(self, root, session_key,
                                                session_cert, relay_key,
                                                relay_cert):
        """Frame scan on the relaying node's own process (the B2 MITM-test
        posture): a canary that crosses in BOTH directions never appears
        in anything the relay observed."""
        async def run():
            relay = RecordingPeerRelay(ORG, root.public_hex, relay_key, relay_cert)
            port = await relay.start()
            parker, task = await _park_node(port, root, session_key, session_cert)
            try:
                payload = CANARY + secrets.token_bytes(200_000) + CANARY
                channel = await dial_via_peer_relay(
                    f"ws://127.0.0.1:{port}", org=ORG, root_pub=root.public_hex,
                    target_pub=session_key.public_hex, session=new_session_id(),
                )
                async with channel:
                    await channel.send_message(payload)
                    assert await channel.recv_message() == payload
            finally:
                await _stop_parker(parker, task)
                await relay.stop()

            observed = bytes(relay.observed)
            # positive control: the round trip really crossed this process
            assert len(observed) > 2 * len(payload)
            assert CANARY not in observed
        asyncio.run(run())

    def test_mitm_by_authorized_relay_fails_closed(self, root, session_key,
                                                   session_cert, relay_key,
                                                   relay_cert):
        """relay:serve authorizes CARRYING, nothing else: an authorized
        relay substituting ECDH material still breaks against the org-key
        pin (I5)."""
        async def run():
            relay = EvilPeerRelay(ORG, root.public_hex, relay_key, relay_cert)
            port = await relay.start()
            parker, task = await _park_node(port, root, session_key, session_cert)
            try:
                with pytest.raises(HandshakeError):
                    await dial_via_peer_relay(
                        f"ws://127.0.0.1:{port}", org=ORG,
                        root_pub=root.public_hex,
                        target_pub=session_key.public_hex,
                        session=new_session_id(),
                    )
            finally:
                await _stop_parker(parker, task)
                await relay.stop()
        asyncio.run(run())


# ── the fallback chain (in-process rungs) ────────────────────────────


class TestFallbackChain:
    def test_direct_wins_when_reachable(self, root, session_key, session_cert):
        async def run():
            server = DirectChannelServer(ORG, session_key, session_cert)
            port = await server.start()
            try:
                result = await dial_peer(
                    org=ORG, root_pub=root.public_hex,
                    target_pub=session_key.public_hex,
                    direct_addrs=[f"ws://127.0.0.1:{port}"],
                )
                assert result.path == PATH_DIRECT
                assert result.attempts == []
                async with result.channel as channel:
                    await channel.send_message(b"zero middlemen")
                    assert await channel.recv_message() == b"zero middlemen"
            finally:
                await server.stop()
        asyncio.run(run())

    def test_direct_blocked_falls_to_peer_relay(self, root, session_key,
                                                session_cert, relay_key,
                                                relay_cert, unused_tcp_port):
        async def run():
            relay = PeerRelay(ORG, root.public_hex, relay_key, relay_cert)
            port = await relay.start()
            parker, task = await _park_node(port, root, session_key, session_cert)
            try:
                result = await dial_peer(
                    org=ORG, root_pub=root.public_hex,
                    target_pub=session_key.public_hex,
                    # simulated NAT: the announced address is a black hole
                    direct_addrs=[f"ws://127.0.0.1:{unused_tcp_port}"],
                    relays=[{"node": relay_key.public_hex,
                             "relay_url": f"ws://127.0.0.1:{port}"}],
                    attempt_timeout=1.0,
                )
                assert result.path == PATH_PEER_RELAY
                assert [a[0] for a in result.attempts] == [PATH_DIRECT]
                async with result.channel as channel:
                    await channel.send_message(b"one middleman, zero eyes")
                    assert await channel.recv_message() == b"one middleman, zero eyes"
            finally:
                await _stop_parker(parker, task)
                await relay.stop()
        asyncio.run(run())

    def test_unauthorized_relay_routed_around(self, root, session_key,
                                              session_cert, relay_key,
                                              relay_cert, imposter_cert):
        """An undelegated relay in the candidate list is refused and the
        chain moves on — refusal reroutes, never strands."""
        async def run():
            imposter = PeerRelay(ORG, root.public_hex, relay_key, imposter_cert)
            imposter_port = await imposter.start()
            genuine = PeerRelay(ORG, root.public_hex, relay_key, relay_cert)
            genuine_port = await genuine.start()
            parker, task = await _park_node(genuine_port, root, session_key,
                                            session_cert)
            try:
                result = await dial_peer(
                    org=ORG, root_pub=root.public_hex,
                    target_pub=session_key.public_hex,
                    relays=[f"ws://127.0.0.1:{imposter_port}",
                            f"ws://127.0.0.1:{genuine_port}"],
                    attempt_timeout=2.0,
                )
                assert result.path == PATH_PEER_RELAY
                assert result.via == f"ws://127.0.0.1:{genuine_port}"
                assert "RelayVerifyError" in result.attempts[0][2]
                await result.channel.close()
            finally:
                await _stop_parker(parker, task)
                await genuine.stop()
                await imposter.stop()
        asyncio.run(run())

    def test_everything_dead_raises_dial_error(self, root, session_key,
                                               unused_tcp_port):
        async def run():
            with pytest.raises(DialError) as info:
                await dial_peer(
                    org=ORG, root_pub=root.public_hex,
                    target_pub=session_key.public_hex,
                    direct_addrs=[f"ws://127.0.0.1:{unused_tcp_port}"],
                    attempt_timeout=0.5,
                )
            assert [a[0] for a in info.value.attempts] == [PATH_DIRECT]
        asyncio.run(run())


# ── discovery: the ledger names relays, hints locate them ────────────


class TestLedgerDiscovery:
    def test_relay_candidates_is_ledger_intersect_hints(self):
        """The relay:serve grant lives in the org authority ledger as a
        delegate event; candidates come from a REAL deterministic fold,
        never a config file."""
        sim = OrgSim(ORG)
        store = sim.store()
        relay_node, plain_node = KeyPair.generate(), KeyPair.generate()
        sim.delegate(store, child=relay_node,
                     scope=("relay:serve", "tunnel:serve"))
        sim.delegate(store, child=plain_node, scope=("tunnel:serve",))

        live_keys = build_live_keys(store.fold())
        assert "relay:serve" in live_keys[relay_node.public_hex]

        hints = [
            {"node": relay_node.public_hex, "addrs": [],
             "relay_url": "ws://relay.example:9420"},
            # holds the delegation but announces no relay endpoint
            {"node": relay_node.public_hex, "addrs": [], "relay_url": None},
            # announces a relay endpoint but the ledger never delegated it
            {"node": plain_node.public_hex, "addrs": [],
             "relay_url": "ws://rogue.example:9999"},
            # unknown key entirely
            {"node": "ff" * 32, "addrs": [], "relay_url": "ws://x.example:1"},
        ]
        candidates = relay_candidates(live_keys, hints)
        assert candidates == [{"node": relay_node.public_hex,
                               "relay_url": "ws://relay.example:9420"}]

    def test_revoked_delegation_drops_the_candidate(self):
        """Cascade (L4): revoking the ledger delegation removes the node
        from the candidate set on the next fold."""
        sim = OrgSim(ORG)
        store = sim.store()
        relay_node = KeyPair.generate()
        delegate_id = sim.delegate(store, child=relay_node,
                                   scope=("relay:serve",))
        hints = [{"node": relay_node.public_hex, "addrs": [],
                  "relay_url": "ws://relay.example:9420"}]

        assert relay_candidates(build_live_keys(store.fold()), hints)
        sim.emit(store, sim.root, {"type": "revoke", "target_event": delegate_id,
                                   "reason": "node decommissioned"})
        assert relay_candidates(build_live_keys(store.fold()), hints) == []
