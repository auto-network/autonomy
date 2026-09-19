"""The delta channel framing: one transaction-header frame, then bare
operation frames.

There is one protocol version and every machine runs it. The header carries
the group index and whether the group is the transaction's last, so the
receiver knows when a paged transaction is whole (auto-85jlk).
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
    FLEET_SYNC_PROTOCOL_VERSION,
    _digest_add,
    FleetSyncProtocolError,
    FleetSyncRuntimeConfig,
    FleetSyncScheduler,
    SQLiteFleetSyncStore,
    decode_operation_frame,
    decode_transaction_header,
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


def test_v4_frames_round_trip_and_reject_malformed(tmp_path: Path) -> None:
    machine = KeyPair.generate()
    path = tmp_path / "sample.db"
    _prepare(path, machine)
    _insert(path, "rt-row", "round trip")
    item = _journal_transactions(path)[0][0]

    header = encode_transaction_header(
        item.origin_incarnation, item.transaction_id, 3, group=2, last=True,
    )
    assert decode_transaction_header(header) == (
        item.origin_incarnation, item.transaction_id, 3, 2, True
    )
    frame = encode_operation_frame(item)
    operation, mutation = decode_operation_frame(frame)
    assert operation == item.operation_index
    assert mutation == item.mutation

    with pytest.raises(FleetSyncProtocolError):
        decode_transaction_header(b"FSTXnot json")
    with pytest.raises(FleetSyncProtocolError):
        decode_transaction_header(encode_transaction_header(
            item.origin_incarnation, item.transaction_id, 1, group=0, last=True,
        ).replace(b'"operations":1', b'"operations":0'))
    # group and last are not optional: a header without them, or with a
    # malformed one, is refused rather than read as "complete".
    from tools.network.fleet_sync_scheduler import _TRANSACTION_MAGIC
    import json as _json
    for body in (
        {"origin": "a" * 64, "transaction": "tx", "operations": 1},
        {"origin": "a" * 64, "transaction": "tx", "operations": 1, "group": 0},
        {"origin": "a" * 64, "transaction": "tx", "operations": 1, "group": -1, "last": True},
        {"origin": "a" * 64, "transaction": "tx", "operations": 1, "group": 0, "last": 1},
    ):
        with pytest.raises(FleetSyncProtocolError):
            decode_transaction_header(_TRANSACTION_MAGIC + _json.dumps(body).encode())
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


def test_a_pair_converges_over_the_delta_channel(tmp_path: Path) -> None:
    async def run() -> None:
        root, left_key, right_key, left_path, right_path, entries = (
            _pair_configs(tmp_path)
        )
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
                    version=FLEET_SYNC_PROTOCOL_VERSION,
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
    apply as they arrive; what a digest mismatch
    must prevent is the pull SUCCEEDING — no acknowledgement, no resume
    advancement, a recorded retry."""
    def frames(items, digest):
        header = encode_transaction_header(
            items[0].origin_incarnation, items[0].transaction_id, len(items),
            group=0, last=True,
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
            len(items) + 1, group=0, last=True,
        )
        _digest_add(digest, header)
        yield header
        for item in items:
            frame = encode_operation_frame(item)
            _digest_add(digest, frame)
            yield frame

    left_path = _crafted_server_test(tmp_path, frames)
    assert _title(left_path, "crafted-row") is None


def _insert_together(path: Path, rows) -> None:
    """Insert every row inside a SINGLE database transaction — what a real
    multi-row write (a note: sources + content + thoughts) actually is."""
    db = GraphDB(path)
    try:
        db.conn.execute("BEGIN")
        for source_id, title in rows:
            db.conn.execute(
                "INSERT INTO sources (id, type, title) VALUES (?,?,?)",
                (source_id, "note", title),
            )
        db.conn.commit()
    finally:
        db.close()


def test_a_transaction_larger_than_one_serve_group_arrives(
    tmp_path: Path,
) -> None:
    """A transaction that spans several serve groups arrives whole.

    No test in this suite had ever built a transaction large enough to span
    a serve group, so the group boundary was exercised by nothing until the
    2026-09-09 autonomy-scope outage. With the cursor, the receiver also
    holds the origin's watermark until the LAST group lands.
    """
    from tools.network.fleet_sync_scheduler import SERVE_GROUP_OPERATIONS

    async def run() -> None:
        root, left_key, right_key, left_path, right_path, entries = (
            _pair_configs(tmp_path)
        )
        rows = [
            (f"big-{i:05d}", f"row {i}")
            for i in range(SERVE_GROUP_OPERATIONS + 1)
        ]
        _insert_together(right_path, rows)
        transactions = _journal_transactions(right_path)
        assert len(transactions) == 1, (
            f"the fixture must build ONE transaction, got {len(transactions)}")
        assert len(transactions[0]) > SERVE_GROUP_OPERATIONS, (
            "the fixture must exceed one serve group or it proves nothing")

        right = _scheduler(right_key, root, entries, right_path)
        await right.start()
        left = _scheduler(
            left_key, root, entries, left_path,
            peers={right_key.public_hex: [f"ws://127.0.0.1:{right.port}"]},
        )
        _insert(left_path, "left-seed", "keeps the delta path")
        await left.start()
        try:
            # The LAST row: it lands only if every group of the transaction
            # was accepted.
            await _eventually(
                lambda: _title(left_path, rows[-1][0]) is not None,
                timeout=30.0,
            )
        finally:
            await left.stop()
            await right.stop()

    asyncio.run(run())




def test_the_wire_has_one_version_and_refuses_every_other() -> None:
    """Every machine runs the same code, so there is no negotiation: a
    request or summary at any other version is refused with a typed error
    (graph://6ad52a52-f75 principle 3)."""
    from tools.network.fleet_sync_scheduler import (
        decode_pull_request, encode_pull_request,
    )
    assert FLEET_SYNC_PROTOCOL_VERSION == 6
    request = encode_pull_request("ab" * 32, compat="cd" * 32)
    assert decode_pull_request(request)[5] == FLEET_SYNC_PROTOCOL_VERSION
    for other in (3, 4, 5, 7):
        with pytest.raises(FleetSyncProtocolError):
            encode_pull_request("ab" * 32, compat="cd" * 32, version=other)
        with pytest.raises(FleetSyncProtocolError):
            decode_pull_request(request.replace(
                b'"v":%d' % FLEET_SYNC_PROTOCOL_VERSION, b'"v":%d' % other,
            ))
