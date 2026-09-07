"""The dashboard lifespan under the zero-downtime hand-off.

Cold start: ready marker written, predecessor-exclusive steps run inline.
Hand-off: ready marker written at the end of startup, the exclusive steps wait
for the supervisor's activation marker while the predecessor is still alive.
"""

from __future__ import annotations

import os
import time

from starlette.testclient import TestClient

from tools.dashboard import worker_handoff as wh


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _spy_exclusive_steps(monkeypatch, server):
    calls = []
    import agents.dispatch_db as dispatch_db
    monkeypatch.setattr(
        dispatch_db, "fail_stale_prelaunch_runs",
        lambda exclude_run_ids=None: calls.append(("sweep", set(exclude_run_ids or ()))) or 0,
    )

    async def recover():
        calls.append(("recover", None))
    monkeypatch.setattr(server, "_recover_stuck_lifecycle_rows", recover)
    return calls


def test_cold_start_marks_ready_and_activates_inline(test_app, tmp_path, monkeypatch):
    from tools.dashboard import server
    marker = tmp_path / "cold.ready"
    monkeypatch.setenv(wh.READY_MARKER_ENV, str(marker))
    monkeypatch.delenv(wh.PREDECESSOR_PID_ENV, raising=False)
    calls = _spy_exclusive_steps(monkeypatch, server)

    with TestClient(test_app):
        assert marker.exists()
        assert server._worker_activated is True
        assert ("sweep", set()) in calls
        assert ("recover", None) in calls
        assert server._activation_task is None

    assert server._worker_activated is False      # reset for the next lifespan


def test_handoff_defers_exclusive_steps_until_activation(test_app, tmp_path, monkeypatch):
    from tools.dashboard import server
    marker = tmp_path / "replacement.ready"
    monkeypatch.setenv(wh.READY_MARKER_ENV, str(marker))
    # Our own pid stands in for a live predecessor.
    monkeypatch.setenv(wh.PREDECESSOR_PID_ENV, str(os.getpid()))
    calls = _spy_exclusive_steps(monkeypatch, server)
    server._pending_agentic_launches["run-owned-here"] = {"ctx": True}
    try:
        with TestClient(test_app):
            assert marker.exists(), "readiness must be published before activation"
            assert server._worker_activated is False
            assert calls == []
            assert server._activation_task is not None and not server._activation_task.done()

            wh.signal_activation(marker)

            assert _wait_until(lambda: server._worker_activated), "activation never ran"
            assert _wait_until(lambda: ("recover", None) in calls)
            sweeps = [c for c in calls if c[0] == "sweep"]
            assert sweeps == [("sweep", {"run-owned-here"})], (
                "the sweep must spare rows this worker accepted during the overlap"
            )
    finally:
        server._pending_agentic_launches.pop("run-owned-here", None)

    assert server._worker_activated is False
    assert server._activation_task is None


def test_handoff_activates_when_predecessor_vanishes(test_app, tmp_path, monkeypatch):
    from tools.dashboard import server
    import subprocess
    marker = tmp_path / "orphan.ready"
    gone = subprocess.Popen(["true"])
    gone.wait()
    monkeypatch.setenv(wh.READY_MARKER_ENV, str(marker))
    monkeypatch.setenv(wh.PREDECESSOR_PID_ENV, str(gone.pid))
    calls = _spy_exclusive_steps(monkeypatch, server)

    with TestClient(test_app):
        assert _wait_until(lambda: server._worker_activated), (
            "a vanished predecessor must activate the worker without a marker"
        )
        assert _wait_until(lambda: any(c[0] == "sweep" for c in calls))


def test_restart_notice_handoff_mode_snapshots_without_countdown(test_app, monkeypatch):
    from tools.dashboard import server
    monkeypatch.setenv("DASHBOARD_RESTART_TOKEN", "tok")
    broadcasts = []
    original = server.event_bus.broadcast

    async def spy(topic, payload, **kw):
        broadcasts.append((topic, payload))
        return await original(topic, payload, **kw)
    monkeypatch.setattr(server.event_bus, "broadcast", spy)

    with TestClient(test_app) as client:
        broadcasts.clear()
        resp = client.post(
            "/api/internal/restart-notice",
            json={"mode": "handoff"},
            headers={"X-Dashboard-Restart-Token": "tok"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["mode"] == "handoff"
        assert body["snapshot"]["event_bus"] is True
        assert body["snapshot"]["vault"] is False      # no warm vault in tests
        announced = [p for t, p in broadcasts if t == "server:restart"]
        assert len(announced) == 1, "hand-off announces the background reload once"
        assert announced[0]["phase"] == "restarting"
        assert "countdown_ends_at_ms" not in announced[0], (
            "no countdown: the page stays live while the replacement boots"
        )
        assert announced[0]["started_at_ms"] == body["started_at_ms"]
        assert server.EVENT_BUS_STATE_PATH.exists()

        # The legacy body still announces the countdown (on a worker that has
        # not announced yet: the announcement is once per worker).
        server._restart_notice_payload = None
        resp = client.post(
            "/api/internal/restart-notice",
            json={},
            headers={"X-Dashboard-Restart-Token": "tok"},
        )
        assert resp.status_code == 200
        assert resp.json()["countdown_seconds"] == server._RESTART_WARNING_SECONDS
        assert any(
            t == "server:restart" and p.get("phase") == "countdown" for t, p in broadcasts
        )

        bad = client.post(
            "/api/internal/restart-notice",
            json={"mode": "handoff"},
            headers={"X-Dashboard-Restart-Token": "wrong"},
        )
        assert bad.status_code == 403
