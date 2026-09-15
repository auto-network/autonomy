"""The connector reports WHY its tunnel is up or down over connector-status.

Before this the connector knew its last close code, how long the tunnel
lived and its backoff, and wrote them only to its log: over the control
socket a connector that lost its tunnel an hour ago and had failed every
reconnect since looked identical to one that started five seconds ago
(dynbench, 2026-09-15).
"""
from __future__ import annotations

import asyncio

from tools.network.relaykit import connector as connector_module
from tools.network.relaykit.tests.test_connector_backoff import (
    _Clock, _connector, _install_common_fakes,
)


class _Closed(Exception):
    def __init__(self, code, reason=""):
        super().__init__(f"received {code}")
        self.code = code
        self.reason = reason


def test_fresh_connector_reports_no_history():
    state = _connector().tunnel_state
    assert state == {"connected_since": None, "last_served_at": None,
                     "reconnect_attempts": 0, "last_disconnect": None,
                     "next_retry_at": None}


def test_disconnect_then_failed_reconnects_are_visible(monkeypatch):
    connector = _connector()
    clock = _Clock()
    _install_common_fakes(monkeypatch, connector, clock)
    wall = [1_000.0]
    monkeypatch.setattr(connector_module.time, "time", lambda: wall[0])
    attempts = []

    async def handshake(_ws):
        attempts.append(1)
        if len(attempts) > 1:
            raise ConnectionError("hello refused")   # every reconnect fails at the hello

    async def serve(_ws):
        # First tunnel: served 40s, then the relay closed it abnormally.
        clock.now += 40.0
        wall[0] += 40.0
        raise _Closed(1006, "")

    async def sleep(delay):
        clock.now += delay
        wall[0] += delay
        if len(attempts) == 3:
            connector.stop()

    connector._handshake = handshake
    connector._serve = serve
    monkeypatch.setattr(connector_module.asyncio, "sleep", sleep)
    asyncio.run(connector.run())

    state = connector.tunnel_state
    assert state["connected_since"] is None            # down right now
    assert state["last_served_at"] == 1_040.0          # when the first tunnel ended
    assert state["reconnect_attempts"] == 3            # every attempt since
    last = state["last_disconnect"]
    assert last["lived_s"] is None                     # the reconnects never served
    assert last["error"] == "ConnectionError: hello refused"
    assert state["next_retry_at"] is not None          # a retry was scheduled when it stopped


def test_the_first_disconnect_keeps_its_close_code(monkeypatch):
    connector = _connector()
    clock = _Clock()
    _install_common_fakes(monkeypatch, connector, clock)
    monkeypatch.setattr(connector_module.time, "time", lambda: 5_000.0)

    async def serve(_ws):
        clock.now += 3.0
        raise _Closed(4409, "replaced")

    async def sleep(delay):
        connector.stop()

    connector._serve = serve
    monkeypatch.setattr(connector_module.asyncio, "sleep", sleep)
    asyncio.run(connector.run())
    last = connector.tunnel_state["last_disconnect"]
    assert last["close_code"] == 4409
    assert last["reason"] == "replaced"
    assert last["lived_s"] == 3.0
    assert connector.tunnel_state["reconnect_attempts"] == 1


def test_a_connected_tunnel_reports_since_when(monkeypatch):
    connector = _connector()
    clock = _Clock()
    _install_common_fakes(monkeypatch, connector, clock)
    monkeypatch.setattr(connector_module.time, "time", lambda: 7_000.0)
    seen = {}

    async def serve(_ws):
        seen["while_up"] = connector.tunnel_state
        connector.stop()

    connector._serve = serve
    asyncio.run(connector.run())
    assert seen["while_up"]["connected_since"] == 7_000.0
    assert seen["while_up"]["reconnect_attempts"] == 0
