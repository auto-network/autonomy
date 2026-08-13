"""Reconnect health is useful service, not a successful tunnel hello."""

from __future__ import annotations

import asyncio

from tools.network.relaykit import connector as connector_module
from tools.network.relaykit.connector import TunnelConnector


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now


class _SocketContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _connector() -> TunnelConnector:
    # Handshake construction is replaced at the instance seam below, so these
    # placeholders never enter cryptographic code.
    return TunnelConnector(
        "ws://relay.invalid", "test-org", object(), object(),
        min_backoff=0.2, max_backoff=0.8,
    )


def _install_common_fakes(monkeypatch, connector: TunnelConnector, clock: _Clock):
    monkeypatch.setattr(connector_module.websockets, "connect",
                        lambda *args, **kwargs: _SocketContext())
    monkeypatch.setattr(connector_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(connector_module.random, "random", lambda: 0.0)

    async def handshake(_ws):
        return None

    connector._handshake = handshake


def test_immediate_post_hello_failures_reach_the_backoff_cap(monkeypatch):
    connector = _connector()
    clock = _Clock()
    _install_common_fakes(monkeypatch, connector, clock)

    async def serve(_ws):
        raise ConnectionError("post-hello flap")

    async def sleep(delay):
        clock.sleeps.append(delay)
        clock.now += delay
        if len(clock.sleeps) == 5:
            connector.stop()

    connector._serve = serve
    monkeypatch.setattr(connector_module.asyncio, "sleep", sleep)

    asyncio.run(connector.run())

    assert clock.sleeps == [0.2, 0.4, 0.8, 0.8, 0.8]


def test_useful_service_resets_the_next_reconnect_to_minimum(monkeypatch):
    connector = _connector()
    clock = _Clock()
    _install_common_fakes(monkeypatch, connector, clock)
    attempts = 0

    async def serve(_ws):
        nonlocal attempts
        attempts += 1
        if attempts == 4:
            clock.now += connector._max_backoff
        raise ConnectionError("drop")

    async def sleep(delay):
        clock.sleeps.append(delay)
        clock.now += delay
        if len(clock.sleeps) == 4:
            connector.stop()

    connector._serve = serve
    monkeypatch.setattr(connector_module.asyncio, "sleep", sleep)

    asyncio.run(connector.run())

    assert clock.sleeps == [0.2, 0.4, 0.8, 0.2]


def test_disconnect_logging_time_does_not_count_as_useful_service(monkeypatch):
    connector = _connector()
    clock = _Clock()
    _install_common_fakes(monkeypatch, connector, clock)
    attempts = 0

    async def serve(_ws):
        nonlocal attempts
        attempts += 1
        raise ConnectionError("immediate drop")

    def slow_log(_exc, _served_at):
        # A blocked log sink is not tunnel service and must not reset health.
        clock.now += connector._max_backoff

    async def sleep(delay):
        clock.sleeps.append(delay)
        if len(clock.sleeps) == 3:
            connector.stop()

    connector._serve = serve
    connector._log_disconnect = slow_log
    monkeypatch.setattr(connector_module.asyncio, "sleep", sleep)

    asyncio.run(connector.run())

    assert attempts == 3
    assert clock.sleeps == [0.2, 0.4, 0.8]
