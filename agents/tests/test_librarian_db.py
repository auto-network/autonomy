"""Tests for librarian_db — job queue functions."""

from __future__ import annotations

import tempfile
from pathlib import Path

import agents.dispatch_db as dispatch_db
import agents.librarian_db as ldb


def _use_temp_db():
    """Point dispatch_db (and thus librarian_db) at a temp file."""
    tmp = tempfile.mktemp(suffix=".db")
    dispatch_db.DB_PATH = Path(tmp)
    ldb.DB_PATH = Path(tmp)
    dispatch_db.init_db()
    return tmp


# ---------------------------------------------------------------------------
# Table creation
# ---------------------------------------------------------------------------

def test_init_creates_librarian_jobs_table():
    import sqlite3
    tmp = _use_temp_db()
    conn = sqlite3.connect(tmp)
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    conn.close()
    assert "librarian_jobs" in tables


# ---------------------------------------------------------------------------
# enqueue
# ---------------------------------------------------------------------------

def test_enqueue_returns_job_id():
    _use_temp_db()
    job_id = ldb.enqueue("refine", '{"bead_id": "auto-abc"}')
    assert isinstance(job_id, str)
    assert len(job_id) == 36  # UUID4


def test_enqueue_job_appears_as_pending():
    _use_temp_db()
    job_id = ldb.enqueue("refine", '{"bead_id": "auto-x"}', priority=2)

    jobs = ldb.list_jobs(status="pending")
    assert len(jobs) == 1
    job = jobs[0]
    assert job["id"] == job_id
    assert job["status"] == "pending"
    assert job["job_type"] == "refine"
    assert job["priority"] == 2
    assert job["attempts"] == 0


def test_enqueue_default_priority():
    _use_temp_db()
    ldb.enqueue("specify")
    jobs = ldb.list_jobs()
    assert jobs[0]["priority"] == 1


# ---------------------------------------------------------------------------
# dequeue
# ---------------------------------------------------------------------------

def test_dequeue_returns_job_and_sets_running():
    _use_temp_db()
    job_id = ldb.enqueue("refine", priority=1)

    result = ldb.dequeue(max_concurrent=2)
    assert result is not None
    assert result["id"] == job_id
    assert result["status"] == "running"
    assert result["started_at"] is not None


def test_dequeue_returns_none_when_empty():
    _use_temp_db()
    assert ldb.dequeue(max_concurrent=2) is None


def test_dequeue_respects_max_concurrent():
    """dequeue returns None when running count for a job_type hits max_concurrent."""
    _use_temp_db()
    ldb.enqueue("refine")
    ldb.enqueue("refine")
    ldb.enqueue("refine")

    # First dequeue succeeds (0 running < 1 max)
    job1 = ldb.dequeue(max_concurrent=1)
    assert job1 is not None
    assert job1["status"] == "running"

    # Second dequeue blocked: 1 running == 1 max
    job2 = ldb.dequeue(max_concurrent=1)
    assert job2 is None


def test_dequeue_allows_concurrent_different_job_types():
    """Jobs of different types don't count against each other's max_concurrent."""
    _use_temp_db()
    ldb.enqueue("refine")
    ldb.enqueue("specify")

    job1 = ldb.dequeue(max_concurrent=1)
    assert job1 is not None

    job2 = ldb.dequeue(max_concurrent=1)
    assert job2 is not None
    assert job2["job_type"] != job1["job_type"]


def test_dequeue_priority_order():
    """Higher priority jobs are dequeued first."""
    _use_temp_db()
    low_id = ldb.enqueue("refine", priority=1)
    high_id = ldb.enqueue("refine", priority=5)

    first = ldb.dequeue(max_concurrent=2)
    assert first["id"] == high_id


def test_dequeue_fifo_same_priority():
    """Among equal priority, oldest job is dequeued first."""
    _use_temp_db()
    import time
    id1 = ldb.enqueue("refine", priority=1)
    time.sleep(0.01)  # ensure different created_at
    id2 = ldb.enqueue("refine", priority=1)

    first = ldb.dequeue(max_concurrent=2)
    assert first["id"] == id1


# ---------------------------------------------------------------------------
# complete_job
# ---------------------------------------------------------------------------

def test_complete_job_sets_done():
    _use_temp_db()
    job_id = ldb.enqueue("refine")
    ldb.dequeue(max_concurrent=2)

    ldb.complete_job(job_id)

    jobs = ldb.list_jobs()
    job = next(j for j in jobs if j["id"] == job_id)
    assert job["status"] == "done"
    assert job["completed_at"] is not None


def test_complete_job_custom_status():
    _use_temp_db()
    job_id = ldb.enqueue("refine")
    ldb.dequeue(max_concurrent=2)

    ldb.complete_job(job_id, status="skipped")

    jobs = ldb.list_jobs()
    job = next(j for j in jobs if j["id"] == job_id)
    assert job["status"] == "skipped"


def test_complete_job_records_session_id():
    _use_temp_db()
    job_id = ldb.enqueue("specify")
    ldb.dequeue(max_concurrent=2)

    ldb.complete_job(job_id, session_id="sess-abc123")

    jobs = ldb.list_jobs()
    job = next(j for j in jobs if j["id"] == job_id)
    assert job["session_id"] == "sess-abc123"


# ---------------------------------------------------------------------------
# fail_job
# ---------------------------------------------------------------------------

def test_fail_job_increments_attempts_and_requeues():
    _use_temp_db()
    job_id = ldb.enqueue("refine")
    ldb.dequeue(max_concurrent=2)

    ldb.fail_job(job_id)

    jobs = ldb.list_jobs(status="pending")
    job = next(j for j in jobs if j["id"] == job_id)
    assert job["attempts"] == 1
    assert job["status"] == "pending"


def test_fail_job_exceeds_max_attempts():
    """Job moves to failed after exhausting all attempts."""
    _use_temp_db()
    job_id = ldb.enqueue("refine")

    # Run through all attempts (default max_attempts=3)
    for _ in range(3):
        ldb.dequeue(max_concurrent=2)
        ldb.fail_job(job_id)

    jobs = ldb.list_jobs(status="failed")
    assert len(jobs) == 1
    job = jobs[0]
    assert job["id"] == job_id
    assert job["status"] == "failed"
    assert job["attempts"] == 3


def test_fail_job_last_attempt_is_not_requeued():
    """At max_attempts, fail_job does not reset status to pending."""
    _use_temp_db()
    job_id = ldb.enqueue("refine")

    # Exhaust attempts
    for _ in range(3):
        if ldb.dequeue(max_concurrent=2) is None:
            break  # already failed, can't dequeue
        ldb.fail_job(job_id)

    pending = ldb.list_jobs(status="pending")
    assert not any(j["id"] == job_id for j in pending)


# ---------------------------------------------------------------------------
# list_jobs
# ---------------------------------------------------------------------------

def test_list_jobs_no_filter():
    _use_temp_db()
    ldb.enqueue("refine")
    ldb.enqueue("specify")
    jobs = ldb.list_jobs()
    assert len(jobs) == 2


def test_list_jobs_filter_status():
    _use_temp_db()
    ldb.enqueue("refine")
    ldb.enqueue("specify")
    ldb.dequeue(max_concurrent=2)

    running = ldb.list_jobs(status="running")
    assert len(running) == 1


def test_list_jobs_filter_job_type():
    _use_temp_db()
    ldb.enqueue("refine")
    ldb.enqueue("refine")
    ldb.enqueue("specify")

    refine_jobs = ldb.list_jobs(job_type="refine")
    assert len(refine_jobs) == 2
    assert all(j["job_type"] == "refine" for j in refine_jobs)


def test_list_jobs_limit():
    _use_temp_db()
    for _ in range(5):
        ldb.enqueue("refine")

    jobs = ldb.list_jobs(limit=3)
    assert len(jobs) == 3


# ---------------------------------------------------------------------------
# get_running_librarians
# ---------------------------------------------------------------------------

def test_get_running_librarians_empty():
    _use_temp_db()
    assert ldb.get_running_librarians() == []


def test_get_running_librarians_returns_running_jobs():
    _use_temp_db()
    ldb.enqueue("refine")
    ldb.enqueue("specify")
    ldb.dequeue(max_concurrent=2)
    ldb.dequeue(max_concurrent=2)

    running = ldb.get_running_librarians()
    assert len(running) == 2
    assert all(j["status"] == "running" for j in running)


def test_get_running_librarians_excludes_completed():
    _use_temp_db()
    job_id = ldb.enqueue("refine")
    ldb.dequeue(max_concurrent=2)
    ldb.complete_job(job_id)

    assert ldb.get_running_librarians() == []
