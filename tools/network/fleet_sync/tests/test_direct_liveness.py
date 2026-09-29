"""Direct pull channel liveness (auto-fkqz6): judged by data or a pong on wake,
never by a wall deadline.

The 2026-09-03 policy pinned the websocket library's keepalive at 20/20 on
both direct endpoints. The library fails the socket when its pong deadline
passes on the wall clock; a puller whose event loop is busy applying (SJC-2,
2026-09-29: loop stalls of 7-28 s) reads the pong late and so killed its own
pull with 1011 every ~90 s, and a 46k-transaction backlog drained one round
at a time. The relay pull path learned the same lesson on 2026-09-06
(fleet_relay_sync.PULL_PING_TIMEOUT_S: "frame silence, not pong latency, is
that caller's liveness signal").

Now: no library keepalive on the direct channel; an endpoint pings only
while it WAITS ON THE PEER and declares it dead only when neither data nor
the pong has arrived — checked with done() after the wait, so a local stall
resolves as alive on wake. A streaming serve is alive while its sends
complete; one that cannot complete a send in SERVE_SEND_STALL_S ends 1011.
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import time
from pathlib import Path

import pytest

from tools.graph.db import GraphDB
from tools.graph.models import Source
from tools.network import fleet_sync_channel as channel_mod
from tools.network import fleet_sync_scheduler as scheduler_mod
from tools.network.fleet_roster import enroll
from tools.network.fleet_sync_channel import (
    FleetAuthenticator,
    FleetDirectServer,
    PeerUnresponsive,
    wait_alive,
)
from tools.network.fleet_sync_scheduler import (
    FleetSyncRuntimeConfig,
    FleetSyncScheduler,
)
from tools.network.idkit import KeyPair


# ── wait_alive: the rule, in isolation ─────────────────────────────────

def _pinger(pong_after: float | None, sends: list | None = None):
    """A transport ping whose pong completes ``pong_after`` seconds later
    (None: never)."""
    async def ping():
        if sends is not None:
            sends.append(time.monotonic())
        pong = asyncio.get_running_loop().create_future()
        if pong_after is not None:
            asyncio.get_running_loop().call_later(pong_after, lambda: pong.done() or pong.set_result(0.001))
        return pong
    return ping


def test_data_that_arrives_needs_no_ping():
    async def run():
        sends: list = []
        result = await wait_alive(asyncio.sleep(0.05, result="frame"), ping=_pinger(0.01, sends),
                                  interval_s=0.2, timeout_s=0.2)
        assert result == "frame" and sends == []
    asyncio.run(run())


def test_a_slow_peer_that_answers_pongs_is_alive_until_its_data_comes():
    async def run():
        sends: list = []
        started = time.monotonic()
        result = await wait_alive(asyncio.sleep(0.55, result="late frame"), ping=_pinger(0.02, sends),
                                  interval_s=0.1, timeout_s=0.1)
        assert result == "late frame"
        assert len(sends) >= 3 and time.monotonic() - started >= 0.55
    asyncio.run(run())


def test_neither_data_nor_pong_is_a_dead_peer_in_interval_plus_timeout():
    async def run():
        started = time.monotonic()
        with pytest.raises(PeerUnresponsive, match="neither data nor a pong"):
            await wait_alive(asyncio.sleep(10, result="never"), ping=_pinger(None),
                             interval_s=0.1, timeout_s=0.1)
        assert 0.2 <= time.monotonic() - started < 1.0
    asyncio.run(run())


def test_a_ping_that_cannot_be_sent_is_a_peer_that_stopped_reading():
    async def run():
        async def stuck_ping():
            await asyncio.sleep(10)
        with pytest.raises(PeerUnresponsive, match="could not be sent"):
            await wait_alive(asyncio.sleep(10), ping=stuck_ping, interval_s=0.05, timeout_s=0.1)
    asyncio.run(run())


def test_a_local_loop_stall_longer_than_the_timeout_cannot_fail_a_peer_that_answered():
    """The failure on SJC-2, in miniature: the ping goes out, the loop then
    blocks for longer than the pong timeout, and the pong (and the frame)
    arrive while it is blocked. On wake both are done: alive, data returned.
    The library's keepalive judged the same situation dead."""
    async def run():
        loop = asyncio.get_running_loop()
        frame = loop.create_future()
        pong_future = loop.create_future()
        pinged = asyncio.Event()

        async def ping():
            pinged.set()
            return pong_future

        async def stall_after_ping():
            await pinged.wait()
            # Both answers land in the loop's ready queue, then the loop is
            # blocked past the timeout before either callback can run.
            loop.call_soon(pong_future.set_result, 0.002)
            loop.call_soon(frame.set_result, "frame after stall")
            time.sleep(0.5)   # a blocked event loop: nothing runs

        stall = asyncio.ensure_future(stall_after_ping())
        result = await wait_alive(frame, ping=ping, interval_s=0.05, timeout_s=0.1)
        await stall
        assert result == "frame after stall"
    asyncio.run(run())


def test_without_a_ping_the_wait_is_plain():
    async def run():
        assert await wait_alive(asyncio.sleep(0.01, result="x"), ping=None) == "x"
    asyncio.run(run())


# ── the serving side: a send that cannot complete ends the serve 1011 ──

def test_a_serve_whose_send_cannot_complete_closes_1011(monkeypatch):
    """serve_fleet_transport bounds every send by SERVE_SEND_STALL_S: a peer
    that stopped reading is closed 1011 with the reason, never held open
    forever (the direct listener's library keepalive is gone)."""
    from tools.network.fleet_sync_channel import serve_fleet_transport

    monkeypatch.setattr(channel_mod, "SERVE_SEND_STALL_S", 0.2)
    root, server_key, client_key = KeyPair.generate(), KeyPair.generate(), KeyPair.generate()
    entries = [enroll(root, machine_pub=server_key.public_hex), enroll(root, machine_pub=client_key.public_hex)]
    server_auth = FleetAuthenticator(server_key, root_pub=root.public_hex, roster_entries=lambda: entries)
    client_auth = FleetAuthenticator(client_key, root_pub=root.public_hex, roster_entries=lambda: entries)
    session = "ab" * 16

    async def run():
        to_server: asyncio.Queue = asyncio.Queue()
        # A one-slot pipe to the client: a send completes only when the
        # client has read the previous record, like a socket whose buffer
        # is full — a client that stops reading blocks the server's send.
        to_client: asyncio.Queue = asyncio.Queue(maxsize=1)
        closed: list = []

        async def server_recv():
            return await to_server.get()

        async def server_send(payload):
            await to_client.put(payload)

        async def close(code, reason):
            closed.append((code, reason))

        async def handler(_token, _message, _peer, **_kw):
            async def stream():
                for i in range(6):
                    yield b"frame-%d" % i
            return stream()

        serve = asyncio.ensure_future(serve_fleet_transport(
            token=session, recv=server_recv, send=server_send, handler=handler, close=close,
            authenticator=server_auth, ping=None))
        # Client half: handshake through the queues, one request, then stop reading.
        private_key, hello = client_auth.build_client_hello(session, peer=server_key.public_hex)
        await to_server.put(hello)
        from tools.network.relaykit.viewer import read_viewer_record
        from tools.network.relaykit.channel import ChannelCrypto
        import json
        server_hello = read_viewer_record(await to_client.get())
        server_eph, transcript = client_auth.verify_server(
            server_hello, session=session, client_eph=json.loads(hello)["eph_pub"],
            expected_machine_pub=server_key.public_hex)
        crypto = ChannelCrypto.client(private_key, server_eph, transcript)
        for record in crypto.seal_message(b"pull"):
            await to_server.put(record)
        await to_client.get()            # the first record read; then the client stops reading
        with pytest.raises(PeerUnresponsive, match="stopped reading"):
            await asyncio.wait_for(serve, 5)
        assert closed and closed[0][0] == 1011 and "stopped reading" in closed[0][1]
    asyncio.run(run())


# ── end to end over a real direct listener ─────────────────────────────

def _prepare(path: Path, machine: KeyPair) -> None:
    db = GraphDB(path)
    try:
        db.activate_fleet_sync_writers(machine.public_hex)
    finally:
        db.close()


def _fill(path: Path, count: int) -> None:
    db = GraphDB(path)
    try:
        for i in range(count):
            db.insert_source(Source(id=f"src-{i:06d}", type="note", title=f"row {i}"))
    finally:
        db.close()


def _count_sources(path: Path) -> int:
    with sqlite3.connect(path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0])


def _peer_row(path: Path, peer_pub: str) -> tuple[int, str | None]:
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(retries),0), MAX(last_error_code) "
            "FROM fleet_sync_peer_state WHERE machine_public_key=?", (peer_pub,),
        ).fetchone()
    return int(row[0]), row[1]


def _pair(tmp_path: Path, *, backlog: int):
    root, server_key, puller_key = KeyPair.generate(), KeyPair.generate(), KeyPair.generate()
    server_db, puller_db = tmp_path / "server.db", tmp_path / "puller.db"
    _prepare(server_db, server_key)
    _prepare(puller_db, puller_key)
    _fill(server_db, backlog)
    entries = [enroll(root, machine_pub=server_key.public_hex), enroll(root, machine_pub=puller_key.public_hex)]
    server = FleetSyncScheduler(FleetSyncRuntimeConfig(
        machine_key=server_key, personal_root_pub=root.public_hex, roster_entries=lambda: entries,
        peer_addresses=lambda: {}, personal_db_path=server_db, poll_interval=0.05,
        min_backoff=0.01, max_backoff=0.05, listen_host="127.0.0.1", listen_port=0,
    ))
    return root, server, server_key, puller_key, server_db, puller_db, entries


def _puller(puller_key, root, entries, puller_db, peers) -> FleetSyncScheduler:
    return FleetSyncScheduler(FleetSyncRuntimeConfig(
        machine_key=puller_key, personal_root_pub=root.public_hex, roster_entries=lambda: entries,
        peer_addresses=lambda: dict(peers), personal_db_path=puller_db, poll_interval=0.05,
        min_backoff=0.01, max_backoff=0.05,
        pull_first_frame_allowance_s=30.0, pull_stream_silence_limit_s=10.0,
    ))


async def _eventually(predicate, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.1)


def test_a_backlog_pull_survives_puller_loop_stalls_longer_than_the_ping_timeout(tmp_path, monkeypatch):
    """A lab pair: the puller's event loop is blocked for longer than the
    ping timeout, repeatedly, while frames stream (what SJC-2's apply did
    under the GIL). The pull completes in ONE round: zero retries, no 1011."""
    monkeypatch.setattr(channel_mod, "DIRECT_PING_INTERVAL_S", 0.2)
    monkeypatch.setattr(channel_mod, "DIRECT_PING_TIMEOUT_S", 0.2)
    backlog = 600
    root, server, server_key, puller_key, server_db, puller_db, entries = _pair(tmp_path, backlog=backlog)
    stalls = {"n": 0, "frames": 0}
    real_stream = scheduler_mod.bounded_stream_frames

    async def stalling_stream(channel, **kwargs):
        # The consumer's own work between frames, on the loop thread: block
        # the puller's loop past the ping timeout a few times mid-stream,
        # as a GIL-starved loop is blocked (SJC-2's stall stacks).
        async for item in real_stream(channel, **kwargs):
            stalls["frames"] += 1
            if stalls["n"] < 3 and stalls["frames"] % 40 == 0:
                stalls["n"] += 1
                time.sleep(0.6)
            yield item

    monkeypatch.setattr(scheduler_mod, "bounded_stream_frames", stalling_stream)

    async def run():
        await server.start()
        puller = _puller(puller_key, root, entries, puller_db,
                         {server_key.public_hex: [f"ws://127.0.0.1:{server.port}"]})
        await puller.start()
        try:
            await _eventually(lambda: _count_sources(puller_db) >= backlog, timeout=120)
        finally:
            await puller.stop()
            await server.stop()
        retries, error = _peer_row(puller_db, server_key.public_hex)
        assert (retries, error) == (0, None), (retries, error)
        assert stalls["n"] == 3
    asyncio.run(run())


def test_a_frozen_server_is_still_caught_on_the_first_frame_wait(tmp_path, monkeypatch):
    """The 2026-09-03 goal kept: a peer that answers nothing — no frame, no
    pong — is declared dead in interval + timeout on the first-frame wait,
    not after the 900 s first-frame allowance, and the error code names it."""
    monkeypatch.setattr(channel_mod, "DIRECT_PING_INTERVAL_S", 0.2)
    monkeypatch.setattr(channel_mod, "DIRECT_PING_TIMEOUT_S", 0.2)
    root, server_key, puller_key = KeyPair.generate(), KeyPair.generate(), KeyPair.generate()
    puller_db = tmp_path / "puller.db"
    _prepare(puller_db, puller_key)
    entries = [enroll(root, machine_pub=server_key.public_hex), enroll(root, machine_pub=puller_key.public_hex)]
    engaged = asyncio.Event
    frozen: dict = {}

    async def never_serves(_token, _message, _peer, **_kw):
        async def stream():
            frozen["engaged"].set()
            await frozen["release"].wait()
            return
            yield b""
        return stream()

    async def run():
        frozen["engaged"], frozen["release"] = engaged(), engaged()
        server = FleetDirectServer(
            FleetAuthenticator(server_key, root_pub=root.public_hex, roster_entries=lambda: entries), never_serves)
        await server.start()
        # A frozen peer: its transport answers no ping. The client's channel
        # ping is replaced by one whose pong never comes.
        from tools.network.relaykit import viewer as viewer_mod

        async def dead_ping(self):
            return asyncio.get_running_loop().create_future()
        monkeypatch.setattr(viewer_mod.ViewerChannel, "ping", dead_ping)
        puller = _puller(puller_key, root, entries, puller_db,
                         {server_key.public_hex: [f"ws://127.0.0.1:{server.port}"]})
        started = time.monotonic()
        await puller.start()
        try:
            await _eventually(lambda: _peer_row(puller_db, server_key.public_hex)[0] >= 1, timeout=10)
        finally:
            frozen["release"].set()
            await puller.stop()
            await server.stop()
        retries, error = _peer_row(puller_db, server_key.public_hex)
        assert retries >= 1 and error == "PeerUnresponsive"
        assert time.monotonic() - started < 8.0
    asyncio.run(run())


# ── after the reply is written, the listener never pings the applying puller ──

def test_the_listener_does_not_ping_a_puller_that_is_applying_the_tail_of_its_reply(monkeypatch):
    """Reviewer auto-0925-123637 on 1fe61c3b: after the last send, the
    listener's next request-wait pinged the puller, which may be stalled or
    paused while applying the reply's tail from its buffers; a 1011 there
    aborts the TCP connection and discards the unread tail. Now the wait
    after a reply is a plain on-wake bound; the puller's close ends it."""
    from tools.network.fleet_sync_channel import serve_fleet_transport

    monkeypatch.setattr(channel_mod, "DIRECT_PING_INTERVAL_S", 0.05)
    monkeypatch.setattr(channel_mod, "DIRECT_PING_TIMEOUT_S", 0.05)
    root, server_key, client_key = KeyPair.generate(), KeyPair.generate(), KeyPair.generate()
    entries = [enroll(root, machine_pub=server_key.public_hex), enroll(root, machine_pub=client_key.public_hex)]
    server_auth = FleetAuthenticator(server_key, root_pub=root.public_hex, roster_entries=lambda: entries)
    client_auth = FleetAuthenticator(client_key, root_pub=root.public_hex, roster_entries=lambda: entries)
    session = "cd" * 16

    async def run():
        import json
        from tools.network.relaykit.channel import ChannelCrypto
        from tools.network.relaykit.viewer import read_viewer_record

        to_server: asyncio.Queue = asyncio.Queue()
        to_client: asyncio.Queue = asyncio.Queue()
        pings: list = []
        closed: list = []

        async def server_recv():
            return await to_server.get()

        async def server_send(payload):
            await to_client.put(payload)

        async def ping():
            pings.append(time.monotonic())
            return asyncio.get_running_loop().create_future()   # no pong will come: the puller is busy

        async def close(code, reason):
            closed.append((code, reason))

        async def handler(_token, _message, _peer, **_kw):
            async def stream():
                for i in range(3):
                    yield b"frame-%d" % i
            return stream()

        serve = asyncio.ensure_future(serve_fleet_transport(
            token=session, recv=server_recv, send=server_send, handler=handler, close=close,
            authenticator=server_auth, ping=ping))
        private_key, hello = client_auth.build_client_hello(session, peer=server_key.public_hex)
        await to_server.put(hello)
        server_hello = read_viewer_record(await to_client.get())
        server_eph, transcript = client_auth.verify_server(
            server_hello, session=session, client_eph=json.loads(hello)["eph_pub"],
            expected_machine_pub=server_key.public_hex)
        crypto = ChannelCrypto.client(private_key, server_eph, transcript)
        for record in crypto.seal_message(b"pull"):
            await to_server.put(record)
        records = [await to_client.get() for _ in range(3)]   # the whole reply, read from the buffers
        pings_before = len(pings)
        # The puller now applies the tail for far longer than interval + timeout.
        await asyncio.sleep(0.5)
        assert not serve.done() and closed == [] and len(pings) == pings_before
        await to_server.put(None)   # the puller closes when it is done
        await asyncio.wait_for(serve, 2)
        assert closed == [] and len(records) == 3
    asyncio.run(run())


def test_the_silence_bound_is_judged_on_wake():
    """A frame that arrives while the puller's loop is blocked longer than
    the silence limit is a delivered frame, not a silent peer."""
    from tools.network.fleet_sync_channel import wait_bounded_on_wake

    async def run():
        loop = asyncio.get_running_loop()
        frame = loop.create_future()

        async def stall():
            await asyncio.sleep(0.05)
            loop.call_soon(frame.set_result, "frame")
            time.sleep(0.4)   # blocked past the 0.2 s bound; the frame lands during it

        stalled = asyncio.ensure_future(stall())
        assert await wait_bounded_on_wake(frame, 0.2) == "frame"
        await stalled
        with pytest.raises(asyncio.TimeoutError):
            await wait_bounded_on_wake(asyncio.sleep(10), 0.1)
    asyncio.run(run())
