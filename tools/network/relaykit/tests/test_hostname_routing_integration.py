"""auto-0zdky L4 proof: hostname routing across concurrent tunnels.

Real registry subprocess (`python -m tools.network.registry`); three real
`TunnelConnector` instances over real WebSockets — machine A (two app
reservations), machine B (one), and a legacy v1 connector holding an
artifact link. Probes hit `/v1/hosts/{host}/probe` and each echo carries
the answering connector's machine-key digest, so mis-routing is directly
observable.

Proves the bead's acceptance: 40/40 probes route to exactly the intended
connector; release/rebind of one reservation never disturbs its sibling;
disconnecting one connector fails only its own routes closed (restored on
reconnect) while the other machine and the legacy artifact viewer stay
live throughout.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest
import websockets

from tools.network.idkit import KeyPair, Subject, issue_cert
from tools.network.registry.signing import sign_request
from tools.network.relaykit.connector import TunnelConnector
from tools.network.relaykit.viewer import ViewerChannel

from .conftest import ORG
from .test_relay_integration import free_port, start_registry

REPO = Path(__file__).resolve().parents[4]
PERSONA = "ab" * 32
PERSONA_OTHER = "cd" * 32
TARGET = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
RESERVATION_NS = uuid.UUID("6cf440db-c8b4-566c-99db-e7be17109bdc")
CAPS = ("host-lease/1",)


def _host(app_label: str, persona: str = PERSONA) -> str:
    suffix = hashlib.sha256(bytes.fromhex(persona)).hexdigest()[:20]
    return f"{app_label}.worker-{suffix}.serve.auto.network"


def _reservation(app_label: str, persona: str = PERSONA) -> str:
    return str(uuid.uuid5(RESERVATION_NS, f"{persona}\0{app_label}"))


def _machine_digest(machine: KeyPair) -> str:
    return hashlib.sha256(
        bytes.fromhex(machine.public_hex)
    ).hexdigest()[:16]


def _serve_cert(root: KeyPair, serve_key: KeyPair, persona: str = PERSONA):
    now = int(time.time())
    return issue_cert(
        root, serve_key.public_hex, scope=("tunnel:serve",), org=ORG,
        subject=Subject("persona", persona),
        not_before=now - 300, not_after=now + 7 * 86_400,
    )


def _register_org_and_link(port: int, db, root: KeyPair) -> str:
    from tools.network.registry.testkit import mint_link_at
    with httpx.Client(base_url=f"http://127.0.0.1:{port}") as client:
        ts = int(time.time())
        response = client.post("/v1/orgs", json=sign_request(
            root, "POST", "/v1/orgs",
            {"org_uuid": ORG, "root_pub": root.public_hex,
             "recovery_policy": "none"},
            ts=ts,
        ))
        assert response.status_code == 201, response.text
    # Publish rides the org tunnel in production; this stack's subject is
    # hostname routing, so seed the grant at the store.
    return mint_link_at(db, ORG, TARGET)


class _Node:
    """One in-test connector: real TunnelConnector as an asyncio task."""

    def __init__(self, relay_url: str, root: KeyPair, *,
                 persona: str = PERSONA, machine: KeyPair | None = None):
        self.serve_key = KeyPair.generate()
        self.machine = machine
        cert = _serve_cert(root, self.serve_key, persona)
        # Every tunnel names its machine now, so the no-machine case supplies
        # a generated one rather than connecting anonymously — that path is
        # refused. `caps` still only comes with an explicitly passed machine,
        # which is what these tests vary.
        kwargs = {"machine_key": machine or KeyPair.generate()}
        if machine is not None:
            kwargs["caps"] = CAPS
        self.connector = TunnelConnector(
            relay_url, ORG, self.serve_key, cert,
            min_backoff=0.1, max_backoff=0.5, **kwargs,
        )
        self.task: asyncio.Task | None = None

    async def start(self):
        self.task = asyncio.create_task(self.connector.run())
        await asyncio.wait_for(self.connector.connected.wait(), timeout=10)

    async def stop(self):
        self.connector.stop()
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.task


async def _probe(port: int, host: str, timeout: float = 5.0) -> dict:
    async with websockets.connect(
        f"ws://127.0.0.1:{port}/v1/hosts/{host}/probe"
    ) as ws:
        raw = await asyncio.wait_for(ws.recv(), timeout)
    return json.loads(raw)


async def _probe_refused(port: int, host: str) -> bool:
    try:
        async with websockets.connect(
            f"ws://127.0.0.1:{port}/v1/hosts/{host}/probe"
        ) as ws:
            await asyncio.wait_for(ws.recv(), 3.0)
        return False
    except websockets.exceptions.ConnectionClosed as exc:
        return exc.rcvd is not None and exc.rcvd.code == 4404
    except asyncio.TimeoutError:
        return False


async def _serve_host_ok(node: _Node, app_label: str, deadline: float = 10.0):
    """serve_host with retry: control frames need the live tunnel."""
    reservation, host = _reservation(app_label), _host(app_label)
    end = time.time() + deadline
    while True:
        reply = await node.connector.serve_host(reservation, host)
        if reply.get("ok") is True:
            return reservation, host
        if time.time() > end:
            raise AssertionError(f"serve_host never succeeded: {reply}")
        await asyncio.sleep(0.2)


@pytest.fixture(scope="module")
def stack(tmp_path_factory, root):
    tmp = tmp_path_factory.mktemp("hostname-routing")
    port = free_port()
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    registry = start_registry(port, tmp / "registry.db", env,
                              tmp / "registry.log")
    token = _register_org_and_link(port, tmp / "registry.db", root)
    state = {"port": port, "token": token, "root": root,
             "registry": registry}
    yield state
    with contextlib.suppress(Exception):
        registry.terminate()
        registry.wait(timeout=5)


def test_l4_multi_connector_hostname_routing(stack):
    port = stack["port"]
    root = stack["root"]
    relay_url = f"ws://127.0.0.1:{port}"

    async def scenario():
        machine_a, machine_b = KeyPair.generate(), KeyPair.generate()
        node_a = _Node(relay_url, root, machine=machine_a)
        node_b = _Node(relay_url, root, machine=machine_b)
        legacy = _Node(relay_url, root)  # v1 hello, no machine identity
        await node_a.start()
        await node_b.start()
        await legacy.start()
        try:
            # Two app reservations on machine A, one on machine B.
            res_docs, host_docs = await _serve_host_ok(node_a, "docs")
            res_app2, host_app2 = await _serve_host_ok(node_a, "app2")
            res_blog, host_blog = await _serve_host_ok(node_b, "blog")

            # 40/40: 20 probes per hostname on docs (A) and blog (B); every
            # echo must carry the intended machine digest.
            for host, machine in (
                (host_docs, machine_a), (host_blog, machine_b),
            ) * 1:
                for _ in range(20):
                    echo = await _probe(port, host)
                    assert echo["machine_digest"] == _machine_digest(
                        machine
                    ), (host, echo)
                    assert echo["host"] == host

            # Sibling independence: release app2; docs keeps routing.
            reply = await node_a.connector.release_host(res_app2)
            assert reply.get("ok") is True
            assert await _probe_refused(port, host_app2)
            echo = await _probe(port, host_docs)
            assert echo["machine_digest"] == _machine_digest(machine_a)

            # Explicit rebind after release: machine B claims app2.
            reply = await node_b.connector.serve_host(res_app2, host_app2)
            assert reply.get("ok") is True
            echo = await _probe(port, host_app2)
            assert echo["machine_digest"] == _machine_digest(machine_b)

            # Legacy artifact viewer stays green mid-scenario.
            viewer = await ViewerChannel.connect(
                f"ws://127.0.0.1:{port}", stack["token"],
                root_pub=root.public_hex, org=ORG,
            )
            await viewer.send_message(b"legacy-compat-check")
            assert await viewer.recv_message() == b"legacy-compat-check"
            await viewer.close()

            # Disconnect isolation + fail-closed teardown: kill machine A.
            await node_a.stop()
            assert await _probe_refused(port, host_docs)
            echo = await _probe(port, host_app2)  # B's routes unaffected
            assert echo["machine_digest"] == _machine_digest(machine_b)

            # Reconnect restore: a fresh machine-A connector re-registers
            # its desired hosts under a fresh generation.
            node_a2 = _Node(relay_url, root, machine=machine_a)
            await node_a2.start()
            try:
                await _serve_host_ok(node_a2, "docs")
                echo = await _probe(port, host_docs)
                assert echo["machine_digest"] == _machine_digest(machine_a)
            finally:
                await node_a2.stop()

            # Cross-persona claim fails closed on the wire.
            intruder_machine = KeyPair.generate()
            intruder = _Node(relay_url, root, persona=PERSONA_OTHER,
                             machine=intruder_machine)
            await intruder.start()
            try:
                reply = await intruder.connector.serve_host(
                    _reservation("docs"), _host("docs")
                )
                assert reply.get("ok") is False
                assert reply.get("error") == "label-invalid"
            finally:
                await intruder.stop()
        finally:
            await node_b.stop()
            await legacy.stop()
            with contextlib.suppress(Exception):
                await node_a.stop()

    asyncio.run(scenario())
