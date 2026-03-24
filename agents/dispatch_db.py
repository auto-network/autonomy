"""SQLite storage for dispatch run metadata.

Stores structured metadata for every agent dispatch in data/dispatch.db.
The dispatcher writes a RUNNING row at launch via insert_launch_run(),
then updates it on completion via insert_run() (upsert).
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("DISPATCH_DB", str(REPO_ROOT / "data" / "dispatch.db")))

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
  output_dir TEXT,
  last_snippet TEXT,
  token_count INTEGER,
  tool_count INTEGER,
  turn_count INTEGER,
  cpu_pct REAL,
  cpu_usec INTEGER,
  mem_mb INTEGER,
  last_activity DATETIME,
  jsonl_offset INTEGER DEFAULT 0,
  librarian_type TEXT,
  failure_class TEXT
)
"""

# Migrations for columns added after initial schema deployment.
# Each entry is (column_name, ALTER TABLE statement).
# init_db() runs these and ignores "duplicate column name" errors.
_MIGRATIONS = [
    "ALTER TABLE dispatch_runs ADD COLUMN last_snippet TEXT",
    "ALTER TABLE dispatch_runs ADD COLUMN token_count INTEGER",
    "ALTER TABLE dispatch_runs ADD COLUMN cpu_pct REAL",
    "ALTER TABLE dispatch_runs ADD COLUMN cpu_usec INTEGER",
    "ALTER TABLE dispatch_runs ADD COLUMN mem_mb INTEGER",
    "ALTER TABLE dispatch_runs ADD COLUMN last_activity DATETIME",
    "ALTER TABLE dispatch_runs ADD COLUMN jsonl_offset INTEGER DEFAULT 0",
    "ALTER TABLE dispatch_runs ADD COLUMN tool_count INTEGER",
    "ALTER TABLE dispatch_runs ADD COLUMN turn_count INTEGER",
    "ALTER TABLE dispatch_runs ADD COLUMN librarian_type TEXT",
    "ALTER TABLE dispatch_runs ADD COLUMN failure_class TEXT",
]

CREATE_INDEX = """\
CREATE INDEX IF NOT EXISTS idx_dispatch_runs_completed ON dispatch_runs(completed_at DESC)
"""

CREATE_LIBRARIAN_JOBS = """\
CREATE TABLE IF NOT EXISTS librarian_jobs (
    id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    payload TEXT,
    status TEXT DEFAULT 'pending',
    priority INTEGER DEFAULT 1,
    created_at DATETIME DEFAULT (datetime('now')),
    started_at DATETIME,
    completed_at DATETIME,
    librarian_type TEXT,
    session_id TEXT,
    attempts INTEGER DEFAULT 0,
    max_attempts INTEGER DEFAULT 3
)
"""

CREATE_LIBRARIAN_JOBS_INDEX = """\
CREATE INDEX IF NOT EXISTS idx_librarian_jobs_status ON librarian_jobs(status, priority DESC, created_at ASC)
"""

CREATE_DISPATCHER_STATE = """\
CREATE TABLE IF NOT EXISTS dispatcher_state (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""


def _get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    """Create the dispatch_runs table and indexes if they don't exist.

    Also runs column migrations for databases created before new columns
    were added. ALTER TABLE errors from duplicate columns are silently ignored.
    """
    conn = _get_conn()
    try:
        conn.execute(CREATE_TABLE)
        conn.execute(CREATE_INDEX)
        conn.execute(CREATE_LIBRARIAN_JOBS)
        conn.execute(CREATE_LIBRARIAN_JOBS_INDEX)
        conn.execute(CREATE_DISPATCHER_STATE)
        for stmt in _MIGRATIONS:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # Column already exists
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


def is_bead_claimed(bead_id: str) -> bool:
    """Check if a bead already has a RUNNING row in dispatch_runs.

    Used as a pre-launch guard to prevent double-dispatch.
    """
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM dispatch_runs WHERE bead_id = ? AND status = 'RUNNING' LIMIT 1",
            (bead_id,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def insert_launch_run(
    *,
    run_id: str,
    bead_id: str,
    started_at: float,
    branch: str,
    branch_base: str,
    image: str,
    container_name: str,
    output_dir: str,
    librarian_type: str | None = None,
) -> None:
    """Insert a RUNNING row at agent launch time.

    Only the fields known at launch are populated. Completion fields
    (decision, commit_hash, exit_code, completed_at, etc.) are left NULL
    and filled in by insert_run() when the agent finishes.
    """
    started_dt = datetime.fromtimestamp(started_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if started_at else None

    conn = _get_conn()
    try:
        conn.execute(
            """\
            INSERT OR IGNORE INTO dispatch_runs (
                id, bead_id, started_at, status,
                branch, branch_base, image, container_name, output_dir, librarian_type
            ) VALUES (?, ?, ?, 'RUNNING', ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id, bead_id, started_dt,
                branch or None, branch_base or None,
                image or None, container_name or None, output_dir or None,
                librarian_type,
            ),
        )
        conn.commit()
    finally:
        conn.close()


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
    librarian_type: str | None = None,
    failure_class: str | None = None,
) -> None:
    """Upsert a dispatch run row on completion.

    If a RUNNING row was inserted at launch, this updates it with completion
    data. If no prior row exists (e.g. backfill), it inserts a new one.
    All LLM-produced fields extracted from decision.
    """
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
                discovered_beads_count, has_experience_report, output_dir, librarian_type,
                failure_class
            ) VALUES (
                ?, ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?
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
                discovered_beads_count, has_experience_report, output_dir or None, librarian_type,
                failure_class,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a sqlite3.Row to a plain dict."""
    return {k: row[k] for k in row.keys()}


def list_runs(limit: int = 200, offset: int = 0, *, completed_only: bool = False) -> list[dict]:
    """List dispatch runs ordered by most recent first.

    When completed_only=True, excludes RUNNING rows (for timeline/stats).
    When completed_only=False (default), returns all rows including RUNNING.
    Orders by started_at DESC so RUNNING rows (NULL completed_at) sort first.
    """
    conn = _get_conn()
    conn.row_factory = sqlite3.Row
    try:
        if completed_only:
            rows = conn.execute(
                "SELECT * FROM dispatch_runs WHERE status != 'RUNNING' "
                "ORDER BY completed_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM dispatch_runs "
                "ORDER BY COALESCE(completed_at, started_at) DESC LIMIT ? OFFSET ?",
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
    """Get dispatch runs for a bead, most recent first.

    RUNNING rows (NULL completed_at) sort before completed rows so the
    active run appears first.
    """
    conn = _get_conn()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM dispatch_runs WHERE bead_id = ? "
            "ORDER BY COALESCE(completed_at, started_at) DESC",
            (bead_id,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def get_currently_running() -> list[dict]:
    """Get runs that are currently in progress (status=RUNNING)."""
    conn = _get_conn()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM dispatch_runs WHERE status = 'RUNNING'"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def get_consecutive_failures(bead_id: str) -> tuple[int, int]:
    """Count consecutive failures for a bead, stopping at first DONE.

    Scans dispatch_runs in reverse chronological order (most recent first).
    Counts agent failures (FAILED/BLOCKED) and merge failures (MERGE_FAILED)
    separately. Both counters reset at the first DONE.
    RUNNING rows are skipped (in-flight agents don't affect the count).

    Returns (agent_failures, merge_failures).
    """
    conn = _get_conn()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT status FROM dispatch_runs "
            "WHERE bead_id = ? ORDER BY started_at DESC",
            (bead_id,),
        ).fetchall()

        agent_failures = 0
        merge_failures = 0
        for row in rows:
            status = row["status"]
            if status == "DONE":
                break
            elif status == "MERGE_FAILED":
                merge_failures += 1
            elif status in ("FAILED", "BLOCKED"):
                agent_failures += 1
            # RUNNING rows are skipped
        return agent_failures, merge_failures
    finally:
        conn.close()


def reset_circuit_breaker(bead_id: str) -> str:
    """Insert a synthetic DONE record to reset the circuit breaker for a bead.

    The circuit breaker in dispatcher.py checks get_consecutive_failures()
    which scans dispatch_runs by started_at DESC and stops at the first DONE.
    Inserting a synthetic DONE record breaks the failure streak.

    Returns the run_id of the inserted record.
    """
    import uuid

    now = datetime.now(timezone.utc)
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    run_id = f"reset-{bead_id}-{uuid.uuid4().hex[:8]}"

    conn = _get_conn()
    try:
        conn.execute(
            """\
            INSERT INTO dispatch_runs (
                id, bead_id, started_at, completed_at, duration_secs,
                status, reason
            ) VALUES (?, ?, ?, ?, 0, 'DONE', 'Circuit breaker reset (synthetic)')
            """,
            (run_id, bead_id, now_str, now_str),
        )
        conn.commit()
        return run_id
    finally:
        conn.close()


def update_live_stats(
    *,
    run_id: str,
    last_snippet: str | None = None,
    context_tokens: int = 0,
    tool_delta: int = 0,
    turn_delta: int = 0,
    cpu_pct: float | None = None,
    cpu_usec: int | None = None,
    mem_mb: int | None = None,
    last_activity: str | None = None,
    jsonl_offset: int = 0,
) -> None:
    """Update live monitoring stats on a RUNNING dispatch_runs row.

    Called by the dispatcher's poll cycle for each running agent.
    Best-effort — silently ignores all errors so a stats failure never
    disrupts the dispatch loop.

    context_tokens replaces token_count with the latest context window size.
    tool_delta and turn_delta are added to cumulative counts.
    All other fields replace the previous value when provided.
    Fields left at their default (None / 0) are not touched.
    """
    updates: list[str] = []
    params: list = []

    if last_snippet is not None:
        updates.append("last_snippet = ?")
        params.append(last_snippet)

    if context_tokens > 0:
        updates.append("token_count = ?")
        params.append(context_tokens)

    if tool_delta > 0:
        updates.append("tool_count = COALESCE(tool_count, 0) + ?")
        params.append(tool_delta)

    if turn_delta > 0:
        updates.append("turn_count = COALESCE(turn_count, 0) + ?")
        params.append(turn_delta)

    if cpu_pct is not None:
        updates.append("cpu_pct = ?")
        params.append(cpu_pct)

    if cpu_usec is not None:
        updates.append("cpu_usec = ?")
        params.append(cpu_usec)

    if mem_mb is not None:
        updates.append("mem_mb = ?")
        params.append(mem_mb)

    if last_activity is not None:
        updates.append("last_activity = ?")
        params.append(last_activity)

    if jsonl_offset > 0:
        updates.append("jsonl_offset = ?")
        params.append(jsonl_offset)

    if not updates:
        return

    params.append(run_id)
    try:
        conn = _get_conn()
        try:
            conn.execute(
                f"UPDATE dispatch_runs SET {', '.join(updates)} WHERE id = ?",
                params,
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


# ── Dispatcher pause/resume ──────────────────────────────────────


def set_dispatcher_paused(reason_json: dict) -> None:
    """Write pause state to dispatcher_state table.

    reason_json should contain: reason, paused_at, bead_id, message.
    """
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO dispatcher_state (key, value, updated_at) "
            "VALUES ('paused', ?, datetime('now'))",
            (json.dumps(reason_json),),
        )
        conn.commit()
    finally:
        conn.close()


def clear_paused() -> None:
    """Remove the paused key from dispatcher_state."""
    conn = _get_conn()
    try:
        conn.execute("DELETE FROM dispatcher_state WHERE key = 'paused'")
        conn.commit()
    finally:
        conn.close()


def is_paused() -> bool:
    """Return True if the dispatcher is paused."""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM dispatcher_state WHERE key = 'paused' LIMIT 1"
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def get_pause_reason() -> dict | None:
    """Return the pause reason dict, or None if not paused."""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT value FROM dispatcher_state WHERE key = 'paused' LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return json.loads(row[0])
    except (json.JSONDecodeError, IndexError):
        return None
    finally:
        conn.close()
