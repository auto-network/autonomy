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


def test_route_404_back_link_is_fixed_safe_target(app_client):
    """The 404 page's Back link points at a fixed safe destination
    (``/sessions``), not the Referer header. Originally the route used
    the Referer for a contextual back-link, but using attacker-influenced
    header text in an href is a defense-in-depth red flag — and the
    security review for this branch flagged it. Fixed target keeps the
    affordance without the surface."""
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
    assert 'href="/sessions"' in r.text
    # The Referer is NOT reflected anywhere in the body.
    assert "abc123-source-id" not in r.text


def test_route_404_escapes_session_id_against_xss(app_client):
    """auto-0524-000705 security review (HIGH): the 404 branch
    interpolates the URL path parameter ``session_id`` into the response
    HTML. Starlette's default ``str`` path converter accepts arbitrary
    percent-encoded characters including ``<``, ``>``, ``"``. Without
    escaping this is reflected XSS — an attacker visits
    ``/session/%3Cscript%3Ealert(1)%3C%2Fscript%3E`` and the injected
    script runs on the dashboard origin.

    Fix: ``html.escape(session_id)`` before interpolating into the
    response body. This test asserts the fix is in place by sending an
    XSS-shaped session id and verifying the raw `<script>` tag is NOT
    present in the response body."""
    # NOTE: the decoded form must NOT contain a literal '/' or the
    # request matches the two-segment ``/session/{project}/{session_id}``
    # route instead of the single-segment route under test. Use a
    # payload without slashes that still proves the escape contract.
    with patch(
        "tools.dashboard.server.dashboard_db.get_session",
        return_value=None,
    ), patch(
        "tools.graph.ops.get_session",
        return_value=None,
    ):
        r = app_client.get(
            "/session/%3Cscript%3Ealert(1)%3C%21--",  # <script>alert(1)<!--
            follow_redirects=False,
        )
    assert r.status_code == 404, (
        f"expected 404, got {r.status_code} — payload may have matched "
        "a different route"
    )
    # The raw injection MUST NOT appear in the response body.
    assert "<script>alert(1)" not in r.text, (
        "reflected XSS regression — session_id was not HTML-escaped"
    )
    # The escaped form should be there (proves the value reached the
    # template and was escaped rather than dropped or filtered upstream).
    assert "&lt;script&gt;alert(1)" in r.text


def test_route_404_escapes_quotes_in_session_id(app_client):
    """Additional XSS coverage: a session id containing a double quote
    should land as ``&quot;`` in the body, never as a raw ``"`` that
    could break out of an attribute context. The current 404 uses a
    ``<code>`` element rather than an attribute, so quote escaping is
    defense-in-depth, but the contract is "every interpolated value
    is escaped" and worth pinning."""
    with patch(
        "tools.dashboard.server.dashboard_db.get_session",
        return_value=None,
    ), patch(
        "tools.graph.ops.get_session",
        return_value=None,
    ):
        r = app_client.get(
            '/session/%22onerror%3Dalert(1)',  # "onerror=alert(1)
            follow_redirects=False,
        )
    assert r.status_code == 404
    assert '"onerror=alert(1)' not in r.text
    assert "&quot;onerror=alert(1)" in r.text


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
