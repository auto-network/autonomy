"""Fleet crosstalk (Path A) prototype: directive shape + synced delivery.

The delivery is dependency-injected, so these tests exercise the real routing
logic (scoped to the set; the owner self-selects by local session presence)
without the live sync/tmux stack.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from tools.dashboard import fleet_crosstalk as fc


@dataclass
class _Addr:
    set_id: str
    schema_revision: int
    key: str


def _row(target: str, body: str, sender: str = "home") -> dict:
    return {"payload": {"target_session": target, "body": body, "sender": sender}}


def test_set_id_is_personal_fleet_crosstalk():
    assert fc.FLEET_CROSSTALK_SET_ID == "dashboard.session.crosstalk.fleet-message"
    # Personal home is what makes the row cross the operator's fleet.
    assert fc.FleetCrosstalkV1.__dict__.get("_home") == "personal"
    assert fc.FleetCrosstalkV1.schema_revision == 1


def test_delivers_to_a_local_target():
    sent: list[tuple[str, str]] = []

    async def send(target, body):
        sent.append((target, body))

    n = asyncio.run(fc.deliver_synced_crosstalk(
        [_Addr(fc.FLEET_CROSSTALK_SET_ID, 1, "k1")],
        read_row=lambda k: _row("auto-sjc-1", "ping from home"),
        session_exists=lambda s: s == "auto-sjc-1",
        session_send=send,
    ))
    assert n == 1
    assert sent == [("auto-sjc-1", "ping from home")]


def test_skips_when_target_not_local():
    # The node that does NOT own the session no-ops — the owner self-selects.
    sent = []

    async def send(target, body):
        sent.append((target, body))

    n = asyncio.run(fc.deliver_synced_crosstalk(
        [_Addr(fc.FLEET_CROSSTALK_SET_ID, 1, "k1")],
        read_row=lambda k: _row("auto-sjc-1", "ping"),
        session_exists=lambda s: False,   # not our session
        session_send=send,
    ))
    assert n == 0 and sent == []


def test_ignores_other_sets_and_gaps():
    called = {"read": 0}

    def read(k):
        called["read"] += 1
        return _row("x", "y")

    async def send(target, body):
        raise AssertionError("must not deliver")

    # A different set's address is filtered before any read.
    n = asyncio.run(fc.deliver_synced_crosstalk(
        [_Addr("dashboard.session.crosstalk.request-rebase", 1, "k")],
        read_row=read, session_exists=lambda s: True, session_send=send,
    ))
    assert n == 0 and called["read"] == 0

    # A gap hint carries no specific row to deliver.
    n = asyncio.run(fc.deliver_synced_crosstalk(
        [_Addr(fc.FLEET_CROSSTALK_SET_ID, 1, "k")],
        gap=True, read_row=read, session_exists=lambda s: True, session_send=send,
    ))
    assert n == 0 and called["read"] == 0


def test_skips_malformed_row():
    async def send(target, body):
        raise AssertionError("must not deliver a bodyless row")

    n = asyncio.run(fc.deliver_synced_crosstalk(
        [_Addr(fc.FLEET_CROSSTALK_SET_ID, 1, "k")],
        read_row=lambda k: {"payload": {"target_session": "auto-x"}},  # no body
        session_exists=lambda s: True, session_send=send,
    ))
    assert n == 0
