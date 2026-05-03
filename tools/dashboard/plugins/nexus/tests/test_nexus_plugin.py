"""L1 unit tests for the Settings Nexus plugin (bead auto-ct3ey).

Covers:

* Manifest validation — the plugin.yaml round-trips through the
  substrate's PluginManifest model, ships dormant
  (``default_enabled: false``), and declares both schemas.
* Schema validation — payload acceptance + rejection for
  ``NexusSceneV1`` (singleton) and ``NexusTileV1`` (keyed).
* Plugin discovery — the substrate's loader picks the plugin up.
* Page render — the page.html fragment carries the expected
  testids that downstream behavioral tests can target.

Browser-driven L2 tests against the rendered timeline live alongside
the plugin in ``test_nexus_page.py``.

The tests live with the plugin per the structural rule (comment
1ee79942 on graph://f6c6c43e-24a). Run explicitly with::

    pytest tools/dashboard/plugins/nexus/tests/
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tools.dashboard.plugin_api import loader
from tools.dashboard.plugin_api.manifest import PluginManifest
from tools.dashboard.plugins.nexus.entrypoints import schemas as nexus_schemas
from tools.graph.schemas.registry import (
    SchemaValidationError,
    get_schema,
)


PLUGIN_DIR = (
    Path(__file__).resolve().parents[1]
)


# ── Manifest ─────────────────────────────────────────────────────────


def test_manifest_yaml_parses_and_validates():
    """plugin.yaml round-trips through the substrate's PluginManifest."""
    manifest_path = PLUGIN_DIR / "plugin.yaml"
    raw = yaml.safe_load(manifest_path.read_text())
    manifest = PluginManifest.model_validate(raw)
    assert manifest.id == "nexus"
    assert manifest.api_version == 1
    assert manifest.org == "autonomy"
    assert manifest.default_enabled is False
    assert manifest.paths == ["/nexus", "/settings-nexus"]
    assert manifest.assets.template == "page.html"
    assert manifest.assets.script == "page.js"
    assert manifest.nav.label == "Nexus"
    assert manifest.frontend.alpine_root == "nexus"
    assert manifest.entrypoints.schemas == [
        "tools.dashboard.plugins.nexus.entrypoints.schemas:NexusSceneV1",
        "tools.dashboard.plugins.nexus.entrypoints.schemas:NexusTileV1",
    ]
    assert manifest.entrypoints.actions in (None, [])


def test_static_files_present():
    """Substrate-required assets exist alongside the manifest."""
    for fname in ("plugin.yaml", "page.html", "page.js"):
        assert (PLUGIN_DIR / fname).is_file(), f"missing plugin asset: {fname}"
    assert (PLUGIN_DIR / "entrypoints" / "schemas.py").is_file()


def test_plugin_discovers_with_real_substrate():
    """The shipped substrate finds the plugin directory."""
    discovered = loader.discover()
    by_id = {d.manifest.id: d for d in discovered}
    assert "nexus" in by_id, (
        "loader.discover() did not pick up the nexus plugin"
    )
    assert by_id["nexus"].plugin_dir == PLUGIN_DIR


def test_load_all_resolves_two_schemas():
    """``load_all`` resolves the plugin with both schemas registered."""
    loaded = loader.load_all()
    by_id = {p.id: p for p in loaded}
    plugin = by_id.get("nexus")
    assert plugin is not None, (
        "load_all() did not include nexus — entrypoint import failed?"
    )
    assert plugin.routes == []
    assert len(plugin.schemas) == 2
    schema_ids = {s.set_id for s in plugin.schemas}
    assert schema_ids == {"dashboard.nexus.scene", "dashboard.nexus.tile"}


def test_schemas_register_into_global_registry():
    """Both schemas land in the global registry at revision 1."""
    scene = get_schema("dashboard.nexus.scene", 1)
    tile = get_schema("dashboard.nexus.tile", 1)
    assert scene is nexus_schemas.NexusSceneV1
    assert tile is nexus_schemas.NexusTileV1


def test_module_exposes_synopsis_above_register_calls():
    """SYNOPSIS dict is defined at module level (pitfall graph://4f142305-6fb)."""
    syn = getattr(nexus_schemas, "SYNOPSIS", None)
    assert isinstance(syn, dict)
    assert syn.get("summary")
    assert isinstance(syn.get("nouns"), list) and syn["nouns"]


def test_scene_has_singleton_access_pattern():
    """NexusSceneV1 carries the @singleton(key='active') stamp."""
    cls = nexus_schemas.NexusSceneV1
    assert cls._access_pattern == "singleton"
    assert cls._key_strategy == "fixed:active"


def test_tile_has_keyed_per_entity_access_pattern():
    """NexusTileV1 carries the @keyed_per_entity stamp."""
    cls = nexus_schemas.NexusTileV1
    assert cls._access_pattern == "keyed_per_entity"


# ── NexusSceneV1 ────────────────────────────────────────────────────


class TestNexusSceneSchema:
    def _validate(self, payload):
        nexus_schemas.NexusSceneV1.validate(payload)

    def test_minimum_payload_passes(self):
        self._validate({"title": "Settings"})

    def test_full_payload_passes(self):
        self._validate({
            "title": "Settings",
            "subtitle": "Substrate done.",
            "anchor_sentence": "Settings is the substrate.",
            "presenter": "ea2cef72-ed4",
            "presenter_label": "Settings Nexus",
            "focus_tile_id": "intro",
            "layout": "stream",
        })

    def test_missing_title_rejected(self):
        with pytest.raises(SchemaValidationError):
            self._validate({"subtitle": "no title here"})

    def test_blank_title_rejected(self):
        with pytest.raises(SchemaValidationError):
            self._validate({"title": "   "})

    def test_title_must_be_string(self):
        with pytest.raises(SchemaValidationError):
            self._validate({"title": 7})

    def test_optional_string_fields_must_be_strings_or_null(self):
        for f in ("subtitle", "anchor_sentence", "presenter",
                  "presenter_label", "focus_tile_id"):
            with pytest.raises(SchemaValidationError):
                self._validate({"title": "Settings", f: 42})
        # null is accepted
        self._validate({"title": "Settings", "subtitle": None})

    def test_layout_must_be_in_enum(self):
        with pytest.raises(SchemaValidationError):
            self._validate({"title": "Settings", "layout": "tilemap"})
        # all enum values pass
        for v in ("spotlight", "grid", "stream"):
            self._validate({"title": "Settings", "layout": v})

    def test_unknown_field_rejected(self):
        with pytest.raises(SchemaValidationError):
            self._validate({"title": "Settings", "unexpected": True})

    def test_non_dict_payload_rejected(self):
        with pytest.raises(SchemaValidationError):
            self._validate("not a dict")
        with pytest.raises(SchemaValidationError):
            self._validate(None)


# ── NexusTileV1 ─────────────────────────────────────────────────────


class TestNexusTileSchema:
    def _validate(self, payload):
        nexus_schemas.NexusTileV1.validate(payload)

    def test_minimum_payload_passes(self):
        self._validate({"kind": "markdown"})

    def test_full_payload_passes(self):
        self._validate({
            "kind": "status",
            "order": 90,
            "width": "third",
            "title": "Right now",
            "body": "first visit",
            "ts": "2026-05-02T18:11:00Z",
            "data": {"state": "running", "label": "Right now",
                     "detail": "bootstrap row"},
        })

    def test_missing_kind_rejected(self):
        with pytest.raises(SchemaValidationError):
            self._validate({"body": "no kind here"})

    def test_blank_kind_rejected(self):
        with pytest.raises(SchemaValidationError):
            self._validate({"kind": ""})

    def test_kind_must_be_in_enum(self):
        with pytest.raises(SchemaValidationError):
            self._validate({"kind": "wishful-thinking"})
        # spot-check enum members from the design fixture's kinds
        for v in ("markdown", "status", "code", "merge",
                  "session-card", "phases"):
            self._validate({"kind": v})

    def test_order_must_be_int(self):
        with pytest.raises(SchemaValidationError):
            self._validate({"kind": "markdown", "order": "first"})
        # bool is rejected because isinstance(True, int) is True in
        # Python — so we accept bools here; a stricter constraint would
        # reject. Document the behaviour.
        self._validate({"kind": "markdown", "order": 0})

    def test_width_must_be_in_enum(self):
        with pytest.raises(SchemaValidationError):
            self._validate({"kind": "markdown", "width": "huge"})
        for v in ("full", "half", "third"):
            self._validate({"kind": "markdown", "width": v})

    def test_string_fields_must_be_strings_or_null(self):
        for f in ("title", "body", "ts"):
            with pytest.raises(SchemaValidationError):
                self._validate({"kind": "markdown", f: 99})
        self._validate({"kind": "markdown", "title": None})

    def test_data_must_be_dict_or_null(self):
        with pytest.raises(SchemaValidationError):
            self._validate({"kind": "markdown", "data": "not a dict"})
        with pytest.raises(SchemaValidationError):
            self._validate({"kind": "markdown", "data": [1, 2, 3]})
        self._validate({"kind": "markdown", "data": {}})
        self._validate({"kind": "markdown", "data": None})

    def test_unknown_field_rejected(self):
        with pytest.raises(SchemaValidationError):
            self._validate({"kind": "markdown", "extra": True})

    def test_non_dict_payload_rejected(self):
        with pytest.raises(SchemaValidationError):
            self._validate(42)


# ── Page fragment ───────────────────────────────────────────────────


class TestNexusPageFragment:
    def test_page_html_carries_expected_testids(self):
        html = (PLUGIN_DIR / "page.html").read_text()
        # Banner + anchor + dimensions
        assert 'data-testid="nx-banner"' in html
        assert 'data-testid="nx-title"' in html
        assert 'data-testid="nx-anchor"' in html
        assert 'data-testid="nx-dimensions"' in html
        # Timeline / tiles
        assert 'data-testid="nx-zone-narrative"' in html
        assert 'data-testid="nx-tiles"' in html
        # Static rail content from the design (v1 hardcoded zones)
        assert 'data-testid="nx-zone-ops"' in html
        assert 'data-testid="nx-zone-roadmap"' in html
        assert 'data-testid="nx-zone-productization"' in html

    def test_page_html_uses_the_alpine_root(self):
        html = (PLUGIN_DIR / "page.html").read_text()
        assert 'x-data="nexus()"' in html

    def test_page_html_no_doctype_or_body(self):
        """page.html is a fragment — no <html>/<head>/<body> wrappers."""
        html = (PLUGIN_DIR / "page.html").read_text().lower()
        assert "<!doctype" not in html
        assert "<body" not in html
        assert "<html" not in html

    def test_page_js_exposes_factory_on_window_and_module(self):
        js = (PLUGIN_DIR / "page.js").read_text()
        assert "window.nexus = nexus" in js
        assert "module.exports" in js

    def test_page_js_binds_both_schemas_through_alpine_runtime(self):
        js = (PLUGIN_DIR / "page.js").read_text()
        assert "Scene: 'dashboard.nexus.scene'" in js
        assert "Tile:  'dashboard.nexus.tile'" in js \
            or "Tile: 'dashboard.nexus.tile'" in js
