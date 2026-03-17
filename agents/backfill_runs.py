"""Backfill dispatch_runs table from existing agent-runs directories.

One-time migration that scans data/agent-runs/*, reads each run's dotfiles
and decision.json, and inserts rows into the dispatch_runs SQLite table.

Idempotent — uses INSERT OR IGNORE so it's safe to run multiple times.

Usage:
    python -m agents.backfill_runs
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from agents.dispatch_db import (
    DB_PATH,
    REPO_ROOT,
    _get_conn,
    _git_commit_message,
    _git_diff_stats,
    init_db,
)

AGENT_RUNS_DIR = REPO_ROOT / "data" / "agent-runs"


def _read_dotfile(run_dir: Path, name: str) -> str:
    """Read a dotfile from a run directory, returning empty string if missing."""
    p = run_dir / name
    if p.exists():
        return p.read_text().strip()
    return ""


def _parse_dir_name(name: str) -> tuple[str, str]:
    """Parse bead_id and timestamp from directory name.

    Directory names follow the pattern: <bead-id>-YYYYMMDD-HHMMSS
    Bead IDs can contain hyphens (e.g. auto-abc), so we split from the right.

    Returns (bead_id, timestamp_str) where timestamp_str is 'YYYYMMDD-HHMMSS'.
    """
    parts = name.rsplit("-", 2)
    if len(parts) < 3:
        return name, ""
    return parts[0], f"{parts[1]}-{parts[2]}"


def _timestamp_to_epoch(ts: str) -> float | None:
    """Convert YYYYMMDD-HHMMSS to epoch seconds, or None.

    Dir names use local time (launch.sh calls date +%Y%m%d-%H%M%S without TZ).
    """
    if not ts:
        return None
    try:
        dt = datetime.strptime(ts, "%Y%m%d-%H%M%S")
        # Local time — no tzinfo, .timestamp() uses local timezone
        return dt.timestamp()
    except ValueError:
        return None


def _git_commit_epoch(commit_hash: str) -> float | None:
    """Get commit timestamp as epoch seconds via git log."""
    if not commit_hash:
        return None
    try:
        import subprocess
        result = subprocess.run(
            ["git", "log", "--format=%ct", "-1", commit_hash],
            capture_output=True, text=True, timeout=5,
            cwd=str(REPO_ROOT),
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        pass
    return None


def _estimate_completed_at(run_dir: Path, commit_hash: str) -> float | None:
    """Best estimate of completion time: git commit timestamp, else dir mtime."""
    epoch = _git_commit_epoch(commit_hash)
    if epoch:
        return epoch
    try:
        return run_dir.stat().st_mtime
    except OSError:
        return None


def backfill_one(run_dir: Path, *, dry_run: bool = False) -> dict:
    """Process a single agent-run directory and return the row data.

    Returns a dict with the row fields. If dry_run is True, does not insert.
    """
    name = run_dir.name
    bead_id, timestamp = _parse_dir_name(name)
    run_id = name  # Use directory name as unique ID

    # Read dotfiles
    commit_hash = _read_dotfile(run_dir, ".commit_hash")
    branch = _read_dotfile(run_dir, ".branch")
    branch_base = _read_dotfile(run_dir, ".branch_base")

    # Read decision.json
    decision = None
    decision_path = run_dir / "decision.json"
    if decision_path.exists():
        try:
            decision = json.loads(decision_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    # Status from decision or UNKNOWN
    status = (decision or {}).get("status", "UNKNOWN")
    reason = (decision or {}).get("reason", "")

    # Scores and time breakdown (all optional)
    scores = (decision or {}).get("scores") or {}
    time_breakdown = (decision or {}).get("time_breakdown") or {}
    failure_category = (decision or {}).get("failure_category") if status in ("BLOCKED", "FAILED") else None
    discovered_beads = (decision or {}).get("discovered_beads") or []

    # Timestamps
    started_at = _timestamp_to_epoch(timestamp)
    completed_at = _estimate_completed_at(run_dir, commit_hash)
    duration_secs = int(completed_at - started_at) if started_at and completed_at and completed_at >= started_at else None

    # Git stats
    lines_added, lines_removed, files_changed = _git_diff_stats(branch_base, commit_hash)
    commit_message = _git_commit_message(commit_hash)

    # Experience report
    has_experience_report = (run_dir / "experience_report.md").exists()

    # Format timestamps
    started_dt = (
        datetime.fromtimestamp(started_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        if started_at else None
    )
    completed_dt = (
        datetime.fromtimestamp(completed_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        if completed_at else None
    )

    row = (
        run_id, bead_id, started_dt, completed_dt, duration_secs,
        status, reason, failure_category,
        commit_hash or None, commit_message, branch or None, branch_base or None,
        None, None, None,  # image, container_name, exit_code (not available from dirs)
        lines_added, lines_removed, files_changed,
        scores.get("tooling"), scores.get("clarity"), scores.get("confidence"),
        time_breakdown.get("research_pct"), time_breakdown.get("coding_pct"),
        time_breakdown.get("debugging_pct"), time_breakdown.get("tooling_workaround_pct"),
        len(discovered_beads), has_experience_report, str(run_dir),
    )

    return {
        "run_id": run_id,
        "bead_id": bead_id,
        "status": status,
        "row": row,
        "dry_run": dry_run,
    }


def backfill(*, dry_run: bool = False) -> list[dict]:
    """Scan all agent-runs directories and insert into dispatch_runs.

    Returns list of processed run summaries.
    """
    if not AGENT_RUNS_DIR.exists():
        print(f"No agent-runs directory at {AGENT_RUNS_DIR}")
        return []

    init_db()

    results = []
    dirs = sorted(AGENT_RUNS_DIR.iterdir())
    run_dirs = [d for d in dirs if d.is_dir()]

    if not run_dirs:
        print("No run directories found.")
        return []

    print(f"Found {len(run_dirs)} run directories to process.")

    conn = _get_conn()
    try:
        inserted = 0
        skipped = 0
        for run_dir in run_dirs:
            info = backfill_one(run_dir, dry_run=dry_run)
            results.append(info)

            if dry_run:
                print(f"  [DRY-RUN] {info['run_id']} -> {info['status']}")
                continue

            try:
                conn.execute(
                    """\
                    INSERT OR IGNORE INTO dispatch_runs (
                        id, bead_id, started_at, completed_at, duration_secs,
                        status, reason, failure_category,
                        commit_hash, commit_message, branch, branch_base,
                        image, container_name, exit_code,
                        lines_added, lines_removed, files_changed,
                        score_tooling, score_clarity, score_confidence,
                        time_research_pct, time_coding_pct, time_debugging_pct, time_tooling_pct,
                        discovered_beads_count, has_experience_report, output_dir
                    ) VALUES (
                        ?, ?, ?, ?, ?,
                        ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?,
                        ?, ?, ?,
                        ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?
                    )
                    """,
                    info["row"],
                )
                if conn.total_changes:
                    inserted += 1
                    print(f"  [INSERT] {info['run_id']} -> {info['status']}")
                else:
                    skipped += 1
                    print(f"  [SKIP]   {info['run_id']} (already exists)")
            except Exception as e:
                print(f"  [ERROR]  {info['run_id']}: {e}")

        if not dry_run:
            conn.commit()
            print(f"\nDone: {inserted} inserted, {skipped} skipped out of {len(run_dirs)} total.")
    finally:
        conn.close()

    return results


def main():
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        print("Dry-run mode — no database writes.\n")
    backfill(dry_run=dry_run)


if __name__ == "__main__":
    main()
