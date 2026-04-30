"""L1 unit tests for the Settings UI plugin manifest (bead auto-yurkd).

The substrate's ``test_plugin_substrate.py`` covers the loader's
discovery + enable-filter machinery. This file pins the manifest's
shape so a regression in ``PluginManifest`` (added required field,
renamed key) trips here before the plugin's behavioral tests fire up
a uvicorn server.
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

    assert manifest.id == "settings"
    assert manifest.api_version == 1
    # Operator's primary org-DB hosts canonical dashboard.plugin#1 +
    # autonomy.org rows, so the install scope must default there.
    assert manifest.org == "autonomy"
    assert manifest.paths == ["/settings"]
    assert manifest.assets.template == "page.html"
    assert manifest.assets.script == "page.js"
    assert manifest.assets.style == "page.css"
    assert manifest.nav.label == "Settings"
    assert manifest.frontend.alpine_root == "settingsPage"


def test_manifest_default_enabled_omitted_so_plugin_boots_enabled() -> None:
    """Spec: ``default_enabled`` is OMITTED — Settings is the operator's
    primary tool for managing plugins, so it must come up enabled out
    of the box without needing a CLI bootstrap.

    The loader's bootstrap fallback (directory not starting with ``_``)
    then resolves to enabled.
    """
    raw = yaml.safe_load(_MANIFEST_PATH.read_text())
    assert "default_enabled" not in raw, (
        "Settings plugin must not declare default_enabled — operator "
        "would have to bootstrap via CLI to access the very UI that "
        "manages plugins."
    )
    manifest = PluginManifest.model_validate(raw)
    assert manifest.default_enabled is None
