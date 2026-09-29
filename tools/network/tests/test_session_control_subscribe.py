"""session-control subscriptions end to end on a real relay with two real
connectors (bead "Live remote session events"): one channel, one subscribe,
and every event the host's dashboard publishes flows to the subscriber until
the channel ends; a refusal is reported and marked as one."""

from __future__ import annotations

import asyncio

from tools.network import fleet_roster, session_control
from tools.network.idkit import KeyPair
from tools.network.relaykit.fleet_stream_wire import (
    CAP_FLEET_DIRECTED_STREAM,
    CAP_SESSION_CONTROL,
)
from tools.network.tests.test_fleet_relay_carrier import (
    _free_port,
    _live_registry,
    _register,
    _stop,
)
from tools.network.tests.test_session_control_relay import Runtime, _connector


def test_one_subscription_carries_every_published_event_until_it_ends(monkeypatch):
    root = KeyPair.generate()
    port = _free_port()
    with _live_registry(port):
        asyncio.run(_scenario(root, port, monkeypatch))


async def _next_items(count, timeout=5.0):
    items = []
    while len(items) < count:
        items.extend(await asyncio.wait_for(session_control._collect(timeout), timeout + 1))
    return items


async def _scenario(root, port, monkeypatch):
    monkeypatch.setattr(session_control, "_received", asyncio.Queue(maxsize=64))
    monkeypatch.setattr(session_control, "_published", {})
    monkeypatch.setattr(session_control, "_subscribed", {})
    _register(port, root)
    machine_a, machine_b = KeyPair.generate(), KeyPair.generate()
    id_a, id_b = "a1" * 32, "b1" * 32
    roster = (
        fleet_roster.enroll(root, machine_id=id_a, machine_pub=machine_a.public_hex, seq=0),
        fleet_roster.enroll(root, machine_id=id_b, machine_pub=machine_b.public_hex, seq=0),
    )
    caps = (CAP_FLEET_DIRECTED_STREAM, CAP_SESSION_CONTROL)
    runtime_a = Runtime(root, machine_a, id_a, roster)
    runtime_b = Runtime(root, machine_b, id_b, roster)
    broker_a, broker_b = session_control.InboundBroker(), session_control.InboundBroker()
    a, task_a = await _connector(port, root, machine_a, runtime_a, broker_a, caps=caps)
    b, task_b = await _connector(port, root, machine_b, runtime_b, broker_b, caps=caps)
    try:
        # One subscribe; B's dashboard accepts it and learns the sub_id from
        # its own connector, alongside the proved peer.
        session_control.subscribe(a, runtime_a, machine_b.public_hex, "persona-p", timeout=5)
        item = await broker_b.next(10)
        assert item["op"] == "subscribe"
        assert item["body"]["persona"] == "persona-p"
        assert item["peer_machine_pub"] == machine_a.public_hex
        sub_id = item["body"]["sub_id"]
        broker_b.reply(item["id"], {"v": 1, "ok": True, "result": {"subscribed": True}})
        (ack,) = await _next_items(1)
        assert ack == {"machine_pub": machine_b.public_hex, "subscribed": True}

        # Every published event flows, in order, as it is published.
        for n in range(3):
            assert session_control.publish(sub_id, {"topic": "t", "data": {"n": n}}) == {"ok": True}
        events = await _next_items(3)
        assert [e["event"]["data"]["n"] for e in events] == [0, 1, 2]

        # The host ends it (its dashboard reloaded): the subscriber is told,
        # and it is not a refusal, so it will subscribe again. Both connectors
        # share this process, so end only the host's side of the channel.
        session_control._end_subscription(sub_id)
        (end,) = await _next_items(1)
        assert end["machine_pub"] == machine_b.public_hex and end["refused"] is False
        assert session_control.publish(sub_id, {"topic": "t", "data": {}})["error_kind"] == \
            session_control.SUBSCRIPTION_NOT_FOUND

        # The subscriber goes away (its connector restarted): the host ends
        # its side at once, without waiting for an event to fail on it.
        session_control.subscribe(a, runtime_a, machine_b.public_hex, "persona-p", timeout=5)
        item = await broker_b.next(10)
        sub_id = item["body"]["sub_id"]
        broker_b.reply(item["id"], {"v": 1, "ok": True, "result": {"subscribed": True}})
        await _next_items(1)
        session_control._subscribed.pop(machine_b.public_hex).cancel()
        for _ in range(100):
            if sub_id not in session_control._published:
                break
            await asyncio.sleep(0.05)
        assert sub_id not in session_control._published

        # A dashboard that refuses the persona: reported, and marked refused.
        session_control.subscribe(a, runtime_a, machine_b.public_hex, "persona-q", timeout=5)
        item = await broker_b.next(10)
        broker_b.reply(item["id"], {"v": 1, "ok": False, "refusal": "subscribe-persona-refused"})
        (end,) = await _next_items(1)
        assert (end["end"], end["at"], end["refused"]) == ("subscribe-persona-refused", "peer", True)
    finally:
        session_control.close_subscriptions()
        await _stop(a, task_a)
        await _stop(b, task_b)


def test_a_subscription_too_far_behind_is_ended(monkeypatch):
    async def run():
        monkeypatch.setattr(session_control, "_published", {})
        queue = asyncio.Queue(maxsize=2)
        session_control._published["s"] = queue
        assert session_control.publish("s", {"n": 1}) == {"ok": True}
        assert session_control.publish("s", {"n": 2}) == {"ok": True}
        assert session_control.publish("s", {"n": 3})["error_kind"] == \
            session_control.SUBSCRIPTION_BEHIND
        assert "s" not in session_control._published
        assert queue.get_nowait() is None    # the channel's end marker

    asyncio.run(run())
