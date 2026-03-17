"""SQLite storage for dispatch run metadata.

Stores structured metadata for every agent dispatch in data/dispatch.db.
The dispatcher writes a row on completion via insert_run().
"""

from __future__ import annotations

import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "data" / "dispatch.db"

CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS dispatch_runs (
  id TEXT PRIMARY KEY,
  bead_id TEXT,
  started_at DATETIME,
  completed_at DATETIME,
  duration_secs INTEGER,
  status TEXT,
  reason TEXT,
  failure_category TEXT,
  commit_hash TEXT,
  commit_message TEXT,
  branch TEXT,
  branch_base TEXT,
  image TEXT,
  container_name TEXT,
  exit_code INTEGER,
  lines_added INTEGER,
  lines_removed INTEGER,
  files_changed INTEGER,
  score_tooling INTEGER,
  score_clarity INTEGER,
  score_confidence INTEGER,
  time_research_pct INTEGER,
  time_coding_pct INTEGER,
  time_debugging_pct INTEGER,
  time_tooling_pct INTEGER,
  discovered_beads_count INTEGER,
  has_experience_report BOOLEAN,
  output_dir TEXT
)
"""


def _get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    """Create the dispatch_runs table if it doesn't exist."""
    conn = _get_conn()
    try:
        conn.execute(CREATE_TABLE)
        conn.commit()
    finally:
        conn.close()


def _git_diff_stats(branch_base: str, commit_hash: str) -> tuple[int | None, int | None, int | None]:
    """Compute lines_added, lines_removed, files_changed from git diff --stat.

    Returns (None, None, None) if either ref is missing or the command fails.
    """
    if not branch_base or not commit_hash:
        return None, None, None
    try:
        result = subprocess.run(
            ["git", "diff", "--numstat", f"{branch_base}..{commit_hash}"],
            capture_output=True, text=True, timeout=10,
            cwd=str(REPO_ROOT),
        )
        if result.returncode != 0:
            return None, None, None

        added = 0
        removed = 0
        files = 0
        for line in result.stdout.strip().splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                files += 1
                # Binary files show '-' for added/removed
                if parts[0] != "-":
                    added += int(parts[0])
                if parts[1] != "-":
                    removed += int(parts[1])
        return added, removed, files
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        return None, None, None


def _git_commit_message(commit_hash: str) -> str | None:
    """Get the commit subject line."""
    if not commit_hash:
        return None
    try:
        result = subprocess.run(
            ["git", "log", "--format=%s", "-1", commit_hash],
            capture_output=True, text=True, timeout=5,
            cwd=str(REPO_ROOT),
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def insert_run(
    *,
    run_id: str,
    bead_id: str,
    started_at: float,
    completed_at: float,
    status: str,
    reason: str,
    decision: dict | None,
    commit_hash: str,
    branch: str,
    branch_base: str,
    image: str,
    container_name: str,
    exit_code: int,
    output_dir: str,
) -> None:
    """Insert a dispatch run row. All LLM-produced fields extracted from decision."""
    duration_secs = int(completed_at - started_at) if started_at and completed_at else None

    # Derive git stats
    lines_added, lines_removed, files_changed = _git_diff_stats(branch_base, commit_hash)
    commit_message = _git_commit_message(commit_hash)

    # Extract decision fields (all optional)
    scores = (decision or {}).get("scores") or {}
    time_breakdown = (decision or {}).get("time_breakdown") or {}
    failure_category = (decision or {}).get("failure_category") if status in ("BLOCKED", "FAILED") else None
    discovered_beads = (decision or {}).get("discovered_beads") or []
    discovered_beads_count = len(discovered_beads)

    # Check for experience report
    has_experience_report = (Path(output_dir) / "experience_report.md").exists() if output_dir else False

    # Convert timestamps
    started_dt = datetime.fromtimestamp(started_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if started_at else None
    completed_dt = datetime.fromtimestamp(completed_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if completed_at else None

    conn = _get_conn()
    try:
        conn.execute(
            """\
            INSERT OR REPLACE INTO dispatch_runs (
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
            (
                run_id, bead_id, started_dt, completed_dt, duration_secs,
                status, reason, failure_category,
                commit_hash or None, commit_message, branch or None, branch_base or None,
                image or None, container_name or None, exit_code,
                lines_added, lines_removed, files_changed,
                scores.get("tooling"), scores.get("clarity"), scores.get("confidence"),
                time_breakdown.get("research_pct"), time_breakdown.get("coding_pct"),
                time_breakdown.get("debugging_pct"), time_breakdown.get("tooling_workaround_pct"),
                discovered_beads_count, has_experience_report, output_dir or None,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a sqlite3.Row to a plain dict."""
    return {k: row[k] for k in row.keys()}


def list_runs(limit: int = 200, offset: int = 0) -> list[dict]:
    """List dispatch runs ordered by completed_at DESC.

    Returns dicts with all columns from dispatch_runs.
    """
    conn = _get_conn()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM dispatch_runs ORDER BY completed_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def get_run(run_id: str) -> dict | None:
    """Get a single dispatch run by its id (directory name)."""
    conn = _get_conn()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM dispatch_runs WHERE id = ?", (run_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def get_runs_for_bead(bead_id: str) -> list[dict]:
    """Get dispatch runs for a bead, most recent first."""
    conn = _get_conn()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM dispatch_runs WHERE bead_id = ? ORDER BY completed_at DESC",
            (bead_id,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def get_currently_running() -> list[dict]:
    """Get runs that are currently in progress (started but not completed)."""
    conn = _get_conn()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM dispatch_runs WHERE status IS NULL AND started_at IS NOT NULL"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()
