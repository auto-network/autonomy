"""The dashboard's wiring of the relay carrier, on a real relay with two
real outbound connectors: the responder serves offers with the fleet
runtime's own handler and authenticator, the initiator gets the same
``ViewerChannel`` the pull path consumes, and the probe proves it from one
machine against every other slot the relay reports."""

from __future__ import annotations

import asyncio
import contextlib
import socket
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest
import uvicorn

from tools.network import fleet_relay_carrier as carrier
from tools.network import fleet_roster
from tools.network.fleet_sync_channel import FleetAuthenticator
from tools.network.idkit import KeyPair, Subject, issue_cert
from tools.network.registry.app import create_app
from tools.network.registry.signing import sign_request
from tools.network.relaykit.connector import TunnelConnector
from tools.network.relaykit.fleet_stream_wire import CAP_FLEET_DIRECTED_STREAM as CAP

ORG = "77777777-7777-4777-8777-777777777777"
PERSONA = "ab" * 32


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def _live_registry(port: int):
    app = create_app(":memory:", base_url=f"http://127.0.0.1:{port}", secure_cookies=False)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and thread.is_alive() and time.time() < deadline:
        time.sleep(0.02)
    assert server.started
    try:
        yield app
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _register(port, root):
    resp = httpx.post(f"http://127.0.0.1:{port}/v1/orgs", json=sign_request(
        root, "POST", "/v1/orgs",
        {"org_uuid": ORG, "root_pub": root.public_hex, "recovery_policy": "none"},
        ts=int(time.time())), timeout=10)
    assert resp.status_code == 201, resp.text


class Runtime:
    """What ``fleet_relay_sync.connector_runtime`` exposes to the carrier:
    a scheduler with the fleet authenticator and the request handler, or
    None while unarmed."""

    def __init__(self, authenticator=None):
        self.locked_refusals = 0
        self.first_locked_refusal_at = None
        self.served = []
        self.scheduler = None
        if authenticator is not None:
            self.arm(authenticator)

    def arm(self, authenticator):
        async def _handle(token, message, client_pub, *, telemetry_channel="direct", **extra):
            self.served.append((client_pub, telemetry_channel, message))
            return b"echo:" + message

        self.scheduler = SimpleNamespace(
            authenticator=authenticator, _handle=_handle, _org_channel_for_genesis=None,
        )


async def _machine(port, root, runtime):
    serve_key, machine_key = KeyPair.generate(), KeyPair.generate()
    now = int(time.time())
    cert = issue_cert(root, serve_key.public_hex, scope=("tunnel:serve",), org=ORG,
                      subject=Subject("persona", PERSONA),
                      not_before=now - 100, not_after=now + 30 * 86400)
    connector = TunnelConnector(
        f"ws://127.0.0.1:{port}", ORG, serve_key, cert, machine_key=machine_key,
        caps=(CAP,), fleet_stream_offer=carrier.fleet_stream_offer_handler(runtime),
        min_backoff=0.05, max_backoff=0.2,
    )
    task = asyncio.create_task(connector.run())
    await asyncio.wait_for(connector.connected.wait(), 10)
    return connector, task


async def _stop(connector, task):
    connector.stop()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


def test_probe_and_authenticated_channel_over_the_relay():
    root = KeyPair.generate()
    port = _free_port()
    with _live_registry(port) as app:
        asyncio.run(_scenario(root, port, app.state.directed_streams))


async def _scenario(root, port, broker):
    _register(port, root)
    fleet_a, fleet_b = KeyPair.generate(), KeyPair.generate()
    roster = (
        fleet_roster.enroll(root, machine_pub=fleet_a.public_hex, seq=0),
        fleet_roster.enroll(root, machine_pub=fleet_b.public_hex, seq=0),
    )
    auth_a = FleetAuthenticator(fleet_a, root_pub=root.public_hex, roster_entries=lambda: roster)
    auth_b = FleetAuthenticator(fleet_b, root_pub=root.public_hex, roster_entries=lambda: roster)
    runtime_a, runtime_b = Runtime(auth_a), Runtime()          # B starts UNARMED
    a, task_a = await _machine(port, root, runtime_a)
    b, task_b = await _machine(port, root, runtime_b)
    try:
        # The relay lists both slots for this org, with the capability.
        slots = await carrier.list_org_slots(a)
        assert {s["machine"] for s in slots} == {a.serving_slot["machine"], b.serving_slot["machine"]}
        assert all(CAP in s["caps"] for s in slots)

        # Unarmed B refuses the offer and counts it like a locked pull.
        probe = await carrier.relay_probe(a, runtime_a, timeout=3)
        assert probe["ok"] is False
        (record,) = probe["results"]
        assert record["machine"] == b.serving_slot["machine"] and record["ok"] is False
        assert runtime_b.locked_refusals == 1
        await asyncio.sleep(0.1)
        assert broker.snapshot()["pairs"] == 0

        # Armed B: the probe pairs, runs the real fleet handshake and names
        # the durable peer it proved — not the serving slot.
        runtime_b.arm(auth_b)
        probe = await carrier.relay_probe(a, runtime_a, timeout=5)
        assert probe["ok"] is True, probe
        (record,) = probe["results"]
        assert record["durable_peer"] == fleet_b.public_hex
        assert record["handshake_ms"] >= 0
        assert probe["own_durable"] == fleet_a.public_hex

        # The initiator's channel is the pull path's ViewerChannel; the
        # responder is the runtime's own handler, on the relay channel.
        channel = await carrier.fleet_relay_connect(
            a, b.serving_slot["persona_pub"], b.serving_slot["machine"],
            authenticator=auth_a, expected_machine_pub=fleet_b.public_hex,
            claimed_machine_pub=fleet_a.public_hex,
        )
        await channel.send_message(b"pull?")
        assert await asyncio.wait_for(channel.recv_message(), 5) == b"echo:pull?"
        assert runtime_b.served[-1][:2] == (fleet_a.public_hex, "relay")
        await channel.close()

        # A wrong expected peer is a refused handshake, never a false pass.
        with pytest.raises(Exception):
            await carrier.fleet_relay_connect(
                a, b.serving_slot["persona_pub"], b.serving_slot["machine"],
                authenticator=auth_a, expected_machine_pub="cc" * 32, timeout=5,
            )
        deadline = time.time() + 5
        while time.time() < deadline and broker.snapshot()["pairs"]:
            await asyncio.sleep(0.05)
        assert broker.snapshot()["pairs"] == 0
    finally:
        await _stop(a, task_a)
        await _stop(b, task_b)
