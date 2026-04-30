"""L1 unit tests for the dashboard plugin substrate.

Covers manifest validation, plugin discovery, Setting-driven enable
filtering, and entrypoint resolution. See bead auto-a79f6 + design note
graph://f77a5415-04f.
"""
from __future__ import annotations

import logging
import sys
import textwrap
from pathlib import Path

import pytest
from starlette.routing import Route

from tools.dashboard.plugin_api import loader
from tools.dashboard.plugin_api.manifest import PluginManifest


# ── Test fixtures: routes used as entrypoint targets ─────────────────


async def _fake_handler(request):  # pragma: no cover — never called
    from starlette.responses import JSONResponse
    return JSONResponse({})


_FAKE_ROUTES = [Route("/api/_test_plugin/ping", _fake_handler)]


def _fake_badge_counter() -> int:  # pragma: no cover — never called
    return 0


# ── Helpers ──────────────────────────────────────────────────────────


_MIN_MANIFEST_FIELDS = {
    "id": "foo",
    "api_version": 1,
    "paths": ["/foo"],
    "assets": {"template": "page.html", "script": "page.js"},
    "nav": {"label": "Foo"},
    "frontend": {"alpine_root": "fooPage"},
}


def _min_manifest_yaml(plugin_id: str = "foo") -> str:
    return textwrap.dedent(f"""
        id: {plugin_id}
        api_version: 1
        paths:
          - /{plugin_id}
        assets:
          template: page.html
          script: page.js
        nav:
          label: {plugin_id.capitalize()}
        frontend:
          alpine_root: {plugin_id}Page
    """).lstrip()


def _write_plugin(plugins_dir: Path, dir_name: str, manifest_yaml: str) -> Path:
    pdir = plugins_dir / dir_name
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "plugin.yaml").write_text(manifest_yaml)
    (pdir / "page.html").write_text(
        '<div x-data="fooPage()" data-testid="foo-fragment-root">x</div>'
    )
    (pdir / "page.js").write_text(
        "Alpine.data('fooPage', () => ({ init() {} }));"
    )
    return pdir


# ── Manifest validation ──────────────────────────────────────────────


def test_manifest_validates_required_fields():
    """PluginManifest rejects manifests missing any required field."""
    PluginManifest.model_validate(_MIN_MANIFEST_FIELDS)

    for key in ("id", "api_version", "paths", "assets", "nav", "frontend"):
        bad = {k: v for k, v in _MIN_MANIFEST_FIELDS.items() if k != key}
        with pytest.raises(Exception):
            PluginManifest.model_validate(bad)

    # Nested required fields under assets / nav / frontend
    bad = {**_MIN_MANIFEST_FIELDS, "assets": {"script": "page.js"}}
    with pytest.raises(Exception):
        PluginManifest.model_validate(bad)
    bad = {**_MIN_MANIFEST_FIELDS, "assets": {"template": "page.html"}}
    with pytest.raises(Exception):
        PluginManifest.model_validate(bad)
    bad = {**_MIN_MANIFEST_FIELDS, "nav": {}}
    with pytest.raises(Exception):
        PluginManifest.model_validate(bad)
    bad = {**_MIN_MANIFEST_FIELDS, "frontend": {}}
    with pytest.raises(Exception):
        PluginManifest.model_validate(bad)


def test_manifest_entrypoints_all_optional():
    """Manifest with entrypoints: {} or omitted validates; each entrypoint
    field is optional independently.
    """
    PluginManifest.model_validate(_MIN_MANIFEST_FIELDS)
    PluginManifest.model_validate({**_MIN_MANIFEST_FIELDS, "entrypoints": {}})
    PluginManifest.model_validate({
        **_MIN_MANIFEST_FIELDS,
        "entrypoints": {"api": "x.y:routes"},
    })
    PluginManifest.model_validate({
        **_MIN_MANIFEST_FIELDS,
        "entrypoints": {"badge_counter": "x.y:count"},
    })
    PluginManifest.model_validate({
        **_MIN_MANIFEST_FIELDS,
        "entrypoints": {"schemas": ["x.y:S1", "x.y:S2"]},
    })


# ── Discovery / validation ───────────────────────────────────────────


def test_loader_skips_invalid_yaml(tmp_path, caplog):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    # malformed YAML — colons piled up so PyYAML raises
    bad_dir = plugins_dir / "broken"
    bad_dir.mkdir()
    (bad_dir / "plugin.yaml").write_text("not: valid: yaml: ::: [")
    _write_plugin(plugins_dir, "good", _min_manifest_yaml("good"))

    with caplog.at_level(logging.WARNING):
        discovered = loader.discover(plugins_dir=plugins_dir)

    ids = {d.manifest.id for d in discovered}
    assert "good" in ids
    assert "broken" not in ids
    assert any("broken" in record.getMessage() for record in caplog.records)


def test_loader_skips_unsupported_api_version(tmp_path, caplog):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    bad_yaml = _min_manifest_yaml("bad").replace("api_version: 1", "api_version: 999")
    _write_plugin(plugins_dir, "bad", bad_yaml)
    _write_plugin(plugins_dir, "good", _min_manifest_yaml("good"))

    with caplog.at_level(logging.WARNING):
        discovered = loader.discover(plugins_dir=plugins_dir)

    ids = {d.manifest.id for d in discovered}
    assert ids == {"good"}
    assert any("999" in record.getMessage() or "api_version" in record.getMessage()
               for record in caplog.records)


def test_loader_rejects_duplicate_ids(tmp_path, caplog):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    yaml1 = _min_manifest_yaml("dup")
    yaml2 = _min_manifest_yaml("dup")
    _write_plugin(plugins_dir, "dir-a", yaml1)
    _write_plugin(plugins_dir, "dir-b", yaml2)
    _write_plugin(plugins_dir, "ok", _min_manifest_yaml("ok"))

    with caplog.at_level(logging.WARNING):
        discovered = loader.discover(plugins_dir=plugins_dir)

    ids = {d.manifest.id for d in discovered}
    assert ids == {"ok"}
    assert any("dup" in record.getMessage() for record in caplog.records)


# ── Entrypoint resolution ────────────────────────────────────────────


def test_loader_resolves_entrypoints(tmp_path, monkeypatch):
    """A plugin with entrypoints.api set has its `routes` populated with
    the resolved Route list after load_enabled.
    """
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    yaml = _min_manifest_yaml("withapi") + (
        "entrypoints:\n"
        "  api: tools.dashboard.tests.test_plugin_substrate:_FAKE_ROUTES\n"
    )
    _write_plugin(plugins_dir, "withapi", yaml)

    monkeypatch.setattr(loader, "_read_plugin_settings", lambda org=None: {})
    loaded = loader.load_enabled(plugins_dir=plugins_dir)

    by_id = {p.id: p for p in loaded}
    assert "withapi" in by_id
    # Loader returns the resolved list — same length and Route paths as
    # the entrypoint target. (Object identity check is fragile under
    # pytest-xdist module re-imports.)
    routes = by_id["withapi"].routes
    assert len(routes) == 1
    assert routes[0].path == "/api/_test_plugin/ping"


def test_loader_handles_entrypoint_import_error(tmp_path, monkeypatch, caplog):
    """A manifest pointing at a missing module is downgraded to disabled +
    logged; siblings still load.
    """
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    bad_yaml = _min_manifest_yaml("bad") + (
        "entrypoints:\n"
        "  api: this.module.does.not.exist:routes\n"
    )
    _write_plugin(plugins_dir, "bad", bad_yaml)
    _write_plugin(plugins_dir, "good", _min_manifest_yaml("good"))

    monkeypatch.setattr(loader, "_read_plugin_settings", lambda org=None: {})
    with caplog.at_level(logging.WARNING):
        loaded = loader.load_enabled(plugins_dir=plugins_dir)

    ids = {p.id for p in loaded}
    assert "good" in ids
    assert "bad" not in ids
    assert any("bad" in r.getMessage() or "this.module" in r.getMessage()
               for r in caplog.records)


# ── Enable filtering / bootstrap ─────────────────────────────────────


def test_bootstrap_default_enabled(tmp_path, monkeypatch):
    """A plugin with no `dashboard.plugin#1` row loads as enabled (when
    its directory does NOT start with `_`).
    """
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    _write_plugin(plugins_dir, "foo", _min_manifest_yaml("foo"))

    monkeypatch.setattr(loader, "_read_plugin_settings", lambda org=None: {})
    loaded = loader.load_enabled(plugins_dir=plugins_dir)
    assert {p.id for p in loaded} == {"foo"}


def test_setting_disable_excludes_plugin(tmp_path, monkeypatch):
    """`dashboard.plugin#1: {enabled: false}` keyed by plugin id excludes
    the plugin from `load_enabled` output.
    """
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    _write_plugin(plugins_dir, "foo", _min_manifest_yaml("foo"))
    _write_plugin(plugins_dir, "bar", _min_manifest_yaml("bar"))

    monkeypatch.setattr(loader, "_read_plugin_settings",
                        lambda org=None: {"foo": {"enabled": False}})
    loaded = loader.load_enabled(plugins_dir=plugins_dir)
    assert {p.id for p in loaded} == {"bar"}


def test_pure_frontend_plugin_loads(tmp_path, monkeypatch):
    """A plugin with no `entrypoints` block loads cleanly; substrate
    registers page+fragment routes only, no API routes appended.
    """
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    _write_plugin(plugins_dir, "pure", _min_manifest_yaml("pure"))

    monkeypatch.setattr(loader, "_read_plugin_settings", lambda org=None: {})
    loaded = loader.load_enabled(plugins_dir=plugins_dir)
    by_id = {p.id: p for p in loaded}
    assert "pure" in by_id
    assert by_id["pure"].routes == []
    assert by_id["pure"].badge_counter is None
    assert by_id["pure"].schemas == []
