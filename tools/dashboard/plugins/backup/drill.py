"""Restore drills: run and record (auto-mu7qf).

The checks themselves live in tools/graph/backup-restore.sh (drill
subcommand) — restic restore to scratch, marker requirement, integrity
through the app's SQL-function registration, sources sanity, beads
count. This module runs that script as a subprocess, parses its
``@``-event lines into a BackupDrillV1 row written once, when it ends.

Exactly one drill runs at a time. The drill in flight is known only to
this process (:func:`running`); nothing about it is stored until it
finishes, so a restart cannot leave a record claiming a drill is still
running. Timeouts kill the subprocess group and record ``timeout`` — a
drill that cannot finish is a failed drill, not a silent absence.
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

from tools.dashboard.plugins.backup.entrypoints.schemas import (
    DRILL_SET_ID,
    SCHEMA_REVISION,
)

logger = logging.getLogger(__name__)

# tools/dashboard/plugins/backup/drill.py -> four parents up is the repo
# root. parents[3] was tools/, which sent every scheduled drill to
# tools/tools/graph/backup-restore.sh and made it fail before its
# first check (live 2026-09-13: "No such file or directory", 0 checks).
_REPO = Path(__file__).resolve().parents[4]
DRILL_SCRIPT = _REPO / "tools" / "graph" / "backup-restore.sh"

#: Bounded tail of drill output stored as row evidence.
EVIDENCE_LIMIT = 4000

_lock = threading.Lock()
#: The drill in flight in this process: {"key", "trigger", "started_at"}.
_running: dict | None = None


class DrillAlreadyRunning(RuntimeError):
    def __init__(self, stamp: str):
        self.stamp = stamp
        super().__init__(f"drill {stamp} is already running")


def running_stamp() -> str | None:
    return _running["key"] if _running else None


def running() -> dict | None:
    return dict(_running) if _running else None


def parse_events(output: str) -> tuple[list[dict], str, str]:
    """(checks, snapshot_id, script_verdict) from drill output lines."""
    checks: list[dict] = []
    snapshot_id = ""
    verdict = ""
    for line in output.splitlines():
        if not line.startswith("@"):
            continue
        parts = line.split(" ", 3)
        if parts[0] == "@snapshot" and len(parts) >= 2:
            snapshot_id = parts[1]
        elif parts[0] == "@check" and len(parts) >= 3:
            status = parts[2] if parts[2] in ("ok", "fail", "skipped") else "fail"
            checks.append({
                "name": parts[1],
                "status": status,
                "detail": parts[3] if len(parts) > 3 else "",
            })
        elif parts[0] == "@verdict" and len(parts) >= 2:
            verdict = parts[1]
    return checks, snapshot_id, verdict


def _upsert(stamp: str, payload: dict) -> None:
    from tools.graph import settings_ops
    # write_by_key: the running row and its finalization share one key.
    settings_ops.write_by_key(
        DRILL_SET_ID, SCHEMA_REVISION, stamp, payload, org="machine")


def _drill_retention() -> int:
    from tools.dashboard.plugins.backup.entrypoints.api import _read_config
    try:
        return max(1, int(_read_config().get("drill_retention", 25)))
    except Exception:
        return 25


def _prune() -> None:
    from tools.graph import settings_ops
    try:
        members = sorted(
            settings_ops.read_set(DRILL_SET_ID, org="machine", peers=[]),
            key=lambda m: m.key, reverse=True)
    except Exception:
        return
    for member in members[_drill_retention():]:
        try:
            settings_ops.remove_setting(member.id, org="machine")
        except Exception as exc:
            logger.warning("drill prune failed for %s: %s", member.key, exc)


def run_drill(trigger: str = "manual", *, timeout_s: float | None = None,
              script: Path | None = None, env: dict | None = None) -> dict:
    """Run one drill to completion and return its recorded row payload.

    Raises DrillAlreadyRunning instead of queueing — a second concurrent
    drill would fight the first for the restic repo and scratch space.
    """
    global _running
    from tools.dashboard.plugins.backup.entrypoints.api import _read_config
    config = _read_config()
    if timeout_s is None:
        timeout_s = float(config.get("drill_timeout_minutes", 30)) * 60
    started = datetime.now(timezone.utc)
    with _lock:
        if _running is not None:
            raise DrillAlreadyRunning(_running["key"])
        stamp = started.strftime("%Y%m%d-%H%M%S")
        _running = {"key": stamp, "trigger": trigger,
                    "started_at": started.isoformat()}
    try:
        run_env = {**os.environ, **(env or {})}
        if env is None:
            # Vault-released offsite credentials (auto-uy896): injected
            # while the vault is warm so the drill's restic reaches the
            # repo without agents/backup.env. When unavailable the drill
            # still runs — the script's own credential resolution states
            # what is missing, which is the honest evidence.
            try:
                from tools.dashboard.plugins.backup import credentials
                vault_env, status = credentials.offsite_env()
                if vault_env:
                    run_env.update(vault_env)
                else:
                    logger.info("drill running without vault credentials "
                                "(%s)", status)
            except Exception:
                logger.exception("vault credential injection failed")
        # Popen + killpg, not subprocess.run: a timeout must kill the
        # whole process GROUP — the drill's restic/python grandchildren
        # would survive a kill aimed at bash alone and keep holding the
        # restic repo lock and the scratch space.
        proc = subprocess.Popen(
            ["bash", str(script or DRILL_SCRIPT), "drill"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            env=run_env, start_new_session=True,
        )
        try:
            output = proc.communicate(timeout=timeout_s)[0] or ""
            checks, snapshot_id, script_verdict = parse_events(output)
            verdict = ("pass" if proc.returncode == 0
                       and script_verdict == "pass" else "fail")
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, 9)
            except (ProcessLookupError, PermissionError):
                pass
            output = proc.communicate()[0] or ""
            checks, snapshot_id, _ = parse_events(output)
            checks.append({"name": "runtime", "status": "fail",
                           "detail": f"killed after {timeout_s:.0f}s"})
            verdict = "timeout"
        finished = datetime.now(timezone.utc)
        payload = {
            "verdict": verdict,
            "trigger": trigger,
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "duration_seconds": (finished - started).total_seconds(),
            "snapshot_id": snapshot_id,
            "checks": checks,
            "evidence": output[-EVIDENCE_LIMIT:],
        }
        _upsert(stamp, payload)
        _prune()
        return {"stamp": stamp, **payload}
    finally:
        with _lock:
            _running = None
