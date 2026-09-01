"""D19 §3: the loopback control listener + supervisor.control() IPC.

The connector runs in a spawned subprocess, so the dashboard drives its
tunnel control ops over a loopback listener described by a ``.ctl`` file.
These pin that seam in isolation (stub connector, no real relay).
"""

from __future__ import annotations

import asyncio
import json
import os
import socket

import pytest

from tools.dashboard import link_serving
from tools.dashboard import link_serving_supervisor as sup


class _StubConnector:
    def __init__(self, reply=None, raise_conn=False, accepted_caps=()):
        self._reply = reply or {"ok": True, "token": "t" * 32}
        self._raise = raise_conn
        self.calls = []
        self.connected = asyncio.Event()
        self.accepted_caps = tuple(accepted_caps)

    async def control(self, op, args, timeout=10.0):
        self.calls.append((op, args))
        if self._raise:
            raise ConnectionError("no live tunnel to carry a control frame")
        return {**self._reply, "op": op}


async def _with_listener(connector, ctl_path, body):
    task = asyncio.create_task(
        link_serving._serve_control_listener(connector, ctl_path))
    # Wait for the descriptor to appear.
    for _ in range(200):
        if os.path.exists(ctl_path):
            break
        await asyncio.sleep(0.01)
    try:
        return await body()
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


def _roundtrip(port, payload):
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))


def test_listener_forwards_authorized_control(tmp_path):
    ctl = str(tmp_path / "serve.ctl")
    connector = _StubConnector(reply={"ok": True, "token": "z" * 32})

    async def body():
        descriptor = json.loads(open(ctl).read())
        reply = await asyncio.to_thread(
            _roundtrip, descriptor["port"],
            {"auth": descriptor["auth"], "op": "create-link",
             "args": {"target_uuid": "u", "target_type": "present"}})
        assert reply["ok"] is True
        assert reply["token"] == "z" * 32
        assert connector.calls == [
            ("create-link", {"target_uuid": "u", "target_type": "present"})]
        return reply

    asyncio.run(_with_listener(connector, ctl, body))
    # Descriptor removed on listener shutdown.
    assert not os.path.exists(ctl)


def test_listener_reports_local_serving_state_without_forwarding(tmp_path):
    ctl = str(tmp_path / "serve.ctl")
    connector = _StubConnector(accepted_caps=("host-lease/1", "dns-01/1"))

    async def body():
        descriptor = json.loads(open(ctl).read())
        request = {
            "auth": descriptor["auth"],
            "op": "connector-status",
            "args": {},
        }
        down = await asyncio.to_thread(
            _roundtrip, descriptor["port"], request
        )
        assert down["ok"] is True
        assert down["serving"] is False
        assert down["accepted_caps"] == ["host-lease/1", "dns-01/1"]
        connector.connected.set()
        up = await asyncio.to_thread(
            _roundtrip, descriptor["port"], request
        )
        assert up["ok"] is True
        assert up["serving"] is True
        assert up["accepted_caps"] == ["host-lease/1", "dns-01/1"]
        assert connector.calls == []

    asyncio.run(_with_listener(connector, ctl, body))


def test_listener_rejects_bad_auth(tmp_path):
    ctl = str(tmp_path / "serve.ctl")
    connector = _StubConnector()

    async def body():
        descriptor = json.loads(open(ctl).read())
        reply = await asyncio.to_thread(
            _roundtrip, descriptor["port"],
            {"auth": "wrong", "op": "create-link", "args": {}})
        assert reply["ok"] is False
        assert "auth rejected" in reply["error"]
        assert connector.calls == []  # never reached the connector

    asyncio.run(_with_listener(connector, ctl, body))


def test_listener_maps_no_tunnel(tmp_path):
    ctl = str(tmp_path / "serve.ctl")
    connector = _StubConnector(raise_conn=True)

    async def body():
        descriptor = json.loads(open(ctl).read())
        reply = await asyncio.to_thread(
            _roundtrip, descriptor["port"],
            {"auth": descriptor["auth"], "op": "revoke-link",
             "args": {"token": "t" * 32}})
        assert reply["ok"] is False
        assert reply["error_kind"] == "no-tunnel"

    asyncio.run(_with_listener(connector, ctl, body))


def test_supervisor_control_unavailable_without_provisioning(monkeypatch):
    monkeypatch.setattr(sup, "serve_cert_state", lambda org, **k: {"status": "missing"})
    with pytest.raises(sup.TunnelUnavailable) as exc:
        sup.control("org", "create-link", {})
    assert "provision serving" in str(exc.value)


def test_supervisor_control_unavailable_without_listener(monkeypatch, tmp_path):
    key_path = str(tmp_path / "serve.hex")
    monkeypatch.setattr(sup, "serve_cert_state",
                        lambda org, **k: {"status": "ok", "key_path": key_path})
    # No .ctl file next to the key → the connector is not running.
    with pytest.raises(sup.TunnelUnavailable) as exc:
        sup.control("org", "create-link", {})
    assert "not running" in str(exc.value)


def test_supervisor_control_reaches_a_live_listener(monkeypatch, tmp_path):
    key_path = str(tmp_path / "serve.hex")
    ctl = sup._control_path_for(key_path)
    monkeypatch.setattr(sup, "serve_cert_state",
                        lambda org, **k: {"status": "ok", "key_path": key_path})
    connector = _StubConnector(reply={"ok": True, "token": "q" * 32})

    async def body():
        # supervisor.control is blocking sockets — run it off the loop.
        return await asyncio.to_thread(
            sup.control, "org", "create-link",
            {"target_uuid": "u", "target_type": "present"})

    reply = asyncio.run(_with_listener(connector, ctl, body))
    assert reply["ok"] is True and reply["token"] == "q" * 32
    assert connector.calls[0][0] == "create-link"
