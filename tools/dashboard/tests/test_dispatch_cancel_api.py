"""Tests for POST /api/dispatch/cancel/{bead_id} (auto-je5rv item 3).

The cancel endpoint is the inverse of resume: it kills the bead's running
container and records the run CANCELLED (terminal, no reopen), then strips
readiness:approved so the pipeline stops re-dispatching. The dashboard owns
the docker socket, so this replaces escalating a runaway dispatch to a host
``docker kill``.
"""

from __future__ import annotations

from starlette.testclient import TestClient


def _wire(monkeypatch, *, bead, running):
    """Patch the server + dispatcher seams the cancel path touches."""
    from tools.dashboard import server
    import agents.dispatcher as dispatcher

    async def fake_run_cli_json(cmd, *a, **k):
        return bead

    updates = []

    async def fake_run_cli(cmd, *a, **k):
        updates.append(cmd)
        return ("", "", 0)

    recorded = {}
    killed = {}

    monkeypatch.setattr(server, "run_cli_json", fake_run_cli_json)
    monkeypatch.setattr(server, "run_cli", fake_run_cli)
    monkeypatch.setattr(server, "mark_run_cancelling", lambda bead_id: running)
    monkeypatch.setattr(
        server, "record_run_cancelled",
        lambda run_id, reason="": recorded.update(run_id=run_id, reason=reason))
    monkeypatch.setattr(
        dispatcher, "kill_container",
        lambda name: killed.update(name=name))
    return updates, recorded, killed


def test_cancel_kills_container_and_records_cancelled(test_app, monkeypatch):
    updates, recorded, killed = _wire(
        monkeypatch,
        bead={"id": "auto-cx",
              "labels": ["org:autonomy", "readiness:approved"]},
        running={"id": "run-1", "container_name": "cont-1"},
    )

    client = TestClient(test_app)
    r = client.post("/api/dispatch/cancel/auto-cx")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "CANCELLED"
    assert body["run_id"] == "run-1"
    assert body["container"] == "cont-1"

    # Container was killed and the terminal row recorded.
    assert killed["name"] == "cont-1"
    assert recorded["run_id"] == "run-1"

    # readiness:approved stripped so the pipeline stops re-dispatching.
    assert any("--remove-label" in c and "readiness:approved" in c
               for c in updates)


def test_cancel_no_running_dispatch_is_404(test_app, monkeypatch):
    _wire(
        monkeypatch,
        bead={"id": "auto-idle", "labels": ["org:autonomy"]},
        running=None,  # mark_run_cancelling finds no RUNNING row
    )

    client = TestClient(test_app)
    r = client.post("/api/dispatch/cancel/auto-idle")
    assert r.status_code == 404
    assert "no running dispatch" in r.json()["error"]


def test_cancel_unknown_bead_is_404(test_app, monkeypatch):
    from tools.dashboard import server

    async def fake_run_cli_json(cmd, *a, **k):
        return {"error": "not found"}

    monkeypatch.setattr(server, "run_cli_json", fake_run_cli_json)

    client = TestClient(test_app)
    r = client.post("/api/dispatch/cancel/auto-missing")
    assert r.status_code == 404
    assert r.json()["error"] == "bead not found"


def test_cancel_before_launch_withdraws_the_approval(test_app, monkeypatch, tmp_path):
    """auto-diqwv: an approved bead whose launch keeps failing has no run
    row; cancel withdraws its approval and backoff instead of answering
    'no running dispatch' (removing the label by hand was the only stop)."""
    import agents.dispatcher as dispatcher
    monkeypatch.setattr(dispatcher, "LAUNCH_BACKOFF_PATH", tmp_path / "backoff.json")
    dispatcher._write_launch_backoff({"auto-loop": {"count": 2, "next_at": 9e12}})
    updates, _recorded, _killed = _wire(
        monkeypatch,
        bead={"id": "auto-loop", "labels": ["org:autonomy", "readiness:approved"]},
        running=None,
    )
    r = TestClient(test_app).post("/api/dispatch/cancel/auto-loop")
    assert r.status_code == 200 and r.json()["status"] == "APPROVAL_WITHDRAWN"
    assert any("--remove-label" in c and "readiness:approved" in c for c in updates)
    assert not dispatcher.launch_backoff_active("auto-loop")


def test_cancel_before_launch_reports_a_failed_withdrawal(test_app, monkeypatch, tmp_path):
    """Review of 1155d6a6: bd failing to remove the label is a 502, and the
    backoff stays, so the still-approved bead does not launch next cycle."""
    import agents.dispatcher as dispatcher
    from tools.dashboard import server
    monkeypatch.setattr(dispatcher, "LAUNCH_BACKOFF_PATH", tmp_path / "backoff.json")
    dispatcher._write_launch_backoff({"auto-loop": {"count": 2, "next_at": 9e12}})
    _wire(monkeypatch,
          bead={"id": "auto-loop", "labels": ["org:autonomy", "readiness:approved"]},
          running=None)

    async def failing_run_cli(cmd, *a, **k):
        return ("", "bd: connection refused", 1)
    monkeypatch.setattr(server, "run_cli", failing_run_cli)
    r = TestClient(test_app).post("/api/dispatch/cancel/auto-loop")
    assert r.status_code == 502
    assert "connection refused" in r.json()["detail"]
    assert dispatcher.launch_backoff_active("auto-loop", now=0.0)
