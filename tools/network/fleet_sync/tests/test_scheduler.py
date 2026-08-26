from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from pathlib import Path

import pytest

from tools.graph.db import GraphDB
from tools.graph.models import Source
from tools.network.fleet_roster import enroll, kick, resolve
from tools.network.fleet_sync_scheduler import (
    DashboardFleetSyncService,
    encode_done,
    FleetSyncRuntimeConfig,
    FleetSyncScheduler,
    SQLiteFleetSyncStore,
    roster_epoch,
)
from tools.network.fleet_sync_channel import FleetAuthenticator, FleetDirectServer
from tools.network.fleet_sync_connection import FleetSyncQuiescenceError
from tools.network.fleet_sync.sync import (
    FleetSyncAlpha,
    transport_checkpoint_via_raptorq,
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


def _max_retries(path: Path) -> int:
    with sqlite3.connect(path) as conn:
        return int(conn.execute(
            "SELECT COALESCE(MAX(retries),0) FROM fleet_sync_peer_state"
        ).fetchone()[0])


def _applied_transactions(path: Path) -> int:
    with sqlite3.connect(path) as conn:
        return int(conn.execute(
            "SELECT COALESCE(SUM(transactions_applied),0) "
            "FROM fleet_sync_peer_state"
        ).fetchone()[0])


def _acknowledgements(path: Path) -> int:
    with sqlite3.connect(path) as conn:
        return int(conn.execute(
            "SELECT COALESCE(SUM(acknowledgements),0) "
            "FROM fleet_sync_peer_state"
        ).fetchone()[0])


async def _eventually(predicate, *, timeout: float = 4.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition did not become true")
        await asyncio.sleep(0.02)


def test_scheduler_store_uses_the_catalog_row_shape(tmp_path: Path) -> None:
    path = tmp_path / "personal.db"
    _prepare(path, KeyPair.generate())
    conn, _catalog = SQLiteFleetSyncStore(path)._open()
    try:
        assert conn.row_factory is sqlite3.Row
        assert isinstance(
            conn.execute("SELECT * FROM fleet_sync_state").fetchone(),
            sqlite3.Row,
        )
        assert conn.execute("SELECT fleet_sha256_text('fleet')").fetchone()[0] == (
            "5eb2ce291c7d227dd684ec83f9ddc05776e2fe9a0c4e62927b4592383e66fb28"
        )
    finally:
        conn.close()


def test_two_schedulers_transfer_once_and_resume_after_reconnect(tmp_path: Path) -> None:
    async def run() -> None:
        telemetry = []
        acknowledged = {}

        def record(peer, **values):
            telemetry.append({"peer": peer, **values})
            if "acknowledged_transaction_ref" in values:
                acknowledged[peer] = values["acknowledged_transaction_ref"]
        root = KeyPair.generate()
        left_key = KeyPair.generate()
        right_key = KeyPair.generate()
        left_path = tmp_path / "left.db"
        right_path = tmp_path / "right.db"
        _prepare(left_path, left_key)
        _prepare(right_path, right_key)
        entries = [
            enroll(root, machine_pub=left_key.public_hex),
            enroll(root, machine_pub=right_key.public_hex),
        ]
        right = FleetSyncScheduler(
            FleetSyncRuntimeConfig(
                machine_key=right_key,
                personal_root_pub=root.public_hex,
                roster_entries=lambda: entries,
                peer_addresses=lambda: {},
                personal_db_path=right_path,
                poll_interval=0.03,
            )
        )
        await right.start()
        right_addr = f"ws://127.0.0.1:{right.port}"
        left_config = FleetSyncRuntimeConfig(
            machine_key=left_key,
            personal_root_pub=root.public_hex,
            roster_entries=lambda: entries,
            peer_addresses=lambda: {right_key.public_hex: [right_addr]},
            personal_db_path=left_path,
            poll_interval=0.03,
            min_backoff=0.01,
            max_backoff=0.05,
            telemetry_recorder=record,
            resume_cursor=lambda peer: acknowledged.get(peer, 0),
        )
        _insert(right_path, "first-crossing", "first")
        left = FleetSyncScheduler(left_config)
        await left.start()
        try:
            await _eventually(lambda: _title(left_path, "first-crossing") == "first")
            await _eventually(lambda: _applied_transactions(left_path) >= 1)
            await _eventually(lambda: _acknowledgements(left_path) >= 1)
            # Observe one empty follow-up. The receiver's acknowledged source
            # position must prevent a replay; don't encode scheduler timing in
            # this assertion.
            await _eventually(lambda: any(
                row["outcome"] == "success" and row["mutation_frames"] == 0
                for row in telemetry
            ))
        finally:
            await left.stop()

        with sqlite3.connect(left_path) as conn:
            remote_transactions = conn.execute(
                "SELECT COUNT(*) FROM fleet_sync_transactions t "
                "JOIN fleet_sync_origins o ON o.id=t.origin_id "
                "WHERE o.incarnation=?",
                (right_key.public_hex,),
            ).fetchone()[0]
        assert remote_transactions == 1

        _insert(right_path, "after-reconnect", "second")
        left = FleetSyncScheduler(left_config)
        await left.start()
        try:
            await _eventually(lambda: _title(left_path, "after-reconnect") == "second")
            await _eventually(lambda: _applied_transactions(left_path) >= 2)
            await _eventually(lambda: _acknowledgements(left_path) >= 2)
            await _eventually(
                lambda: acknowledged.get(right_key.public_hex, 0) >= 2
            )
        finally:
            await left.stop()
            await right.stop()

        assert left.server.connection_count == 0
        assert right.server.connection_count == 0
        with sqlite3.connect(left_path) as conn:
            state = conn.execute(
                "SELECT online,bytes_sent,bytes_received,transactions_applied,"
                "acknowledgements,retries FROM fleet_sync_peer_state "
                "WHERE machine_public_key=? ORDER BY updated_at_ns DESC LIMIT 1",
                (right_key.public_hex,),
            ).fetchone()
        assert state[0] == 0
        assert state[1] > 0 and state[2] > 0
        assert state[3] == 2
        assert state[4] >= 2
        assert state[5] == 0
        successes = [row for row in telemetry if row["outcome"] == "success"]
        assert successes
        assert all(row["peer"] == right_key.public_hex for row in successes)
        assert any(row["bytes_received"] > 0 for row in successes)
        assert any(row["mutation_frames"] >= 1 for row in successes)
        assert any(row["transactions"] >= 1 for row in successes)
        assert any(row["mutation_frames"] == 0 for row in successes)
        assert max(
            row.get("acknowledged_transaction_ref", 0) for row in successes
        ) >= 2
        assert all(row["duration_ms"] >= 0 for row in successes)

    asyncio.run(run())


def test_scheduler_retries_bad_candidate_then_uses_authenticated_peer(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        root = KeyPair.generate()
        left_key = KeyPair.generate()
        right_key = KeyPair.generate()
        left_path = tmp_path / "left.db"
        right_path = tmp_path / "right.db"
        _prepare(left_path, left_key)
        _prepare(right_path, right_key)
        _insert(right_path, "after-retry", "arrived")
        entries = [
            enroll(root, machine_pub=left_key.public_hex),
            enroll(root, machine_pub=right_key.public_hex),
        ]
        candidates = {right_key.public_hex: ["ws://127.0.0.1:1"]}
        left = FleetSyncScheduler(
            FleetSyncRuntimeConfig(
                machine_key=left_key,
                personal_root_pub=root.public_hex,
                roster_entries=lambda: entries,
                peer_addresses=lambda: candidates,
                personal_db_path=left_path,
                poll_interval=0.02,
                connect_timeout=3.0,
                min_backoff=0.01,
                max_backoff=0.03,
            )
        )
        right = FleetSyncScheduler(
            FleetSyncRuntimeConfig(
                machine_key=right_key,
                personal_root_pub=root.public_hex,
                roster_entries=lambda: entries,
                peer_addresses=lambda: {},
                personal_db_path=right_path,
                poll_interval=0.02,
            )
        )
        await left.start()
        try:
            await _eventually(
                lambda: _max_retries(left_path) > 0
            )
            await right.start()
            candidates[right_key.public_hex] = [
                f"ws://127.0.0.1:{right.port}"
            ]
            await _eventually(lambda: _title(left_path, "after-retry") == "arrived")
            await _eventually(lambda: _acknowledgements(left_path) >= 1)
        finally:
            await left.stop()
            await right.stop()

        with sqlite3.connect(left_path) as conn:
            row = conn.execute(
                "SELECT retries,last_error_code,online FROM fleet_sync_peer_state "
                "WHERE machine_public_key=? ORDER BY updated_at_ns DESC LIMIT 1",
                (right_key.public_hex,),
            ).fetchone()
        assert row[0] >= 1
        assert row[1] is None
        assert row[2] == 0

    asyncio.run(run())


def test_scheduler_rejects_malformed_authenticated_peer_stream(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        root = KeyPair.generate()
        left_key = KeyPair.generate()
        right_key = KeyPair.generate()
        left_path = tmp_path / "left.db"
        _prepare(left_path, left_key)
        entries = [
            enroll(root, machine_pub=left_key.public_hex),
            enroll(root, machine_pub=right_key.public_hex),
        ]

        async def malformed(_token: str, _message: bytes, _peer: str) -> bytes:
            return b"authenticated but not a fleet transaction"

        server = FleetDirectServer(
            FleetAuthenticator(
                right_key,
                root_pub=root.public_hex,
                roster_entries=lambda: entries,
            ),
            malformed,
        )
        await server.start()
        scheduler = FleetSyncScheduler(
            FleetSyncRuntimeConfig(
                machine_key=left_key,
                personal_root_pub=root.public_hex,
                roster_entries=lambda: entries,
                peer_addresses=lambda: {
                    right_key.public_hex: [f"ws://127.0.0.1:{server.port}"]
                },
                personal_db_path=left_path,
                poll_interval=0.02,
                min_backoff=0.01,
                max_backoff=0.03,
            )
        )
        await scheduler.start()
        try:
            await _eventually(lambda: _max_retries(left_path) > 0)
        finally:
            await scheduler.stop()
            await server.stop()

        with sqlite3.connect(left_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0

    asyncio.run(run())


def test_scheduler_disconnects_peer_kicked_during_live_stream(tmp_path: Path) -> None:
    async def run() -> None:
        root = KeyPair.generate()
        left_key = KeyPair.generate()
        right_key = KeyPair.generate()
        left_path = tmp_path / "left.db"
        _prepare(left_path, left_key)
        entries = [
            enroll(root, machine_pub=left_key.public_hex),
            enroll(root, machine_pub=right_key.public_hex),
        ]
        request_seen = asyncio.Event()
        release = asyncio.Event()

        async def delayed(_token: str, _message: bytes, _peer: str):
            async def response():
                request_seen.set()
                await release.wait()
                yield encode_done(
                    epoch=roster_epoch(entries, root.public_hex),
                    count=0,
                    digest=hashlib.sha256().hexdigest(),
                )

            return response()

        server = FleetDirectServer(
            FleetAuthenticator(
                right_key,
                root_pub=root.public_hex,
                roster_entries=lambda: entries,
            ),
            delayed,
        )
        await server.start()
        scheduler = FleetSyncScheduler(
            FleetSyncRuntimeConfig(
                machine_key=left_key,
                personal_root_pub=root.public_hex,
                roster_entries=lambda: entries,
                peer_addresses=lambda: {
                    right_key.public_hex: [f"ws://127.0.0.1:{server.port}"]
                },
                personal_db_path=left_path,
                poll_interval=0.02,
                min_backoff=0.01,
                max_backoff=0.03,
            )
        )
        await scheduler.start()
        try:
            await asyncio.wait_for(request_seen.wait(), timeout=3.0)
            entries.append(kick(root, machine_pub=right_key.public_hex, seq=1))
            await _eventually(
                lambda: right_key.public_hex not in resolve(
                    scheduler._roster_snapshot,
                    anchor_root_pub=root.public_hex,
                )
            )
            release.set()
            await _eventually(lambda: _max_retries(left_path) >= 1)
        finally:
            release.set()
            await scheduler.stop()
            await server.stop()
        assert server.connection_count == 0
        assert _acknowledgements(left_path) == 0

    asyncio.run(run())


def test_dashboard_service_is_healthy_without_runtime_or_peers() -> None:
    async def run() -> None:
        service = DashboardFleetSyncService()
        await service.start()
        await asyncio.sleep(0)
        assert service.scheduler is None
        await service.stop()
        assert service.scheduler is None
        assert service._task is None

    asyncio.run(run())


def test_dashboard_service_owns_configured_scheduler_lifecycle(tmp_path: Path) -> None:
    async def run() -> None:
        root = KeyPair.generate()
        machine = KeyPair.generate()
        path = tmp_path / "personal.db"
        _prepare(path, machine)
        entries = [enroll(root, machine_pub=machine.public_hex)]
        service = DashboardFleetSyncService()
        service.configure(FleetSyncRuntimeConfig(
            machine_key=machine,
            personal_root_pub=root.public_hex,
            roster_entries=lambda: entries,
            peer_addresses=lambda: {},
            personal_db_path=path,
            poll_interval=0.02,
        ))
        await service.start()
        await _eventually(
            lambda: service.scheduler is not None and service.scheduler.running
        )
        scheduler = service.scheduler
        service.configure(None)
        await _eventually(lambda: service.scheduler is None)
        assert scheduler is not None
        assert scheduler.running is False
        assert scheduler.server.connection_count == 0
        await service.stop()

    asyncio.run(run())


def test_dashboard_checkpoint_handoff_resumes_late_row_and_tombstone_deltas(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        root = KeyPair.generate()
        left_key = KeyPair.generate()
        right_key = KeyPair.generate()
        left_path = tmp_path / "left.db"
        right_path = tmp_path / "right.db"
        checkpoint = tmp_path / "checkpoint"
        received = tmp_path / "received"
        entries = [
            enroll(root, machine_pub=left_key.public_hex),
            enroll(root, machine_pub=right_key.public_hex),
        ]
        epoch = roster_epoch(entries, root.public_hex)
        active = tuple(sorted(resolve(entries, anchor_root_pub=root.public_hex)))

        with FleetSyncAlpha(right_path, right_key.public_hex) as source:
            with source.author(100, "checkpoint-seed"):
                source.graph.conn.execute(
                    "INSERT INTO sources(id,type,title,metadata,created_at,ingested_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        "gone-after-cut", "note", "temporary", "{}",
                        "2026-08-21T00:00:00Z", "2026-08-21T00:00:00Z",
                    ),
                )
            source.checkpoint(
                checkpoint,
                roster_epoch=epoch,
                active_roster=active,
                target_chunk_bytes=4096,
            )
            transport_checkpoint_via_raptorq(
                checkpoint, received, symbol_size=256
            )
            with source.author(200, "after-cut"):
                source.graph.conn.execute(
                    "DELETE FROM sources WHERE id='gone-after-cut'"
                )
                source.graph.conn.execute(
                    "INSERT INTO sources(id,type,title,metadata,created_at,ingested_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        "late-row", "note", "after checkpoint", "{}",
                        "2026-08-21T00:00:01Z", "2026-08-21T00:00:01Z",
                    ),
                )

        _prepare(left_path, left_key)
        addresses: dict[str, list[str]] = {}
        service = DashboardFleetSyncService()
        service.configure(FleetSyncRuntimeConfig(
            machine_key=left_key,
            personal_root_pub=root.public_hex,
            roster_entries=lambda: entries,
            peer_addresses=lambda: addresses,
            personal_db_path=left_path,
            poll_interval=0.02,
            min_backoff=0.01,
            max_backoff=0.03,
        ))
        await service.start()
        await _eventually(
            lambda: service.scheduler is not None and service.scheduler.running
        )
        live_writer = GraphDB(left_path)
        with pytest.raises(FleetSyncQuiescenceError, match="live production"):
            await service.install_checkpoint(
                received, source_machine_pub=right_key.public_hex
            )
        live_writer.close()
        await _eventually(
            lambda: service.scheduler is not None and service.scheduler.running
        )
        with pytest.raises(RuntimeError, match="not an active remote"):
            await service.install_checkpoint(
                received, source_machine_pub=KeyPair.generate().public_hex
            )
        await _eventually(
            lambda: service.scheduler is not None and service.scheduler.running
        )
        await service.install_checkpoint(
            received, source_machine_pub=right_key.public_hex
        )
        assert _title(left_path, "gone-after-cut") == "temporary"
        with sqlite3.connect(left_path) as conn:
            receipt = conn.execute(
                "SELECT checkpoints_received,peer_watermark "
                "FROM fleet_sync_peer_state WHERE machine_public_key=?",
                (right_key.public_hex,),
            ).fetchone()
        assert tuple(receipt) == (1, 100)

        right = FleetSyncScheduler(FleetSyncRuntimeConfig(
            machine_key=right_key,
            personal_root_pub=root.public_hex,
            roster_entries=lambda: entries,
            peer_addresses=lambda: {},
            personal_db_path=right_path,
            poll_interval=0.02,
        ))
        await right.start()
        addresses[right_key.public_hex] = [f"ws://127.0.0.1:{right.port}"]
        try:
            await _eventually(lambda: _title(left_path, "late-row") == "after checkpoint")
            await _eventually(lambda: _title(left_path, "gone-after-cut") is None)
        finally:
            await service.stop()
            await right.stop()
        assert right.server.connection_count == 0

    asyncio.run(run())
