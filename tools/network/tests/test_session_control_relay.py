"""session-control/1 end to end on a real relay with two real connectors
(graph://7eb29bc8-31a §9.1, bead auto-99ioi): the pair is brokered under its
own capability, the session:control handshake proves the sender, one request
gets one reply from the destination's dashboard broker, and every failure is a
typed refusal -- including a relay that predates the capability."""

from __future__ import annotations

import asyncio
from pathlib import Path
import time

import pytest

from tools.network import fleet_roster, session_control
from tools.network.fleet_sync_channel import FleetAuthenticator
from tools.network.idkit import KeyPair, Subject, issue_cert
from tools.network.relaykit.connector import TunnelConnector
from tools.network.relaykit.fleet_stream import FleetStreamAdapter
from tools.network.relaykit.fleet_stream_wire import (
    CAP_FLEET_DIRECTED_STREAM,
    CAP_SESSION_CONTROL,
)
from tools.network.tests.test_fleet_relay_carrier import (
    ORG,
    PERSONA,
    _free_port,
    _live_registry,
    _register,
    _stop,
)


class Runtime:
    """What connector_runtime exposes: an armed scheduler's authenticator
    and, when the browser minted it, the session:control delegation."""

    def __init__(self, root, machine, machine_id, roster, *, session_cert=True):
        self.process = KeyPair.generate()
        now = int(time.time())

        def cert(scope):
            return issue_cert(
                machine, self.process.public_hex, scope=[scope],
                org=f"personal:{root.public_hex}",
                subject=Subject(kind="machine", id=machine_id),
                not_before=now - 30, not_after=now + 3600,
            )

        self.scheduler = type("S", (), {})()
        self.scheduler.authenticator = FleetAuthenticator(
            self.process, root_pub=root.public_hex,
            roster_entries=lambda: roster, roster_machine_pub=machine.public_hex,
            delegation_cert=cert("fleet:sync"), require_delegation=True,
        )
        self.session_control_cert = cert("session:control") if session_cert else None


async def _connector(port, root, machine, runtime, broker, *, caps):
    serve_key = KeyPair.generate()
    now = int(time.time())
    cert = issue_cert(root, serve_key.public_hex, scope=("tunnel:serve",), org=ORG,
                      subject=Subject("persona", PERSONA),
                      not_before=now - 100, not_after=now + 30 * 86400)
    connector = TunnelConnector(
        f"ws://127.0.0.1:{port}", ORG, serve_key, cert, machine_key=machine,
        caps=caps,
        session_control_offer=session_control.session_control_offer_handler(
            runtime, broker),
        min_backoff=0.05, max_backoff=0.2,
    )
    task = asyncio.create_task(connector.run())
    await asyncio.wait_for(connector.connected.wait(), 10)
    return connector, task


async def _answer(broker, *, count=1):
    """The destination dashboard's pump, reduced to: echo who asked."""
    for _ in range(count):
        item = await broker.next(10)
        assert item is not None
        broker.reply(item["id"], {"v": 1, "ok": True, "result": {
            "op": item["op"], "body": item["body"],
            "peer": item["peer_machine_pub"]}})


def test_session_control_request_reply_over_the_relay():
    root = KeyPair.generate()
    port = _free_port()
    with _live_registry(port) as app:
        asyncio.run(_scenario(root, port, app.state.directed_streams))


async def _scenario(root, port, broker):
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
        assert a.session_streams is not None and b.session_streams is not None

        # One request, one reply; the reply names the sender the HANDSHAKE
        # proved, and the relay counted a session-control pair, not a sync one.
        answering = asyncio.create_task(_answer(broker_b))
        started = time.monotonic()
        reply = await session_control.request(
            a, runtime_a, machine_pub=machine_b.public_hex, op="status",
            body={"n": 1}, timeout=5)
        elapsed = time.monotonic() - started
        await answering
        assert reply["ok"] is True, reply
        assert reply["result"] == {"op": "status", "body": {"n": 1},
                                   "peer": machine_a.public_hex}
        assert elapsed < 2.0
        deadline = time.time() + 5
        while time.time() < deadline and broker.snapshot()["pairs"]:
            await asyncio.sleep(0.05)
        assert broker.snapshot()["pairs"] == 0

        # A machine that is not in the roster is refused before any pair.
        reply = await session_control.request(
            a, runtime_a, machine_pub="cc" * 32, op="status", body={}, timeout=5)
        assert reply == {"v": 1, "ok": False, "refusal": "peer-not-in-roster",
                         "detail": reply.get("detail")}

        # A runtime without the session:control delegation cannot ask.
        runtime_a.session_control_cert = None
        reply = await session_control.request(
            a, runtime_a, machine_pub=machine_b.public_hex, op="status", body={},
            timeout=5)
        assert reply["refusal"] == "session-cap-missing"

        # ...and a destination without one refuses the pair.
        runtime_a.session_control_cert = Runtime(root, machine_a, id_a, roster) \
            .session_control_cert
        runtime_b.session_control_cert = None
        reply = await session_control.request(
            a, runtime_a, machine_pub=machine_b.public_hex, op="status", body={},
            timeout=5)
        assert reply["ok"] is False
        assert reply["refusal"] in (session_control.PEER_REFUSED,
                                    "session-control-failed"), reply
    finally:
        await _stop(a, task_a)
        await _stop(b, task_b)


# ── a relay that predates session-control/1 ─────────────────────────────────


def test_an_old_relay_is_a_typed_refusal_not_a_crash():
    """Between the dashboard merge and the relay deploy, the relay answers
    session-open with "unknown control op". The adapter turns that into
    FleetStreamRefused, and request() into a refusal record."""

    async def old_relay_control(op, args, timeout):
        return {"ok": False, "error": f"unknown control op: {op!r}"}

    async def run():
        adapter = FleetStreamAdapter(
            lambda *a: None, old_relay_control, capability=CAP_SESSION_CONTROL)

        class Connector:
            session_streams = adapter

        root = KeyPair.generate()
        machine_a, machine_b = KeyPair.generate(), KeyPair.generate()
        roster = (
            fleet_roster.enroll(root, machine_id="a1" * 32,
                                machine_pub=machine_a.public_hex, seq=0),
            fleet_roster.enroll(root, machine_id="b1" * 32,
                                machine_pub=machine_b.public_hex, seq=0),
        )
        runtime = Runtime(root, machine_a, "a1" * 32, roster)

        async def slot(connector, runtime, machine_pub, timeout):
            return (PERSONA, machine_b.public_hex), "org-slots"

        return await session_control.request(
            Connector(), runtime, machine_pub=machine_b.public_hex, op="status",
            body={}, timeout=2, resolve_slot=slot)

    reply = asyncio.run(run())
    assert reply["ok"] is False
    assert reply["refusal"] == "unknown control op: 'session-open'"


def test_a_relay_that_did_not_negotiate_the_capability_is_refused_by_name():
    class Connector:
        session_streams = None

    reply = asyncio.run(session_control.request(
        Connector(), object(), machine_pub="bb" * 32, op="status", body={}))
    assert reply == {"v": 1, "ok": False,
                     "refusal": session_control.NOT_NEGOTIATED,
                     "detail": "the relay did not negotiate session-control/1"}


# ── the ctl ops and the broker ──────────────────────────────────────────────


def test_broker_hands_one_request_to_the_dashboard_and_returns_its_reply():
    async def run():
        broker = session_control.InboundBroker(reply_timeout=2)
        waiting = asyncio.create_task(
            broker.submit("status", {}, peer_machine_pub="aa" * 32))
        polled = await session_control.handle_ctl(
            None, None, "session-control-next", {"wait": 1}, broker)
        item = polled["request"]
        assert item["op"] == "status" and item["peer_machine_pub"] == "aa" * 32
        answered = await session_control.handle_ctl(
            None, None, "session-control-reply",
            {"id": item["id"], "reply": {"v": 1, "ok": True, "result": {}}}, broker)
        assert answered == {"ok": True, "delivered": True}
        return await waiting

    assert asyncio.run(run()) == {"v": 1, "ok": True, "result": {}}


def test_an_unanswered_request_times_out_as_a_typed_refusal():
    async def run():
        broker = session_control.InboundBroker(reply_timeout=0.1)
        return await broker.submit("status", {}, peer_machine_pub="aa" * 32)

    assert asyncio.run(run())["refusal"] == session_control.DASHBOARD_UNAVAILABLE


def test_an_empty_poll_returns_no_request():
    async def run():
        broker = session_control.InboundBroker()
        return await session_control.handle_ctl(
            None, None, "session-control-next", {"wait": 0.05}, broker)

    assert asyncio.run(run()) == {"ok": True, "request": None}


@pytest.mark.parametrize("raw", [b"not json", b'{"v": 2, "op": "status"}',
                                 b'{"v": 1, "op": 3}'])
def test_malformed_requests_are_refused(raw):
    with pytest.raises(session_control.SessionControlError):
        session_control.parse_request(raw)


# ── streamed replies (auto-yi2pe) ───────────────────────────────────────────


def test_a_file_streams_over_the_relay_and_lands_intact(tmp_path, monkeypatch):
    monkeypatch.setattr(session_control, "_data_root", lambda: tmp_path)
    source_dir = tmp_path / "agent-runs" / "auto-1-20260928"
    source_dir.mkdir(parents=True)
    payload = bytes(range(256)) * 5000                       # ~1.2 MiB, 7 chunks
    (source_dir / "proof.bin").write_bytes(payload)
    root = KeyPair.generate()
    port = _free_port()
    with _live_registry(port):
        asyncio.run(_stream_scenario(root, port, source_dir / "proof.bin", payload,
                                     tmp_path))


async def _stream_scenario(root, port, source, payload, data_root):
    import hashlib

    _register(port, root)
    machine_a, machine_b = KeyPair.generate(), KeyPair.generate()
    roster = (
        fleet_roster.enroll(root, machine_id="a1" * 32, machine_pub=machine_a.public_hex, seq=0),
        fleet_roster.enroll(root, machine_id="b1" * 32, machine_pub=machine_b.public_hex, seq=0),
    )
    caps = (CAP_FLEET_DIRECTED_STREAM, CAP_SESSION_CONTROL)
    runtime_a = Runtime(root, machine_a, "a1" * 32, roster)
    runtime_b = Runtime(root, machine_b, "b1" * 32, roster)
    broker_b = session_control.InboundBroker()
    a, task_a = await _connector(port, root, machine_a, runtime_a,
                                 session_control.InboundBroker(), caps=caps)
    b, task_b = await _connector(port, root, machine_b, runtime_b, broker_b, caps=caps)

    async def answer(result):
        item = await broker_b.next(10)
        broker_b.reply(item["id"], {"v": 1, "ok": True, "result": result})

    try:
        answering = asyncio.create_task(answer(
            {"name": "proof.bin", "stream_file": str(source)}))
        reply = await session_control.request(
            a, runtime_a, machine_pub=machine_b.public_hex, op="output",
            body={}, timeout=10, stream=True)
        await answering
        assert reply["ok"] is True, reply
        result = reply["result"]
        assert result["stream"] == {"size": len(payload)}
        assert result["sha256"] == hashlib.sha256(payload).hexdigest()
        landed = Path(result["file"])
        assert landed.read_bytes() == payload
        assert landed.parent == data_root / "session-transfer"
        assert source.exists()                                # not asked to delete

        # A path outside the allowed roots is refused, never streamed.
        outside = data_root / "elsewhere.txt"
        outside.write_text("secret")
        answering = asyncio.create_task(answer({"stream_file": str(outside)}))
        reply = await session_control.request(
            a, runtime_a, machine_pub=machine_b.public_hex, op="output",
            body={}, timeout=10, stream=True)
        await answering
        assert reply["ok"] is False and reply["refusal"] == session_control.BAD_REQUEST

        # A staged transfer asked to be deleted is removed after streaming.
        staged = data_root / "session-transfer" / "staged.bundle"
        staged.write_bytes(b"bundle")
        answering = asyncio.create_task(answer(
            {"stream_file": str(staged), "stream_delete": True}))
        reply = await session_control.request(
            a, runtime_a, machine_pub=machine_b.public_hex, op="fetch-branch",
            body={}, timeout=10, stream=True)
        await answering
        assert Path(reply["result"]["file"]).read_bytes() == b"bundle"
        deadline = time.time() + 5
        while staged.exists() and time.time() < deadline:
            await asyncio.sleep(0.05)
        assert not staged.exists()
    finally:
        await _stop(a, task_a)
        await _stop(b, task_b)
