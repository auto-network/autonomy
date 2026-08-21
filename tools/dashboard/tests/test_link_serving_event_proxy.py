"""The dashboard-side event proxy: our own bus events to the connector.

A guest's stream key lives in the connector's process, so a frame sealed
anywhere else is undecryptable to them and the news has to cross one
process boundary. These cover the crossing itself -- that it is private,
that it knows nothing about what it carries, and that it can never delay
the request that emitted the event.
"""

from __future__ import annotations

import asyncio

import pytest

from tools.dashboard import link_serving


class _Bus:
    """The subscribe/unsubscribe shape the real bus offers."""

    def __init__(self):
        self.queue: asyncio.Queue = asyncio.Queue()
        self.unsubscribed = False

    def subscribe(self, client_id=None):
        return self.queue

    def unsubscribe(self, queue):
        self.unsubscribed = True


async def _drain(bus, calls, *, routes=None, max_pending=256, control=None):
    """Run the proxy until the queue empties, then stop it."""
    stop = asyncio.Event()

    def _control(org, op, args, **kw):
        calls.append((org, op, args))

    task = asyncio.create_task(link_serving.proxy_events_to_connectors(
        bus, org="autonomy", stop=stop, control=control or _control,
        max_pending=max_pending,
    ))
    for _ in range(200):
        await asyncio.sleep(0)
        if bus.queue.empty():
            break
    stop.set()
    await bus.queue.put(("wake", {}, 0))
    with __import__("contextlib").suppress(asyncio.TimeoutError):
        await asyncio.wait_for(task, timeout=2)
    task.cancel()
    return calls


def test_a_subscribed_topic_reaches_the_consumer_that_asked_for_it():
    bus = _Bus()
    topic = next(iter(link_serving.event_routes()))
    bus.queue.put_nowait((topic, {"mission_id": "m1"}, 1))
    calls = asyncio.run(_drain(bus, []))
    assert calls, "a declared topic must be forwarded"
    org, op, args = calls[0]
    assert op == link_serving.EVENT_OP
    assert args["topic"] == topic
    assert args["consumer"] in link_serving.EVENT_CONSUMERS
    assert args["data"] == {"mission_id": "m1"}


def test_a_topic_nobody_subscribed_to_never_crosses_the_boundary():
    bus = _Bus()
    bus.queue.put_nowait(("nobody:wants-this", {"mission_id": "m1"}, 1))
    assert asyncio.run(_drain(bus, [])) == []


def test_a_connector_that_is_down_does_not_stop_the_proxy():
    """No tunnel is the ordinary state, not an error: the guest misses the
    frame and recovers on their own channel, which is authoritative."""
    bus = _Bus()
    topic = next(iter(link_serving.event_routes()))
    seen = []

    def _explode(org, op, args, **kw):
        seen.append(args["topic"])
        raise ConnectionError("no connector is running")

    bus.queue.put_nowait((topic, {"mission_id": "m1"}, 1))
    bus.queue.put_nowait((topic, {"mission_id": "m2"}, 2))
    asyncio.run(_drain(bus, [], control=_explode))
    assert len(seen) == 2, "a failed delivery must not end the proxy"


def test_a_backlog_is_dropped_rather_than_grown_without_bound():
    """Behind a tunnel that may be down for hours, an unbounded queue is a
    memory leak. A live update is best-effort by design."""
    bus = _Bus()
    topic = next(iter(link_serving.event_routes()))
    for i in range(12):
        bus.queue.put_nowait((topic, {"mission_id": f"m{i}"}, i))
    calls = asyncio.run(_drain(bus, [], max_pending=2))
    assert calls, "some events still get through"
    assert len(calls) < 12, "a backlog past the bound must be dropped"


def test_the_subscription_is_released_when_the_proxy_stops():
    bus = _Bus()
    asyncio.run(_drain(bus, []))
    assert bus.unsubscribed is True


def test_the_pipe_holds_no_list_of_its_own():
    """Routes come from asking each consumer, so a consumer that changes
    what it wants needs no edit here."""
    routes = link_serving.event_routes()
    assert routes, "at least one consumer must be registered"
    for topic, consumers in routes.items():
        assert isinstance(topic, str) and topic
        for consumer in consumers:
            assert link_serving._event_dispatch(consumer) is not None


def test_an_unknown_consumer_is_refused_rather_than_dispatched():
    reply = asyncio.run(link_serving._dispatch_event(
        object(), {"consumer": "not-a-consumer", "topic": "x", "data": {}}))
    assert reply["ok"] is False


def test_a_consumer_that_faults_never_takes_the_connector_down():
    class _Boom:
        async def publish_event(self, *a, **kw):
            raise RuntimeError("consumer exploded")

    consumer = next(iter(link_serving.EVENT_CONSUMERS))
    real = link_serving._event_dispatch
    link_serving._event_dispatch = lambda name: _Boom() if name == consumer else None
    try:
        reply = asyncio.run(link_serving._dispatch_event(
            object(), {"consumer": consumer, "topic": "x", "data": {}}))
    finally:
        link_serving._event_dispatch = real
    assert reply["ok"] is False
    assert "faulted" in reply["error"]
