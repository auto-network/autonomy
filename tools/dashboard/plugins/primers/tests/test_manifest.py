"""L1 unit tests for the Primers UI plugin manifest (bead auto-9fyy0).

Pins the manifest's shape so a regression in ``PluginManifest`` (added
required field, renamed key) trips here before the plugin's behavioral
tests fire up a uvicorn server.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from tools.dashboard.plugin_api.manifest import PluginManifest


_MANIFEST_PATH = (
    Path(__file__).resolve().parent.parent / "plugin.yaml"
)


def test_manifest_validates() -> None:
    """``plugin.yaml`` parses + passes ``PluginManifest`` validation."""
    raw = yaml.safe_load(_MANIFEST_PATH.read_text())
    manifest = PluginManifest.model_validate(raw)

    assert manifest.id == "primers"
    assert manifest.api_version == 1
    assert manifest.org == "autonomy"
    assert manifest.paths == ["/primers"]
    assert manifest.assets.template == "page.html"
    assert manifest.assets.script == "page.js"
    assert manifest.assets.style == "page.css"
    assert manifest.nav.label == "Primers"
    assert manifest.frontend.alpine_root == "primersPage"
    assert manifest.entrypoints.api == (
        "tools.dashboard.plugins.primers.entrypoints.api:routes"
    )


def test_manifest_default_enabled_omitted_so_plugin_boots_enabled() -> None:
    """Spec: ``default_enabled`` is OMITTED — Primers ships visible from
    day one. The loader's bootstrap fallback (directory not starting
    with ``_``) then resolves to enabled.
    """
    raw = yaml.safe_load(_MANIFEST_PATH.read_text())
    assert "default_enabled" not in raw, (
        "Primers plugin must not declare default_enabled — operators' "
        "primary use case is 'show me what agents see' which isn't "
        "gated on opt-in."
    )
    manifest = PluginManifest.model_validate(raw)
    assert manifest.default_enabled is None
