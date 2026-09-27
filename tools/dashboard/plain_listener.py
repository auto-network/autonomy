"""A plain-HTTP listener beside the TLS listener, in the same dashboard worker.

The dashboard serves HTTPS with a self-signed certificate on 8080. Two things
need the same application without TLS: the first onboarding screen, which
must open on ``http://localhost`` (a secure context with no certificate
interstitial, so WebAuthn and the clipboard work from the first page), and the
service gateway, which proxies a published dashboard route to a plain upstream
and terminates TLS itself. Both are the SAME process: the human gate, unlock
sessions, the event bus and hot reload are all process-local, so a second
process would be a second dashboard, not a second door to this one.

Where the socket comes from decides whether a reload can drop connections:

* under ``tools.dashboard.reload_with_notice`` the SUPERVISOR binds the plain
  socket once, appends it to the sockets every worker inherits, and the worker
  splits it back off (``SplitTarget``). Incumbent and replacement accept from
  the same socket during a hand-off, exactly like the TLS listener, so no
  connection is ever queued on a socket nobody answers;
* run directly (``DASHBOARD_RELOAD=off``, or ``uvicorn`` by hand) the worker
  binds the socket itself.

The listener is off unless ``DASHBOARD_PLAIN_PORT`` names a port; ``deploy/
serve.sh`` sets ``8081`` in the node container and nothing else does, so the
dev box, the mock server and the test suite are unchanged. It is served by a
second ``uvicorn.Server`` on the worker's event loop with the lifespan OFF
(the primary server already ran the application's lifespan) and driven through
``Server._serve`` rather than ``serve()``: the public entry point installs its
own signal handlers, which would displace the primary server's.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger("uvicorn.error")

PORT_ENV = "DASHBOARD_PLAIN_PORT"
HOST_ENV = "DASHBOARD_PLAIN_HOST"
DEFAULT_HOST = "0.0.0.0"
_OFF = {"", "off", "0", "none", "false"}

_inherited: socket.socket | None = None


def configured_port(environ=None) -> int | None:
    """The plain listener's port, or None when the listener is off."""
    env = os.environ if environ is None else environ
    raw = (env.get(PORT_ENV) or "").strip().lower()
    if raw in _OFF:
        return None
    try:
        port = int(raw)
    except ValueError:
        logger.warning("ignoring malformed %s=%r; plain listener off", PORT_ENV, raw)
        return None
    if not 0 < port < 65536:
        logger.warning("ignoring out-of-range %s=%r; plain listener off", PORT_ENV, raw)
        return None
    return port


def configured_host(environ=None) -> str:
    env = os.environ if environ is None else environ
    return (env.get(HOST_ENV) or "").strip() or DEFAULT_HOST


def bind(port: int, host: str = DEFAULT_HOST) -> socket.socket:
    """Bind (not listen) the plain socket, inheritable, the way uvicorn binds its own."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family=family)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError:
        sock.close()
        raise
    sock.set_inheritable(True)
    return sock


# ── supervisor side ───────────────────────────────────────────────────────


class SplitTarget:
    """Picklable worker entry: peel the plain socket off the inherited list.

    uvicorn hands a worker every socket the supervisor passed and serves them
    all with the one configuration (TLS included), so the plain socket must
    leave the list before ``Server.run`` sees it.
    """

    def __init__(self, run: Callable[..., Any]) -> None:
        self.run = run

    def __call__(self, sockets: list[socket.socket] | None = None) -> Any:
        sockets = list(sockets or [])
        if sockets:
            inherit(sockets.pop())
        return self.run(sockets=sockets)


def attach_to_supervisor(supervisor, environ=None) -> socket.socket | None:
    """Bind the configured plain socket once and share it with every worker.

    Returns the socket, or None when the listener is off. Called by the
    reload supervisor before it spawns its first worker; ``BaseReload.shutdown``
    closes every socket in ``supervisor.sockets``, this one included.
    """
    port = configured_port(environ)
    if port is None:
        return None
    host = configured_host(environ)
    sock = bind(port, host)
    supervisor.sockets = [*supervisor.sockets, sock]
    supervisor.target = SplitTarget(supervisor.target)
    logger.info(
        "Plain listener bound on http://%s:%d, shared with every worker", host, sock.getsockname()[1],
    )
    return sock


# ── worker side ───────────────────────────────────────────────────────────


def inherit(sock: socket.socket) -> None:
    global _inherited
    _inherited = sock


def take_inherited() -> socket.socket | None:
    global _inherited
    sock, _inherited = _inherited, None
    return sock


@dataclass
class PlainListener:
    server: Any
    task: asyncio.Task
    sock: socket.socket

    @property
    def port(self) -> int:
        return self.sock.getsockname()[1]

    async def stop(self, timeout: float = 10.0) -> None:
        """Stop accepting, drain, and wait for the serving task to finish."""
        self.server.should_exit = True
        try:
            await asyncio.wait_for(asyncio.shield(self.task), timeout)
        except asyncio.TimeoutError:
            logger.error("plain listener did not stop within %.0fs; cancelling", timeout)
            self.task.cancel()
        except Exception:
            logger.exception("plain listener ended with an error")


async def start(app, sock: socket.socket, *, timeout_graceful_shutdown: float = 5.0) -> PlainListener:
    """Serve ``app`` on ``sock`` without TLS from the running event loop."""
    import uvicorn

    config = uvicorn.Config(
        app,
        host=sock.getsockname()[0],
        port=sock.getsockname()[1],
        lifespan="off",
        access_log=False,
        log_config=None,
        timeout_graceful_shutdown=timeout_graceful_shutdown,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server._serve(sockets=[sock]), name="plain-listener")
    # Fail fast: a bind or protocol error surfaces here, not on the first request.
    while not server.started and not task.done():
        await asyncio.sleep(0.01)
    if task.done():
        task.result()  # raises the startup error
        raise RuntimeError("plain listener exited during startup")
    return PlainListener(server=server, task=task, sock=sock)


async def start_configured(app, environ=None) -> PlainListener | None:
    """Start the listener the environment asks for, on the inherited socket if
    the supervisor bound one, else on a socket of our own. None when off."""
    port = configured_port(environ)
    sock = take_inherited()
    if sock is None:
        if port is None:
            return None
        sock = bind(port, configured_host(environ))
    listener = await start(app, sock)
    logger.info("Plain listener serving on http://%s:%d", sock.getsockname()[0], listener.port)
    return listener
