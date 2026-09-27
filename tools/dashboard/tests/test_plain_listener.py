"""The plain-HTTP listener: same worker, second socket, no TLS.

Covers the three claims the module makes: the supervisor binds the socket once
and every worker peels it back off the inherited list; a worker without a
supervisor binds its own; and the dashboard lifespan starts it before the ready
marker and stops it on shutdown.
"""

from __future__ import annotations

import asyncio
import socket
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from tools.dashboard import plain_listener as pl
from tools.dashboard import worker_handoff as wh


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def _hello(scope, receive, send):
    if scope["type"] != "http":
        return
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b"plain " + scope["path"].encode()})


def _get(url: str, timeout: float = 5.0) -> tuple[int, bytes]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.status, response.read()


def _refused(url: str) -> bool:
    try:
        urllib.request.urlopen(url, timeout=2)
    except urllib.error.URLError as exc:
        return isinstance(exc.reason, ConnectionRefusedError)
    return False


# ── configuration ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", ["", "off", "0", "none", "false", "OFF"])
def test_listener_is_off_unless_a_port_is_named(raw):
    assert pl.configured_port({pl.PORT_ENV: raw}) is None
    assert pl.configured_port({}) is None


@pytest.mark.parametrize("raw", ["abc", "70000", "-1"])
def test_malformed_port_means_off_not_a_crash(raw):
    assert pl.configured_port({pl.PORT_ENV: raw}) is None


def test_port_and_host_are_read_from_the_environment():
    assert pl.configured_port({pl.PORT_ENV: " 8081 "}) == 8081
    assert pl.configured_host({}) == "0.0.0.0"
    assert pl.configured_host({pl.HOST_ENV: "127.0.0.1"}) == "127.0.0.1"


# ── supervisor side ───────────────────────────────────────────────────────

def test_supervisor_binds_once_and_the_worker_peels_the_socket_off():
    port = _free_port()
    received = {}

    def run(sockets=None):
        received["sockets"] = list(sockets)
        return "ran"

    tls = socket.socket()
    sup = SimpleNamespace(sockets=[tls], target=run)
    try:
        sock = pl.attach_to_supervisor(sup, {pl.PORT_ENV: str(port), pl.HOST_ENV: "127.0.0.1"})
        assert sock is not None
        assert sock.getsockname() == ("127.0.0.1", port)
        assert sock.get_inheritable() is True
        assert sup.sockets == [tls, sock]
        assert isinstance(sup.target, pl.SplitTarget)

        # What the spawned worker does with the inherited list.
        assert sup.target(sockets=[tls, sock]) == "ran"
        assert received["sockets"] == [tls]
        assert pl.take_inherited() is sock
        assert pl.take_inherited() is None  # consumed exactly once
    finally:
        tls.close()
        if sock is not None:
            sock.close()


def test_supervisor_leaves_everything_alone_when_the_listener_is_off():
    run = object()
    sup = SimpleNamespace(sockets=["tls"], target=run)
    assert pl.attach_to_supervisor(sup, {}) is None
    assert sup.sockets == ["tls"]
    assert sup.target is run


def test_bind_failure_surfaces_and_closes_the_socket():
    holder = socket.socket()
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    try:
        with pytest.raises(OSError):
            pl.bind(port, "127.0.0.1")
    finally:
        holder.close()


# ── worker side: serving ──────────────────────────────────────────────────

def test_uvicorn_private_serve_contract_still_holds():
    """start() drives Server._serve(sockets=...) instead of serve(): in uvicorn
    0.44 serve() is exactly capture_signals() around _serve(), and the signal
    capture would displace the primary server's handlers. An upgrade that
    changes either fact must fail here, not on the node."""
    import inspect

    import uvicorn

    assert "sockets" in inspect.signature(uvicorn.Server._serve).parameters
    source = inspect.getsource(uvicorn.Server.serve)
    assert "capture_signals()" in source
    assert "self._serve(sockets)" in source

def test_serves_the_app_without_tls_and_stops_cleanly():
    port = _free_port()

    async def scenario():
        sock = pl.bind(port, "127.0.0.1")
        listener = await pl.start(_hello, sock)
        assert listener.port == port
        status, body = await asyncio.to_thread(_get, f"http://127.0.0.1:{port}/first-screen")
        assert (status, body) == (200, b"plain /first-screen")
        await listener.stop()
        assert listener.task.done()
        return await asyncio.to_thread(_refused, f"http://127.0.0.1:{port}/")

    assert asyncio.run(scenario()) is True


def test_start_configured_prefers_the_inherited_socket_over_binding():
    inherited_port = _free_port()
    other_port = _free_port()

    async def scenario():
        pl.inherit(pl.bind(inherited_port, "127.0.0.1"))
        listener = await pl.start_configured(_hello, {pl.PORT_ENV: str(other_port)})
        try:
            assert listener.port == inherited_port
            status, _ = await asyncio.to_thread(_get, f"http://127.0.0.1:{inherited_port}/")
            assert status == 200
        finally:
            await listener.stop()

    asyncio.run(scenario())


def test_start_configured_binds_its_own_socket_without_a_supervisor():
    port = _free_port()

    async def scenario():
        assert pl.take_inherited() is None
        listener = await pl.start_configured(
            _hello, {pl.PORT_ENV: str(port), pl.HOST_ENV: "127.0.0.1"})
        try:
            assert listener.port == port
        finally:
            await listener.stop()

    asyncio.run(scenario())
    assert asyncio.run(pl.start_configured(_hello, {})) is None


# ── the dashboard lifespan ────────────────────────────────────────────────

def test_dashboard_lifespan_serves_the_plain_door_until_shutdown(test_app, tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    from tools.dashboard import server

    port = _free_port()
    monkeypatch.setenv(pl.PORT_ENV, str(port))
    monkeypatch.setenv(pl.HOST_ENV, "127.0.0.1")
    marker = tmp_path / "worker.ready"
    monkeypatch.setenv(wh.READY_MARKER_ENV, str(marker))
    monkeypatch.delenv(wh.PREDECESSOR_PID_ENV, raising=False)

    with TestClient(test_app):
        assert server._plain_listener is not None
        assert server._plain_listener.port == port
        assert marker.exists()  # ready implies both listeners answer
        status, _ = _get(f"http://127.0.0.1:{port}/api/ping")
        assert status == 200

    assert server._plain_listener is None
    assert _refused(f"http://127.0.0.1:{port}/api/ping")
