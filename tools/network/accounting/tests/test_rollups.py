from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tools.network.accounting import (
    DAY,
    FIVE_MINUTE,
    HOUR,
    MONTH,
    RollupRow,
    UsageBatch,
    UsageLedger,
    UsageLedgerConflict,
    UsageProgress,
    UsageRollupIncomplete,
    UsageRollups,
    steady_state_rows,
)
from tools.network.idkit import KeyPair

ORG = "55555555-5555-4555-8555-555555555555"
SIGNER_A = KeyPair.from_private_hex("05" * 32)
SIGNER_B = KeyPair.from_private_hex("06" * 32)


def _ts(year: int, month: int, day: int, hour: int = 0) -> int:
    return int(datetime(year, month, day, hour, tzinfo=UTC).timestamp())


def _authorize(
    ledger: UsageLedger,
    signer: KeyPair,
    active_from: int,
    meter_class: str = "default",
) -> None:
    ledger.authorize_producer(
        producer=signer.public_hex,
        organization_id=ORG,
        counter_families={"relay.egress_bytes", "relay.viewer_seconds"},
        active_from=active_from,
        meter_class=meter_class,
    )


def _batch(
    signer: KeyPair,
    sequence: int,
    start: int,
    counters: dict[str, int],
) -> bytes:
    return UsageBatch.create(
        signer=signer,
        organization_id=ORG,
        sequence=sequence,
        interval_start=start,
        interval_end=start + 300,
        created_at=start + 302,
        counters=counters,
    ).to_json()


def _progress(
    signer: KeyPair, sequence: int, closed_through: int
) -> bytes:
    return UsageProgress.create(
        signer=signer,
        organization_id=ORG,
        sequence=sequence,
        closed_through=closed_through,
        created_at=closed_through + 2,
    ).to_json()


def test_rollups_sum_producers_fill_idle_intervals_and_rerun_exactly(tmp_path):
    start = _ts(2028, 2, 1)
    with UsageLedger(tmp_path / "ledger.sqlite") as ledger:
        _authorize(ledger, SIGNER_A, start, "hetzner.fsn1.relay.public")
        _authorize(ledger, SIGNER_B, start, "aws.use1.relay.public")
        ledger.ingest(
            _batch(SIGNER_A, 1, start, {"relay.egress_bytes": 10})
        )
        ledger.ingest(
            _batch(SIGNER_B, 1, start, {"relay.egress_bytes": 7})
        )
        ledger.ingest(
            _batch(
                SIGNER_A,
                2,
                start + 300,
                {"relay.viewer_seconds": 2},
            )
        )
        ledger.accept_progress(_progress(SIGNER_A, 2, start + 3_600))
        ledger.accept_progress(_progress(SIGNER_B, 1, start + 3_600))

        rollups = UsageRollups(ledger)
        first = rollups.materialize(ORG, start=start)
        assert (first.inserted, first.unchanged) == (13, 0)
        five = rollups.rows(ORG, FIVE_MINUTE)
        assert len(five) == 12
        assert five[0].meters == {
            "aws.use1.relay.public": {"relay.egress_bytes": 7},
            "hetzner.fsn1.relay.public": {"relay.egress_bytes": 10},
        }
        assert five[0].source_rows == 2
        assert five[1].meters == {
            "hetzner.fsn1.relay.public": {"relay.viewer_seconds": 2}
        }
        assert five[2].meters == {}
        hour = rollups.rows(ORG, HOUR)
        assert len(hour) == 1
        assert hour[0].meters == {
            "aws.use1.relay.public": {"relay.egress_bytes": 7},
            "hetzner.fsn1.relay.public": {
                "relay.egress_bytes": 10,
                "relay.viewer_seconds": 2,
            },
        }
        assert rollups.verify_parent(ORG, HOUR, start) is True

        second = rollups.materialize(ORG, start=start)
        assert (second.inserted, second.unchanged) == (0, 13)


def test_authorization_lifetimes_control_historical_settlement(tmp_path):
    start = _ts(2028, 4, 1)
    with UsageLedger(tmp_path / "ledger.sqlite") as ledger:
        _authorize(ledger, SIGNER_A, start)
        _authorize(ledger, SIGNER_B, start + 600)
        ledger.accept_progress(_progress(SIGNER_A, 0, start + 900))
        rollups = UsageRollups(ledger)

        assert ledger.interval_is_settled(ORG, start + 300, start + 600) is True
        assert ledger.interval_is_settled(ORG, start + 600, start + 900) is False
        first = rollups.materialize(ORG, start=start)
        assert first.settled_through == start + 600
        assert len(rollups.rows(ORG, FIVE_MINUTE)) == 2

        ledger.accept_progress(_progress(SIGNER_B, 0, start + 900))
        assert ledger.interval_is_settled(ORG, start + 600, start + 900) is True
        second = rollups.materialize(ORG, start=start)
        assert second.settled_through == start + 900
        assert (second.inserted, second.unchanged) == (1, 2)

        ledger.authorize_producer(
            producer=SIGNER_B.public_hex,
            organization_id=ORG,
            counter_families={"relay.egress_bytes"},
            enabled=False,
            active_from=start + 600,
            active_through=start + 900,
        )
        ledger.accept_progress(_progress(SIGNER_A, 0, start + 1_200))
        third = rollups.materialize(ORG, start=start)
        assert third.settled_through == start + 1_200
        assert len(rollups.rows(ORG, FIVE_MINUTE)) == 4

        with pytest.raises(UsageLedgerConflict, match="closed watermark"):
            ledger.ingest(
                _batch(SIGNER_A, 1, start + 900, {"relay.egress_bytes": 1})
            )


def test_leap_month_boundary_and_retention_are_exact_and_idempotent(tmp_path):
    start = _ts(2028, 2, 1)
    end = _ts(2028, 3, 1)
    with UsageLedger(tmp_path / "ledger.sqlite") as ledger:
        _authorize(ledger, SIGNER_A, start)
        first_day_end = start + 86_400
        ledger.accept_progress(_progress(SIGNER_A, 0, first_day_end))
        rollups = UsageRollups(ledger)
        run = rollups.materialize(ORG, start=start)

        assert run.inserted == 288 + 24 + 1
        assert len(rollups.rows(ORG, FIVE_MINUTE)) == 288
        assert len(rollups.rows(ORG, HOUR)) == 24
        assert len(rollups.rows(ORG, DAY)) == 1
        with ledger._transaction():
            for day in range(1, 29):
                rollups._insert_exact(
                    RollupRow(
                        DAY,
                        ORG,
                        start + (day * 86_400),
                        start + ((day + 1) * 86_400),
                        {},
                        HOUR,
                        24,
                        f"{day:064x}",
                    )
                )
            rollups._parent(MONTH, DAY, ORG, start, end, 29)
        month = rollups.rows(ORG, MONTH)
        assert len(month) == 1
        assert month[0].interval_start == start
        assert month[0].interval_end == end
        assert month[0].source_rows == 29
        assert rollups.verify_parent(ORG, MONTH, start) is True
        assert steady_state_rows() == 3_867

        pruned = rollups.prune(ORG, as_of=_ts(2031, 3, 1))
        assert pruned.five_minute_deleted == 288
        assert pruned.hour_deleted == 24
        assert pruned.day_deleted == 29
        assert len(rollups.rows(ORG, MONTH)) == 1
        assert rollups.prune(ORG, as_of=_ts(2031, 3, 1)) == type(pruned)(
            organization_id=ORG,
            as_of=_ts(2031, 3, 1),
            five_minute_deleted=0,
            hour_deleted=0,
            day_deleted=0,
        )


def test_prune_refuses_a_parent_that_does_not_match_retained_children(tmp_path):
    start = _ts(2028, 5, 1)
    with UsageLedger(tmp_path / "ledger.sqlite") as ledger:
        _authorize(ledger, SIGNER_A, start)
        ledger.accept_progress(_progress(SIGNER_A, 0, start + 3_600))
        rollups = UsageRollups(ledger)
        rollups.materialize(ORG, start=start)
        ledger._db.execute(
            "UPDATE usage_rollups SET meters=? WHERE tier=? "
            "AND organization_id=? AND interval_start=?",
            (b'{"default":{"relay.egress_bytes":1}}', HOUR, ORG, start),
        )

        assert rollups.verify_parent(ORG, HOUR, start) is False
        with pytest.raises(UsageRollupIncomplete, match="unverified"):
            rollups.prune(ORG, as_of=start + (8 * 86_400))
        assert len(rollups.rows(ORG, FIVE_MINUTE)) == 12
