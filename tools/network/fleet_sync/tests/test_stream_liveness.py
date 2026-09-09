"""Stream liveness policy (auto-fzy8s) on the direct pull channel.

Two client-side bounds with distinct rationales: a first-frame allowance
sized for the server's silent bootstrap phase, and a tight
inter-frame bound for the structurally small gaps everywhere else.
Transport ping/pong (pinned at both direct-channel endpoints) already
covers dead and frozen peers; these bounds cover the one class it cannot
see — a serve wedged while its event loop keeps answering pongs, which
before this policy froze the puller's entire round loop forever with
peer-state stuck at online=1 and nothing recorded (proven live 2026-09-03
against the pre-policy code).
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
from tools.network.fleet_roster import enroll
from tools.network.fleet_sync_channel import (
    FleetAuthenticator,
    FleetDirectServer,
)
from tools.network.fleet_sync_scheduler import (
    FleetSyncFirstFrameSilence,
    FleetSyncRuntimeConfig,
    FleetSyncScheduler,
    FleetSyncStreamSilence,
    bounded_stream_frames,
    canonical_json,
    encode_done,
    encode_transaction_header,
)
from tools.network.idkit import KeyPair


def _prepare(path: Path, machine: KeyPair) -> None:
    db = GraphDB(path)
    try:
        db.activate_fleet_sync_writers(machine.public_hex)
    finally:
        db.close()


def _insert(path: Path, source_id: str, title: str) -> None:
    db = GraphDB(path)
    try:
        db.insert_source(Source(id=source_id, type="note", title=title))
    finally:
        db.close()


def _title(path: Path, source_id: str) -> str | None:
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT title FROM sources WHERE id=?", (source_id,)
        ).fetchone()
    return None if row is None else str(row[0])


def _peer_row(path: Path, peer_pub: str) -> tuple[int, str | None]:
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(retries),0), MAX(last_error_code) "
            "FROM fleet_sync_peer_state WHERE machine_public_key=?",
            (peer_pub,),
        ).fetchone()
    return int(row[0]), row[1]


def _acknowledgements(path: Path) -> int:
    with sqlite3.connect(path) as conn:
        return int(conn.execute(
            "SELECT COALESCE(SUM(acknowledgements),0) "
            "FROM fleet_sync_peer_state"
        ).fetchone()[0])


async def _eventually(predicate, *, timeout: float = 20.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition did not become true")
        await asyncio.sleep(0.02)


def _fleet(tmp_path: Path, extra_peers: int = 1):
    root = KeyPair.generate()
    puller_key = KeyPair.generate()
    peer_keys = [KeyPair.generate() for _ in range(extra_peers)]
    puller_db = tmp_path / "puller.db"
    _prepare(puller_db, puller_key)
    entries = [enroll(root, machine_pub=puller_key.public_hex)] + [
        enroll(root, machine_pub=key.public_hex) for key in peer_keys
    ]
    return root, puller_key, peer_keys, puller_db, entries


def _server(key, root, entries, handler) -> FleetDirectServer:
    return FleetDirectServer(
        FleetAuthenticator(
            key, root_pub=root.public_hex, roster_entries=lambda: entries
        ),
        handler,
    )


def _puller(
    puller_key, root, entries, puller_db, peers,
    *, first_allowance: float, silence_limit: float,
) -> FleetSyncScheduler:
    return FleetSyncScheduler(FleetSyncRuntimeConfig(
        machine_key=puller_key,
        personal_root_pub=root.public_hex,
        roster_entries=lambda: entries,
        peer_addresses=lambda: dict(peers),
        personal_db_path=puller_db,
        poll_interval=0.05,
        min_backoff=0.01,
        max_backoff=0.05,
        pull_first_frame_allowance_s=first_allowance,
        pull_stream_silence_limit_s=silence_limit,
    ))


def _wedged_handler(engaged: asyncio.Event, release: asyncio.Event):
    """A serve that wedges with a live event loop — and a release valve.

    The valve matters: FleetDirectServer.stop() waits for its handler
    tasks, so a handler wedged forever would hang the TEST's own
    teardown (the engine under test shuts down fine — proven — but the
    fake server would not)."""
    async def handler(_token, _message, _peer):
        async def stream():
            engaged.set()
            await release.wait()
            return
            yield b""  # marks this function as an async generator
        return stream()
    return handler


def test_wedged_serve_recovers_the_round_loop_and_names_the_wedge(
    tmp_path: Path,
) -> None:
    """The proven pre-policy defect, now bounded: a serve that never sends
    a frame (event loop alive, transport healthy) times out at the
    first-frame allowance, is recorded under its own error code, and the
    round loop keeps syncing the healthy peer."""
    async def run() -> None:
        root, puller_key, (wedged_key, healthy_key), puller_db, entries = (
            _fleet(tmp_path, extra_peers=2)
        )
        healthy_db = tmp_path / "healthy.db"
        _prepare(healthy_db, healthy_key)
        healthy = FleetSyncScheduler(FleetSyncRuntimeConfig(
            machine_key=healthy_key,
            personal_root_pub=root.public_hex,
            roster_entries=lambda: entries,
            peer_addresses=lambda: {},
            personal_db_path=healthy_db,
            poll_interval=0.05,
        ))
        await healthy.start()
        engaged = asyncio.Event()
        release = asyncio.Event()
        wedged = _server(
            wedged_key, root, entries, _wedged_handler(engaged, release)
        )
        await wedged.start()
        puller = _puller(
            puller_key, root, entries, puller_db,
            {
                wedged_key.public_hex: [f"ws://127.0.0.1:{wedged.port}"],
                healthy_key.public_hex: [f"ws://127.0.0.1:{healthy.port}"],
            },
            first_allowance=1.0, silence_limit=0.4,
        )
        _insert(puller_db, "puller-seed", "keeps the delta path")
        await puller.start()
        try:
            await asyncio.wait_for(engaged.wait(), 20)
            _insert(healthy_db, "after-wedge", "must still cross")
            # The wedged pull must fail within its bound and be NAMED...
            await _eventually(lambda: _peer_row(
                puller_db, wedged_key.public_hex
            )[0] > 0)
            _retries, error_code = _peer_row(
                puller_db, wedged_key.public_hex
            )
            assert error_code == "FleetSyncFirstFrameSilence"
            # ...and the round loop must survive it: the healthy peer's
            # post-wedge write crosses. Pre-policy this froze forever.
            await _eventually(
                lambda: _title(puller_db, "after-wedge") == "must still cross"
            )
        finally:
            release.set()
            await puller.stop()
            await healthy.stop()
            await wedged.stop()

    asyncio.run(run())


def test_slow_silent_first_frame_completes_without_a_kill(
    tmp_path: Path,
) -> None:
    """The revert's lesson, encoded: a silence several times the
    inter-frame bound — the shape of a bootstrap serve — completes
    untouched because only the first-frame allowance applies to it."""
    async def run() -> None:
        root, puller_key, (server_key,), puller_db, entries = (
            _fleet(tmp_path)
        )

        async def slow_then_done(_token, _message, _peer):
            async def stream():
                await asyncio.sleep(1.2)  # 3x the inter-frame bound
                yield encode_done(
                    epoch="ab" * 32, count=0,
                    digest=hashlib.sha256().hexdigest(),
                )
            return stream()

        server = _server(server_key, root, entries, slow_then_done)
        await server.start()
        puller = _puller(
            puller_key, root, entries, puller_db,
            {server_key.public_hex: [f"ws://127.0.0.1:{server.port}"]},
            first_allowance=10.0, silence_limit=0.4,
        )
        await puller.start()
        try:
            await _eventually(lambda: _acknowledgements(puller_db) >= 1)
            retries, error_code = _peer_row(
                puller_db, server_key.public_hex
            )
            assert retries == 0 and error_code is None
        finally:
            await puller.stop()
            await server.stop()

    asyncio.run(run())


def test_mid_stream_silence_is_bounded_by_the_inter_frame_limit(
    tmp_path: Path,
) -> None:
    """After the first frame the bootstrap allowance no longer
    shields the peer: silence is caught at the tight inter-frame bound,
    far below the first-frame allowance."""
    async def run() -> None:
        root, puller_key, (server_key,), puller_db, entries = (
            _fleet(tmp_path)
        )
        engaged = asyncio.Event()
        release = asyncio.Event()

        async def header_then_silence(_token, _message, _peer):
            # The serving channel transmits frame N only once frame N+1
            # is produced (one-item final-boundary lookahead), so a real
            # mid-stream wedge means TWO produced frames with the client
            # having received exactly one.
            async def stream():
                yield encode_transaction_header("c" * 64, "txn-1", 2)
                yield encode_transaction_header("c" * 64, "txn-2", 2)
                engaged.set()
                await release.wait()
            return stream()

        server = _server(server_key, root, entries, header_then_silence)
        await server.start()
        puller = _puller(
            puller_key, root, entries, puller_db,
            {server_key.public_hex: [f"ws://127.0.0.1:{server.port}"]},
            first_allowance=30.0, silence_limit=0.5,
        )
        await puller.start()
        try:
            await asyncio.wait_for(engaged.wait(), 20)
            engaged_at = time.monotonic()
            await _eventually(lambda: _peer_row(
                puller_db, server_key.public_hex
            )[0] > 0)
            elapsed = time.monotonic() - engaged_at
            _retries, error_code = _peer_row(
                puller_db, server_key.public_hex
            )
            assert error_code == "FleetSyncStreamSilence"
            assert elapsed < 10.0, (
                f"inter-frame silence took {elapsed:.1f}s to surface — "
                "the first-frame allowance leaked past the first frame"
            )
        finally:
            release.set()
            await puller.stop()
            await server.stop()

    asyncio.run(run())


def test_silence_timeout_never_downgrades_the_protocol(
    tmp_path: Path,
) -> None:
    """A wedged peer fails with zero frames received — the same shape as
    an old server rejecting a v4 request. The downgrade heuristic must
    not conflate them: silence is a liveness verdict, not a version one."""
    async def run() -> None:
        root, puller_key, (wedged_key,), puller_db, entries = (
            _fleet(tmp_path)
        )
        engaged = asyncio.Event()
        release = asyncio.Event()
        wedged = _server(
            wedged_key, root, entries, _wedged_handler(engaged, release)
        )
        await wedged.start()
        puller = _puller(
            puller_key, root, entries, puller_db,
            {wedged_key.public_hex: [f"ws://127.0.0.1:{wedged.port}"]},
            first_allowance=0.5, silence_limit=0.3,
        )
        await puller.start()
        try:
            await _eventually(lambda: _peer_row(
                puller_db, wedged_key.public_hex
            )[0] > 0)
            assert wedged_key.public_hex not in puller._peer_protocol
        finally:
            release.set()
            await puller.stop()
            await wedged.stop()

    asyncio.run(run())


def test_stop_is_bounded_while_a_pull_is_wedged(tmp_path: Path) -> None:
    """The shutdown promise from the design review: a wedged in-flight
    pull must never block scheduler.stop() — bulk applies pause
    the runtime through exactly this path."""
    async def run() -> None:
        root, puller_key, (wedged_key,), puller_db, entries = (
            _fleet(tmp_path)
        )
        engaged = asyncio.Event()
        release = asyncio.Event()
        wedged = _server(
            wedged_key, root, entries, _wedged_handler(engaged, release)
        )
        await wedged.start()
        puller = _puller(
            puller_key, root, entries, puller_db,
            {wedged_key.public_hex: [f"ws://127.0.0.1:{wedged.port}"]},
            first_allowance=60.0, silence_limit=60.0,
        )
        await puller.start()
        try:
            await asyncio.wait_for(engaged.wait(), 20)
        finally:
            await asyncio.wait_for(puller.stop(), 5.0)
            release.set()
            await wedged.stop()

    asyncio.run(run())


def test_keepalive_control_frames_are_tolerated(tmp_path: Path) -> None:
    """Forward compatibility for a future v5 build-phase keepalive: an
    unknown-to-this-build {"kind": "keepalive"} control is ignored —
    outside the digest, outside the count — instead of failing the
    stream."""
    async def run() -> None:
        root, puller_key, (server_key,), puller_db, entries = (
            _fleet(tmp_path)
        )

        async def keepalives_then_done(_token, _message, _peer):
            async def stream():
                for _ in range(3):
                    yield canonical_json({"kind": "keepalive"})
                    await asyncio.sleep(0.05)
                yield encode_done(
                    epoch="ab" * 32, count=0,
                    digest=hashlib.sha256().hexdigest(),
                )
            return stream()

        server = _server(server_key, root, entries, keepalives_then_done)
        await server.start()
        puller = _puller(
            puller_key, root, entries, puller_db,
            {server_key.public_hex: [f"ws://127.0.0.1:{server.port}"]},
            first_allowance=10.0, silence_limit=5.0,
        )
        await puller.start()
        try:
            await _eventually(lambda: _acknowledgements(puller_db) >= 1)
            retries, _error = _peer_row(puller_db, server_key.public_hex)
            assert retries == 0
        finally:
            await puller.stop()
            await server.stop()

    asyncio.run(run())


def test_bounded_stream_helper_raises_typed_silence() -> None:
    """The helper both receive paths share, exercised directly (the blob
    drain uses it with the inter-frame bound in both positions)."""
    class StallingChannel:
        def __init__(self, frames, stall_index):
            self._frames = frames
            self._stall_index = stall_index

        async def recv_message_stream(self):
            for index, frame in enumerate(self._frames):
                if index == self._stall_index:
                    await asyncio.Event().wait()
                yield frame, False

    async def collect(channel, first, rest):
        received = []
        async for item in bounded_stream_frames(
            channel, first_allowance_s=first, silence_limit_s=rest
        ):
            received.append(item)
        return received

    async def run() -> None:
        with pytest.raises(FleetSyncFirstFrameSilence):
            await collect(StallingChannel([(b"a")], 0), 0.1, 0.05)
        with pytest.raises(FleetSyncStreamSilence) as caught:
            await collect(StallingChannel([b"a", b"b"], 1), 5.0, 0.1)
        assert not isinstance(caught.value, FleetSyncFirstFrameSilence)
        assert await collect(
            StallingChannel([b"a", b"b"], None), 0.5, 0.5
        ) == [(b"a", False), (b"b", False)]

    asyncio.run(run())
