from __future__ import annotations

import sqlite3

import pytest

from tools.network.accounting import (
    BATCH_INTERVAL_SECONDS,
    UsageBatch,
    UsageBatchMalformed,
    UsageLedger,
    UsageLedgerAuthorizationError,
    UsageLedgerConflict,
    UsageLedgerCorrupt,
    UsageLedgerLocked,
)
from tools.network.idkit import KeyPair

ORG_A = "33333333-3333-4333-8333-333333333333"
ORG_B = "44444444-4444-4444-8444-444444444444"
START = 1_756_000_000 - (1_756_000_000 % BATCH_INTERVAL_SECONDS)
SIGNER_A = KeyPair.from_private_hex("03" * 32)
SIGNER_B = KeyPair.from_private_hex("04" * 32)


def _wire(
    sequence: int,
    *,
    signer: KeyPair = SIGNER_A,
    organization_id: str = ORG_A,
    counters: dict[str, int] | None = None,
    interval_sequence: int | None = None,
) -> bytes:
    interval_sequence = sequence if interval_sequence is None else interval_sequence
    start = START + ((interval_sequence - 1) * BATCH_INTERVAL_SECONDS)
    return UsageBatch.create(
        signer=signer,
        organization_id=organization_id,
        sequence=sequence,
        interval_start=start,
        interval_end=start + BATCH_INTERVAL_SECONDS,
        created_at=start + BATCH_INTERVAL_SECONDS + 2,
        counters=counters or {"relay.egress_bytes": sequence},
    ).to_json()


def _authorize(ledger: UsageLedger) -> None:
    ledger.authorize_producer(
        producer=SIGNER_A.public_hex,
        organization_id=ORG_A,
        counter_families={"relay.egress_bytes", "relay.viewer_seconds"},
    )


def test_identical_retry_is_one_batch_and_receipt_is_exact(tmp_path):
    path = tmp_path / "ledger.sqlite"
    wire = _wire(1)
    batch = UsageBatch.from_json(wire)
    with UsageLedger(path) as ledger:
        _authorize(ledger)
        first = ledger.ingest(wire)
        retry = ledger.ingest(wire)
        assert first.accepted is True
        assert retry.accepted is False
        assert retry.batch_id == batch.batch_id
        assert retry.checksum == batch.checksum
        assert ledger.accepted_count(ORG_A) == 1


def test_same_id_changed_body_and_same_sequence_changed_interval_conflict(tmp_path):
    path = tmp_path / "ledger.sqlite"
    with UsageLedger(path) as ledger:
        _authorize(ledger)
        ledger.ingest(_wire(1, counters={"relay.egress_bytes": 10}))
        with pytest.raises(UsageLedgerConflict, match="different content"):
            ledger.ingest(_wire(1, counters={"relay.egress_bytes": 11}))

    second_path = tmp_path / "sequence-ledger.sqlite"
    with UsageLedger(second_path) as ledger:
        _authorize(ledger)
        ledger.ingest(_wire(1))
        with pytest.raises(UsageLedgerConflict, match="sequence"):
            ledger.ingest(_wire(1, interval_sequence=2))


def test_authorization_blocks_cross_org_unknown_counter_and_revoked_key(tmp_path):
    path = tmp_path / "ledger.sqlite"
    with UsageLedger(path) as ledger:
        _authorize(ledger)
        with pytest.raises(UsageLedgerAuthorizationError, match="not enabled"):
            ledger.ingest(_wire(1, organization_id=ORG_B))
        with pytest.raises(UsageLedgerAuthorizationError, match="counter families"):
            ledger.ingest(_wire(1, counters={"turn.egress_bytes": 1}))
        ledger.authorize_producer(
            producer=SIGNER_A.public_hex,
            organization_id=ORG_A,
            counter_families={"relay.egress_bytes"},
            enabled=False,
        )
        with pytest.raises(UsageLedgerAuthorizationError, match="not enabled"):
            ledger.ingest(_wire(1))
        assert ledger.accepted_count(ORG_A) == 0
        assert ledger.accepted_count(ORG_B) == 0


def test_out_of_order_batches_are_accepted_and_gaps_remain_visible(tmp_path):
    path = tmp_path / "ledger.sqlite"
    with UsageLedger(path) as ledger:
        _authorize(ledger)
        third = ledger.ingest(_wire(3))
        assert (third.contiguous_sequence, third.highest_sequence, third.gap_count) == (
            0,
            3,
            2,
        )
        assert ledger.reconciliation(
            producer=SIGNER_A.public_hex, organization_id=ORG_A
        ).gaps == ((1, 2),)

        ledger.ingest(_wire(1))
        assert ledger.reconciliation(
            producer=SIGNER_A.public_hex, organization_id=ORG_A
        ).gaps == ((2, 2),)

        second = ledger.ingest(_wire(2))
        assert (
            second.contiguous_sequence,
            second.highest_sequence,
            second.gap_count,
        ) == (3, 3, 0)
        assert ledger.health().streams_with_gaps == 0
        assert ledger.health().missing_sequences == 0


def test_large_sequence_gap_is_compact_not_linear_in_gap_width(tmp_path):
    path = tmp_path / "ledger.sqlite"
    largest = 2**63 - 1
    with UsageLedger(path) as ledger:
        _authorize(ledger)
        receipt = ledger.ingest(_wire(largest, interval_sequence=1))
        assert receipt.gap_count == largest - 1
        assert ledger.reconciliation(
            producer=SIGNER_A.public_hex, organization_id=ORG_A
        ).gaps == ((1, largest - 1),)
        assert ledger.health().missing_sequences == largest - 1


def test_two_organizations_are_isolated_and_state_survives_restart(tmp_path):
    path = tmp_path / "ledger.sqlite"
    with UsageLedger(path) as ledger:
        _authorize(ledger)
        ledger.authorize_producer(
            producer=SIGNER_B.public_hex,
            organization_id=ORG_B,
            counter_families={"relay.egress_bytes"},
        )
        ledger.ingest(_wire(1))
        ledger.ingest(_wire(1, signer=SIGNER_B, organization_id=ORG_B))

    with UsageLedger(path) as ledger:
        assert ledger.accepted_count(ORG_A) == 1
        assert ledger.accepted_count(ORG_B) == 1
        assert ledger.reconciliation(
            producer=SIGNER_A.public_hex, organization_id=ORG_A
        ).contiguous_sequence == 1
        assert ledger.reconciliation(
            producer=SIGNER_B.public_hex, organization_id=ORG_B
        ).contiguous_sequence == 1


def test_one_service_key_can_hold_separate_narrow_org_bindings(tmp_path):
    path = tmp_path / "ledger.sqlite"
    with UsageLedger(path) as ledger:
        _authorize(ledger)
        ledger.authorize_producer(
            producer=SIGNER_A.public_hex,
            organization_id=ORG_B,
            counter_families={"relay.viewer_seconds"},
        )
        ledger.ingest(
            _wire(
                1,
                organization_id=ORG_B,
                counters={"relay.viewer_seconds": 1},
            )
        )
        with pytest.raises(UsageLedgerAuthorizationError, match="counter families"):
            ledger.ingest(_wire(2, organization_id=ORG_B))
        assert ledger.accepted_count(ORG_A) == 0
        assert ledger.accepted_count(ORG_B) == 1
        assert ledger.health().authorization_bindings == 2


def test_failed_insert_rolls_back_batch_and_stream_cursor_together(tmp_path):
    path = tmp_path / "ledger.sqlite"
    with UsageLedger(path) as ledger:
        _authorize(ledger)
        ledger._db.execute(
            "CREATE TRIGGER fail_stream BEFORE INSERT ON stream_state "
            "BEGIN SELECT RAISE(ABORT, 'forced stream failure'); END"
        )
        with pytest.raises(UsageLedgerConflict, match="forced stream failure"):
            ledger.ingest(_wire(1))
        assert ledger.accepted_count(ORG_A) == 0
        assert ledger.reconciliation(
            producer=SIGNER_A.public_hex, organization_id=ORG_A
        ).highest_sequence == 0


def test_second_ledger_process_owner_is_rejected(tmp_path):
    path = tmp_path / "ledger.sqlite"
    with UsageLedger(path):
        with pytest.raises(UsageLedgerLocked):
            UsageLedger(path)


def test_audit_detects_retained_wire_corruption(tmp_path):
    path = tmp_path / "ledger.sqlite"
    with UsageLedger(path) as ledger:
        _authorize(ledger)
        receipt = ledger.ingest(_wire(1))
    with sqlite3.connect(path) as raw:
        raw.execute(
            "UPDATE accepted_batches SET wire=? WHERE batch_id=?",
            (b"{}", receipt.batch_id),
        )
        stored = raw.execute(
            "SELECT wire FROM accepted_batches WHERE batch_id=?", (receipt.batch_id,)
        ).fetchone()[0]
    with pytest.raises(UsageBatchMalformed):
        UsageBatch.from_json(stored)
    with UsageLedger(path) as ledger:
        with pytest.raises(UsageLedgerCorrupt, match=receipt.batch_id):
            ledger.audit()
