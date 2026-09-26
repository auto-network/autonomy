"""Session output files are frameable by the dashboard itself, nothing else is widened."""

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from tools.dashboard.server import _CSPMiddleware


def _client():
    async def ok(request):
        return PlainTextResponse("x")
    app = Starlette(routes=[Route("/{path:path}", ok)])
    app.add_middleware(_CSPMiddleware)
    return TestClient(app)


def test_session_output_file_may_be_framed_by_the_dashboard_only():
    csp = _client().get("/api/session/auto-x/output/.attachments/1/a.sh").headers["content-security-policy"]
    assert "frame-ancestors 'self'" in csp
    assert "frame-ancestors 'none'" not in csp


def test_other_session_routes_and_pages_still_refuse_framing():
    client = _client()
    for path in ("/api/session/auto-x/tail", "/api/session/auto-x", "/sessions", "/api/session/x/outputs/y"):
        assert "frame-ancestors 'none'" in client.get(path).headers["content-security-policy"], path
