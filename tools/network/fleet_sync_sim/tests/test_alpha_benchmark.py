from pathlib import Path

from tools.network.fleet_sync_sim.alpha_benchmark import run


def test_alpha_benchmark_drives_complete_lifecycle(tmp_path: Path) -> None:
    result = run(
        tmp_path / "evidence", rows=40, payload_bytes=32, batch_rows=20,
        segment_bytes=64 * 1024, symbol_size=1024,
    )
    assert result["status"] == "pass"
    assert result["base_records"] == 40
    assert result["winner_records"] == 40
    assert result["checkpoint_artifact_bytes"] > 0
