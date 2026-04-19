"""Tests for server-side org identity enrichment on search / source / session
endpoints. Spec: auto-w005w.

All three surfaces must return a resolved ``org`` object on their payloads so
the templates can render the glyph + name without a second round-trip or
client-side cascade logic. Dates on search results are normalised to
``YYYY-MM-DD HH:MM`` (24hr).
"""

from __future__ import annotations

import os
from unittest.mock import patch

from starlette.testclient import TestClient


# ── /api/search — org + date enrichment (mock mode) ───────────────────


def _mock_search_results():
    """Minimal search result fixtures matching the shape api_search returns."""
    return [
        {
            "id": "src-001",
            "source_id": "src-001",
            "source_title": "Anchore note",
            "result_type": "note",
            "project": "anchore",
            "content": "...",
            "created_at": "2026-03-22T14:32:00Z",
        },
        {
            "id": "src-002",
            "source_id": "src-002",
            "source_title": "Legacy session",
            "result_type": "session",
            "project": "-workspace-repo",  # path-derived junk
            "content": "...",
            "created_at": "2026-04-01T08:15:42Z",
        },
    ]


def test_api_search_attaches_org_and_date(test_app):
    """Each search result gets a resolved ``org`` object and a 24hr date."""
    from tools.dashboard import server

    os.environ["DASHBOARD_MOCK"] = "1"
    try:
        with TestClient(test_app) as client:
            # Inject search() inside the TestClient lifespan so startup uses
            # the real dao_beads but this request does not.
            with patch.object(
                server.dao_beads,
                "search",
                staticmethod(lambda q, limit=20, project=None: _mock_search_results()),
                create=True,
            ):
                r = client.get("/api/search?q=anchore")
                assert r.status_code == 200
                results = r.json()
    finally:
        os.environ.pop("DASHBOARD_MOCK", None)

    assert len(results) == 2
    first, second = results

    # Anchore source — resolved, real org.
    assert first["org"]["slug"] == "anchore"
    assert first["org"]["resolved"] is True
    # Date — 24hr "YYYY-MM-DD HH:MM".
    assert first["date"] == "2026-03-22 14:32"

    # Path-derived project — unresolved, renders "?".
    assert second["org"]["slug"] == "unknown"
    assert second["org"]["resolved"] is False
    assert second["org"]["initial"] == "?"
    assert second["date"] == "2026-04-01 08:15"


# ── /api/source/{id} — org enrichment (mock mode) ─────────────────────


def test_api_source_read_attaches_org(test_app):
    """``src.org`` is populated on the source payload so the header can render
    the org glyph."""
    mock_source = {
        "id": "src-001",
        "title": "Anchore doc",
        "type": "note",
        "project": "anchore",
        "created_at": "2026-03-22T14:32:00Z",
        "metadata": "{}",
        "content": "# Hello",
    }
    from tools.dashboard import server

    os.environ["DASHBOARD_MOCK"] = "1"
    try:
        with TestClient(test_app) as client:
            with patch.object(
                server.dao_beads,
                "get_source",
                staticmethod(lambda sid: mock_source),
                create=True,
            ):
                r = client.get("/api/source/src-001")
                assert r.status_code == 200
                body = r.json()
    finally:
        os.environ.pop("DASHBOARD_MOCK", None)

    assert body["org"]["slug"] == "anchore"
    assert body["org"]["resolved"] is True


# ── /api/session/{tmux_name} — org on session detail ──────────────────


def test_api_session_get_includes_org(test_app):
    """``/api/session/{tmux_name}`` returns the resolved org identity so the
    session viewer header renders the ORG stat cell."""
    with TestClient(test_app) as client:
        r = client.get("/api/session/auto-test-designer")
        assert r.status_code == 200
        body = r.json()

    assert "org" in body
    # Mock sessions have project=autonomy in conftest.
    assert body["org"]["slug"] == "autonomy"
    assert body["org"]["resolved"] is True
