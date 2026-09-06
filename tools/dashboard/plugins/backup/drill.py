"""Restore drills: run, record, alert (auto-mu7qf).

The checks themselves live in tools/graph/backup-restore.sh (drill
subcommand) — restic restore to scratch, marker requirement, integrity
through the app's SQL-function registration, sources sanity, beads
count. This module runs that script as a subprocess, parses its
``@``-event lines into a BackupDrillV1 row, and feeds the outcome to
Central attention (restore_drill_failed raised on fail/timeout,
resolved by the next pass).

Exactly one drill runs at a time: the row is upserted ``running`` when
the subprocess starts, so an in-flight drill is visible on /backup and
diagnosable if the process dies. Timeouts kill the subprocess group and
record ``timeout`` — a drill that cannot finish is a failed drill, not
a silent absence.
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

_REPO = Path(__file__).resolve().parents[3]
DRILL_SCRIPT = _REPO / "tools" / "graph" / "backup-restore.sh"

#: Bounded tail of drill output stored as row evidence.
EVIDENCE_LIMIT = 4000

_lock = threading.Lock()
_running_stamp: str | None = None


class DrillAlreadyRunning(RuntimeError):
    def __init__(self, stamp: str):
        self.stamp = stamp
        super().__init__(f"drill {stamp} is already running")


def running_stamp() -> str | None:
    return _running_stamp


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
    settings_ops.add_setting(
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


def _publish_outcome(verdict: str, stamp: str, checks: list[dict]) -> None:
    """restore_drill_failed raised on fail/timeout, resolved on pass."""
    from tools.dashboard import attention_routes
    from tools.dashboard.plugins.backup.deriver import publish_conditions
    failed_names = [c["name"] for c in checks if c.get("status") == "fail"]
    if verdict == "pass":
        state, title = "resolved", "Restore drill passed"
        summary = f"Drill {stamp}: {len(checks)} checks ok"
    else:
        state, title = "needs_attention", "Restore drill failed"
        summary = (f"Drill {stamp}: "
                   + (f"failed checks: {', '.join(failed_names)}"
                      if failed_names else f"verdict {verdict}"))
    try:
        publish_conditions(attention_routes._runtime.index, [{
            "kind": "backup.drill_failed",
            "attention_id": "backup:drill",
            "object_ref": "backup:drill",
            "attention_state": state,
            "safe_title": title,
            "safe_summary": summary,
            "occurred_at": datetime.now(timezone.utc).timestamp(),
            "source_version": int("".join(ch for ch in stamp if ch.isdigit())),
        }])
    except Exception:
        logger.exception("drill attention publish failed")


def run_drill(trigger: str = "manual", *, timeout_s: float | None = None,
              script: Path | None = None, env: dict | None = None) -> dict:
    """Run one drill to completion and return its recorded row payload.

    Raises DrillAlreadyRunning instead of queueing — a second concurrent
    drill would fight the first for the restic repo and scratch space.
    """
    global _running_stamp
    from tools.dashboard.plugins.backup.entrypoints.api import _read_config
    config = _read_config()
    if timeout_s is None:
        timeout_s = float(config.get("drill_timeout_minutes", 30)) * 60
    with _lock:
        if _running_stamp is not None:
            raise DrillAlreadyRunning(_running_stamp)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        _running_stamp = stamp
    started = datetime.now(timezone.utc)
    try:
        _upsert(stamp, {
            "verdict": "running",
            "trigger": trigger,
            "started_at": started.isoformat(),
        })
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
        _publish_outcome(verdict, stamp, checks)
        return {"stamp": stamp, **payload}
    finally:
        with _lock:
            _running_stamp = None
