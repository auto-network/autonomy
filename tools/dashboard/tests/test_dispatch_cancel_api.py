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
