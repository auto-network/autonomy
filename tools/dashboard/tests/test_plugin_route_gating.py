"""Plugin API routes are enablement-gated per request (route_policy.
gate_plugin_enabled): dormant means dormant for the API surface too,
with live-Setting semantics — flipping the map changes behavior with
no restart. Non-API routes pass through untouched.
"""
from __future__ import annotations

from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from tools.dashboard import route_policy


def _app(state: dict):
    async def api(request):
        return JSONResponse({"hello": True})

    async def page(request):
        return PlainTextResponse("page")

    routes = route_policy.gate_plugin_enabled(
        "toy",
        [Route("/api/toy/x", api, methods=["GET"]),
         Route("/toy-page", page, methods=["GET"])],
        lambda: dict(state))
    return Starlette(routes=routes)


def test_enabled_serves_disabled_404s_live():
    state = {"toy": True}
    client = TestClient(_app(state))
    assert client.get("/api/toy/x").status_code == 200
    state["toy"] = False                      # no restart
    assert client.get("/api/toy/x").status_code == 404
    state["toy"] = True
    assert client.get("/api/toy/x").status_code == 200


def test_unknown_plugin_defaults_closed_and_pages_untouched():
    client = TestClient(_app({}))
    assert client.get("/api/toy/x").status_code == 404
    assert client.get("/toy-page").status_code == 200
