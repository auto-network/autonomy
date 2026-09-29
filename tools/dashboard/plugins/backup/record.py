"""Record one backup run: its report file becomes its backup.run row.

tools/graph/backup-all.sh runs this the moment a run's report is written
(and again when the offsite push stamps its verdict), so the row exists
because the run finished, not because something went looking for it:

    python -m tools.dashboard.plugins.backup.record <tier>

The engine writes ``<data root>/backup-reports/<tier>-latest.json``; the
row key is ``<tier>:<stamp>``, the capture directory's own name.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from tools.dashboard.plugins.backup.entrypoints.schemas import (
    RUN_SET_ID,
    SCHEMA_REVISION,
    TIERS,
)

_logger = logging.getLogger(__name__)


def default_report_root() -> Path:
    from tools.data_paths import DATA_ROOT
    return DATA_ROOT / "backup-reports"


def _run_retention() -> int:
    from tools.dashboard.plugins.backup.entrypoints.api import _read_config
    try:
        return max(1, int(_read_config().get("run_retention", 50)))
    except Exception:
        return 50


def record(tier: str, report_root: Path | str | None = None) -> str:
    """Write *tier*'s latest report as its run row; returns the row key.

    Raises ValueError for an unknown tier or a malformed report, and
    OSError when the report cannot be read."""
    from tools.graph import settings_ops
    if tier not in TIERS:
        raise ValueError(f"unknown tier {tier!r}")
    root = Path(report_root) if report_root else default_report_root()
    path = root / f"{tier}-latest.json"
    report = json.loads(path.read_text())
    if not isinstance(report, dict):
        raise ValueError(f"malformed report at {path}")
    stamp = report.pop("stamp", "")
    if not stamp or (report.pop("tier", "") or tier) != tier:
        raise ValueError(f"malformed report at {path}")
    key = f"{tier}:{stamp}"
    # write_by_key upserts: the offsite verdict arrives as a second write
    # to the same run's key.
    settings_ops.write_by_key(
        RUN_SET_ID, SCHEMA_REVISION, key, report, org="machine")
    _prune(tier, _run_retention())
    return key


def _prune(tier: str, retention: int) -> int:
    """Keep the newest *retention* run rows of *tier* — Settings must
    stay bounded (the store is not a log)."""
    from tools.graph import settings_ops
    members = settings_ops.read_set(RUN_SET_ID, org="machine", peers=[])
    rows = sorted((m for m in members if m.key.startswith(f"{tier}:")),
                  key=lambda m: m.key, reverse=True)
    removed = 0
    for member in rows[retention:]:
        try:
            settings_ops.remove_setting(member.id, org="machine")
            removed += 1
        except Exception as exc:  # cleanup never fails the recording
            _logger.warning("backup run prune failed for %s: %s",
                            member.key, exc)
    return removed


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python -m tools.dashboard.plugins.backup.record <tier>")
    print(record(sys.argv[1]))
