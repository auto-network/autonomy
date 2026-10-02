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
    image: str = "autonomy-session-test",
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


class _ApprovedScope:
    """Stand in for the identity middleware, which these routes depend on.

    ``X-Graph-Org`` is an INPUT to that middleware, never authority on its
    own: it computes the effective organization (a bearer's org wins over a
    conflicting header; a non-org-bound caller selects with the header) and
    stamps it into request state, and
    ``api_auth.organization_scope_from_request`` reads only that. Handlers are
    forbidden from re-parsing headers, which is the plugin spoof 965225e5
    closed.

    A bare app has no middleware, so the approved scope was never set and
    every caller looked unscoped — which is why the two filtering tests here
    saw no filtering. This mimics the middleware's one relevant decision for a
    caller with no bearer, so the routes are exercised against an approved
    scope rather than against a raw header.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or ())
            selected = headers.get(b"x-graph-org")
            scope.setdefault("state", {})["api_organization"] = (
                selected.decode("latin-1") if selected else None
            )
        await self.app(scope, receive, send)


def _client() -> TestClient:
    """Mount the plugin's Routes on a bare Starlette app for testing."""
    app = Starlette(routes=primers_api.routes)
    return TestClient(_ApprovedScope(app))


# ── Route 1: list workspaces ─────────────────────────────────────────────


def test_list_workspaces_returns_loaded_set():
    """``GET /api/primers/workspaces`` returns one entry per
    ``load_workspaces()`` member with org metadata attached."""
    fake = {
        "autonomy":      _ws("autonomy",      name="Autonomy",       org="autonomy"),
        "widgets-ng": _ws("widgets-ng", name="Widgets NG",     org="anchore",
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
    assert set(by_id) == {"autonomy", "widgets-ng"}
    assert by_id["autonomy"]["org"] == "autonomy"
    assert by_id["autonomy"]["name"] == "Autonomy"
    assert by_id["autonomy"]["image"] == "autonomy-session-test"
    assert by_id["autonomy"]["writable"] is False
    assert by_id["widgets-ng"]["org"] == "anchore"
    assert by_id["widgets-ng"]["writable"] is True


def test_list_workspaces_filters_by_caller_org_header():
    """``X-Graph-Org: anchore`` filters the rail to just anchore-owned
    workspaces; the autonomy-owned row is excluded."""
    fake = {
        "autonomy":      _ws("autonomy",      org="autonomy"),
        "widgets-ng": _ws("widgets-ng", org="anchore"),
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
    assert {r["id"] for r in rows} == {"widgets-ng"}


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
    ws = _ws("widgets-ng", name="Widgets NG", org="anchore")
    fake_markdown = "# Hello\n\nSome workspace primer content. " * 10

    fake = {"widgets-ng": ws}
    with patch("agents.workspace_settings.load_workspaces",
               lambda: fake, create=True), \
         patch("agents.primer_renderer.render_workspace_primer",
               lambda w: fake_markdown, create=True):
        client = _client()
        resp = client.get("/api/primers/workspace/widgets-ng")

    assert resp.status_code == 200
    data = resp.json()
    assert data["markdown"] == fake_markdown
    assert data["token_estimate"] == len(fake_markdown) // 4
    assert data["workspace"]["id"] == "widgets-ng"
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
    fake = {"widgets-ng": _ws("widgets-ng", org="anchore")}
    with patch("agents.workspace_settings.load_workspaces",
               lambda: fake, create=True):
        client = _client()
        resp = client.get(
            "/api/primers/workspace/widgets-ng",
            headers={"X-Graph-Org": "autonomy"},
        )

    assert resp.status_code == 404


def test_render_workspace_swallows_renderer_exception():
    """If the renderer raises, the route returns a generic 500
    ``{"error": "rendering failed"}`` — the underlying message is logged
    server-side but never echoed to the browser."""
    ws = _ws("widgets-ng", org="anchore")
    fake = {"widgets-ng": ws}

    def _boom(_):
        raise RuntimeError("KeyError('image') — this should not leak")

    with patch("agents.workspace_settings.load_workspaces",
               lambda: fake, create=True), \
         patch("agents.primer_renderer.render_workspace_primer",
               _boom, create=True):
        client = _client()
        resp = client.get("/api/primers/workspace/widgets-ng")

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


# ── Host terminal (auto-yk9dx) ───────────────────────────────────────────


def test_render_host_terminal_primer_for_an_unscoped_caller():
    """``host`` is the built-in host terminal, not a workspace row: it
    renders through ``render_host_terminal_primer`` without consulting
    ``load_workspaces``."""
    def _no_workspaces():
        raise AssertionError("host must not read workspace rows")

    with patch("agents.workspace_settings.load_workspaces",
               _no_workspaces, create=True), \
         patch("agents.primer_renderer.render_host_terminal_primer",
               lambda: "# Host Terminal primer", create=True):
        resp = _client().get("/api/primers/workspace/host")

    assert resp.status_code == 200
    body = resp.json()
    assert body["markdown"] == "# Host Terminal primer"
    assert body["workspace"]["id"] == "host"
    assert body["workspace"]["org"] == "personal"
    assert body["workspace"]["image"] == "autonomy-host-terminal"


def test_render_host_terminal_primer_hidden_from_an_org_scoped_caller():
    """Caller scope comes from the identity middleware, which the bare test
    app does not run; stub the helper the route reads."""
    with patch("agents.primer_renderer.render_host_terminal_primer",
               lambda: "# Host Terminal primer", create=True), \
         patch.object(primers_api.api_auth, "organization_scope_from_request",
                      lambda request: "autonomy"):
        resp = _client().get("/api/primers/workspace/host")
    assert resp.status_code == 404
