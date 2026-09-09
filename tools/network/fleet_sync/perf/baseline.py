"""Baseline retention and comparison for the fleet sync perf suite.

A *result* is one full run of the suite serialized as JSON.  The *retained
baseline* is the most recent result for the same scale, kept in a shared
store so any future session on this workspace compares against it.  The
comparison is direction-aware: every reported metric declares whether
higher or lower is better, and a change past the regression threshold is
flagged — reported, never gating.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
REGRESSION_THRESHOLD = 0.10

# metric name -> (direction, unit).  Direction "higher" means larger values
# are better.  Only metrics listed here appear in the comparison table;
# everything else in a benchmark's payload is retained but informational.
METRIC_DIRECTIONS: dict[str, dict[str, tuple[str, str]]] = {
    "kernel": {
        "insert_rows_per_s": ("higher", "rows/s"),
        "update_rows_per_s": ("higher", "rows/s"),
        "file_full_bytes": ("lower", "B"),
        "file_steady_bytes": ("lower", "B"),
        "steady_tracking_overhead_percent": ("lower", "%"),
        "journal_rows_after_ack": ("lower", "rows"),
        "prune_vacuum_s": ("lower", "s"),
    },
    "storage": {
        "storage_overhead_percent_vs_indexed": ("lower", "%"),
        "steady_file_delta_bytes_per_row": ("lower", "B/row"),
        "catalog_bytes_per_row": ("lower", "B/row"),
        "write_rate_ratio_tracked_to_indexed": ("higher", "ratio"),
        "tracked_rows_per_s": ("higher", "rows/s"),
    },
    "crsqlite": {
        "insert_rows_per_s": ("higher", "rows/s"),
        "update_rows_per_s": ("higher", "rows/s"),
        "db_file_bytes": ("lower", "B"),
        "delta_wire_bytes": ("lower", "B"),
    },
}


def default_store() -> Path:
    """Resolve the retained-baseline store, most durable location first."""
    override = os.environ.get("FLEET_SYNC_PERF_BASELINES")
    if override:
        return Path(override)
    shared = Path("/opt/autonomy-developer")
    if shared.is_dir():
        return shared / "fleet-sync-perf"
    return Path(__file__).resolve().parents[4] / "data" / "perf" / "fleet_sync"


def host_fingerprint() -> dict[str, Any]:
    return {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpus": os.cpu_count(),
    }


def new_result(scale: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat(
            timespec="seconds"
        ),
        "scale": scale,
        "host": host_fingerprint(),
        "benchmarks": {},
    }


def load_result(path: Path) -> dict[str, Any]:
    body = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(body, dict) or body.get("schema") != SCHEMA_VERSION:
        raise ValueError(f"{path} is not a schema-{SCHEMA_VERSION} perf result")
    return body


def baseline_path(store: Path, scale: str) -> Path:
    return store / f"baseline-{scale}.json"


def retain(store: Path, result: dict[str, Any]) -> tuple[Path, Path]:
    """Write the run into history and promote it to the scale's baseline."""
    store.mkdir(parents=True, exist_ok=True)
    runs = store / "runs"
    runs.mkdir(exist_ok=True)
    stamp = str(result["created_at"]).replace(":", "").replace("+0000", "Z")
    body = json.dumps(result, sort_keys=True, indent=2) + "\n"
    history = runs / f"{stamp}-{result['scale']}.json"
    history.write_text(body, encoding="utf-8")
    promoted = baseline_path(store, str(result["scale"]))
    promoted.write_text(body, encoding="utf-8")
    return history, promoted


@dataclass(frozen=True)
class MetricRow:
    benchmark: str
    metric: str
    unit: str
    baseline: float | None
    current: float | None
    change_percent: float | None
    regression: bool
    improvement: bool


def _format_value(value: float | None, unit: str) -> str:
    if value is None:
        return "—"
    if unit == "B":
        return f"{value / 1_000_000:,.1f} MB" if value >= 1_000_000 else f"{value:,.0f} B"
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    if abs(value) >= 10:
        return f"{value:,.1f}"
    return f"{value:,.3f}"


def compare(
    baseline: dict[str, Any] | None, current: dict[str, Any]
) -> list[MetricRow]:
    rows: list[MetricRow] = []
    base_benchmarks = (baseline or {}).get("benchmarks", {})
    for name, spec in METRIC_DIRECTIONS.items():
        entry = current["benchmarks"].get(name)
        if not entry or entry.get("status") != "pass":
            continue
        base_entry = base_benchmarks.get(name) or {}
        base_metrics = (
            base_entry.get("metrics", {})
            if base_entry.get("status") == "pass" else {}
        )
        for metric, (direction, unit) in spec.items():
            value = entry.get("metrics", {}).get(metric)
            if value is None:
                continue
            previous = base_metrics.get(metric)
            change = None
            regression = improvement = False
            if previous not in (None, 0):
                change = (float(value) - float(previous)) / abs(float(previous))
                worse = change < 0 if direction == "higher" else change > 0
                if abs(change) >= REGRESSION_THRESHOLD:
                    regression = worse
                    improvement = not worse
            rows.append(MetricRow(
                name, metric, unit, previous, value,
                None if change is None else 100 * change,
                regression, improvement,
            ))
    return rows


def render_table(
    rows: list[MetricRow], baseline: dict[str, Any] | None
) -> str:
    header = f"{'benchmark':<12} {'metric':<38} {'baseline':>12} {'current':>12} {'change':>9}"
    lines = [header, "-" * len(header)]
    for row in rows:
        change = "" if row.change_percent is None else f"{row.change_percent:+.1f}%"
        marker = " ◂ regression" if row.regression else (
            " ◂ improved" if row.improvement else ""
        )
        lines.append(
            f"{row.benchmark:<12} {row.metric:<38} "
            f"{_format_value(row.baseline, row.unit):>12} "
            f"{_format_value(row.current, row.unit):>12} {change:>9}{marker}"
        )
    regressions = sum(1 for row in rows if row.regression)
    if baseline is None:
        lines.append("(no retained baseline for this scale — first run recorded)")
    elif regressions:
        lines.append(
            f"{regressions} regression(s) past "
            f"{REGRESSION_THRESHOLD:.0%} — reported, not gating"
        )
    else:
        lines.append("no regressions past the threshold")
    return "\n".join(lines)
