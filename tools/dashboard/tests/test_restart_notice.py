"""Restart-notice handoff tests.

The UI receives the countdown live; this module verifies the durable half of
the contract, which is what lets the next process report an exact duration.
"""

import asyncio
import json
import os

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from tools.dashboard import server
from tools.dashboard.event_bus import EventBus


def test_restart_notice_round_trip_emits_completion(tmp_path, monkeypatch):
    notice_path = tmp_path / "restart_notice.state"
    bus = EventBus()
    monkeypatch.setattr(server, "RESTART_NOTICE_STATE_PATH", notice_path)
    monkeypatch.setattr(server, "event_bus", bus)
    monkeypatch.setattr(server, "_current_headline_context", lambda: {
        "commit_hash": "abc123", "commit_headline": "Make reloads visible",
    })

    server._write_restart_notice({"started_at_ms": 1})
    queue = bus.subscribe()
    asyncio.run(server._emit_restart_complete())

    topic, payload, seq = queue.get_nowait()
    assert topic == "server:restart"
    assert seq > 0
    assert payload["phase"] == "complete"
    assert payload["started_at_ms"] == 1
    assert payload["duration_ms"] >= 0
    assert payload["commit_headline"] == "Make reloads visible"
    assert not notice_path.exists()


def test_restart_notice_reader_rejects_invalid_state(tmp_path, monkeypatch):
    notice_path = tmp_path / "restart_notice.state"
    notice_path.write_text(json.dumps({"started_at_ms": "not-a-time"}))
    monkeypatch.setattr(server, "RESTART_NOTICE_STATE_PATH", notice_path)

    assert server._read_restart_notice() is None


def test_authenticated_restart_preflight_emits_one_countdown(tmp_path, monkeypatch):
    notice_path = tmp_path / "restart_notice.state"
    bus = EventBus()
    monkeypatch.setattr(server, "RESTART_NOTICE_STATE_PATH", notice_path)
    monkeypatch.setattr(server, "event_bus", bus)
    monkeypatch.setattr(server, "_restart_notice_payload", None)
    monkeypatch.setattr(server, "_restart_notice_lock", asyncio.Lock())
    # Keep this test about the countdown/notice contract, not git state:
    # a real recent HEAD would otherwise add attribution to the notice.
    monkeypatch.setattr(server, "_restart_attribution", lambda changed_files: {})
    monkeypatch.setenv("DASHBOARD_RESTART_TOKEN", "test-restart-token")
    app = Starlette(routes=[
        Route("/api/internal/restart-notice", server.api_internal_restart_notice, methods=["POST"]),
    ])
    queue = bus.subscribe()

    with TestClient(app) as client:
        assert client.post("/api/internal/restart-notice").status_code == 403
        assert client.post(
            "/api/internal/restart-notice",
            headers={"X-Dashboard-Restart-Token": "wrong"},
        ).status_code == 403
        response = client.post(
            "/api/internal/restart-notice",
            headers={"X-Dashboard-Restart-Token": "test-restart-token"},
        )
        assert response.status_code == 200
        assert response.json()["countdown_seconds"] == 3
        assert client.post(
            "/api/internal/restart-notice",
            headers={"X-Dashboard-Restart-Token": "test-restart-token"},
        ).status_code == 200

    topic, payload, _seq = queue.get_nowait()
    assert topic == "server:restart"
    assert payload["phase"] == "countdown"
    assert payload["countdown_ends_at_ms"] - payload["started_at_ms"] == 3_000
    assert queue.empty()
    assert bus.subscribe().empty()
    assert server._read_restart_notice() == {"started_at_ms": payload["started_at_ms"]}


# ── restart attribution (who/what caused the restart) ──────────────

def _init_git_repo(root):
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    return root


def _commit(root, message):
    import subprocess
    (root / "app.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "app.py"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=root, check=True)


def test_attribution_merge_reads_head_provenance_trailer(tmp_path, monkeypatch):
    repo = _init_git_repo(tmp_path)
    _commit(repo, "feat: add thing\n\nAutonomy-Provenance: autonomy://PK/auto-abc/9")
    monkeypatch.setattr(server, "_REPO_ROOT", repo)

    attr = server._restart_attribution([str(repo / "app.py")])
    assert attr["trigger"] == "merge"
    assert attr["session"] == "auto-abc"
    assert attr["commit_headline"] == "feat: add thing"
    assert attr["summary"] == "auto-abc · feat: add thing"


def test_attribution_direct_edit_detects_uncommitted_change(tmp_path, monkeypatch):
    repo = _init_git_repo(tmp_path)
    _commit(repo, "feat: add thing\n\nAutonomy-Provenance: autonomy://PK/auto-abc/9")
    monkeypatch.setattr(server, "_REPO_ROOT", repo)
    (repo / "app.py").write_text("x = 2\n")  # uncommitted host edit

    attr = server._restart_attribution([str(repo / "app.py")])
    assert attr["trigger"] == "direct-edit"
    assert attr["files"] == ["app.py"]
    assert attr["summary"] == "host terminal · direct file edit · app.py"


def test_attribution_old_head_without_changed_files_is_empty(tmp_path, monkeypatch):
    import subprocess
    repo = _init_git_repo(tmp_path)
    # Commit with a committer date far in the past so the recency fallback fails.
    env = {**os.environ,
           "GIT_COMMITTER_DATE": "2020-01-01T00:00:00",
           "GIT_AUTHOR_DATE": "2020-01-01T00:00:00"}
    (repo / "app.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "old commit"], cwd=repo, check=True, env=env)
    monkeypatch.setattr(server, "_REPO_ROOT", repo)

    assert server._restart_attribution(None) == {}


def test_attribution_round_trips_into_completion_payload(tmp_path, monkeypatch):
    notice_path = tmp_path / "restart_notice.state"
    bus = EventBus()
    monkeypatch.setattr(server, "RESTART_NOTICE_STATE_PATH", notice_path)
    monkeypatch.setattr(server, "event_bus", bus)
    monkeypatch.setattr(server, "_current_headline_context", lambda: {
        "commit_hash": "abc123", "commit_headline": "Make reloads visible",
    })

    attribution = {"trigger": "merge", "session": "auto-xyz",
                   "commit_headline": "feat: land it",
                   "summary": "auto-xyz · feat: land it"}
    server._write_restart_notice({"started_at_ms": 5, "attribution": attribution})
    # The notice survives a read (post-restart worker).
    assert server._read_restart_notice()["attribution"] == attribution

    queue = bus.subscribe()
    asyncio.run(server._emit_restart_complete())
    _topic, payload, _seq = queue.get_nowait()
    assert payload["phase"] == "complete"
    assert payload["attribution"] == attribution


def test_announce_restart_includes_attribution_in_countdown(tmp_path, monkeypatch):
    notice_path = tmp_path / "restart_notice.state"
    bus = EventBus()
    monkeypatch.setattr(server, "RESTART_NOTICE_STATE_PATH", notice_path)
    monkeypatch.setattr(server, "event_bus", bus)
    monkeypatch.setattr(server, "_restart_notice_payload", None)
    monkeypatch.setattr(server, "_restart_notice_lock", asyncio.Lock())
    attribution = {"trigger": "direct-edit", "files": ["server.py"],
                   "summary": "host terminal · direct file edit · server.py"}
    monkeypatch.setattr(server, "_restart_attribution", lambda changed_files: attribution)

    queue = bus.subscribe()
    payload = asyncio.run(server._announce_restart(["/abs/server.py"]))
    assert payload["attribution"] == attribution
    _topic, broadcast, _seq = queue.get_nowait()
    assert broadcast["attribution"] == attribution
    # Persisted so the post-restart completion can repeat it.
    assert server._read_restart_notice()["attribution"] == attribution
