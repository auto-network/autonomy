from __future__ import annotations

from pathlib import Path

import yaml

from tools.dashboard.plugin_api.manifest import PluginManifest
from tools.dashboard.plugin_api import loader


PLUGIN_DIR = Path(__file__).resolve().parents[1]


def test_manifest_validates():
    raw = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())
    manifest = PluginManifest.model_validate(raw)
    assert manifest.id == "commit-api"
    assert manifest.api_version == 1
    assert manifest.org == "autonomy"
    assert manifest.default_enabled is False
    assert manifest.paths == ["/commit"]
    assert manifest.assets.template == "page.html"
    assert manifest.assets.script == "page.js"
    assert manifest.nav.label == "Commit API"
    assert manifest.frontend.alpine_root == "commitApiPage"
    assert manifest.entrypoints.api == "tools.dashboard.plugins.commit_api.entrypoints.api:routes"


def test_plugin_discovers_and_loads_routes():
    discovered = loader.discover()
    by_id = {d.manifest.id: d for d in discovered}
    assert "commit-api" in by_id
    loaded = loader.load_all()
    plugin = {p.id: p for p in loaded}["commit-api"]
    assert {route.path for route in plugin.routes} == {
        "/api/capabilities/commit/v1/resolve-policy",
        "/api/capabilities/commit/v1/policy/describe",
        "/api/capabilities/commit/v1/proposals",
        "/api/capabilities/commit/v1/workflows/{workflow_id}/commit",
        "/api/capabilities/commit/v1/workflows/{workflow_id}/signature-request",
        "/api/capabilities/commit/v1/signing-requests/{signing_request_id}/attach",
        "/api/capabilities/commit/v1/workflows/{workflow_id}/publish",
    }
