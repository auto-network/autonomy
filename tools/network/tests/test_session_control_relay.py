"""session-control/1 end to end on a real relay with two real connectors
(graph://7eb29bc8-31a §9.1, bead auto-99ioi): the pair is brokered under its
own capability, the session:control handshake proves the sender, one request
gets one reply from the destination's dashboard broker, and every failure is a
typed refusal -- including a relay that predates the capability."""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
import time

import pytest

from tools.network import fleet_roster, session_control
from tools.network.fleet_sync_channel import FleetAuthenticator, serve_fleet_transport
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
    """What connector_runtime exposes: an armed scheduler's authenticator,
    whose one process delegation carries session:control beside fleet:sync
    when the browser granted it."""

    def __init__(self, root, machine, machine_id, roster, *, session_scope=True):
        self.process = KeyPair.generate()
        self._mint = (root, machine, machine_id)
        self.scheduler = type("S", (), {})()
        self.scheduler.authenticator = FleetAuthenticator(
            self.process, root_pub=root.public_hex,
            roster_entries=lambda: roster, roster_machine_pub=machine.public_hex,
            delegation_cert=self._cert(session_scope), require_delegation=True,
        )

    def _cert(self, session_scope):
        root, machine, machine_id = self._mint
        now = int(time.time())
        scope = ["fleet:sync", "session:control"] if session_scope else ["fleet:sync"]
        return issue_cert(
            machine, self.process.public_hex, scope=scope,
            org=f"personal:{root.public_hex}",
            subject=Subject(kind="machine", id=machine_id),
            not_before=now - 30, not_after=now + 3600,
        )

    def grant(self, session_scope):
        """Re-arm with or without the session:control scope."""
        self.scheduler.authenticator.delegation_cert = self._cert(session_scope)


async def _connector(port, root, machine, runtime, broker, *, caps, offer=None):
    """A connector; its session-control pairs are served by the dashboard
    *broker* path, or by *offer* (a test's own handler, see _test_offer)."""
    serve_key = KeyPair.generate()
    now = int(time.time())
    cert = issue_cert(root, serve_key.public_hex, scope=("tunnel:serve",), org=ORG,
                      subject=Subject("persona", PERSONA),
                      not_before=now - 100, not_after=now + 30 * 86400)
    connector = TunnelConnector(
        f"ws://127.0.0.1:{port}", ORG, serve_key, cert, machine_key=machine,
        caps=caps,
        session_control_offer=offer or session_control.session_control_offer_handler(
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


def test_session_control_request_reply_over_the_relay(caplog):
    root = KeyPair.generate()
    port = _free_port()
    caplog.set_level("INFO", logger="fleet.session_control")
    with _live_registry(port) as app:
        asyncio.run(_scenario(root, port, app.state.directed_streams))
    # Each open names its steps (auto-5ovcb); a reused channel opened nothing.
    lines = [r.getMessage() for r in caplog.records
             if r.getMessage().startswith("session-control request op=status")]
    assert "channel=new" in lines[0]
    for step in ("slot_ms=", "pair_ms=", "handshake_ms="):
        assert step in lines[0], lines[0]
    assert "channel=reused" in lines[1] and "steps=none" in lines[1]


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
        # The channel stays open: a second request reuses it, with no new
        # pair and no second handshake.
        assert broker.snapshot()["pairs"] == 1
        answering = asyncio.create_task(_answer(broker_b))
        started = time.monotonic()
        reply = await session_control.request(
            a, runtime_a, machine_pub=machine_b.public_hex, op="status",
            body={"n": 2}, timeout=5)
        reused_elapsed = time.monotonic() - started
        await answering
        assert reply["result"]["body"] == {"n": 2}
        assert broker.snapshot()["pairs"] == 1
        assert reused_elapsed < elapsed

        # A machine that is not in the roster is refused before any pair.
        reply = await session_control.request(
            a, runtime_a, machine_pub="cc" * 32, op="status", body={}, timeout=5)
        assert reply == {"v": 1, "ok": False, "refusal": "peer-not-in-roster",
                         "detail": reply.get("detail"), "at": "local"}

        # A runtime whose delegation lacks session:control cannot ask, and
        # says so by name, decided here.
        runtime_a.grant(False)
        reply = await session_control.request(
            a, runtime_a, machine_pub=machine_b.public_hex, op="status", body={},
            timeout=5)
        assert (reply["refusal"], reply["at"]) == (session_control.NOT_GRANTED, "local")

        # A destination whose delegation lacks it answers the pair with the
        # same typed refusal in place of its hello: decided THERE.
        runtime_a.grant(True)
        runtime_b.grant(False)
        reply = await session_control.request(
            a, runtime_a, machine_pub=machine_b.public_hex, op="status", body={},
            timeout=5)
        assert (reply["ok"], reply["refusal"], reply["at"]) == (
            False, session_control.NOT_GRANTED, "peer"), reply

        # A destination whose handshake check refuses our hello names that
        # check: here B's roster no longer carries A.
        runtime_b.grant(True)
        auth_b = runtime_b.scheduler.authenticator
        auth_b._roster_entries = lambda: (roster[1],)
        auth_b.invalidate_authorization_cache()
        reply = await session_control.request(
            a, runtime_a, machine_pub=machine_b.public_hex, op="status", body={},
            timeout=5)
        assert (reply["ok"], reply["refusal"], reply["at"]) == (
            False, "peer-not-in-roster", "peer"), reply
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


def test_a_failed_open_names_the_step_it_failed_at(caplog):
    """A slow or failed open logged nothing about which step it spent its
    time in (auto-5ovcb): the slot lookup, the relay pairing the stream, or
    the handshake."""
    from tools.network.relaykit.fleet_stream import FleetStreamClosed

    caplog.set_level("INFO", logger="fleet.session_control")

    class Streams:
        async def open(self, *a, **k):
            await asyncio.sleep(0.05)
            raise FleetStreamClosed("pair-1", "declined")

    class Connector:
        session_streams = Streams()

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
        await asyncio.sleep(0.03)
        return (PERSONA, machine_b.public_hex), "org-slots"

    reply = asyncio.run(session_control.request(
        Connector(), runtime, machine_pub=machine_b.public_hex, op="tail",
        body={}, timeout=2, resolve_slot=slot, stream=True))
    assert reply["refusal"] == session_control.PEER_CLOSED_AT_OPEN
    (line,) = [r.getMessage() for r in caplog.records
               if "session-control open" in r.getMessage()]
    assert "failed at=pair" in line and "slot_ms=" in line, line
    after_ms = int(line.split("after_ms=")[1].split()[0])
    slot_ms = int(line.split("slot_ms=")[1].split()[0])
    assert after_ms >= 40 and slot_ms >= 20


def test_a_relay_that_did_not_negotiate_the_capability_is_refused_by_name():
    class Connector:
        session_streams = None

    reply = asyncio.run(session_control.request(
        Connector(), object(), machine_pub="bb" * 32, op="status", body={}))
    assert reply == {"v": 1, "ok": False,
                     "refusal": session_control.NOT_NEGOTIATED,
                     "detail": "the relay did not negotiate session-control/1",
                     "at": "local"}


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
        assert reply["ok"] is False
        assert (reply["refusal"], reply["at"]) == (session_control.FILE_NOT_STREAMABLE, "peer")

        # A staged transfer asked to be deleted is removed after streaming.
        staged = data_root / "session-transfer" / "staged.bundle"
        staged.write_bytes(b"bundle")
        answering = asyncio.create_task(answer(
            {"stream_file": str(staged), "stream_delete": True}))
        reply = await session_control.request(
            a, runtime_a, machine_pub=machine_b.public_hex, op="output",
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


def test_open_streamable_refuses_a_file_swapped_for_a_symlink(tmp_path, monkeypatch):
    """The check-then-open race: once the checked file is replaced by a
    symlink to a file outside the roots, nothing is opened."""
    import os

    monkeypatch.setattr(session_control, "_data_root", lambda: tmp_path)
    runs = tmp_path / "agent-runs" / "auto-1-x"
    runs.mkdir(parents=True)
    secret = tmp_path / "secret.db"
    secret.write_text("vault")
    checked = runs / "out.txt"
    checked.write_text("ok")
    assert session_control.stream_path_allowed(checked)
    opened = session_control.open_streamable(checked)
    assert opened is not None
    os.close(opened[0])
    checked.unlink()
    checked.symlink_to(secret)                     # the swap
    assert session_control.open_streamable(checked) is None


def test_open_streamable_refuses_a_directory_swapped_for_a_symlink(tmp_path, monkeypatch):
    """O_NOFOLLOW guards only the last component; a swapped parent directory
    is caught by checking the open descriptor's real path."""
    import os

    monkeypatch.setattr(session_control, "_data_root", lambda: tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "out.txt").write_text("secret")
    runs = tmp_path / "agent-runs"
    runs.mkdir()
    (runs / "auto-1-x").symlink_to(outside, target_is_directory=True)
    assert session_control.open_streamable(runs / "auto-1-x" / "out.txt") is None


def test_open_streamable_announces_the_size_of_what_it_opened(tmp_path, monkeypatch):
    import os

    monkeypatch.setattr(session_control, "_data_root", lambda: tmp_path)
    runs = tmp_path / "agent-runs" / "auto-1-x"
    runs.mkdir(parents=True)
    (runs / "out.txt").write_bytes(b"12345")
    fd, size = session_control.open_streamable(runs / "out.txt")
    os.close(fd)
    assert size == 5
    monkeypatch.setattr(session_control, "MAX_STREAM_BYTES", 4)
    assert session_control.open_streamable(runs / "out.txt") is None


def test_a_dripping_sender_is_cut_off_by_the_overall_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(session_control, "_data_root", lambda: tmp_path)
    monkeypatch.setattr(session_control, "TRANSFER_DEADLINE_CAP_S", 0.3)

    class Drip:
        def recv_message_stream(self):
            async def gen():
                yield json.dumps({"v": 1, "ok": True, "result": {
                    "stream": {"size": 1000}}}).encode(), False
                while True:
                    await asyncio.sleep(0.1)
                    yield b"x", False
            return gen()

    with pytest.raises(session_control.SessionControlError, match="deadline"):
        asyncio.run(session_control._receive_stream(Drip(), timeout=1.0))
    assert not list((tmp_path / "session-transfer").glob("in-*"))


def test_open_streamable_refuses_a_fifo_without_blocking(tmp_path, monkeypatch):
    import os

    monkeypatch.setattr(session_control, "_data_root", lambda: tmp_path)
    runs = tmp_path / "agent-runs" / "auto-1-x"
    runs.mkdir(parents=True)
    os.mkfifo(runs / "out.txt")
    started = time.monotonic()
    assert session_control.open_streamable(runs / "out.txt") is None
    assert time.monotonic() - started < 1.0


def test_a_non_streamed_reply_may_not_name_a_local_file(tmp_path, monkeypatch):
    """``result.file`` is set by the receiver to name the transfer it wrote.
    A peer that puts one in its header (a single final frame, no stream)
    must not have it opened, served and unlinked on this machine."""
    monkeypatch.setattr(session_control, "_data_root", lambda: tmp_path)

    class OneFrame:
        def recv_message_stream(self):
            async def gen():
                yield json.dumps({"v": 1, "ok": True, "result": {
                    "file": "/etc/passwd", "sha256": "00", "tail": {"ok": True}}}).encode(), True
            return gen()

    header = asyncio.run(session_control._receive_stream(OneFrame(), timeout=1.0))
    assert header["ok"] is True
    assert "file" not in header["result"] and "sha256" not in header["result"]
    assert header["result"]["tail"] == {"ok": True}


# ── T2: replies of any size or duration on the one connection ───────────────
# (graph://9642ab99-bae v4). A reply is a sequence of bounded id-tagged
# chunks, the last carrying the end flag; a tagged live reply ends with an
# explicit empty end chunk; ``cancel {id}`` stops one; the client reads a
# reply's chunks by id from the pooled connection with
# _PeerConnection.stream(). Proven with the TEST's handler behind the real
# fleet handshake: nothing here goes through the paths stage 2 deletes.

BIG_BYTES = 50 * 1024 * 1024
CHUNK = 192 * 1024


class _LiveReply:
    """A handler response with no last chunk of its own (relaykit ``live``)."""

    live = True

    def __init__(self, iterator):
        self._iterator = iterator

    def __aiter__(self):
        return self._iterator


class _Handler:
    """The serving side's replies, as the record loop sees them: one chunk,
    a finite sequence, a live sequence, or one that runs until cancelled."""

    def __init__(self, payload: bytes = b""):
        self.payload = payload
        self.events: asyncio.Queue = asyncio.Queue()      # live chunks; None ends it
        self.endless_closed = asyncio.Event()             # the endless generator's finally ran

    async def __call__(self, _token, message, client_pub, **_extra):
        request = session_control.parse_request(message)
        op = request["op"]
        if op == "status":
            return session_control.encode(
                {"v": 1, "ok": True, "result": {"n": request["body"]["n"]}})
        if op == "big":
            return self._big()
        if op == "live":
            return _LiveReply(self._live())
        if op == "endless":
            return self._endless()
        return session_control.encode(session_control.refusal("unknown-op", op))

    async def _big(self):
        for offset in range(0, len(self.payload), CHUNK):
            yield self.payload[offset:offset + CHUNK]

    async def _live(self):
        while (event := await self.events.get()) is not None:
            yield event

    async def _endless(self):
        try:
            for _ in range(2000):            # bounded, so a missed cancel still ends
                yield b"\x5a" * CHUNK
                await asyncio.sleep(0)
        finally:
            self.endless_closed.set()


def _test_offer(runtime, handler):
    """The serving connector's ``session_control_offer`` with *handler* in
    place of the dashboard broker: the relay pair and the session:control
    handshake are real; the replies are the test's."""
    tasks: set = set()

    async def on_offer(endpoint) -> bool:
        async def run():
            await endpoint.ready.wait()
            if endpoint.closed.is_set():
                return

            async def close(**_kwargs):
                await endpoint.close()

            try:
                await serve_fleet_transport(
                    token=endpoint.session, recv=endpoint.recv, send=endpoint.send,
                    handler=handler, close=close,
                    authenticator=session_control.session_authenticator(runtime))
            except Exception:
                pass
            finally:
                with contextlib.suppress(Exception):
                    await endpoint.close()

        task = asyncio.create_task(run())
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return True

    return on_offer


async def _two(port, root, handler):
    """A and B over the relay; B serves *handler*."""
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
    a, task_a = await _connector(port, root, machine_a, runtime_a,
                                 session_control.InboundBroker(), caps=caps)
    b, task_b = await _connector(port, root, machine_b, runtime_b, None, caps=caps,
                                 offer=_test_offer(runtime_b, handler))
    return a, task_a, b, task_b, runtime_a, machine_b


def _request_record(op, body=None):
    return session_control.encode({"v": 1, "op": op, "body": body or {}})


async def _warm(a, runtime_a, machine_b):
    """Open the pooled connection with one request; the pool entry."""
    warm = await session_control.request(a, runtime_a, machine_pub=machine_b.public_hex,
                                         op="status", body={"n": -1}, timeout=10)
    assert warm["ok"] is True, warm
    return session_control._pool[(machine_b.public_hex, session_control.SCOPE_PERSONAL)]


def test_a_50_mb_reply_and_twenty_small_requests_share_one_pair(monkeypatch):
    monkeypatch.setattr(session_control, "_pool", {})
    import hashlib
    import os

    payload = os.urandom(1024 * 1024) * (BIG_BYTES // (1024 * 1024))
    root = KeyPair.generate()
    port = _free_port()
    with _live_registry(port) as app:
        asyncio.run(_big_scenario(root, port, app.state.directed_streams,
                                  _Handler(payload), hashlib.sha256(payload).hexdigest()))


async def _big_scenario(root, port, custody, handler, digest):
    import hashlib

    a, task_a, b, task_b, runtime_a, machine_b = await _two(port, root, handler)
    try:
        entry = await _warm(a, runtime_a, machine_b)
        reply = await entry.stream(_request_record("big"))
        started = time.monotonic()
        finished: dict[int, float] = {}
        received = hashlib.sha256()
        size = 0

        async def consume():
            nonlocal size
            async for chunk, _end in reply:
                received.update(chunk)
                size += len(chunk)
            return time.monotonic() - started

        async def small(n):
            got = await session_control.request(
                a, runtime_a, machine_pub=machine_b.public_hex, op="status",
                body={"n": n}, timeout=10)
            finished[n] = time.monotonic() - started
            return got

        big_done, *smalls = await asyncio.gather(consume(), *(small(n) for n in range(20)))
        assert [r["result"]["n"] for r in smalls] == list(range(20))
        # Every small request completed while the large reply was still
        # arriving: none of them waited behind it.
        assert max(finished.values()) < big_done, (finished, big_done)
        assert size == BIG_BYTES and received.hexdigest() == digest
        # The relay's own custody count: ONE pair carried all of it.
        assert custody.snapshot()["pairs"] == 1
        assert entry.pending == {}
    finally:
        await _stop(a, task_a)
        await _stop(b, task_b)


def test_a_live_reply_and_requests_share_the_connection(monkeypatch):
    monkeypatch.setattr(session_control, "_pool", {})
    root = KeyPair.generate()
    port = _free_port()
    with _live_registry(port) as app:
        asyncio.run(_live_scenario(root, port, app.state.directed_streams, _Handler()))


async def _live_scenario(root, port, custody, handler):
    a, task_a, b, task_b, runtime_a, machine_b = await _two(port, root, handler)
    try:
        entry = await _warm(a, runtime_a, machine_b)
        live = await entry.stream(_request_record("live"))
        seen = []
        for n in range(3):
            # One chunk per event as the host produces it, with requests
            # interleaving on the same pooled connection.
            handler.events.put_nowait(json.dumps({"n": n}).encode())
            got = await session_control.request(
                a, runtime_a, machine_pub=machine_b.public_hex, op="status",
                body={"n": n}, timeout=10)
            assert got["result"] == {"n": n}
            chunk, final = await live.next(5)
            assert not final
            seen.append(json.loads(chunk)["n"])
        assert seen == [0, 1, 2]
        assert custody.snapshot()["pairs"] == 1
        assert len(entry.pending) == 1 and entry.busy()    # the live reply keeps it busy
        # The host ends it: the explicit end is an empty chunk with the end
        # flag; the reply's state is gone here and the connection lives on.
        handler.events.put_nowait(None)
        assert await live.next(5) == (b"", True)
        assert entry.pending == {} and not entry.busy()
        got = await session_control.request(
            a, runtime_a, machine_pub=machine_b.public_hex, op="status",
            body={"n": 9}, timeout=10)
        assert got["result"] == {"n": 9} and custody.snapshot()["pairs"] == 1
    finally:
        await _stop(a, task_a)
        await _stop(b, task_b)


def test_cancel_ends_a_reply_and_frees_its_state(monkeypatch):
    monkeypatch.setattr(session_control, "_pool", {})
    root = KeyPair.generate()
    port = _free_port()
    with _live_registry(port) as app:
        asyncio.run(_cancel_scenario(root, port, app.state.directed_streams, _Handler()))


async def _cancel_scenario(root, port, custody, handler):
    a, task_a, b, task_b, runtime_a, machine_b = await _two(port, root, handler)
    try:
        entry = await _warm(a, runtime_a, machine_b)
        reply = await entry.stream(_request_record("endless"))
        for _ in range(2):
            chunk, final = await reply.next(10)
            assert len(chunk) == CHUNK and not final
        await reply.cancel()
        # This side forgot it at once; the host's generator was closed.
        assert entry.pending == {} and reply.ended
        await asyncio.wait_for(handler.endless_closed.wait(), 10)
        # The connection is intact: the cancel landed between whole chunks,
        # so the next request on it is answered, still on the one pair.
        got = await session_control.request(
            a, runtime_a, machine_pub=machine_b.public_hex, op="status",
            body={"n": 7}, timeout=10)
        assert got["result"] == {"n": 7}, got
        assert custody.snapshot()["pairs"] == 1 and entry.usable()
    finally:
        await _stop(a, task_a)
        await _stop(b, task_b)


# ── the record loop itself, with a fake crypto ──────────────────────────────


class _PlainCrypto:
    """Records are the messages; a flag suffix shows the end bit."""

    def open_record(self, record):
        return record

    def iter_seal_message(self, message, *, stream_final=True):
        yield message + (b"|end" if stream_final else b"|more")


class _TwoChunks:
    live = True

    def __init__(self):
        self._chunks = iter((b"one", b"two"))

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._chunks)
        except StopIteration:
            raise StopAsyncIteration from None


async def _serve_once(first: bytes, handler, sent: list):
    from tools.network.relaykit import connector as relay_connector

    inbox = asyncio.Queue()
    inbox.put_nowait(first)

    async def recv():
        if inbox.empty():
            await asyncio.sleep(0.05)
            return None
        return await inbox.get()

    async def send(out):
        sent.append(out)

    await relay_connector._serve_channel_records(
        _PlainCrypto(), token="t", recv=recv, send=send, handler=handler)


def test_a_tagged_live_reply_ends_with_an_explicit_empty_end():
    """A live reply has no last chunk of its own: when its iterator ends,
    the loop sends an empty chunk carrying the end flag, so a reader that
    routes by id knows the reply is over without the connection closing."""
    from tools.network.relaykit import connector as relay_connector

    sent: list[bytes] = []
    asyncio.run(_serve_once(relay_connector.tag_message(b"1" * 8, b"live"),
                            lambda _t, _m: _TwoChunks(), sent))
    assert [relay_connector.split_request_id(out) for out in sent] == [
        (b"1" * 8, b"one|more"), (b"1" * 8, b"two|more"), (b"1" * 8, b"|end")]


def test_an_untagged_live_reply_sends_no_extra_message():
    """T1's rule: a message without an id is served exactly as before ids.
    The legacy subscription channel is untagged and ends with the channel;
    it must not get the explicit end."""
    sent: list[bytes] = []
    asyncio.run(_serve_once(b"live", lambda _t, _m: _TwoChunks(), sent))
    assert sent == [b"one|more", b"two|more"]


def test_a_cancel_mid_chunk_never_tears_the_framing():
    """The record loop sends a chunk's records whole even when the cancel
    arrives between two of them: the receiver would otherwise read the next
    reply's records as the tail of a torn chunk."""
    from tools.network.relaykit import connector as relay_connector

    PARTS = 3

    class Crypto:
        def open_record(self, record):
            return record

        def iter_seal_message(self, message, *, stream_final=True):
            for i in range(1, PARTS + 1):
                yield message + b"|%d/%d|%d" % (i, PARTS, stream_final)

    sent: list[bytes] = []
    mid = asyncio.Event()          # the second record of the second chunk is going out
    release = asyncio.Event()      # ...and is held here until the cancel landed
    generator_closed = asyncio.Event()

    class Live:
        live = True

        def __init__(self):
            self._n = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            # Bounded: a test whose cancel never lands must end, not fill memory.
            if self._n >= 20:
                raise StopAsyncIteration
            self._n += 1
            await asyncio.sleep(0)
            return b"chunk%d" % self._n

        async def aclose(self):
            generator_closed.set()

    def handler(_token, message):
        return Live() if message == b"live" else b"plain"

    async def send(out):
        sent.append(out)
        # The record is tagged with the request id ahead of the chunk.
        if relay_connector.split_request_id(out)[1].startswith(b"chunk2|2/"):
            mid.set()
            await release.wait()

    async def run():
        steps = iter(("live", "cancel", "plain", "end"))

        async def recv():
            step = next(steps)
            if step == "live":
                return relay_connector.tag_message(b"1" * 8, b"live")
            if step == "cancel":
                await asyncio.wait_for(mid.wait(), 5)
                return relay_connector.tag_cancel(b"1" * 8)
            if step == "plain":
                await asyncio.sleep(0.02)      # the cancel has been delivered
                release.set()
                return relay_connector.tag_message(b"2" * 8, b"plain")
            await asyncio.sleep(0.05)
            return None

        await relay_connector._serve_channel_records(
            Crypto(), token="t", recv=recv, send=send, handler=handler)

    asyncio.run(run())
    assert generator_closed.is_set()
    ones = [out for out in sent if relay_connector.split_request_id(out)[0] == b"1" * 8]
    # chunk2 went out whole; nothing of the live reply followed the cancel.
    assert [o.split(b"|")[1] for o in ones[-PARTS:]] == [b"1/3", b"2/3", b"3/3"]
    assert all(o.startswith(relay_connector.tag_message(b"1" * 8, b"chunk2")) for o in ones[-PARTS:])
    twos = [out for out in sent if relay_connector.split_request_id(out)[0] == b"2" * 8]
    assert [o.split(b"|")[1:] for o in twos] == [[b"1/3", b"1"], [b"2/3", b"1"], [b"3/3", b"1"]]
