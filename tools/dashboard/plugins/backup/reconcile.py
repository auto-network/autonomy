"""Run-report ingestion: capture ground truth → backup.run rows.

The capture engine writes run-report.json beside each capture and a
copy at ``<data root>/backup-reports/<tier>-latest.json`` (auto-yj2wa).
The tier copy lives on the DATA VOLUME, not the backup destination: a
containerized node cannot see the host's NAS mount at all (the
dangling-symlink lesson, 2026-09-06 — reconcile read "no reports"
forever), and the data volume is the one filesystem host and node
share. The bounded worker-thread read stays anyway (driver S7,
graph://7c45a180-345): it costs nothing on a local volume and keeps the
probe honest if an operator ever points the report dir somewhere
remote. Request handlers never call into the filesystem directly —
they read the Settings rows this module maintains.

The per-tier latest file is deliberately the whole probe surface: one
small known path per tier, never a directory listing.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from tools.dashboard.plugins.backup.entrypoints.schemas import (
    RUN_SET_ID,
    SCHEMA_REVISION,
    TIERS,
)
from tools.graph.schemas.registry import SchemaValidationError

_logger = logging.getLogger(__name__)

#: Hard ceiling on one report read (seconds). An NFS server that cannot
#: answer a 4 KB read inside this is down for our purposes.
PROBE_TIMEOUT_S = 15.0


def default_report_root() -> Path:
    from tools.data_paths import DATA_ROOT
    return DATA_ROOT / "backup-reports"


def _read_json_bounded(path: Path, timeout: float):
    """Read+parse *path* on a worker thread with a hard timeout.

    Returns the parsed dict, ``None`` for a missing file (no capture has
    ever reported — absence, not damage), or raises TimeoutError /
    ValueError / OSError for the probe-stale cases. A DAEMON thread, not
    an executor: a thread stuck in an uninterruptible NFS read must be
    abandoned outright — ThreadPoolExecutor workers are non-daemon and
    are joined at interpreter shutdown, so one hung probe would wedge
    process exit (found live against a FIFO stand-in, 2026-09-06).
    """
    outcome: dict = {}

    def target() -> None:
        try:
            outcome["text"] = path.read_text()
        except BaseException as exc:  # delivered to the caller below
            outcome["error"] = exc

    worker = threading.Thread(target=target, daemon=True,
                              name=f"backup-report-probe:{path.name}")
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise TimeoutError(f"reading {path} exceeded {timeout}s")
    error = outcome.get("error")
    if error is not None:
        if isinstance(error, FileNotFoundError):
            return None
        raise error
    return json.loads(outcome["text"])


def _run_retention() -> int:
    from tools.dashboard.plugins.backup.entrypoints.api import _read_config
    try:
        return max(1, int(_read_config().get("run_retention", 50)))
    except Exception:
        return 50


def reconcile(report_root: Path | str | None = None,
              timeout: float = PROBE_TIMEOUT_S) -> dict:
    """One reconcile pass. Returns what happened, for the caller to
    surface: ``{"ingested": [keys], "probe_errors": {tier: reason},
    "pruned": n}``."""
    from tools.graph import settings_ops
    root = Path(report_root) if report_root else default_report_root()
    ingested: list[str] = []
    probe_errors: dict[str, str] = {}
    pruned = 0
    for tier in TIERS:
        path = root / f"{tier}-latest.json"
        try:
            report = _read_json_bounded(path, timeout)
        except (TimeoutError, OSError, ValueError) as exc:
            probe_errors[tier] = str(exc)
            continue
        if report is None or not isinstance(report, dict):
            continue  # no capture has reported on this tier yet
        stamp = report.pop("stamp", "")
        report_tier = report.pop("tier", "") or tier
        if not stamp or report_tier != tier:
            probe_errors[tier] = f"malformed report at {path}"
            continue
        key = f"{tier}:{stamp}"
        try:
            existing = settings_ops.read_set_key(
                RUN_SET_ID, key, org="machine", peers=[])
        except Exception:
            existing = None
        if existing and existing.get("payload") == report:
            continue  # idempotent: nothing new (offsite verdict updates
            #           re-upsert because the payload differs)
        try:
            settings_ops.add_setting(
                RUN_SET_ID, SCHEMA_REVISION, key, report, org="machine")
        except SchemaValidationError as exc:
            probe_errors[tier] = f"report refused by schema: {exc}"
            continue
        ingested.append(key)
    if ingested:
        pruned = _prune(_run_retention())
    return {"ingested": ingested, "probe_errors": probe_errors,
            "pruned": pruned}


def _prune(retention: int) -> int:
    """Keep the newest *retention* run rows per tier — Settings must
    stay bounded (the store is not a log)."""
    from tools.graph import settings_ops
    try:
        members = list(settings_ops.read_set(
            RUN_SET_ID, org="machine", peers=[]))
    except Exception:
        return 0
    removed = 0
    for tier in TIERS:
        rows = sorted(
            (m for m in members if m.key.startswith(f"{tier}:")),
            key=lambda m: m.key, reverse=True)
        for member in rows[retention:]:
            try:
                settings_ops.remove_setting(member.id, org="machine")
                removed += 1
            except Exception as exc:  # never let cleanup break ingestion
                _logger.warning("backup run prune failed for %s: %s",
                                member.key, exc)
    return removed
