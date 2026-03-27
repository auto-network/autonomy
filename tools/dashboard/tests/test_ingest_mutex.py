"""Tests for the ingest mutex on POST /api/graph/sessions."""
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
    with TestClient(app) as c:
        yield c


def test_single_call_succeeds(client):
    """A single POST /api/graph/sessions works normally."""
    with patch("tools.dashboard.server.run_cli", new_callable=AsyncMock) as mock_cli:
        mock_cli.return_value = ("ingested 5 sessions", "", 0)
        resp = client.post("/api/graph/sessions", json={"all": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "ingested" in body["output"]
    assert "skipped" not in body


def test_single_call_passes_flags(client):
    """Flags from request body are forwarded to graph CLI."""
    with patch("tools.dashboard.server.run_cli", new_callable=AsyncMock) as mock_cli:
        mock_cli.return_value = ("ok", "", 0)
        client.post("/api/graph/sessions", json={"all": True, "project": "test", "force": True})
    cmd = mock_cli.call_args[0][0]
    assert cmd == ["graph", "sessions", "--all", "--project", "test", "--force"]


def test_cli_failure_returns_500(client):
    """CLI error propagates as 500."""
    with patch("tools.dashboard.server.run_cli", new_callable=AsyncMock) as mock_cli:
        mock_cli.return_value = ("", "db locked", 1)
        resp = client.post("/api/graph/sessions", json={})
    assert resp.status_code == 500
    assert "db locked" in resp.json()["error"]


def test_concurrent_calls_second_skipped():
    """Two simultaneous calls: first runs, second returns skipped=True."""
    import json as json_mod

    async def _run():
        from tools.dashboard.server import api_graph_sessions, _ingest_lock

        assert not _ingest_lock.locked()

        slow_event = asyncio.Event()

        async def slow_cli(cmd, timeout=120, stdin_data=None):
            await slow_event.wait()
            return ("done", "", 0)

        with patch("tools.dashboard.server.run_cli", side_effect=slow_cli):

            async def make_request(body):
                req = AsyncMock()
                req.json = AsyncMock(return_value=body)
                return await api_graph_sessions(req)

            # Launch first call (will block on slow_cli)
            task1 = asyncio.create_task(make_request({"all": True}))
            # Give event loop a tick so task1 acquires the lock
            await asyncio.sleep(0.01)

            assert _ingest_lock.locked(), "First call should hold the lock"

            # Second call should return immediately with skipped
            resp2 = await make_request({"all": True})
            body2 = json_mod.loads(resp2.body)
            assert body2["skipped"] is True
            assert body2["ok"] is True

            # Let the first call finish
            slow_event.set()
            resp1 = await task1
            body1 = json_mod.loads(resp1.body)
            assert body1["ok"] is True
            assert "skipped" not in body1

    asyncio.run(_run())
