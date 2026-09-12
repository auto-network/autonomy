"""Small helper tests for the manual Settings predicate benchmark."""
from __future__ import annotations

from pathlib import Path

import pytest

from tools.graph.checks.bench_settings_predicates import (
    PRIMARY_REPOSITORY,
    classify_plans,
    distribution,
    observation_entries,
    observation_key,
    report_shape_failures,
    temporary_database_root,
    threshold_failures,
    validate_workload,
)


@pytest.mark.parametrize("rows,warm_runs", [(97_499, 10), (100_000, 9)])
def test_validation_refuses_non_capstone_workloads(rows, warm_runs):
    with pytest.raises(ValueError):
        validate_workload(rows, warm_runs)


def test_distribution_and_entries_are_deterministic_and_valid():
    entries = observation_entries(200)
    assert len(entries) == 200
    assert entries[0][0] == observation_key(0)
    assert entries[-1][0] == observation_key(199)
    assert all(payload["repository"] == PRIMARY_REPOSITORY for _, payload in entries)
    assert distribution(100_000)["primary_repository_rows"] == 97_500
    assert distribution(100_000)["target_run_id_rows"] == 100


def test_plan_classification_pins_exact_and_payload_indexes():
    index = "idx_settings_payload_deadbeefdeadbeef"
    plans = {
        "exact_key": "SEARCH settings USING INDEX idx_settings_set",
        "repository_unindexed": "SEARCH settings USING INDEX idx_settings_set",
        "run_id_unindexed": "SEARCH settings USING INDEX idx_settings_set",
        "run_id_indexed": f"SEARCH settings USING INDEX {index}",
        "run_id_in_indexed": f"SEARCH settings USING INDEX {index}",
    }
    assert classify_plans(plans, index) == []
    plans["run_id_indexed"] = "SCAN settings"
    assert classify_plans(plans, index) == [
        f"run_id_indexed did not SEARCH {index}"
    ]


def test_thresholds_are_fixed_and_falsifiable():
    passing = {
        "full_read": {"median_ms": 2_000.0},
        "exact_key": {"median_ms": 10.0},
        "run_id_indexed": {"median_ms": 100.0},
    }
    assert threshold_failures(passing) == []
    failing = {
        "full_read": {"median_ms": 500.0},
        "exact_key": {"median_ms": 100.0},
        "run_id_indexed": {"median_ms": 200.0},
    }
    assert len(threshold_failures(failing)) == 3


def test_temporary_database_root_is_removed_after_success_and_failure():
    seen: Path | None = None
    with temporary_database_root() as (_orgs, container):
        seen = container
        assert seen.exists()
    assert seen is not None and not seen.exists()

    failed: Path | None = None
    with pytest.raises(RuntimeError):
        with temporary_database_root() as (_orgs, container):
            failed = container
            raise RuntimeError("boom")
    assert failed is not None and not failed.exists()


def test_report_shape_contract_without_a_large_database():
    measurement = {"samples_ms": [1.0] * 10, "median_ms": 1.0, "members": 1}
    report = {
        "status": "passed",
        "rows": 100_000,
        "warm_runs": 10,
        "sqlite_version": "test",
        "distribution": {},
        "payload_index": "idx_settings_payload_deadbeefdeadbeef",
        "installed_indexes": ["idx_settings_payload_deadbeefdeadbeef"],
        "measurements": {
            name: dict(measurement) for name in (
                "full_read", "exact_key", "repository_unindexed",
                "run_id_unindexed", "run_id_indexed",
            )
        },
        "ratios": {},
        "plans": {name: "SEARCH" for name in (
            "full_read", "exact_key", "repository_unindexed",
            "run_id_unindexed", "run_id_indexed", "run_id_in_indexed",
        )},
        "failures": [],
        "database_cleanup": {"path": "/tmp/gone", "removed": True},
    }
    assert report_shape_failures(report) == []
    del report["plans"]["run_id_in_indexed"]
    assert report_shape_failures(report) == ["plans must contain all six query plans"]
