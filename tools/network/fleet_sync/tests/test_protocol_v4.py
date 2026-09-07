"""Protocol v4: batched transaction headers on the delta channel.

v3 repeats the full ~150-byte transaction header inside every operation
frame; v4 sends one transaction-header frame followed by bare operation
frames. The server answers in the requester's declared version, so a v3
puller against a v4 server syncs unchanged. Mutation frame bytes,
candidate hashes, and journal storage are untouched — the wire wrapper
only.
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import struct
from pathlib import Path

import pytest

from tools.graph.db import GraphDB
from tools.graph.models import Source
from tools.network.fleet_roster import enroll
from tools.network.fleet_sync.catalog import AuthoredMutation
from tools.network.fleet_sync_channel import (
    FleetAuthenticator,
    FleetDirectServer,
)
from tools.network.fleet_sync_scheduler import (
    _digest_add,
    FleetSyncProtocolError,
    FleetSyncRuntimeConfig,
    FleetSyncScheduler,
    SQLiteFleetSyncStore,
    decode_operation_frame,
    decode_transaction_header,
    encode_authored,
    encode_done,
    encode_operation_frame,
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


def _delete(path: Path, source_id: str) -> None:
    db = GraphDB(path)
    try:
        db.conn.execute("DELETE FROM sources WHERE id=?", (source_id,))
        db.conn.commit()
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


async def _eventually(predicate, *, timeout: float = 6.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition did not become true")
        await asyncio.sleep(0.02)


def _journal_transactions(path: Path) -> list[list[AuthoredMutation]]:
    store = SQLiteFleetSyncStore(path)
    cursor = 0
    transactions = []
    while True:
        page = store.next_transaction(cursor)
        if page is None:
            return transactions
        cursor, items = page
        transactions.append(items)


def _fan_out(item: AuthoredMutation, operations: int) -> list[AuthoredMutation]:
    """A synthetic multi-operation transaction reusing one real mutation.

    Wire measurement only — encode functions never validate cross-operation
    semantics, and a bulk transaction is exactly N frames of this shape."""
    return [
        AuthoredMutation(
            item.origin_incarnation, item.transaction_id, index, item.mutation
        )
        for index in range(operations)
    ]


def test_v4_cuts_wire_bytes_forty_percent_on_multi_op_transactions(
    tmp_path: Path,
) -> None:
    machine = KeyPair.generate()
    path = tmp_path / "sample.db"
    _prepare(path, machine)
    _insert(path, "kept-row", "one realistic row payload")
    _insert(path, "bulk-row", "one realistic row payload")
    _delete(path, "bulk-row")
    # Served from rows: the deleted row's insert transaction has nothing
    # surviving (its address now cites the tombstone), so take the
    # realistic insert from the row that is kept.
    items = [item for tx in _journal_transactions(path) for item in tx]
    insert_item = next(i for i in items if not i.mutation.tombstone)
    tombstone_item = next(i for i in items if i.mutation.tombstone)

    def wire(item: AuthoredMutation, operations: int) -> tuple[int, int]:
        ops = _fan_out(item, operations)
        v3 = sum(
            len(encode_authored(op, transaction_operations=operations))
            for op in ops
        )
        v4 = len(encode_transaction_header(
            item.origin_incarnation, item.transaction_id, operations
        )) + sum(len(encode_operation_frame(op)) for op in ops)
        return v3, v4

    # Bulk deletes are the canonical multi-operation transaction: tiny
    # tombstone frames under a repeated full header.
    v3, v4 = wire(tombstone_item, 64)
    assert v4 <= 0.6 * v3, f"tombstone reduction only {1 - v4 / v3:.0%}"
    # Full row inserts still shed the repeated header.
    v3_rows, v4_rows = wire(insert_item, 64)
    assert v4_rows < v3_rows


def test_v4_frames_round_trip_and_reject_malformed(tmp_path: Path) -> None:
    machine = KeyPair.generate()
    path = tmp_path / "sample.db"
    _prepare(path, machine)
    _insert(path, "rt-row", "round trip")
    item = _journal_transactions(path)[0][0]

    header = encode_transaction_header(
        item.origin_incarnation, item.transaction_id, 3
    )
    assert decode_transaction_header(header) == (
        item.origin_incarnation, item.transaction_id, 3
    )
    frame = encode_operation_frame(item)
    operation, mutation = decode_operation_frame(frame)
    assert operation == item.operation_index
    assert mutation == item.mutation

    with pytest.raises(FleetSyncProtocolError):
        decode_transaction_header(b"FSTXnot json")
    with pytest.raises(FleetSyncProtocolError):
        decode_transaction_header(encode_transaction_header(
            item.origin_incarnation, item.transaction_id, 1
        ).replace(b'"operations":1', b'"operations":0'))
    with pytest.raises(FleetSyncProtocolError):
        decode_operation_frame(b"FSO1")
    with pytest.raises(FleetSyncProtocolError):
        decode_operation_frame(b"FSO1" + struct.pack(">I", 0) + b"garbage")


def _pair_configs(tmp_path: Path):
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
    return root, left_key, right_key, left_path, right_path, entries


def _scheduler(key, root, entries, path, peers=None, **extra):
    return FleetSyncScheduler(FleetSyncRuntimeConfig(
        machine_key=key,
        personal_root_pub=root.public_hex,
        roster_entries=lambda: entries,
        peer_addresses=lambda: dict(peers or {}),
        personal_db_path=path,
        poll_interval=0.03,
        min_backoff=0.01,
        max_backoff=0.05,
        **extra,
    ))


def test_v4_pair_converges_without_any_v3_frames(
    tmp_path: Path, monkeypatch
) -> None:
    """Both sides current: the server must never fall back to per-operation
    headers. encode_authored raising proves the v3 path stayed cold."""
    async def run() -> None:
        root, left_key, right_key, left_path, right_path, entries = (
            _pair_configs(tmp_path)
        )
        import tools.network.fleet_sync_scheduler as scheduler_module

        def refuse_v3(*_args, **_kwargs):
            raise AssertionError("v3 framing used on a v4↔v4 pull")

        monkeypatch.setattr(scheduler_module, "encode_authored", refuse_v3)
        right = _scheduler(right_key, root, entries, right_path)
        await right.start()
        left = _scheduler(
            left_key, root, entries, left_path,
            peers={right_key.public_hex: [f"ws://127.0.0.1:{right.port}"]},
        )
        _insert(right_path, "v4-crossing", "arrives batched")
        _insert(left_path, "left-seed", "keeps the delta path")
        await left.start()
        try:
            await _eventually(
                lambda: _title(left_path, "v4-crossing") == "arrives batched"
            )
        finally:
            await left.stop()
            await right.stop()

    asyncio.run(run())


def test_v3_puller_against_v4_server_converges_unchanged(
    tmp_path: Path, monkeypatch
) -> None:
    """Mixed-version guarantee: a peer pinned to v3 receives the exact v3
    per-operation framing (the batched encoders raising proves it) and
    still converges."""
    async def run() -> None:
        root, left_key, right_key, left_path, right_path, entries = (
            _pair_configs(tmp_path)
        )
        import tools.network.fleet_sync_scheduler as scheduler_module

        def refuse_v4(*_args, **_kwargs):
            raise AssertionError("v4 framing served to a v3 puller")

        monkeypatch.setattr(
            scheduler_module, "encode_transaction_header", refuse_v4
        )
        monkeypatch.setattr(
            scheduler_module, "encode_operation_frame", refuse_v4
        )
        right = _scheduler(right_key, root, entries, right_path)
        await right.start()
        left = _scheduler(
            left_key, root, entries, left_path,
            peers={right_key.public_hex: [f"ws://127.0.0.1:{right.port}"]},
        )
        left._peer_protocol[right_key.public_hex] = 3
        _insert(right_path, "v3-crossing", "served in v3 framing")
        _insert(left_path, "left-seed", "keeps the delta path")
        await left.start()
        try:
            await _eventually(
                lambda: _title(left_path, "v3-crossing") == "served in v3 framing"
            )
        finally:
            await left.stop()
            await right.stop()

    asyncio.run(run())


def _crafted_server_test(tmp_path: Path, frames_from) -> Path:
    """Run the scheduler against a server streaming crafted v4 frames,
    assert the pull fails (retries recorded), and return the puller's
    database path for the test's own postcondition."""
    async def run() -> None:
        root = KeyPair.generate()
        left_key = KeyPair.generate()
        right_key = KeyPair.generate()
        left_path = tmp_path / "left.db"
        source_path = tmp_path / "source.db"
        _prepare(left_path, left_key)
        _prepare(source_path, right_key)
        _insert(source_path, "crafted-row", "never lands")
        items = _journal_transactions(source_path)[0]
        entries = [
            enroll(root, machine_pub=left_key.public_hex),
            enroll(root, machine_pub=right_key.public_hex),
        ]

        async def handler(_token: str, _message: bytes, _peer: str):
            async def stream():
                digest = hashlib.sha256()
                count = 0
                for frame in frames_from(items, digest):
                    if frame.startswith(b"FSO1"):
                        count += 1
                    yield frame
                yield encode_done(
                    epoch="ab" * 32,
                    count=count,
                    digest=digest.hexdigest(),
                    version=4,
                )
            return stream()

        server = FleetDirectServer(
            FleetAuthenticator(
                right_key,
                root_pub=root.public_hex,
                roster_entries=lambda: entries,
            ),
            handler,
        )
        await server.start()
        scheduler = _scheduler(
            left_key, root, entries, left_path,
            peers={right_key.public_hex: [f"ws://127.0.0.1:{server.port}"]},
        )
        await scheduler.start()
        try:
            await _eventually(lambda: _max_retries(left_path) > 0)
        finally:
            await scheduler.stop()
            await server.stop()

        return left_path

    return asyncio.run(run())


def _acknowledgements(path: Path) -> int:
    with sqlite3.connect(path) as conn:
        return int(conn.execute(
            "SELECT COALESCE(SUM(acknowledgements),0) "
            "FROM fleet_sync_peer_state"
        ).fetchone()[0])


def test_v4_operation_before_header_is_refused(tmp_path: Path) -> None:
    def frames(items, digest):
        frame = encode_operation_frame(items[0])
        _digest_add(digest, frame)
        yield frame

    left_path = _crafted_server_test(tmp_path, frames)
    assert _title(left_path, "crafted-row") is None


def test_v4_digest_tamper_blocks_acknowledgement(tmp_path: Path) -> None:
    """The summary digest must cover header and operation frames alike.

    Complete, individually authenticated transaction groups legitimately
    apply as they arrive (v3 semantics, unchanged); what a digest mismatch
    must prevent is the pull SUCCEEDING — no acknowledgement, no resume
    advancement, a recorded retry."""
    def frames(items, digest):
        header = encode_transaction_header(
            items[0].origin_incarnation, items[0].transaction_id, len(items)
        )
        # The header is deliberately left OUT of the summary digest.
        yield header
        for item in items:
            frame = encode_operation_frame(item)
            _digest_add(digest, frame)
            yield frame

    left_path = _crafted_server_test(tmp_path, frames)
    assert _acknowledgements(left_path) == 0


def test_v4_operation_count_mismatch_is_refused(tmp_path: Path) -> None:
    def frames(items, digest):
        header = encode_transaction_header(
            items[0].origin_incarnation, items[0].transaction_id,
            len(items) + 1,
        )
        _digest_add(digest, header)
        yield header
        for item in items:
            frame = encode_operation_frame(item)
            _digest_add(digest, frame)
            yield frame

    left_path = _crafted_server_test(tmp_path, frames)
    assert _title(left_path, "crafted-row") is None
