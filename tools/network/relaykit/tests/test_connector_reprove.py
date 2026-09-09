"""Connector answers a pushed reprove-required (auto-3bhy3).

When the registry adopts a newer membership checkpoint it pushes a
``reprove-required {seq}`` control frame. The connector must route that to
its ``on_reprove`` responder and send ``re-prove-membership`` — distinct
from a reply frame, which resolves a pending control() caller. Removed
members have no valid proof, so a None/absent responder simply does not
answer and the registry closes the tunnel at its deadline.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from tools.network.idkit import KeyPair, Subject, issue_cert
from tools.network.relaykit.connector import TunnelConnector

ORG = "11111111-1111-4111-8111-111111111111"


def _connector(on_reprove=None):
    key, root = KeyPair.generate(), KeyPair.generate()
    cert = issue_cert(root, key.public_hex, scope=("tunnel:serve",), org=ORG,
                      subject=Subject("persona", "ab" * 32),
                      not_before=0, not_after=2**40)
    return TunnelConnector("ws://x", ORG, key, cert, on_reprove=on_reprove,
                machine_key=KeyPair.generate(),
            )


def test_push_routes_to_responder_and_sends_reprove():
    async def scenario():
        sent = []
        seen_seq = []

        async def responder(seq):
            seen_seq.append(seq)
            return {"v": 1, "checkpoint_seq": seq, "index": 0, "path": []}

        async def fake_control(op, args, timeout=10.0):
            sent.append((op, args))
            return {"ok": True}

        c = _connector(responder)
        c.control = fake_control
        c._resolve_ctrl_reply(json.dumps(
            {"op": "reprove-required", "args": {"seq": 4}}).encode())
        await asyncio.sleep(0)  # let the dispatched task run
        assert seen_seq == [4]
        assert sent == [("re-prove-membership",
                         {"v": 1, "checkpoint_seq": 4, "index": 0, "path": []})]

    asyncio.run(scenario())


def test_reply_frame_still_resolves_pending_caller():
    async def scenario():
        c = _connector()
        fut = asyncio.get_event_loop().create_future()
        c._pending["corr"] = fut
        c._resolve_ctrl_reply(json.dumps({"id": "corr", "ok": True}).encode())
        assert (await fut) == {"id": "corr", "ok": True}

    asyncio.run(scenario())


def test_push_without_responder_is_silent():
    # A removed member (or an unwired connector) does not answer; the registry
    # deadline enforces. No exception, no control sent.
    async def scenario():
        c = _connector(on_reprove=None)
        sent = []

        async def fake_control(op, args, timeout=10.0):
            sent.append(op)
            return {"ok": True}

        c.control = fake_control
        c._resolve_ctrl_reply(json.dumps(
            {"op": "reprove-required", "args": {"seq": 2}}).encode())
        await asyncio.sleep(0)
        assert sent == []

    asyncio.run(scenario())


def test_responder_returning_none_does_not_answer():
    async def scenario():
        async def responder(seq):
            return None

        c = _connector(responder)
        sent = []

        async def fake_control(op, args, timeout=10.0):
            sent.append(op)
            return {"ok": True}

        c.control = fake_control
        c._resolve_ctrl_reply(json.dumps(
            {"op": "reprove-required", "args": {"seq": 2}}).encode())
        await asyncio.sleep(0)
        assert sent == []

    asyncio.run(scenario())
