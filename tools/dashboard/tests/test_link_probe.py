"""Unit coverage for the end-to-end serving probe — with a hang regression.

The probe's whole job is to make the publish path safe: it must return a
verdict, never block. The regression here reproduces the exact hang that a
reachable registry with NO serving tunnel dialed in produces — the relay
accepts the WebSocket and never sends SERVER_HELLO — and asserts the probe
returns "unreachable" within its budget instead of blocking forever
(``ViewerChannel.connect`` awaits SERVER_HELLO with no timeout of its own).
"""

from __future__ import annotations

import asyncio
import contextlib
import socket

import websockets

from tools.dashboard.link_probe import probe_link, registry_to_relay_ws


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _silent_relay(ws):
    """Accept the viewer channel and NEVER answer — a relay with no serving
    tunnel dialed in. Drains inbound so the client's send completes."""
    with contextlib.suppress(Exception):
        async for _ in ws:
            pass


def test_registry_to_relay_ws_scheme():
    assert registry_to_relay_ws("https://auto.network/") == "wss://auto.network"
    assert registry_to_relay_ws("http://127.0.0.1:8477") == "ws://127.0.0.1:8477"
    assert registry_to_relay_ws("wss://already.ws") == "wss://already.ws"


def test_probe_does_not_hang_on_silent_relay():
    """The hang regression: reachable relay, handshake never completes → the
    probe returns not-live/None BOUNDED by total_timeout, not forever."""
    async def run():
        server = await websockets.serve(_silent_relay, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        loop = asyncio.get_running_loop()
        try:
            t0 = loop.time()
            verdict = await probe_link(
                relay_url=f"ws://127.0.0.1:{port}",
                token="a" * 32, root_pub="b" * 64, org_uuid="org-uuid",
                total_timeout=1.5, connect_timeout=1.0, attempts=2,
            )
            elapsed = loop.time() - t0
        finally:
            server.close()
            await server.wait_closed()
        assert verdict["live"] is False, verdict
        assert verdict["status"] is None, verdict
        # Without the wait_for wall this would never return; with it, bounded.
        assert elapsed < 4.0, elapsed

    asyncio.run(run())


def test_probe_reports_unreachable_on_dead_port():
    """Nothing listening → fast connection refused → unreachable, not a hang."""
    async def run():
        port = _free_port()  # allocated then released; nothing listens on it
        verdict = await probe_link(
            relay_url=f"ws://127.0.0.1:{port}",
            token="a" * 32, root_pub="b" * 64, org_uuid="org-uuid",
            total_timeout=2.0, connect_timeout=1.0, attempts=1,
        )
        assert verdict["live"] is False and verdict["status"] is None, verdict

    asyncio.run(run())
