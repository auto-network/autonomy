"""Tests for POST /api/graph/docs — container-aware docs ingestion.

Containers mount the per-org graph DBs read-only, so ``graph docs-ingest``
cannot write directly (``attempt to write a readonly database``). The
container CLI POSTs here instead; the dashboard runs the ingest in a
``--force-host`` subprocess against the writable host DB. Mirrors
``/api/graph/sessions``.
"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient


@pytest.fixture
def app():
    from tools.dashboard.server import app
    return app


@pytest.fixture
def client(app):
    # No-op dispatch_db.init_db which tries to open data/dispatch.db
    # (may not exist in test/read-only environments)
    with patch("agents.dispatch_db.init_db"):
        with TestClient(app) as c:
            yield c


def test_docs_ingest_success(client):
    """A POST with a path returns the subprocess output."""
    output = "  + BlindHash TOOL: 4 sections\n\nTotal: 1 ingested, 0 skipped\n"
    with patch(
        "tools.dashboard.server._run_graph_docs_ingest_cli",
        new=AsyncMock(return_value=(output, "", 0)),
    ):
        resp = client.post("/api/graph/docs", json={"path": "/workspace/repo/docs"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "1 ingested" in body["output"]


def test_docs_ingest_requires_path(client):
    """Missing path → 400, no subprocess."""
    with patch(
        "tools.dashboard.server._run_graph_docs_ingest_cli",
        new=AsyncMock(),
    ) as mock_ingest:
        resp = client.post("/api/graph/docs", json={})
    assert resp.status_code == 400
    assert "path required" in resp.json()["error"]
    mock_ingest.assert_not_awaited()


def test_docs_ingest_forwards_org_from_header_and_force(client):
    """org (from X-Graph-Org) and force flow to the subprocess helper."""
    mock_ingest = AsyncMock(return_value=("Total: 0 ingested, 1 skipped\n", "", 0))
    with patch(
        "tools.dashboard.server._run_graph_docs_ingest_cli",
        new=mock_ingest,
    ):
        resp = client.post(
            "/api/graph/docs",
            json={"path": "/tmp/TOOL.md", "force": True},
            headers={"X-Graph-Org": "blindhash"},
        )
    assert resp.status_code == 200
    mock_ingest.assert_awaited_once_with(
        path="/tmp/TOOL.md",
        org="blindhash",
        force=True,
    )


def test_docs_ingest_body_org_overrides_header(client):
    """An explicit body ``org`` wins over the header."""
    mock_ingest = AsyncMock(return_value=("Total: 1 ingested, 0 skipped\n", "", 0))
    with patch(
        "tools.dashboard.server._run_graph_docs_ingest_cli",
        new=mock_ingest,
    ):
        client.post(
            "/api/graph/docs",
            json={"path": "/tmp/x", "org": "blindhash"},
            headers={"X-Graph-Org": "autonomy"},
        )
    _, kwargs = mock_ingest.call_args
    assert kwargs["org"] == "blindhash"


def test_docs_ingest_error_returns_500(client):
    """Subprocess failure (e.g. path not found host-side) surfaces as 500."""
    with patch(
        "tools.dashboard.server._run_graph_docs_ingest_cli",
        new=AsyncMock(return_value=("", "Error: /nope not found", 1)),
    ):
        resp = client.post("/api/graph/docs", json={"path": "/nope"})
    assert resp.status_code == 500
    assert "not found" in resp.json()["error"]


def test_docs_ingest_cli_uses_host_mode_and_pins_org(monkeypatch):
    """The subprocess bypasses GRAPH_API (no recursion) and pins GRAPH_ORG."""
    from tools.dashboard import server

    seen = {}

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return b"Total: 2 ingested, 0 skipped\n", b""

    async def fake_create_subprocess_exec(*cmd, stdout, stderr, env):
        seen["cmd"] = cmd
        seen["env"] = env
        return FakeProc()

    monkeypatch.setenv("GRAPH_API", "https://localhost:8080")
    with patch("asyncio.create_subprocess_exec", new=fake_create_subprocess_exec):
        stdout, stderr, rc = asyncio.run(
            server._run_graph_docs_ingest_cli(
                path="/workspace/repo/docs",
                org="blindhash",
                force=True,
            )
        )

    assert rc == 0
    assert "2 ingested" in stdout
    assert seen["cmd"] == (
        "graph",
        "--force-host",
        "docs-ingest",
        "/workspace/repo/docs",
        "--force",
    )
    assert "GRAPH_API" not in seen["env"]
    assert seen["env"]["GRAPH_ORG"] == "blindhash"
