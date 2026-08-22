from __future__ import annotations

import multiprocessing
import os
import sqlite3

import pytest

from tools.network.accounting import (
    BATCH_INTERVAL_SECONDS,
    UsageBatch,
    UsageSpool,
    UsageSpoolAckError,
    UsageSpoolCapacity,
    UsageSpoolConflict,
    UsageSpoolCorrupt,
    UsageSpoolIOError,
    UsageSpoolLocked,
    UsageSpoolSequenceError,
)
from tools.network.idkit import KeyPair

ORG = "22222222-2222-4222-8222-222222222222"
START = 1_756_000_000 - (1_756_000_000 % BATCH_INTERVAL_SECONDS)
SIGNER = KeyPair.from_private_hex("02" * 32)


def _wire(sequence: int, *, count: int | None = None) -> bytes:
    start = START + ((sequence - 1) * BATCH_INTERVAL_SECONDS)
    return UsageBatch.create(
        signer=SIGNER,
        organization_id=ORG,
        sequence=sequence,
        interval_start=start,
        interval_end=start + BATCH_INTERVAL_SECONDS,
        created_at=start + BATCH_INTERVAL_SECONDS + 1,
        counters={"network.egress_bytes": count if count is not None else sequence},
    ).to_json()


def _append_then_die(path: str, wire: bytes) -> None:
    spool = UsageSpool(path)
    spool.append(wire)
    os._exit(23)


def test_committed_append_survives_process_death_and_retries_exact_bytes(tmp_path):
    path = tmp_path / "usage-spool.sqlite"
    wire = _wire(1)
    process = multiprocessing.get_context("fork").Process(
        target=_append_then_die, args=(str(path), wire)
    )
    process.start()
    process.join(timeout=10)
    assert process.exitcode == 23

    with UsageSpool(path) as spool:
        records = list(spool.pending())
        assert [record.wire for record in records] == [wire]
        assert spool.append(wire) is False
        assert spool.health().pending_records == 1


def test_exact_ack_and_prune_survive_restart_without_permitting_replay(tmp_path):
    path = tmp_path / "usage-spool.sqlite"
    batch = UsageBatch.from_json(_wire(1))
    with UsageSpool(path) as spool:
        assert spool.append(batch.to_json()) is True
        assert spool.acknowledge(batch.batch_id, batch.checksum) is True
        assert spool.acknowledge(batch.batch_id, batch.checksum) is False

    with UsageSpool(path) as spool:
        assert list(spool.pending()) == []
        assert spool.health().acknowledged_records == 1
        assert spool.next_sequence(SIGNER.public_hex, ORG) == 2
        assert spool.prune_acked() == 1

    with UsageSpool(path) as spool:
        assert spool.health().acknowledged_records == 0
        assert spool.next_sequence(SIGNER.public_hex, ORG) == 2
        with pytest.raises(UsageSpoolSequenceError, match="stale replay"):
            spool.append(batch.to_json())


def test_false_ack_never_discards_pending_bytes(tmp_path):
    path = tmp_path / "usage-spool.sqlite"
    batch = UsageBatch.from_json(_wire(1))
    with UsageSpool(path) as spool:
        spool.append(batch.to_json())
        with pytest.raises(UsageSpoolAckError, match="checksum"):
            spool.acknowledge(batch.batch_id, "0" * 64)
        with pytest.raises(UsageSpoolAckError, match="unknown"):
            spool.acknowledge("f" * 64, batch.checksum)
        assert [record.wire for record in spool.pending()] == [batch.to_json()]


def test_same_identity_with_changed_content_is_a_conflict(tmp_path):
    path = tmp_path / "usage-spool.sqlite"
    first = _wire(1, count=10)
    changed = _wire(1, count=11)
    assert UsageBatch.from_json(first).batch_id == UsageBatch.from_json(changed).batch_id
    with UsageSpool(path) as spool:
        spool.append(first)
        with pytest.raises(UsageSpoolConflict):
            spool.append(changed)


def test_sequence_gap_is_rejected_without_advancing_stream(tmp_path):
    path = tmp_path / "usage-spool.sqlite"
    with UsageSpool(path) as spool:
        with pytest.raises(UsageSpoolSequenceError, match="expected 1, got 2"):
            spool.append(_wire(2))
        assert spool.next_sequence(SIGNER.public_hex, ORG) == 1
        spool.append(_wire(1))
        assert spool.next_sequence(SIGNER.public_hex, ORG) == 2


def test_corrupt_later_record_fails_visibly_after_preserving_earlier_record(tmp_path):
    path = tmp_path / "usage-spool.sqlite"
    with UsageSpool(path) as spool:
        spool.append(_wire(1))
        spool.append(_wire(2))

    with sqlite3.connect(path) as raw:
        raw.execute("UPDATE batches SET wire=? WHERE sequence=2", (b"{}",))

    with UsageSpool(path) as spool:
        records = iter(spool.pending(limit=10))
        assert next(records).wire == _wire(1)
        with pytest.raises(UsageSpoolCorrupt, match="row 2"):
            next(records)
        assert spool.health().pending_records == 2


def test_record_and_byte_capacity_backpressure_is_bounded_and_recoverable(tmp_path):
    path = tmp_path / "usage-spool.sqlite"
    first = UsageBatch.from_json(_wire(1))
    second = UsageBatch.from_json(_wire(2))
    third = UsageBatch.from_json(_wire(3))
    with UsageSpool(path, max_records=2) as spool:
        spool.append(first.to_json())
        spool.append(second.to_json())
        with pytest.raises(UsageSpoolCapacity):
            spool.append(third.to_json())
        spool.acknowledge(first.batch_id, first.checksum)
        with pytest.raises(UsageSpoolCapacity):
            spool.append(third.to_json())
        assert spool.prune_acked() == 1
        spool.append(third.to_json())
        assert spool.health().pending_records == 2

    byte_path = tmp_path / "byte-bounded.sqlite"
    with UsageSpool(byte_path, max_bytes=len(first.to_json())) as spool:
        spool.append(first.to_json())
        with pytest.raises(UsageSpoolCapacity):
            spool.append(second.to_json())


def test_second_process_owner_is_rejected(tmp_path):
    path = tmp_path / "usage-spool.sqlite"
    with UsageSpool(path):
        with pytest.raises(UsageSpoolLocked):
            UsageSpool(path)


def test_sqlite_disk_full_is_mapped_and_does_not_advance_sequence(tmp_path):
    path = tmp_path / "usage-spool.sqlite"
    counters = {f"network.metric_{index:02d}": index + 1 for index in range(64)}
    with UsageSpool(path) as spool:
        page_count = spool._db.execute("PRAGMA page_count").fetchone()[0]
        spool._db.execute(f"PRAGMA max_page_count={page_count}")
        failed_sequence = None
        for sequence in range(1, 20):
            start = START + ((sequence - 1) * BATCH_INTERVAL_SECONDS)
            wire = UsageBatch.create(
                signer=SIGNER,
                organization_id=ORG,
                sequence=sequence,
                interval_start=start,
                interval_end=start + BATCH_INTERVAL_SECONDS,
                created_at=start + BATCH_INTERVAL_SECONDS + 1,
                counters=counters,
            ).to_json()
            try:
                spool.append(wire)
            except UsageSpoolIOError:
                failed_sequence = sequence
                break
        assert failed_sequence is not None
        assert spool.next_sequence(SIGNER.public_hex, ORG) == failed_sequence


def test_spool_has_no_service_or_application_identity_fields(tmp_path):
    path = tmp_path / "usage-spool.sqlite"
    with UsageSpool(path) as spool:
        spool.append(_wire(1))
    with sqlite3.connect(path) as raw:
        columns = {
            row[1] for row in raw.execute("PRAGMA table_info(batches)").fetchall()
        }
    assert not columns.intersection(
        {"member_id", "session_id", "token", "source_address", "path", "allocation"}
    )
