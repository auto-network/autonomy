"""fleet-directed-stream/1 end to end: a REAL relay (the registry app under
uvicorn), two REAL ``TunnelConnector`` instances that can only dial OUT to
it, and the REAL fleet handshake and record layer running over the pair.

This is the two-outbound-leg proof the carrier contract requires, at the
size CI can afford; the numbers are env-overridable for a soak
(AUTONOMY_FLEET_E2E_MIB, AUTONOMY_FLEET_E2E_STALL_S).

Topology::

    connector A (in-process) ──ws──▶ relay ◀──ws── connector B (in-process)
                                 pair A→B

What is proven:

* exact routing: A reaches B's slot; a machine that is not connected is a
  typed refusal, not a fallback;
* authentication is end to end: ``FleetAuthenticator`` runs over the
  endpoint with the session bound to the pair; the relay never holds a key;
* byte-exact bulk through the record layer, both directions;
* slow-consumer isolation on ONE tunnel pair: a stalled reader stops its
  own sender at zero credit while a second pair between the same two
  machines keeps flowing, relay custody stays under the offered window, and
  nothing is reset or discarded; resuming delivers every byte exactly;
* tunnel replacement: B reconnecting under the same slot ends A's old
  pair with the tunnel-loss code, and a new pair to the replacement works;
* resources return to baseline.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import socket
import threading
import time
from contextlib import contextmanager

import httpx
import pytest
import uvicorn

from tools.network import fleet_roster
from tools.network.fleet_sync_channel import (
    FleetAuthenticator,
    authenticate_fleet_transport,
    serve_fleet_transport,
)
from tools.network.idkit import KeyPair, Subject, issue_cert
from tools.network.registry.app import create_app
from tools.network.registry.signing import sign_request
from tools.network.relaykit.connector import TunnelConnector
from tools.network.relaykit.fleet_stream import FleetStreamClosed
from tools.network.relaykit.fleet_stream_wire import (
    CAP_FLEET_DIRECTED_STREAM as CAP,
    FLEET_STREAM_WINDOW_BYTES,
)
from tools.network.relaykit.stream_wire import RESET_TUNNEL_LOSS

ORG = "77777777-7777-4777-8777-777777777777"
PERSONA = "ab" * 32
MIB = int(os.environ.get("AUTONOMY_FLEET_E2E_MIB", "4"))
STALL_S = float(os.environ.get("AUTONOMY_FLEET_E2E_STALL_S", "1.0"))
MESSAGE = 100 * 1024


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def _live_registry(port: int):
    app = create_app(":memory:", base_url=f"http://127.0.0.1:{port}",
                     secure_cookies=False)
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and thread.is_alive() and time.time() < deadline:
        time.sleep(0.02)
    assert server.started, "registry did not start"
    try:
        yield app
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _register(port: int, root: KeyPair) -> None:
    resp = httpx.post(
        f"http://127.0.0.1:{port}/v1/orgs",
        json=sign_request(root, "POST", "/v1/orgs",
                          {"org_uuid": ORG, "root_pub": root.public_hex,
                           "recovery_policy": "none"},
                          ts=int(time.time())),
        timeout=10,
    )
    assert resp.status_code == 201, resp.text


class Machine:
    """One dashboard: a serving identity for the tunnel hello (what the
    relay slots by) and a DURABLE fleet identity for the fleet handshake
    (what the roster names). Different keys, as in production."""

    def __init__(self, root: KeyPair, port: int, *, serving_key=None):
        self.port = port
        self.serve_key = KeyPair.generate()
        now = int(time.time())
        self.cert = issue_cert(
            root, self.serve_key.public_hex, scope=("tunnel:serve",), org=ORG,
            subject=Subject("persona", PERSONA),
            not_before=now - 100, not_after=now + 30 * 86400,
        )
        self.serving_machine = serving_key or KeyPair.generate()
        self.fleet_key = KeyPair.generate()
        self.connector = None
        self.task = None
        self.offers = []

    @property
    def slot(self) -> str:
        return self.serving_machine.public_hex

    async def start(self, *, accept=True):
        async def on_offer(endpoint):
            self.offers.append(endpoint)
            return accept

        self.connector = TunnelConnector(
            f"ws://127.0.0.1:{self.port}", ORG, self.serve_key, self.cert,
            machine_key=self.serving_machine, caps=(CAP,),
            fleet_stream_offer=on_offer,
            min_backoff=0.05, max_backoff=0.2,
        )
        self.task = asyncio.create_task(self.connector.run())
        await asyncio.wait_for(self.connector.connected.wait(), timeout=10)
        assert CAP in self.connector.accepted_caps
        return self

    async def stop(self):
        if self.connector is None:
            return
        self.connector.stop()
        self.task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self.task
        self.connector = None


def _authenticator(root: KeyPair, machine: Machine, roster) -> FleetAuthenticator:
    return FleetAuthenticator(
        machine.fleet_key, root_pub=root.public_hex, roster_entries=lambda: roster,
    )


async def _serve_echo(endpoint, authenticator):
    """The destination side: the real responder over the pair."""
    async def handler(channel_token, message, client_pub):
        return message

    with contextlib.suppress(FleetStreamClosed, ConnectionError):
        await serve_fleet_transport(
            token=endpoint.session, recv=endpoint.recv, send=endpoint.send,
            handler=handler, authenticator=authenticator,
            close=lambda **kw: endpoint.close(),
        )


async def _settled(broker, predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate(broker.snapshot()):
            return broker.snapshot()
        await asyncio.sleep(0.02)
    return broker.snapshot()


def test_two_outbound_legs_carry_an_authenticated_fleet_channel():
    root = KeyPair.generate()
    port = _free_port()
    with _live_registry(port) as app:
        broker = app.state.directed_streams
        asyncio.run(_scenario(root, port, broker))


async def _scenario(root, port, broker):
    _register(port, root)
    a = await Machine(root, port).start()
    b = await Machine(root, port).start()
    roster = (
        fleet_roster.enroll(root, machine_pub=a.fleet_key.public_hex, seq=0),
        fleet_roster.enroll(root, machine_pub=b.fleet_key.public_hex, seq=0),
    )
    auth_a, auth_b = _authenticator(root, a, roster), _authenticator(root, b, roster)
    servers = []
    try:
        # -- 1. exact routing: a slot nobody holds is a typed refusal ------
        with pytest.raises(ConnectionError, match="destination-slot-absent"):
            await a.connector.fleet_streams.open(PERSONA, "cc" * 32)
        assert broker.snapshot()["pairs"] == 0

        # -- 2. the pair, then the fleet handshake end to end -------------
        endpoint = await a.connector.fleet_streams.open(
            PERSONA, b.slot, claimed_machine_pub=a.fleet_key.public_hex)
        accepted = await asyncio.wait_for(b.connector.fleet_streams.accepted.get(), 5)
        assert accepted.pair_id == endpoint.pair_id
        assert accepted.session == endpoint.session
        assert accepted.claimed_machine_pub == a.fleet_key.public_hex
        assert accepted.peer_machine == a.slot and endpoint.peer_machine == b.slot
        servers.append(asyncio.create_task(_serve_echo(accepted, auth_b)))
        channel = await asyncio.wait_for(authenticate_fleet_transport(
            endpoint, authenticator=auth_a,
            expected_machine_pub=b.fleet_key.public_hex, session=endpoint.session,
        ), 10)

        # -- 3. byte-exact bulk, both directions, through the record layer --
        total = MIB * 1024 * 1024
        digest_out, digest_back = hashlib.sha256(), hashlib.sha256()
        sent = 0
        started = time.monotonic()
        while sent < total:
            chunk = os.urandom(min(MESSAGE, total - sent))
            digest_out.update(chunk)
            await channel.send_message(chunk)
            echoed = await asyncio.wait_for(channel.recv_message(), 30)
            digest_back.update(echoed)
            sent += len(chunk)
        elapsed = time.monotonic() - started
        assert digest_out.digest() == digest_back.digest()
        print(f"\n[fleet e2e] {2 * MIB} MiB through the relay in {elapsed:.2f}s "
              f"({2 * MIB / elapsed:.1f} MiB/s round trip)")
        snap = broker.snapshot()
        assert snap["pairs"] == 1 and snap["queued_bytes"] == 0

        # -- 4. slow-consumer isolation on the same two tunnels ------------
        stalled = await a.connector.fleet_streams.open(PERSONA, b.slot)
        stalled_peer = await asyncio.wait_for(b.connector.fleet_streams.accepted.get(), 5)
        # Three windows' worth, in messages under the endpoint maximum.
        messages = [os.urandom(FLEET_STREAM_WINDOW_BYTES * 3 // 4) for _ in range(4)]

        async def send_all():
            for message in messages:
                await stalled.send(message)

        sender = asyncio.create_task(send_all())
        await asyncio.sleep(STALL_S)                            # B never reads
        assert not sender.done()                                # blocked at zero credit
        assert not stalled.closed.is_set() and not stalled_peer.closed.is_set()
        snap = broker.snapshot()
        assert snap["pairs"] == 2
        assert snap["queued_bytes"] <= FLEET_STREAM_WINDOW_BYTES
        assert snap["outstanding_bytes"] <= 2 * FLEET_STREAM_WINDOW_BYTES  # both pairs
        # The healthy pair keeps its full service meanwhile.
        for _ in range(5):
            probe = os.urandom(MESSAGE)
            await channel.send_message(probe)
            assert await asyncio.wait_for(channel.recv_message(), 10) == probe
        # Resume: every byte arrives exactly, in order, nothing was reset.
        received = [await asyncio.wait_for(stalled_peer.recv(), 10) for _ in messages]
        await asyncio.wait_for(sender, 10)
        assert received == messages
        await stalled.close()
        await _settled(broker, lambda s: s["pairs"] == 1)

        # -- 5. tunnel replacement ends the old pair with code 6 ----------
        waiting = asyncio.create_task(endpoint.recv())
        b2 = await Machine(root, port, serving_key=b.serving_machine).start()
        with pytest.raises(FleetStreamClosed) as excinfo:
            await asyncio.wait_for(waiting, 10)
        assert excinfo.value.code == RESET_TUNNEL_LOSS
        assert excinfo.value.pair_id == endpoint.pair_id
        await _settled(broker, lambda s: s["pairs"] == 0)
        # The replacement is the slot now: a fresh pair reaches it.
        fresh = await a.connector.fleet_streams.open(PERSONA, b2.slot)
        fresh_peer = await asyncio.wait_for(b2.connector.fleet_streams.accepted.get(), 5)
        assert fresh.pair_id != endpoint.pair_id
        await fresh.send(b"hello replacement")
        assert await asyncio.wait_for(fresh_peer.recv(), 5) == b"hello replacement"
        await fresh.close()
        await b2.stop()

        # -- 6. baseline ------------------------------------------------------
        final = await _settled(broker, lambda s: s["pairs"] == 0 and s["schedulers"] == 0)
        assert final == {"pairs": 0, "schedulers": 0, "queued_bytes": 0,
                         "queued_slots": 0, "outstanding_bytes": 0}, final
    finally:
        for task in servers:
            task.cancel()
        await a.stop()
        await b.stop()
