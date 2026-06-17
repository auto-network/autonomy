"""Tests for the ingest mutex on POST /api/graph/sessions.

``api_graph_sessions`` runs the CPU-heavy graph sessions ingest in a
subprocess so JSONL parsing and FTS work cannot hold the dashboard process
GIL. The mutex semantics are unchanged — the first call runs, the second
returns ``skipped=True``.
"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient


@pytest.fixture
def app():
    """Import a fresh server app."""
    from tools.dashboard.server import app
    return app


@pytest.fixture
def client(app):
    # No-op dispatch_db.init_db which tries to open data/dispatch.db
    # (may not exist in test/read-only environments)
    with patch("agents.dispatch_db.init_db"):
        with TestClient(app) as c:
            yield c


def test_single_call_succeeds(client):
    """A single POST /api/graph/sessions works normally."""
    output = "Total: 2 new, 0 updated, 0 refreshed, 0 skipped\n"
    with patch(
        "tools.dashboard.server._run_graph_sessions_ingest_cli",
        new=AsyncMock(return_value=(output, "", 0)),
    ):
        resp = client.post("/api/graph/sessions", json={"all": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["counts"]["ingested"] == 2
    assert "skipped" not in body


def test_single_call_passes_flags(client):
    """Flags from request body are forwarded to the ingest subprocess helper."""
    mock_ingest = AsyncMock(
        return_value=("Total: 0 new, 0 updated, 0 refreshed, 0 skipped\n", "", 0)
    )
    with patch(
        "tools.dashboard.server._run_graph_sessions_ingest_cli",
        new=mock_ingest,
    ):
        resp = client.post("/api/graph/sessions", json={"all": True, "force": True})
    assert resp.status_code == 200
    mock_ingest.assert_awaited_once_with(
        all_projects=True,
        project=None,
        force=True,
    )


def test_ingest_exception_returns_500(client):
    """Subprocess ingest errors propagate as 500."""
    with patch(
        "tools.dashboard.server._run_graph_sessions_ingest_cli",
        new=AsyncMock(return_value=("", "db locked", 1)),
    ):
        resp = client.post("/api/graph/sessions", json={})
    assert resp.status_code == 500
    assert "db locked" in resp.json()["error"]


def test_ingest_cli_uses_host_mode_and_avoids_dashboard_recursion(monkeypatch):
    """The subprocess must bypass GRAPH_API or it can recurse into this route."""
    from tools.dashboard import server

    seen = {}

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return b"Total: 0 new, 1 updated, 0 refreshed, 2 skipped\n", b""

    async def fake_create_subprocess_exec(*cmd, stdout, stderr, env):
        seen["cmd"] = cmd
        seen["stdout"] = stdout
        seen["stderr"] = stderr
        seen["env"] = env
        return FakeProc()

    monkeypatch.setenv("GRAPH_API", "https://localhost:8080")
    with patch("asyncio.create_subprocess_exec", new=fake_create_subprocess_exec):
        stdout, stderr, rc = asyncio.run(
            server._run_graph_sessions_ingest_cli(
                all_projects=False,
                project="/workspace/repo",
                force=True,
            )
        )

    assert rc == 0
    assert stderr == ""
    assert "1 updated" in stdout
    assert seen["cmd"] == (
        "graph",
        "--force-host",
        "sessions",
        "--project",
        "/workspace/repo",
        "--force",
    )
    assert "GRAPH_API" not in seen["env"]


def test_concurrent_calls_second_skipped():
    """Two simultaneous calls: first runs, second returns skipped=True."""
    import json as json_mod

    async def _run():
        from tools.dashboard.server import api_graph_sessions, _ingest_lock

        assert not _ingest_lock.locked()

        slow_event = asyncio.Event()

        async def slow_ingest(**_kwargs):
            await slow_event.wait()
            return "Total: 0 new, 0 updated, 0 refreshed, 0 skipped\n", "", 0

        with patch("tools.dashboard.server._run_graph_sessions_ingest_cli", new=slow_ingest):
            async def make_request(body):
                req = AsyncMock()
                req.json = AsyncMock(return_value=body)
                return await api_graph_sessions(req)

            task1 = asyncio.create_task(make_request({"all": True}))
            await asyncio.sleep(0.01)

            assert _ingest_lock.locked(), "First call should hold the lock"

            resp2 = await make_request({"all": True})
            body2 = json_mod.loads(resp2.body)
            assert body2["skipped"] is True
            assert body2["ok"] is True

            slow_event.set()
            resp1 = await task1
            body1 = json_mod.loads(resp1.body)
            assert body1["ok"] is True
            assert "skipped" not in body1

    asyncio.run(_run())
