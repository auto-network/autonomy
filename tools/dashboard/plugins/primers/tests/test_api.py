"""L1 unit tests for the Primers UI plugin backend (bead auto-9fyy0).

Covers the two routes registered under
``tools.dashboard.plugins.primers.entrypoints.api:routes``. The tests
monkeypatch ``agents.workspace_settings.load_workspaces`` and (where
needed) ``agents.primer_renderer.render_workspace_primer`` so the L1
sweep stays hermetic — no real org DBs, no real renderer.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from agents.workspace_settings import RepoMount, WorkspaceV1
from tools.dashboard.plugins.primers.entrypoints import api as primers_api


# ── Fixtures ────────────────────────────────────────────────────────────


def _ws(
    wid: str,
    *,
    name: str = "",
    org: str = "autonomy",
    image: str = "autonomy-agent:test",
    writable: bool = False,
) -> WorkspaceV1:
    """Build a :class:`WorkspaceV1` with the minimum fields the API uses."""
    repos: tuple[RepoMount, ...] = ()
    if writable:
        repos = (RepoMount.from_url(url="git@host:repo.git", mount="/w/r", writable=True),)
    return WorkspaceV1(
        id=wid,
        name=name or wid,
        description="",
        image=image,
        graph_project=org,
        repos=repos,
    )


def _client() -> TestClient:
    """Mount the plugin's Routes on a bare Starlette app for testing."""
    app = Starlette(routes=primers_api.routes)
    return TestClient(app)


# ── Route 1: list workspaces ─────────────────────────────────────────────


def test_list_workspaces_returns_loaded_set():
    """``GET /api/primers/workspaces`` returns one entry per
    ``load_workspaces()`` member with org metadata attached."""
    fake = {
        "autonomy":      _ws("autonomy",      name="Autonomy",       org="autonomy"),
        "enterprise-ng": _ws("enterprise-ng", name="Enterprise NG",  org="anchore",
                             writable=True),
    }
    with patch("agents.workspace_settings.load_workspaces",
               lambda: fake, create=True):
        client = _client()
        resp = client.get("/api/primers/workspaces")

    assert resp.status_code == 200
    data = resp.json()
    rows = data["workspaces"]
    by_id = {r["id"]: r for r in rows}
    assert set(by_id) == {"autonomy", "enterprise-ng"}
    assert by_id["autonomy"]["org"] == "autonomy"
    assert by_id["autonomy"]["name"] == "Autonomy"
    assert by_id["autonomy"]["image"] == "autonomy-agent:test"
    assert by_id["autonomy"]["writable"] is False
    assert by_id["enterprise-ng"]["org"] == "anchore"
    assert by_id["enterprise-ng"]["writable"] is True


def test_list_workspaces_filters_by_caller_org_header():
    """``X-Graph-Org: anchore`` filters the rail to just anchore-owned
    workspaces; the autonomy-owned row is excluded."""
    fake = {
        "autonomy":      _ws("autonomy",      org="autonomy"),
        "enterprise-ng": _ws("enterprise-ng", org="anchore"),
    }
    with patch("agents.workspace_settings.load_workspaces",
               lambda: fake, create=True):
        client = _client()
        resp = client.get(
            "/api/primers/workspaces",
            headers={"X-Graph-Org": "anchore"},
        )

    assert resp.status_code == 200
    rows = resp.json()["workspaces"]
    assert {r["id"] for r in rows} == {"enterprise-ng"}


def test_list_workspaces_handles_load_failure_gracefully():
    """When ``load_workspaces()`` raises, the route returns an empty list
    plus a warning instead of 500ing the page."""
    def _boom():
        raise RuntimeError("malformed Setting row")
    with patch("agents.workspace_settings.load_workspaces",
               _boom, create=True):
        client = _client()
        resp = client.get("/api/primers/workspaces")

    assert resp.status_code == 200
    data = resp.json()
    assert data["workspaces"] == []
    assert "warning" in data


# ── Route 2: render workspace primer ─────────────────────────────────────


def test_render_workspace_primer_round_trip():
    """``GET /api/primers/workspace/{id}`` returns the same markdown a
    direct ``render_workspace_primer`` call produces; ``token_estimate``
    is ``len(markdown) // 4``."""
    ws = _ws("enterprise-ng", name="Enterprise NG", org="anchore")
    fake_markdown = "# Hello\n\nSome workspace primer content. " * 10

    fake = {"enterprise-ng": ws}
    with patch("agents.workspace_settings.load_workspaces",
               lambda: fake, create=True), \
         patch("agents.primer_renderer.render_workspace_primer",
               lambda w: fake_markdown, create=True):
        client = _client()
        resp = client.get("/api/primers/workspace/enterprise-ng")

    assert resp.status_code == 200
    data = resp.json()
    assert data["markdown"] == fake_markdown
    assert data["token_estimate"] == len(fake_markdown) // 4
    assert data["workspace"]["id"] == "enterprise-ng"
    assert data["workspace"]["org"] == "anchore"


def test_render_workspace_404_for_unknown_id():
    """Unknown workspace id returns 404 with a clear error message; the
    renderer is not called."""
    fake: dict = {}
    sentinel: list = []

    def _renderer_should_not_run(_):
        sentinel.append("called")
        return ""

    with patch("agents.workspace_settings.load_workspaces",
               lambda: fake, create=True), \
         patch("agents.primer_renderer.render_workspace_primer",
               _renderer_should_not_run, create=True):
        client = _client()
        resp = client.get("/api/primers/workspace/does-not-exist")

    assert resp.status_code == 404
    body = resp.json()
    assert "error" in body
    assert "does-not-exist" in body["error"]
    assert sentinel == [], "renderer must not be called for unknown id"


def test_render_workspace_caller_org_filter_returns_404():
    """Asking for a workspace owned by a different org through
    ``X-Graph-Org`` returns 404 (same shape as unknown id) so a single
    error path covers both."""
    fake = {"enterprise-ng": _ws("enterprise-ng", org="anchore")}
    with patch("agents.workspace_settings.load_workspaces",
               lambda: fake, create=True):
        client = _client()
        resp = client.get(
            "/api/primers/workspace/enterprise-ng",
            headers={"X-Graph-Org": "autonomy"},
        )

    assert resp.status_code == 404


def test_render_workspace_swallows_renderer_exception():
    """If the renderer raises, the route returns a generic 500
    ``{"error": "rendering failed"}`` — the underlying message is logged
    server-side but never echoed to the browser."""
    ws = _ws("enterprise-ng", org="anchore")
    fake = {"enterprise-ng": ws}

    def _boom(_):
        raise RuntimeError("KeyError('image') — this should not leak")

    with patch("agents.workspace_settings.load_workspaces",
               lambda: fake, create=True), \
         patch("agents.primer_renderer.render_workspace_primer",
               _boom, create=True):
        client = _client()
        resp = client.get("/api/primers/workspace/enterprise-ng")

    assert resp.status_code == 500
    body = resp.json()
    assert body == {"error": "rendering failed"}


# ── Routes registry ──────────────────────────────────────────────────────


def test_routes_export_is_a_list_of_two_routes():
    """The manifest's ``entrypoints.api`` resolves to this attribute;
    the loader insists it be a list."""
    assert isinstance(primers_api.routes, list)
    assert len(primers_api.routes) == 2
    paths = {r.path for r in primers_api.routes}
    assert paths == {
        "/api/primers/workspaces",
        "/api/primers/workspace/{workspace_id}",
    }
