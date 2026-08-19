from pathlib import Path

from tools.network.fleet_sync_sim.live_workload import run_live_workload


def test_continuous_writes_reads_and_raptorq_checkpoints_catch_up(
    tmp_path: Path,
) -> None:
    report = run_live_workload(
        tmp_path / "live", duration_seconds=0.8,
        checkpoint_interval=0.03, writes_per_transaction=4,
        writer_interval_seconds=0.01,
    )
    # This is deliberately a short CI exercise; the release evidence runs the
    # same workload for several seconds and establishes the sustained rate.
    assert report.committed_transactions > 2
    assert report.checkpoints_installed >= 2
    assert report.source_reads > 0
    assert report.target_reads > 0
    assert report.read_errors == 0
    assert report.final_lag_transactions == 0
    assert report.final_source_rows == report.final_target_rows
