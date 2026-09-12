"""auto-0yxpm — POST /api/monitor/dispatch-nag delivers nags from the dashboard.

The dispatcher container has NO host tmux socket, so raw `tmux paste-buffer`
from there always fails rc=1 and the old code reported a phantom success. The
fix routes delivery through this endpoint, which runs in the dashboard process
(which DOES reach the host tmux server) and AWAITS each paste so a nonzero
tmux rc surfaces as a `failed` entry — never a false success.

These tests drive the real HTTP surface via TestClient. tmux itself is
mocked (no tmux server in CI); the contract under test is the endpoint's
delivered/failed/offline classification, which is what makes a failed nag
distinguishable from a delivered one.
"""
from __future__ import annotations

import pytest

pytest.importorskip("starlette")
pytest.importorskip("pytest_asyncio")


def _client(srv):
    from starlette.testclient import TestClient
    return TestClient(srv.app)


def test_delivers_to_live_session(ipc_env, monkeypatch):
    srv = ipc_env["server"]
    sent: list[tuple[str, str]] = []

    async def fake_awaited(target, text):
        sent.append((target, text))

    monkeypatch.setattr(srv, "_tmux_session_exists", lambda name: True)
    monkeypatch.setattr(srv, "tmux_send_awaited", fake_awaited)

    with _client(srv) as client:
        resp = client.post(
            "/api/monitor/dispatch-nag",
            json={"targets": ["auto-live-1"], "message": "auto-x DONE — Fix"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["delivered"] == ["auto-live-1"]
    assert body["failed"] == []
    assert body["offline"] == []
    # The envelope is stamped from the dispatcher, and the message is inside it.
    assert len(sent) == 1
    _, envelope = sent[0]
    assert 'from="dispatcher"' in envelope
    assert "auto-x DONE — Fix" in envelope


def test_nonzero_tmux_rc_is_failure_not_success(ipc_env, monkeypatch):
    """A nonzero tmux rc (TmuxSendError) must land in `failed`, never
    `delivered` — the core auto-0yxpm defect was reporting it as success."""
    srv = ipc_env["server"]
    from tools.dashboard.tmux_send import TmuxSendError

    async def boom(target, text):
        raise TmuxSendError("paste-buffer", 1,
                            "error connecting to /tmp/tmux-0/default")

    monkeypatch.setattr(srv, "_tmux_session_exists", lambda name: True)
    monkeypatch.setattr(srv, "tmux_send_awaited", boom)

    with _client(srv) as client:
        resp = client.post(
            "/api/monitor/dispatch-nag",
            json={"targets": ["auto-live-1"], "message": "auto-x FAILED"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["delivered"] == []
    assert len(body["failed"]) == 1
    entry = body["failed"][0]
    assert entry["target"] == "auto-live-1"
    assert "rc=1" in entry["error"]
    assert "/tmp/tmux-0/default" in entry["error"]


def test_offline_target_not_reported_delivered(ipc_env, monkeypatch):
    srv = ipc_env["server"]
    delivered_calls: list[str] = []

    async def fake_awaited(target, text):
        delivered_calls.append(target)

    monkeypatch.setattr(srv, "_tmux_session_exists", lambda name: False)
    monkeypatch.setattr(srv, "tmux_send_awaited", fake_awaited)

    with _client(srv) as client:
        resp = client.post(
            "/api/monitor/dispatch-nag",
            json={"targets": ["auto-dead"], "message": "auto-x DONE"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["delivered"] == []
    assert body["offline"] == ["auto-dead"]
    assert delivered_calls == []  # never touched tmux for a dead session


def test_duplicate_targets_delivered_once(ipc_env, monkeypatch):
    srv = ipc_env["server"]
    sent: list[str] = []

    async def fake_awaited(target, text):
        sent.append(target)

    monkeypatch.setattr(srv, "_tmux_session_exists", lambda name: True)
    monkeypatch.setattr(srv, "tmux_send_awaited", fake_awaited)

    with _client(srv) as client:
        resp = client.post(
            "/api/monitor/dispatch-nag",
            json={"targets": ["auto-live-1", "auto-live-1"],
                  "message": "auto-x DONE"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["delivered"] == ["auto-live-1"]
    assert sent == ["auto-live-1"]


def test_bad_body_rejected(ipc_env, monkeypatch):
    srv = ipc_env["server"]
    monkeypatch.setattr(srv, "_tmux_session_exists", lambda name: True)
    with _client(srv) as client:
        r1 = client.post("/api/monitor/dispatch-nag",
                         json={"targets": "auto-live-1", "message": "x"})
        r2 = client.post("/api/monitor/dispatch-nag",
                         json={"targets": ["auto-live-1"], "message": ""})
    assert r1.status_code == 400
    assert r2.status_code == 400
