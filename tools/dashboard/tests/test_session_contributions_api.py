"""Portable plugin contributions aggregated for shared session surfaces."""
from __future__ import annotations

from types import SimpleNamespace

from starlette.applications import Starlette
from starlette.testclient import TestClient
from starlette.routing import Route

from tools.dashboard import server
from tools.dashboard.plugin_api.session_contributions import normalize_descriptor


_ICON = '<svg viewBox="0 0 20 20"><circle cx="10" cy="10" r="2"/></svg>'


def test_descriptor_normalizer_namespaces_plugins_and_rejects_external_links():
    normalized = normalize_descriptor("example", "auto-1", {
        "id": "thing",
        "kind": "badge",
        "label": "Research",
        "href": "/example/thing",
        "icon_svg": _ICON,
    })
    assert normalized is not None
    assert normalized["id"] == "example:thing"
    assert normalized["plugin_id"] == "example"
    assert normalized["session_id"] == "auto-1"

    assert normalize_descriptor("example", "auto-1", {
        "id": "outside",
        "label": "Outside",
        "href": "https://example.com",
        "icon_svg": _ICON,
    }) is None


def test_aggregator_calls_only_enabled_plugin_callbacks_and_normalizes(monkeypatch):
    calls = []

    def contribute(session_ids, request):
        calls.append((session_ids, request.method))
        return {
            "auto-a": [{
                "id": "linked",
                "kind": "action",
                "label": "Example",
                "title": "Open example",
                "href": "/example/linked",
                "icon_svg": _ICON,
                "accent": "#60a5fa",
            }],
            "auto-b": [{"id": "invalid"}],
        }

    monkeypatch.setattr(server, "PLUGIN_REGISTRY", [
        SimpleNamespace(id="enabled", session_contributions=contribute),
        SimpleNamespace(id="disabled", session_contributions=lambda *_: {"auto-a": []}),
        SimpleNamespace(id="plain", session_contributions=None),
    ])
    monkeypatch.setattr(
        server,
        "_plugin_enabled_map",
        lambda: {"enabled": True, "disabled": False, "plain": True},
    )
    app = Starlette(routes=[
        Route("/api/session-contributions", server.api_session_contributions, methods=["POST"]),
    ])

    response = TestClient(app).post(
        "/api/session-contributions",
        json={"session_ids": ["auto-a", "auto-b", "auto-a", ""]},
    )

    assert response.status_code == 200
    assert calls == [(["auto-a", "auto-b"], "POST")]
    sessions = response.json()["sessions"]
    assert sessions["auto-a"][0]["id"] == "enabled:linked"
    assert sessions["auto-a"][0]["href"] == "/example/linked"
    assert sessions["auto-b"] == []
