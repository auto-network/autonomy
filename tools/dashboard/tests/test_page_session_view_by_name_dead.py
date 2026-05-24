"""auto-eik9g: /session/<name> route resolves dead sessions via the graph DB.

The route previously redirected to /sessions?session=<name> whenever the
session wasn't in tmux_sessions (the live table) — that query param was
a no-op on the index page, producing the broken search → source →
viewer workflow.

The fix: when dashboard_db misses, look up the session in the graph DB
via tools.graph.ops.get_session(name). The graph source row carries
metadata.project, so dead-but-ingested sessions still redirect to
/session/<project>/<name>. Genuinely missing sessions get a 404 with
a back-link, not the broken /sessions redirect.
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest
from starlette.testclient import TestClient


@pytest.fixture
def app_client():
    """Boot the dashboard with a no-op lifespan + return a TestClient."""
    from tools.dashboard import server
    return TestClient(server.app)


def test_route_live_session_redirects_to_project_url(app_client):
    """Tier 1: session present in tmux_sessions → 302 to /session/<project>/<name>."""
    fake = {"tmux_name": "auto-live", "project": "autonomy"}
    with patch(
        "tools.dashboard.server.dashboard_db.get_session",
        return_value=fake,
    ):
        r = app_client.get(
            "/session/auto-live", follow_redirects=False,
        )
    assert r.status_code == 302
    assert r.headers["location"] == "/session/autonomy/auto-live"


def test_route_dead_session_falls_back_to_graph_lookup(app_client):
    """Tier 2: dashboard_db miss → graph.ops.get_session → 302."""
    # graph row carries metadata.project for dead-but-ingested sessions.
    graph_row = {
        "id": "src-abc",
        "type": "session",
        "metadata": {"project": "autonomy", "tmux_session": "auto-dead"},
    }
    with patch(
        "tools.dashboard.server.dashboard_db.get_session",
        return_value=None,
    ), patch(
        "tools.graph.ops.get_session",
        return_value=graph_row,
    ):
        r = app_client.get(
            "/session/auto-dead", follow_redirects=False,
        )
    assert r.status_code == 302
    assert r.headers["location"] == "/session/autonomy/auto-dead"


def test_route_dead_session_with_str_metadata(app_client):
    """Some graph rows return metadata as a JSON string, not a dict."""
    graph_row = {
        "id": "src-xyz",
        "type": "session",
        "metadata": '{"project": "enterprise-ng", "tmux_session": "auto-strmeta"}',
    }
    with patch(
        "tools.dashboard.server.dashboard_db.get_session",
        return_value=None,
    ), patch(
        "tools.graph.ops.get_session",
        return_value=graph_row,
    ):
        r = app_client.get(
            "/session/auto-strmeta", follow_redirects=False,
        )
    assert r.status_code == 302
    assert r.headers["location"] == "/session/enterprise-ng/auto-strmeta"


def test_route_genuinely_missing_returns_404_not_sessions_redirect(app_client):
    """Tier 3: dashboard_db miss + graph miss → 404 HTML page, NOT a redirect."""
    with patch(
        "tools.dashboard.server.dashboard_db.get_session",
        return_value=None,
    ), patch(
        "tools.graph.ops.get_session",
        return_value=None,
    ):
        r = app_client.get(
            "/session/auto-doesnotexist", follow_redirects=False,
        )
    assert r.status_code == 404, (
        f"expected 404 for genuinely-missing session, got {r.status_code}"
    )
    assert "Session not found" in r.text
    assert "auto-doesnotexist" in r.text
    # Critical: should NOT be a redirect to /sessions (the broken behaviour).
    assert "location" not in r.headers


def test_route_404_uses_referer_as_back_link(app_client):
    """The 404 page's Back link prefers the Referer header (typically the
    originating source viewer) when present, so the operator has a
    contextual recovery path."""
    with patch(
        "tools.dashboard.server.dashboard_db.get_session",
        return_value=None,
    ), patch(
        "tools.graph.ops.get_session",
        return_value=None,
    ):
        r = app_client.get(
            "/session/auto-missing",
            headers={"Referer": "/graph/abc123-source-id"},
            follow_redirects=False,
        )
    assert r.status_code == 404
    assert 'href="/graph/abc123-source-id"' in r.text


def test_route_graph_lookup_failure_treated_as_miss(app_client):
    """If graph.ops.get_session raises, the route treats it as a miss
    and falls through to the 404 — orientation: a failing graph DB
    must not 500 the dashboard."""
    with patch(
        "tools.dashboard.server.dashboard_db.get_session",
        return_value=None,
    ), patch(
        "tools.graph.ops.get_session",
        side_effect=RuntimeError("graph DB down"),
    ):
        r = app_client.get(
            "/session/auto-graph-down", follow_redirects=False,
        )
    assert r.status_code == 404
    assert "Session not found" in r.text


def test_route_graph_row_without_project_falls_through(app_client):
    """A graph row missing metadata.project hits the 404, not a
    redirect with an empty project segment."""
    graph_row = {
        "id": "src-no-project",
        "type": "session",
        "metadata": {"tmux_session": "auto-noproj"},  # no project
    }
    with patch(
        "tools.dashboard.server.dashboard_db.get_session",
        return_value=None,
    ), patch(
        "tools.graph.ops.get_session",
        return_value=graph_row,
    ):
        r = app_client.get(
            "/session/auto-noproj", follow_redirects=False,
        )
    assert r.status_code == 404
