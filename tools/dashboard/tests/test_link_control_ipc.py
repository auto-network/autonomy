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
        # What connector-status reports beside readiness (auto-fh2nv /
        # auto-e38g4): the relay slot and where it is filed.
        self.serving_slot = {"persona_pub": "00" * 32, "machine": "bb" * 32}
        self.relay_base = "wss://registry.invalid"
        self.org = "org-uuid"

    async def control(self, op, args, timeout=10.0):
        self.calls.append((op, args))
        if self._raise:
            raise ConnectionError("no live tunnel to carry a control frame")
        return {**self._reply, "op": op}

    async def serve_host(self, reservation, host, machine=None):
        args = {"reservation": reservation, "host": host}
        if machine is not None:
            args["machine"] = machine  # the auto-nh1po pin, when declared
        self.calls.append(("serve-host", args))
        return {"ok": True, "lease": {"generation": 1}}

    async def release_host(self, reservation):
        self.calls.append(("release-host", {"reservation": reservation}))
        return {"ok": True}

    @property
    def host_leases(self):
        self.calls.append(("host-leases", {}))
        return {"reservation-id": {"host": "app.example", "leased": True,
                                   "expires_at": 4102444800}}


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
        assert len(down["connector_instance"]) == 32
        assert down["accepted_caps"] == ["host-lease/1", "dns-01/1"]
        connector.connected.set()
        up = await asyncio.to_thread(
            _roundtrip, descriptor["port"], request
        )
        assert up["ok"] is True
        assert up["serving"] is True
        assert up["connector_instance"] == down["connector_instance"]
        assert up["accepted_caps"] == ["host-lease/1", "dns-01/1"]
        assert connector.calls == []

    asyncio.run(_with_listener(connector, ctl, body))


def test_listener_enrolls_host_in_connector_lease_keeper(tmp_path):
    ctl = str(tmp_path / "serve.ctl")
    connector = _StubConnector()

    async def body():
        descriptor = json.loads(open(ctl).read())
        reply = await asyncio.to_thread(
            _roundtrip, descriptor["port"], {
                "auth": descriptor["auth"], "op": "serve-host",
                "args": {"reservation": "reservation-id", "host": "app.example"},
            })
        assert reply["ok"] is True
        assert connector.calls == [("serve-host", {
            "reservation": "reservation-id", "host": "app.example",
        })]

    asyncio.run(_with_listener(connector, ctl, body))


def test_listener_reports_host_leases_read_only(tmp_path):
    """auto-q5xni: per-link status asks the process that holds the leases
    instead of inferring them; the op forwards nothing to the registry."""
    ctl = str(tmp_path / "serve.ctl")
    connector = _StubConnector()

    async def body():
        descriptor = json.loads(open(ctl).read())
        reply = await asyncio.to_thread(
            _roundtrip, descriptor["port"], {
                "auth": descriptor["auth"], "op": "host-leases", "args": {},
            })
        assert reply == {"ok": True, "leases": {
            "reservation-id": {"host": "app.example", "leased": True,
                               "expires_at": 4102444800},
        }}
        assert connector.calls == [("host-leases", {})]

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
    work_base = str(tmp_path / "serve-org-child")
    monkeypatch.setattr(sup, "serve_cert_state",
                        lambda org, **k: {"status": "ok", "work_base": work_base})
    # No .ctl file in the working directory → the connector is not running.
    with pytest.raises(sup.TunnelUnavailable) as exc:
        sup.control("org", "create-link", {})
    assert "not running" in str(exc.value)


def test_supervisor_control_reaches_a_live_listener(monkeypatch, tmp_path):
    work_base = str(tmp_path / "serve-org-child")
    ctl = sup._control_path_for(work_base)
    monkeypatch.setattr(sup, "serve_cert_state",
                        lambda org, **k: {"status": "ok", "work_base": work_base})
    connector = _StubConnector(reply={"ok": True, "token": "q" * 32})

    async def body():
        # supervisor.control is blocking sockets — run it off the loop.
        return await asyncio.to_thread(
            sup.control, "org", "create-link",
            {"target_uuid": "u", "target_type": "present"})

    reply = asyncio.run(_with_listener(connector, ctl, body))
    assert reply["ok"] is True and reply["token"] == "q" * 32
    assert connector.calls[0][0] == "create-link"
