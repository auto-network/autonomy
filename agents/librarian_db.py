"""Job queue for librarian agents in dispatch.db.

Librarian agents refine raw ideas into specified beads automatically.
This module provides enqueue/dequeue/completion functions for the
librarian_jobs table created by dispatch_db.init_db().
"""

from __future__ import annotations

import sqlite3
import uuid

from agents.dispatch_db import DB_PATH, _get_conn


def enqueue(job_type: str, payload: str | None = None, priority: int = 1) -> str:
    """Insert a pending librarian job and return its job_id.

    Args:
        job_type: Category of work (e.g. 'refine', 'specify').
        payload: JSON-serialised job data (caller's responsibility to serialise).
        priority: Higher numbers = higher priority. Default 1.

    Returns:
        The new job's UUID string.
    """
    job_id = str(uuid.uuid4())
    conn = _get_conn()
    try:
        conn.execute(
            """\
            INSERT INTO librarian_jobs (id, job_type, payload, status, priority)
            VALUES (?, ?, ?, 'pending', ?)
            """,
            (job_id, job_type, payload, priority),
        )
        conn.commit()
    finally:
        conn.close()
    return job_id


def dequeue(max_concurrent: int) -> dict | None:
    """Claim the next pending job if the running count for its job_type allows it.

    Finds the oldest pending job (highest priority first, then earliest
    created_at) where the number of currently-running jobs of that same
    job_type is below *max_concurrent*.  Atomically sets its status to
    'running' and records started_at.

    Args:
        max_concurrent: Maximum number of running jobs per job_type.

    Returns:
        The updated job row as a dict, or None if nothing is available.
    """
    conn = _get_conn()
    conn.row_factory = sqlite3.Row
    try:
        # Use immediate transaction so no other writer can sneak in between
        # the SELECT and the UPDATE on this WAL-mode database.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """\
            SELECT * FROM librarian_jobs
            WHERE status = 'pending'
              AND (
                SELECT COUNT(*) FROM librarian_jobs j2
                WHERE j2.job_type = librarian_jobs.job_type
                  AND j2.status = 'running'
              ) < ?
            ORDER BY priority DESC, created_at ASC
            LIMIT 1
            """,
            (max_concurrent,),
        ).fetchone()

        if row is None:
            conn.execute("ROLLBACK")
            return None

        conn.execute(
            """\
            UPDATE librarian_jobs
            SET status = 'running', started_at = datetime('now')
            WHERE id = ?
            """,
            (row["id"],),
        )
        conn.execute("COMMIT")

        # Re-fetch to return the updated row
        updated = conn.execute(
            "SELECT * FROM librarian_jobs WHERE id = ?", (row["id"],)
        ).fetchone()
        return {k: updated[k] for k in updated.keys()}
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def complete_job(job_id: str, status: str = "done", session_id: str | None = None) -> None:
    """Mark a job as completed (or any terminal status).

    Sets completed_at to now and optionally records the session_id of
    the librarian agent that ran the job.

    Args:
        job_id: The job to complete.
        status: Terminal status string, default 'done'.
        session_id: Optional agent session identifier.
    """
    conn = _get_conn()
    try:
        conn.execute(
            """\
            UPDATE librarian_jobs
            SET status = ?, completed_at = datetime('now'), session_id = COALESCE(?, session_id)
            WHERE id = ?
            """,
            (status, session_id, job_id),
        )
        conn.commit()
    finally:
        conn.close()


def fail_job(job_id: str) -> None:
    """Increment attempts and requeue or fail the job.

    If attempts < max_attempts: reset to 'pending' so it will be retried.
    If attempts >= max_attempts: set status to 'failed'.
    """
    conn = _get_conn()
    try:
        conn.execute(
            """\
            UPDATE librarian_jobs
            SET attempts = attempts + 1,
                status = CASE
                    WHEN attempts + 1 < max_attempts THEN 'pending'
                    ELSE 'failed'
                END,
                started_at = CASE
                    WHEN attempts + 1 < max_attempts THEN NULL
                    ELSE started_at
                END
            WHERE id = ?
            """,
            (job_id,),
        )
        conn.commit()
    finally:
        conn.close()


def list_jobs(
    status: str | None = None,
    job_type: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Return jobs filtered by status and/or job_type.

    Results are ordered by priority DESC, created_at ASC so the most
    urgent pending jobs appear first.

    Args:
        status: Filter to this status (e.g. 'pending', 'running', 'done').
        job_type: Filter to this job_type.
        limit: Maximum rows to return (default 50).

    Returns:
        List of job dicts.
    """
    clauses: list[str] = []
    params: list = []

    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if job_type is not None:
        clauses.append("job_type = ?")
        params.append(job_type)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)

    conn = _get_conn()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"SELECT * FROM librarian_jobs {where} "
            "ORDER BY priority DESC, created_at ASC LIMIT ?",
            params,
        ).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]
    finally:
        conn.close()


def get_running_librarians() -> list[dict]:
    """Return all currently-running librarian jobs.

    Used by the dispatcher poll loop to monitor active librarian agents.
    """
    return list_jobs(status="running", limit=1000)
