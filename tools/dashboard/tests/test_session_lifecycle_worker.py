import json
import threading
import time

import pytest

from tools.dashboard.dao import dashboard_db
from tools.dashboard.session_lifecycle_worker import (
    LifecycleJob,
    LifecycleQueueFull,
    SessionLifecycleStateWriter,
    SessionLifecycleWorker,
)


@pytest.fixture(autouse=True)
def _close_dashboard_db_after_test():
    yield
    if dashboard_db._conn is not None:
        dashboard_db._conn.close()
    dashboard_db._conn = None


def _init_db(tmp_path):
    db_path = tmp_path / "dashboard.db"
    if dashboard_db._conn is not None:
        dashboard_db._conn.close()
    dashboard_db._conn = None
    dashboard_db.init_db(db_path)
    return db_path


def _insert_session(tmux_name="auto-life", state="LAUNCHING"):
    # Worker-step tests seed rows the way production does: launch
    # entrypoints birth rows LAUNCHING (register_pending) before any
    # worker step runs.
    dashboard_db.insert_session(
        tmux_name=tmux_name,
        session_type="container",
        project="autonomy",
        state=state,
    )


def _row(tmux_name="auto-life"):
    row = dashboard_db.get_session(tmux_name)
    assert row is not None
    return row


def test_state_writer_maps_lifecycle_to_existing_columns(tmp_path):
    _init_db(tmp_path)
    _insert_session()

    seen = []
    writer = SessionLifecycleStateWriter(on_transition=seen.append)

    writer.set_state("auto-life", "requested")
    assert _row()["startup_state"] == "requesting"
    assert _row()["activity_state"] == "running"
    assert _row()["is_live"] == 1
    assert _row()["lifecycle_detail"] is None

    writer.set_state("auto-life", "running")
    assert _row()["startup_state"] is None
    assert _row()["activity_state"] == "running"
    assert _row()["is_live"] == 1
    assert _row()["lifecycle_detail"] is None

    writer.fail(
        "auto-life",
        phase="injecting",
        reason="composer timeout",
        retryable=False,
        attempt=3,
    )
    assert _row()["startup_state"] == "setup_failed"
    assert _row()["activity_state"] == "failed"
    assert _row()["is_live"] == 0
    detail = json.loads(_row()["lifecycle_detail"])
    assert detail["failed_phase"] == "injecting"
    assert detail["reason"] == "composer timeout"
    assert detail["retryable"] is False
    assert detail["attempt"] == 3
    assert isinstance(detail["last_progress_at"], float)
    assert seen[-1].state == "FAILED"
    assert seen[-1].reason == "composer timeout"
    assert _row()["state"] == "FAILED"
    assert _row()["ended_at"] is not None

    # FAILED is its own terminal: it never becomes ENDED (kept operator-
    # visible until retried) — the authority refuses the move.
    writer.set_state("auto-life", "dead")
    assert _row()["state"] == "FAILED"
    assert _row()["activity_state"] == "failed"
    assert _row()["is_live"] == 0


def test_worker_runs_jobs_serially_on_background_thread(tmp_path):
    _init_db(tmp_path)
    _insert_session("auto-one")
    _insert_session("auto-two")

    done = threading.Event()
    calls = []

    def handle_start(job, writer):
        calls.append((job.tmux_name, threading.current_thread().name))
        writer.set_state(job.tmux_name, "running")
        if len(calls) == 2:
            done.set()

    worker = SessionLifecycleWorker(
        handlers={"start": handle_start},
        name="test-lifecycle",
    )
    worker.start()
    try:
        worker.enqueue(LifecycleJob("start", "auto-one"))
        worker.enqueue(LifecycleJob("start", "auto-two"))
        assert done.wait(2)
    finally:
        worker.shutdown()

    assert [name for name, _thread_name in calls] == ["auto-one", "auto-two"]
    assert all(thread_name == "test-lifecycle" for _name, thread_name in calls)
    assert _row("auto-one")["activity_state"] == "running"
    assert _row("auto-two")["activity_state"] == "running"


def test_worker_marks_job_failed_when_handler_raises(tmp_path):
    _init_db(tmp_path)
    _insert_session()

    done = threading.Event()

    def on_transition(transition):
        if transition.state == "FAILED":
            done.set()

    def bad_handler(_job, _writer):
        raise RuntimeError("boom")

    worker = SessionLifecycleWorker(
        state_writer=SessionLifecycleStateWriter(on_transition=on_transition),
        handlers={"start": bad_handler},
    )
    worker.start()
    try:
        worker.enqueue(LifecycleJob("start", "auto-life"))
        assert done.wait(2)
    finally:
        worker.shutdown()

    assert _row()["startup_state"] == "setup_failed"
    assert _row()["activity_state"] == "failed"
    assert _row()["is_live"] == 0
    detail = json.loads(_row()["lifecycle_detail"])
    assert detail["failed_phase"] == "start"
    assert "RuntimeError: boom" in detail["reason"]


def test_worker_backpressure_is_nonblocking():
    worker = SessionLifecycleWorker(max_queue_size=1)
    worker.enqueue(LifecycleJob("start", "auto-one"))

    started = time.monotonic()
    try:
        worker.enqueue(LifecycleJob("start", "auto-two"))
    except LifecycleQueueFull:
        pass
    else:
        raise AssertionError("expected queue full")

    assert (time.monotonic() - started) < 0.1
    assert worker.try_enqueue(LifecycleJob("start", "auto-three")) is False


def test_derive_lifecycle_state_coarse_mapping():
    from tools.dashboard.session_lifecycle_worker import derive_lifecycle_state

    # The stored state column is the truth when present.
    assert derive_lifecycle_state({"state": "LAUNCHING", "is_live": 0}) == "LAUNCHING"
    # Legacy fallback (pre-backfill rows / mock fixtures):
    # FAILED wins over ENDED: fail writes set is_live=0 AND activity failed.
    assert derive_lifecycle_state(
        {"activity_state": "failed", "is_live": 0, "startup_state": "setup_failed"}
    ) == "FAILED"
    assert derive_lifecycle_state(
        {"activity_state": "idle", "is_live": 1, "startup_state": "setup_failed"}
    ) == "FAILED"
    assert derive_lifecycle_state(
        {"activity_state": "stopping", "is_live": 1, "startup_state": None}
    ) == "STOPPING"
    assert derive_lifecycle_state(
        {"activity_state": "cleaning", "is_live": 1, "startup_state": None}
    ) == "STOPPING"
    assert derive_lifecycle_state(
        {"activity_state": "dead", "is_live": 0, "startup_state": None}
    ) == "ENDED"
    assert derive_lifecycle_state(
        {"activity_state": "idle", "is_live": 0, "startup_state": None}
    ) == "ENDED"
    assert derive_lifecycle_state(
        {"activity_state": "running", "is_live": 1, "startup_state": "setup_running"}
    ) == "LAUNCHING"
    assert derive_lifecycle_state(
        {"activity_state": "idle", "is_live": 1, "startup_state": None}
    ) == "ACTIVE"
