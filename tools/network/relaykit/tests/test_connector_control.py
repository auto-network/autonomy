"""D19 §3: TunnelConnector.control() round-trips against a live registry.

The bead's acceptance 1-2 at the connector seam: a real ``TunnelConnector``
dials a real registry relay over a WebSocket, sends ``create-link`` /
``revoke-link`` control frames, and correlates the replies. Cross-org
isolation and the no-tunnel failure are asserted here too.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from contextlib import contextmanager

import httpx
import pytest
import uvicorn

from tools.network.idkit import KeyPair, Subject, issue_cert
from tools.network.registry.app import create_app
from tools.network.registry.signing import sign_request
from tools.network.relaykit.connector import (
    TunnelConnector,
    TunnelProtocolVersionError,
)
from tools.network.relaykit.hello import HELLO_VERSION

TARGET = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


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
        yield
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _register(port: int, root: KeyPair, org: str) -> None:
    resp = httpx.post(
        f"http://127.0.0.1:{port}/v1/orgs",
        json=sign_request(root, "POST", "/v1/orgs",
                          {"org_uuid": org, "root_pub": root.public_hex,
                           "recovery_policy": "none"},
                          ts=int(time.time())),
        timeout=10,
    )
    assert resp.status_code == 201, resp.text


def _serve_cert(root: KeyPair, serve_key: KeyPair, org: str):
    now = int(time.time())
    return issue_cert(
        root, serve_key.public_hex, scope=("tunnel:serve",), org=org,
        subject=Subject("persona", "ab" * 32),
        not_before=now - 100, not_after=now + 30 * 86400,
    )


async def _connected_connector(port: int, root: KeyPair, org: str):
    serve_key = KeyPair.generate()
    cert = _serve_cert(root, serve_key, org)
    connector = TunnelConnector(
        f"ws://127.0.0.1:{port}", org, serve_key, cert,
        min_backoff=0.05, max_backoff=0.2,
    )
    task = asyncio.create_task(connector.run())
    await asyncio.wait_for(connector.connected.wait(), timeout=10)
    return connector, task


async def _stop(connector, task):
    connector.stop()
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


def test_control_create_and_revoke_roundtrip():
    org = "11111111-1111-4111-8111-111111111111"
    root = KeyPair.generate()
    port = _free_port()

    async def run():
        _register(port, root, org)
        connector, task = await _connected_connector(port, root, org)
        try:
            created = await connector.control(
                "create-link",
                {"target_uuid": TARGET, "target_type": "present"})
            assert created["ok"] is True
            token = created["token"]
            assert created["url"].endswith(f"/l/{token}")
            # The link resolves an envelope over HTTP.
            env = httpx.get(f"http://127.0.0.1:{port}/v1/links/{token}/envelope")
            assert env.status_code == 200

            revoked = await connector.control("revoke-link", {"token": token})
            assert revoked["ok"] is True and revoked["token"] == token
            gone = httpx.get(f"http://127.0.0.1:{port}/v1/links/{token}/envelope")
            assert gone.status_code == 404
        finally:
            await _stop(connector, task)

    with _live_registry(port):
        asyncio.run(run())


def test_control_cross_org_revoke_refused():
    org_a = "11111111-1111-4111-8111-111111111111"
    org_b = "22222222-2222-4222-8222-222222222222"
    root_a, root_b = KeyPair.generate(), KeyPair.generate()
    port = _free_port()

    async def run():
        _register(port, root_a, org_a)
        _register(port, root_b, org_b)
        ca, ta = await _connected_connector(port, root_a, org_a)
        cb, tb = await _connected_connector(port, root_b, org_b)
        try:
            token = (await ca.control(
                "create-link",
                {"target_uuid": TARGET, "target_type": "present"}))["token"]
            reply = await cb.control("revoke-link", {"token": token})
            assert reply["ok"] is False
            assert "another org" in reply["error"]
            # A's link still resolves.
            env = httpx.get(f"http://127.0.0.1:{port}/v1/links/{token}/envelope")
            assert env.status_code == 200
        finally:
            await _stop(ca, ta)
            await _stop(cb, tb)

    with _live_registry(port):
        asyncio.run(run())


def test_control_without_tunnel_raises():
    root = KeyPair.generate()
    connector = TunnelConnector(
        "ws://127.0.0.1:1", "11111111-1111-4111-8111-111111111111",
        root, _serve_cert(root, root, "11111111-1111-4111-8111-111111111111"))

    async def run():
        with pytest.raises(ConnectionError):
            await connector.control("create-link", {"target_uuid": TARGET,
                                                    "target_type": "present"})

    asyncio.run(run())


class _HelloSocket:
    def __init__(self, reply):
        self.reply = reply
        self.sent = []

    async def send(self, value):
        self.sent.append(value)

    async def recv(self):
        return json.dumps(self.reply)


def _connector_for_handshake():
    org = "11111111-1111-4111-8111-111111111111"
    root = KeyPair.generate()
    child = KeyPair.generate()
    return TunnelConnector(
        "ws://registry.invalid", org, child,
        _serve_cert(root, child, org),
    )


@pytest.mark.parametrize(
    ("reply", "remote"),
    [
        ({"ok": True}, None),  # old registry: acknowledgement has no version
        ({"ok": True, "v": HELLO_VERSION + 1}, HELLO_VERSION + 1),
        ({
            "ok": False,
            "error": {
                "code": "protocol_version_mismatch",
                "connector_version": HELLO_VERSION,
                "registry_version": HELLO_VERSION + 1,
            },
        }, HELLO_VERSION + 1),
    ],
)
def test_connector_rejects_missing_or_unequal_registry_version(reply, remote):
    connector = _connector_for_handshake()
    socket = _HelloSocket(reply)

    async def run():
        with pytest.raises(TunnelProtocolVersionError) as mismatch:
            await connector._handshake(socket)
        assert mismatch.value.local_version == HELLO_VERSION
        assert mismatch.value.remote_version == remote
        assert f"connector={HELLO_VERSION}" in str(mismatch.value)
        assert "registry=" in str(mismatch.value)

    asyncio.run(run())
    assert len(socket.sent) == 1


def test_connector_accepts_exact_registry_protocol_version():
    connector = _connector_for_handshake()
    socket = _HelloSocket({
        "ok": True,
        "v": HELLO_VERSION,
        # Diagnostics may grow independently; build identity is not authority.
        "build": "different-commit-is-irrelevant",
    })
    asyncio.run(connector._handshake(socket))
    assert len(socket.sent) == 1
