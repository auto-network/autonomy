from tools.network.fleet_sync.stress import (
    measure_cursor_restart,
    measure_core_scaling,
    measure_transfers,
    run_randomized_schedules,
)


def test_fixed_seed_partition_schedules_converge() -> None:
    results = run_randomized_schedules((7, 42))
    assert [item["seed"] for item in results] == [7, 42]
    assert all(item["converged"] for item in results)
    assert all(item["compaction_invariant"] for item in results)


def test_cursor_restart_exclusions_prevent_wire_duplicates() -> None:
    result = measure_cursor_restart()
    assert result["held_before_restart"] > 0
    assert result["served_after_restart"] > 0
    assert result["wire_duplicates_after_restart"] == 0


def test_core_scaling_reports_affined_segment_workers() -> None:
    result = measure_core_scaling(full=False)[0]
    assert result["workers"] == 1
    assert not result["skipped"]
    assert result["throughput_bytes_per_second"] > 0
    assert result["effective_cores"] > 0


def test_small_fleet_striping_and_collision_measurements() -> None:
    results = {item["name"]: item for item in measure_transfers(full=False)}
    assert results["four-collision-free"]["collision_overhead_packets"] == 0
    assert results["four-default-eight-stripes"]["collision_overhead_packets"] == 0
    assert results["four-forced-collision"]["collision_overhead_packets"] > 0
