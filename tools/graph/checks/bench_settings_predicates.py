#!/usr/bin/env python3
"""One-shot incident-scale benchmark for Settings predicate pushdown.

This command is intentionally outside pytest.  Its default run creates and
destroys a local 100,000-row organization database, then writes durable JSON
evidence to the caller-selected path.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import statistics
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator
from uuid import UUID

from tools.dashboard.plugins.testing.entrypoints.schemas import (
    AgentTestObservationV1,
    OBSERVATION_SET_ID,
)
from tools.graph import settings_ops
from tools.graph.db import GraphDB
from tools.graph.schemas.registry import (
    _payload_index_name,
    payload_json_extract_sql,
    reconcile_payload_indexes,
)

DEFAULT_ROWS = 100_000
DEFAULT_WARM_RUNS = 10
MIN_ROWS = 97_500
MIN_WARM_RUNS = 10
PRIMARY_REPOSITORY = "github.test/autonomy/autonomy"
SECONDARY_REPOSITORY = "github.test/autonomy/other"
RUN_ID_COUNT = 1_000
ORG = "benchmark"


def observation_key(index: int) -> str:
    """Return a deterministic UUIDv4-shaped key accepted by the real schema."""
    return str(UUID(int=index + 1, version=4))


def validate_workload(rows: int, warm_runs: int) -> None:
    """Refuse a scaled-down run that could be mistaken for capstone proof."""
    if rows < MIN_ROWS:
        raise ValueError(f"--rows must be at least {MIN_ROWS}")
    if warm_runs < MIN_WARM_RUNS:
        raise ValueError(f"--warm-runs must be at least {MIN_WARM_RUNS}")


def observation_entries(rows: int) -> list[tuple[str, dict[str, Any]]]:
    """Return the deterministic, schema-valid incident-shaped distribution."""
    primary_count = min(97_500, rows)
    entries: list[tuple[str, dict[str, Any]]] = []
    for index in range(rows):
        payload = {
            "repository": (
                PRIMARY_REPOSITORY if index < primary_count
                else SECONDARY_REPOSITORY
            ),
            "run_id": f"run-{index % RUN_ID_COUNT:04d}",
            "nodeid": f"tests/incident/test_scale.py::test_{index:06d}",
            "duration_seconds": float(index % 1000) / 1000.0,
            "outcome": "passed",
            "recorded_at": 1_789_156_800.0 + index,
        }
        AgentTestObservationV1.validate(payload)
        entries.append((observation_key(index), payload))
    return entries


def distribution(rows: int) -> dict[str, Any]:
    primary_count = min(97_500, rows)
    return {
        "primary_repository": PRIMARY_REPOSITORY,
        "primary_repository_rows": primary_count,
        "secondary_repository": SECONDARY_REPOSITORY,
        "secondary_repository_rows": rows - primary_count,
        "run_id_count": RUN_ID_COUNT,
        "target_run_id": "run-0000",
        "target_run_id_rows": (rows + RUN_ID_COUNT - 1) // RUN_ID_COUNT,
    }


@contextmanager
def temporary_database_root() -> Iterator[tuple[Path, Path]]:
    """Yield a local org root and prove its containing directory is removed."""
    container = Path(tempfile.mkdtemp(prefix="settings-predicate-bench-"))
    orgs = container / "orgs"
    orgs.mkdir()
    try:
        yield orgs, container
    finally:
        GraphDB.close_all_pooled()
        shutil.rmtree(container, ignore_errors=False)


def plan_detail(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> str:
    rows = conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
    return " | ".join(str(row[-1]) for row in rows)


def classify_plans(plans: dict[str, str], payload_index: str) -> list[str]:
    """Return concrete plan-contract failures; an empty list is a pass."""
    failures: list[str] = []
    if "idx_settings_set" not in plans["exact_key"] or "SEARCH" not in plans["exact_key"]:
        failures.append("exact_key did not SEARCH idx_settings_set")
    for name in ("repository_unindexed", "run_id_unindexed"):
        if payload_index in plans[name]:
            failures.append(f"{name} unexpectedly used {payload_index}")
    for name in ("run_id_indexed", "run_id_in_indexed"):
        if "SEARCH" not in plans[name] or payload_index not in plans[name]:
            failures.append(f"{name} did not SEARCH {payload_index}")
    return failures


def timed_samples(call: Callable[[], Any], runs: int) -> tuple[list[float], Any]:
    samples: list[float] = []
    last: Any = None
    for _ in range(runs):
        started = time.perf_counter_ns()
        last = call()
        samples.append((time.perf_counter_ns() - started) / 1_000_000.0)
    return samples, last


def summarize(samples: list[float], count: int) -> dict[str, Any]:
    return {"samples_ms": samples, "median_ms": statistics.median(samples), "members": count}


def threshold_failures(measurements: dict[str, dict[str, Any]]) -> list[str]:
    full = measurements["full_read"]["median_ms"]
    exact = measurements["exact_key"]["median_ms"]
    indexed = measurements["run_id_indexed"]["median_ms"]
    failures: list[str] = []
    if exact <= 0 or full / exact < 10.0:
        failures.append("exact-key speedup is below 10x")
    if indexed > 150.0:
        failures.append("indexed run_id median exceeds 150 ms")
    if indexed <= 0 or full / indexed < 8.0:
        failures.append("indexed run_id speedup is below 8x")
    return failures


def report_shape_failures(report: dict[str, Any]) -> list[str]:
    """Validate the durable evidence contract without running a large fixture."""
    required = {
        "status", "rows", "warm_runs", "sqlite_version", "distribution",
        "payload_index", "installed_indexes", "measurements", "ratios",
        "plans", "failures", "database_cleanup",
    }
    failures = [f"missing report field: {name}" for name in sorted(required - report.keys())]
    warm_runs = report.get("warm_runs")
    measurements = report.get("measurements")
    expected_measurements = {
        "full_read", "exact_key", "repository_unindexed",
        "run_id_unindexed", "run_id_indexed",
    }
    if not isinstance(measurements, dict):
        failures.append("measurements must be an object")
    else:
        for name in sorted(expected_measurements):
            measurement = measurements.get(name)
            if not isinstance(measurement, dict):
                failures.append(f"missing measurement: {name}")
                continue
            if not isinstance(measurement.get("median_ms"), (int, float)):
                failures.append(f"{name}.median_ms must be numeric")
            if not isinstance(measurement.get("members"), int):
                failures.append(f"{name}.members must be an integer")
            samples = measurement.get("samples_ms")
            if not isinstance(samples, list) or len(samples) != warm_runs:
                failures.append(f"{name}.samples_ms must contain warm_runs samples")
    expected_plans = {
        "full_read", "exact_key", "repository_unindexed", "run_id_unindexed",
        "run_id_indexed", "run_id_in_indexed",
    }
    plans = report.get("plans")
    if not isinstance(plans, dict) or not expected_plans.issubset(plans):
        failures.append("plans must contain all six query plans")
    cleanup = report.get("database_cleanup")
    if not isinstance(cleanup, dict) or not isinstance(cleanup.get("removed"), bool):
        failures.append("database_cleanup.removed must be boolean")
    return failures


def _plans(db_path: Path, payload_index: str, *, indexed: bool) -> dict[str, str]:
    expression = payload_json_extract_sql("run_id")
    repository_expression = payload_json_extract_sql("repository")
    conn = sqlite3.connect(db_path)
    try:
        plans = {
            "full_read": plan_detail(
                conn, "SELECT * FROM settings WHERE set_id=?", (OBSERVATION_SET_ID,),
            ),
            "exact_key": plan_detail(
                conn, "SELECT * FROM settings WHERE set_id=? AND key=?",
                (OBSERVATION_SET_ID, observation_key(0)),
            ),
            "repository_unindexed": plan_detail(
                conn,
                f"SELECT * FROM settings WHERE set_id=? AND {repository_expression}=?",
                (OBSERVATION_SET_ID, PRIMARY_REPOSITORY),
            ),
            "run_id_unindexed": plan_detail(
                conn, f"SELECT * FROM settings WHERE set_id=? AND {expression}=?",
                (OBSERVATION_SET_ID, "run-0000"),
            ),
        }
        if indexed:
            plans["run_id_indexed"] = plan_detail(
                conn, f"SELECT * FROM settings WHERE set_id=? AND {expression}=?",
                (OBSERVATION_SET_ID, "run-0000"),
            )
            plans["run_id_in_indexed"] = plan_detail(
                conn,
                f"SELECT * FROM settings WHERE set_id=? AND {expression} IN (?, ?)",
                (OBSERVATION_SET_ID, "run-0000", "run-0001"),
            )
        return plans
    finally:
        conn.close()


def run_benchmark(rows: int, warm_runs: int) -> dict[str, Any]:
    validate_workload(rows, warm_runs)
    previous_orgs = os.environ.get("AUTONOMY_ORGS_DIR")
    previous_graph_db = os.environ.get("GRAPH_DB")
    cleanup_path: Path | None = None
    report: dict[str, Any]
    try:
        with temporary_database_root() as (orgs, container):
            cleanup_path = container
            os.environ["AUTONOMY_ORGS_DIR"] = str(orgs)
            os.environ.pop("GRAPH_DB", None)
            GraphDB.create_org_db(ORG, root=orgs).close()
            settings_ops.append_log_entries(
                OBSERVATION_SET_ID, 1, observation_entries(rows), org=ORG,
            )
            GraphDB.close_all_pooled()
            db_path = orgs / f"{ORG}.db"
            payload_index = _payload_index_name(OBSERVATION_SET_ID, "run_id")
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(f'DROP INDEX IF EXISTS "{payload_index}"')
                conn.commit()
            finally:
                conn.close()

            unindexed_plans = _plans(db_path, payload_index, indexed=False)
            measurements: dict[str, dict[str, Any]] = {}
            samples, result = timed_samples(
                lambda: settings_ops.read_owned_set(OBSERVATION_SET_ID, org=ORG),
                warm_runs,
            )
            measurements["full_read"] = summarize(samples, len(result.members))
            samples, result = timed_samples(
                lambda: settings_ops.read_set_key(
                    OBSERVATION_SET_ID, observation_key(0), org=ORG, peers=[]
                ),
                warm_runs,
            )
            measurements["exact_key"] = summarize(samples, 1 if result else 0)
            samples, result = timed_samples(
                lambda: settings_ops.read_owned_set(
                    OBSERVATION_SET_ID,
                    org=ORG,
                    where_payload={"repository": PRIMARY_REPOSITORY},
                ),
                warm_runs,
            )
            measurements["repository_unindexed"] = summarize(samples, len(result.members))
            samples, unindexed_result = timed_samples(
                lambda: settings_ops.read_owned_set(
                    OBSERVATION_SET_ID,
                    org=ORG,
                    where_payload={"run_id": "run-0000"},
                ),
                warm_runs,
            )
            measurements["run_id_unindexed"] = summarize(
                samples, len(unindexed_result.members)
            )

            db = GraphDB.open_org_db(ORG, mode="rw", root=orgs)
            try:
                installed = reconcile_payload_indexes(db)
            finally:
                db.close()
            indexed_plans = _plans(db_path, payload_index, indexed=True)
            samples, indexed_result = timed_samples(
                lambda: settings_ops.read_owned_set(
                    OBSERVATION_SET_ID,
                    org=ORG,
                    where_payload={"run_id": "run-0000"},
                ),
                warm_runs,
            )
            measurements["run_id_indexed"] = summarize(samples, len(indexed_result.members))

            failures = classify_plans(
                {**unindexed_plans, **{
                    key: value for key, value in indexed_plans.items()
                    if key in ("run_id_indexed", "run_id_in_indexed")
                }},
                payload_index,
            )
            failures.extend(threshold_failures(measurements))
            expected = distribution(rows)
            expected_counts = {
                "full_read": rows,
                "exact_key": 1,
                "repository_unindexed": expected["primary_repository_rows"],
                "run_id_unindexed": expected["target_run_id_rows"],
                "run_id_indexed": expected["target_run_id_rows"],
            }
            for name, count in expected_counts.items():
                if measurements[name]["members"] != count:
                    failures.append(
                        f"{name} returned {measurements[name]['members']}, expected {count}"
                    )
            if {
                member.key for member in unindexed_result.members
            } != {member.key for member in indexed_result.members}:
                failures.append("indexed run_id result differs from unindexed baseline")
            report = {
                "status": "passed" if not failures else "failed",
                "rows": rows,
                "warm_runs": warm_runs,
                "sqlite_version": sqlite3.sqlite_version,
                "distribution": expected,
                "payload_index": payload_index,
                "installed_indexes": installed,
                "measurements": measurements,
                "ratios": {
                    "exact_key_vs_full": (
                        measurements["full_read"]["median_ms"]
                        / measurements["exact_key"]["median_ms"]
                    ),
                    "run_id_indexed_vs_full": (
                        measurements["full_read"]["median_ms"]
                        / measurements["run_id_indexed"]["median_ms"]
                    ),
                    "run_id_indexed_vs_unindexed": (
                        measurements["run_id_unindexed"]["median_ms"]
                        / measurements["run_id_indexed"]["median_ms"]
                    ),
                },
                "plans": {**unindexed_plans, **{
                    key: value for key, value in indexed_plans.items()
                    if key in ("run_id_indexed", "run_id_in_indexed")
                }},
                "failures": failures,
            }
    finally:
        GraphDB.close_all_pooled()
        if previous_orgs is None:
            os.environ.pop("AUTONOMY_ORGS_DIR", None)
        else:
            os.environ["AUTONOMY_ORGS_DIR"] = previous_orgs
        if previous_graph_db is None:
            os.environ.pop("GRAPH_DB", None)
        else:
            os.environ["GRAPH_DB"] = previous_graph_db
    assert cleanup_path is not None
    report["database_cleanup"] = {
        "path": str(cleanup_path),
        "removed": not cleanup_path.exists(),
    }
    if not report["database_cleanup"]["removed"]:
        report["status"] = "failed"
        report["failures"].append("temporary database directory still exists")
    shape_failures = report_shape_failures(report)
    if shape_failures:
        report["status"] = "failed"
        report["failures"].extend(shape_failures)
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=DEFAULT_ROWS)
    parser.add_argument("--warm-runs", type=int, default=DEFAULT_WARM_RUNS)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        validate_workload(args.rows, args.warm_runs)
    except ValueError as exc:
        parser.error(str(exc))
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = run_benchmark(args.rows, args.warm_runs)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "status": report["status"],
        "output": str(args.output),
        "medians_ms": {
            key: value["median_ms"] for key, value in report["measurements"].items()
        },
        "ratios": report["ratios"],
        "failures": report["failures"],
    }, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
