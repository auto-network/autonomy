"""Tests for the ingest mutex on POST /api/graph/sessions.

After the auto-iv6c5 ops migration, ``api_graph_sessions`` calls
:func:`tools.graph.ingest.ingest_all_claude_code` /
:func:`tools.graph.ingest.ingest_claude_code_project` directly instead of
shelling out to the CLI. The mutex semantics are unchanged — the first
call runs, the second returns ``skipped=True``.
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
    with patch("tools.graph.ingest.ingest_all_claude_code") as mock_ingest:
        mock_ingest.return_value = [
            {"status": "ingested", "session_id": "s1"},
            {"status": "ingested", "session_id": "s2"},
        ]
        resp = client.post("/api/graph/sessions", json={"all": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["counts"]["ingested"] == 2
    assert "skipped" not in body


def test_single_call_passes_flags(client):
    """Flags from request body are forwarded to the ingest function."""
    with patch("tools.graph.ingest.ingest_all_claude_code") as mock_ingest:
        mock_ingest.return_value = []
        resp = client.post("/api/graph/sessions", json={"all": True, "force": True})
    assert resp.status_code == 200
    assert mock_ingest.called
    # ingest_all_claude_code(db, force=force) — second kwarg is ``force``.
    _, kwargs = mock_ingest.call_args
    assert kwargs.get("force") is True


def test_ingest_exception_returns_500(client):
    """Ingest error propagates as 500."""
    with patch(
        "tools.graph.ingest.ingest_claude_code_project",
        side_effect=RuntimeError("db locked"),
    ):
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

        async def slow_thread(fn):
            # ``asyncio.to_thread`` passthrough that waits for the test
            # to release the event; allows the test to hold the mutex
            # while the second call races in.
            await slow_event.wait()
            return fn()

        with patch("tools.graph.ingest.ingest_all_claude_code", return_value=[]):
            with patch("asyncio.to_thread", side_effect=slow_thread):
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
