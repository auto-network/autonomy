"""T1 of graph://9642ab99-bae: requests to one peer share ONE connection
and run concurrently on it. Each request carries an id; the shared record
loop serves an id-tagged request as its own task and tags its reply with the
same id, so a slow request holds up no other. The pool keeps one connection
per (peer, scope), evicts the least recently active idle one at its cap,
refuses when every connection is busy, closes idle connections from the
caller's side, and the serving side closes a connection whose peer is no
longer admitted."""

from __future__ import annotations

import asyncio
import time

import pytest

from tools.network import fleet_roster, session_control
from tools.network.idkit import KeyPair
from tools.network.relaykit import connector as relay_connector
from tools.network.relaykit.fleet_stream_wire import (
    CAP_FLEET_DIRECTED_STREAM,
    CAP_SESSION_CONTROL,
)
from tools.network.tests.test_fleet_relay_carrier import _free_port, _live_registry, _register, _stop
from tools.network.tests.test_session_control_relay import Runtime, _connector

SLOW_S = 1.5


async def _pair(port, root):
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
    broker_b = session_control.InboundBroker()
    a, task_a = await _connector(port, root, machine_a, runtime_a,
                                 session_control.InboundBroker(), caps=caps)
    b, task_b = await _connector(port, root, machine_b, runtime_b, broker_b, caps=caps)
    return a, task_a, b, task_b, runtime_a, machine_b, broker_b


async def _serve_dashboard(broker, stop):
    """B's dashboard: answers each request at once, except op ``slow``."""
    async def answer(item, delay):
        await asyncio.sleep(delay)
        broker.reply(item["id"], {"v": 1, "ok": True, "result": {"n": item["body"]["n"]}})

    while not stop.is_set():
        item = await broker.next(0.5)
        if item is not None:
            asyncio.create_task(answer(item, SLOW_S if item["op"] == "slow" else 0))


def test_ten_simultaneous_requests_share_one_pair_and_a_slow_one_holds_up_none(monkeypatch):
    monkeypatch.setattr(session_control, "_pool", {})
    root = KeyPair.generate()
    port = _free_port()
    with _live_registry(port) as app:
        asyncio.run(_ten_scenario(root, port, app.state.directed_streams))


async def _ten_scenario(root, port, custody):
    a, task_a, b, task_b, runtime_a, machine_b, broker_b = await _pair(port, root)
    stop = asyncio.Event()
    dashboard = asyncio.create_task(_serve_dashboard(broker_b, stop))
    try:
        # Open the connection first, so the timings below measure requests.
        warm = await session_control.request(a, runtime_a, machine_pub=machine_b.public_hex,
                                             op="status", body={"n": -1}, timeout=10)
        assert warm["ok"] is True, warm
        finished: dict[int, float] = {}
        started = time.monotonic()

        async def one(n):
            reply = await session_control.request(
                a, runtime_a, machine_pub=machine_b.public_hex,
                op="slow" if n == 0 else "status", body={"n": n}, timeout=10)
            finished[n] = time.monotonic() - started
            return reply

        replies = await asyncio.gather(*(one(n) for n in range(10)))
        assert [r["result"]["n"] for r in replies] == list(range(10))
        # The fast requests finished long before the slow one: none waited.
        assert max(finished[n] for n in range(1, 10)) < SLOW_S / 2, finished
        assert finished[0] >= SLOW_S
        # The relay's own custody count: ONE pair for all of them.
        assert custody.snapshot()["pairs"] == 1
    finally:
        stop.set()
        await dashboard
        await _stop(a, task_a)
        await _stop(b, task_b)


def test_requests_beyond_the_channel_cap_are_refused_by_name(monkeypatch):
    monkeypatch.setattr(session_control, "_pool", {})
    monkeypatch.setattr(relay_connector, "MAX_REQUESTS_IN_FLIGHT", 2)
    root = KeyPair.generate()
    port = _free_port()
    with _live_registry(port):
        asyncio.run(_cap_scenario(root, port))


async def _cap_scenario(root, port):
    a, task_a, b, task_b, runtime_a, machine_b, broker_b = await _pair(port, root)
    try:
        # B's dashboard never answers: two requests stay in flight.
        held = [asyncio.create_task(session_control.request(
            a, runtime_a, machine_pub=machine_b.public_hex, op="status",
            body={"n": n}, timeout=5)) for n in range(2)]
        for _ in range(2):
            assert await broker_b.next(5) is not None
        reply = await session_control.request(
            a, runtime_a, machine_pub=machine_b.public_hex, op="status", body={"n": 9},
            timeout=5)
        assert (reply["ok"], reply["refusal"], reply["at"]) == (
            False, relay_connector.REFUSAL_REQUESTS_AT_CAP, "peer"), reply
        for task in held:
            task.cancel()
        await asyncio.gather(*held, return_exceptions=True)
    finally:
        await _stop(a, task_a)
        await _stop(b, task_b)


# ── the pool rules, without a relay ─────────────────────────────────────────


class _Fake(session_control._PeerConnection):
    def __init__(self, key, *, idle_for, busy=False):
        super().__init__(key)
        self.last_activity = time.monotonic() - idle_for
        if busy:
            self.pending[b"x" * 8] = None
        self.closed_as = None

    async def close(self, ending="done"):
        self.closed_as = ending


def test_at_the_cap_the_least_recently_active_idle_connection_is_evicted(monkeypatch):
    monkeypatch.setattr(session_control, "_scope_cap", lambda scope: (3, "relay-pair-cap"))
    monkeypatch.setattr(session_control, "_sweep_idle", lambda: None)
    old = _Fake(("m1", "personal"), idle_for=300)
    newer = _Fake(("m2", "personal"), idle_for=10)
    busy = _Fake(("m3", "personal"), idle_for=900, busy=True)
    pool = {c.key: c for c in (old, newer, busy)}
    monkeypatch.setattr(session_control, "_pool", pool)
    entry = asyncio.run(session_control._connection_for("m4"))
    assert entry.key == ("m4", "personal")
    # The busy one is older, but nothing is evicted mid-request.
    assert old.closed_as == "done" and ("m1", "personal") not in pool
    assert busy.closed_as is None and newer.closed_as is None


def test_when_every_connection_is_busy_the_call_is_refused(monkeypatch):
    monkeypatch.setattr(session_control, "_scope_cap", lambda scope: (2, "relay-pair-cap"))
    pool = {("m1", "personal"): _Fake(("m1", "personal"), idle_for=5, busy=True),
            ("m2", "personal"): _Fake(("m2", "personal"), idle_for=5, busy=True)}
    monkeypatch.setattr(session_control, "_pool", pool)
    with pytest.raises(session_control.SessionControlError) as raised:
        asyncio.run(session_control._connection_for("m3"))
    assert raised.value.refusal == "relay-pair-cap"
    monkeypatch.setattr(session_control, "POOL_CAP", 2)
    monkeypatch.setattr(session_control, "_scope_cap", lambda scope: (255, "pool-full"))
    with pytest.raises(session_control.SessionControlError) as raised:
        asyncio.run(session_control._connection_for("m3"))
    assert raised.value.refusal == session_control.POOL_FULL


def test_the_caller_closes_connections_idle_past_the_period(monkeypatch):
    idle = _Fake(("m1", "personal"), idle_for=1000)
    fresh = _Fake(("m2", "personal"), idle_for=5)
    busy = _Fake(("m3", "personal"), idle_for=1000, busy=True)
    pool = {c.key: c for c in (idle, fresh, busy)}
    monkeypatch.setattr(session_control, "_pool", pool)
    assert asyncio.run(session_control.close_idle(period=900)) == 1
    assert idle.closed_as == "done" and list(pool) == [("m2", "personal"), ("m3", "personal")]


def test_the_serving_side_closes_a_connection_whose_peer_is_no_longer_admitted(monkeypatch):
    class Endpoint:
        closed = False

        async def close(self):
            self.closed = True

    kept, revoked = Endpoint(), Endpoint()
    monkeypatch.setattr(session_control, "_served", {kept: "aa" * 32, revoked: "bb" * 32})

    class Auth:
        def authorize(self, pub):
            if pub == "bb" * 32:
                raise session_control.SessionControlError("peer-not-in-roster", "removed")

    monkeypatch.setattr(session_control, "session_authenticator", lambda runtime: Auth())
    assert asyncio.run(session_control._close_revoked(object())) == 1
    assert revoked.closed and not kept.closed
    assert list(session_control._served) == [kept]


def test_a_tagged_request_on_a_stateful_channel_is_refused():
    """Per-channel state (ICE signalling) stays single-threaded: a tagged
    request to a for_channel handler is refused, and untagged messages are
    served in order as before."""
    from tools.network.relaykit.channel import ChannelCrypto  # noqa: F401 -- shape only

    class Crypto:
        def open_record(self, record):
            return record

        def iter_seal_message(self, message, *, stream_final=True):
            yield message

    seen = []

    class Stateful:
        def for_channel(self, token):
            return lambda token, message: seen.append(message) or b"ok"

    inbox = asyncio.Queue()
    sent = []

    async def run():
        for m in (relay_connector.tag_message(b"12345678", b"{}"), b"{}", None):
            inbox.put_nowait(m)

        async def send(out):
            sent.append(out)

        await relay_connector._serve_channel_records(
            Crypto(), token="t", recv=inbox.get, send=send, handler=Stateful())

    asyncio.run(run())
    request_id, body = relay_connector.split_request_id(sent[0])
    assert request_id == b"12345678"
    assert relay_connector.REFUSAL_IDS_UNSUPPORTED.encode() in body
    assert sent[1] == b"ok" and seen == [b"{}"]
