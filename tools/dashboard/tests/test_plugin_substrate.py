"""L1 unit tests for the dashboard plugin substrate.

Covers manifest validation, plugin discovery, Setting-driven enable
filtering, and entrypoint resolution. See bead auto-a79f6 + design note
graph://f77a5415-04f.
"""
from __future__ import annotations

import logging
import json
import sys
import textwrap
from pathlib import Path

import pytest
from starlette.routing import Route

from tools.dashboard.plugin_api import loader
from tools.dashboard.plugin_api.manifest import PluginManifest
from tools.dashboard.plugin_api.schema import PLUGIN_OWNED_SETTING_SET_ID
from tools.graph import ops as graph_ops
from tools.graph import org_ops


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
    "org": "autonomy",
    "paths": ["/foo"],
    "assets": {"template": "page.html", "script": "page.js"},
    "nav": {"label": "Foo"},
    "frontend": {"alpine_root": "fooPage"},
}


def _min_manifest_yaml(plugin_id: str = "foo", org: str = "autonomy") -> str:
    return textwrap.dedent(f"""
        id: {plugin_id}
        api_version: 1
        org: {org}
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


def test_manifest_accepts_a_strict_capability_projection_link():
    payload = {
        **_MIN_MANIFEST_FIELDS,
        "capability": {
            "contract": "test_execution",
            "implementation": "autonomy/agent-test",
        },
    }
    manifest = PluginManifest.model_validate(payload)
    assert manifest.capability.contract == "test_execution"
    assert manifest.capability.implementation == "autonomy/agent-test"


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


def _write_setting_plugin(
    plugins_dir: Path,
    *,
    payload: dict,
    plugin_id: str = "settingplug",
) -> Path:
    yaml = _min_manifest_yaml(plugin_id) + textwrap.dedent("""
        settings:
          - set_id: dashboard.agent-actions
            schema_revision: 2
            key: design.refresh-preview
            state: canonical
            payload_file: agent_actions/refresh_preview.json
            uninstall: deprecate_if_unchanged
    """)
    pdir = _write_plugin(plugins_dir, plugin_id, yaml)
    action_dir = pdir / "agent_actions"
    action_dir.mkdir()
    (action_dir / "refresh_preview.json").write_text(json.dumps(payload))
    return pdir


def _agent_action_payload(label: str = "Refresh Preview & Summary") -> dict:
    return {
        "asset_type": "design",
        "label": label,
        "icon": "wand",
        "model": "claude-haiku-4-5-20251001",
        "prompt_template": "Design: {design[design_id]}\\nRevision: {asset[id]}\\n",
        "estimated_seconds": 30,
        "writes": ["design.thumbnail", "design.description"],
    }


@pytest.fixture
def org_graph(tmp_path, monkeypatch):
    orgs_dir = tmp_path / "orgs"
    orgs_dir.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    org_ops.create_org(
        "autonomy",
        type_="shared",
        identity_payload={"name": "Autonomy"},
        root=orgs_dir,
    )
    return orgs_dir


# ── Manifest validation ──────────────────────────────────────────────


def test_manifest_validates_required_fields():
    """PluginManifest rejects manifests missing any required field."""
    PluginManifest.model_validate(_MIN_MANIFEST_FIELDS)

    for key in ("id", "api_version", "org", "paths", "assets", "nav", "frontend"):
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


def test_plugin_asset_rev_changes_when_page_assets_change(tmp_path, monkeypatch):
    """The `/api/plugins` contract needs a cheap stable token so the SPA can
    reload page assets after live deploys instead of reusing stale JS.
    """
    from tools.dashboard.server import _plugin_asset_rev

    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    pdir = _write_plugin(plugins_dir, "foo", _min_manifest_yaml("foo"))

    monkeypatch.setattr(loader, "_read_plugin_settings", lambda org=None: {})
    [plugin] = loader.load_enabled(plugins_dir=plugins_dir)
    before = _plugin_asset_rev(plugin)

    page_js = pdir / "page.js"
    page_js.write_text(page_js.read_text() + "\n// rev bump\n")

    after = _plugin_asset_rev(plugin)
    assert before
    assert after
    assert before != after


def test_api_plugins_has_style_reflects_declared_style_asset(tmp_path, monkeypatch):
    """`/api/plugins`'s `has_style` field is what the SPA shell
    (`refreshPlugins()` in app.js) reads to decide whether to inject a
    `<link rel="stylesheet">` for a plugin's `page.css` — until this
    field existed, no plugin's page.css was ever linked into the page
    at all (declared in the manifest, served as a static file, but
    nothing consumed it). Regression coverage for that gap: a plugin
    with no `style` in its manifest must not claim one.
    """
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    _write_plugin(plugins_dir, "nostyle", _min_manifest_yaml("nostyle"))

    styled_yaml = _min_manifest_yaml("styled").replace(
        "  script: page.js\n", "  script: page.js\n  style: page.css\n",
    )
    styled_dir = _write_plugin(plugins_dir, "styled", styled_yaml)
    (styled_dir / "page.css").write_text(".styled-plugin { color: red; }")

    monkeypatch.setattr(loader, "_read_plugin_settings", lambda org=None: {})
    loaded = {p.id: p for p in loader.load_enabled(plugins_dir=plugins_dir)}

    assert bool(loaded["nostyle"].style) is False
    assert bool(loaded["styled"].style) is True


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


def test_manifest_default_enabled_false_ships_dormant(tmp_path, monkeypatch):
    """``default_enabled: false`` in the manifest disables the plugin
    until an operator flips the dashboard.plugin#1 row, even when the
    directory name doesn't follow the ``_``-prefix sample convention.
    """
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    yaml = _min_manifest_yaml("dormant") + "default_enabled: false\n"
    _write_plugin(plugins_dir, "dormant", yaml)
    _write_plugin(plugins_dir, "ordinary", _min_manifest_yaml("ordinary"))

    # No Setting rows → bootstrap defaults run.
    monkeypatch.setattr(loader, "_read_plugin_settings", lambda org=None: {})
    loaded = loader.load_enabled(plugins_dir=plugins_dir)
    ids = {p.id for p in loaded}
    assert ids == {"ordinary"}, (
        f"default_enabled=false plugin should be dormant; got {ids}"
    )

    # An explicit enable flips it on.
    monkeypatch.setattr(
        loader,
        "_read_plugin_settings",
        lambda org=None: {"dormant": {"enabled": True}},
    )
    loaded = loader.load_enabled(plugins_dir=plugins_dir)
    ids = {p.id for p in loaded}
    assert ids == {"dormant", "ordinary"}


def test_manifest_default_enabled_true_overrides_underscore_dir(tmp_path, monkeypatch):
    """An ``_``-prefixed directory whose manifest declares
    ``default_enabled: true`` boots enabled — the explicit field wins
    over the directory-name fallback.
    """
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    yaml = _min_manifest_yaml("active") + "default_enabled: true\n"
    _write_plugin(plugins_dir, "_active", yaml)

    monkeypatch.setattr(loader, "_read_plugin_settings", lambda org=None: {})
    loaded = loader.load_enabled(plugins_dir=plugins_dir)
    assert {p.id for p in loaded} == {"active"}


# ── Plugin install-org scoping (substrate v1.1) ──────────────────────


def test_manifest_requires_org_field():
    """``PluginManifest`` rejects manifests missing the ``org`` field.

    The manifest's ``org`` declares the install scope — the org-DB
    where the plugin's ``dashboard.plugin#1`` toggle row lives.
    """
    bad = {k: v for k, v in _MIN_MANIFEST_FIELDS.items() if k != "org"}
    with pytest.raises(Exception):
        PluginManifest.model_validate(bad)


def test_loader_reads_each_plugin_org_independently(tmp_path, monkeypatch):
    """Two plugins with different manifest orgs each read their toggle
    row from their own org-DB — never from a sibling plugin's org.
    """
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    _write_plugin(plugins_dir, "alpha", _min_manifest_yaml("alpha", org="autonomy"))
    _write_plugin(plugins_dir, "bravo", _min_manifest_yaml("bravo", org="anchore"))

    calls: list[tuple[str, str | None]] = []

    def fake_read_plugin_settings(org=None):
        calls.append(("_read_plugin_settings", org))
        if org == "autonomy":
            return {"alpha": {"enabled": True}}
        if org == "anchore":
            return {"bravo": {"enabled": True}}
        return {}

    monkeypatch.setattr(loader, "_read_plugin_settings", fake_read_plugin_settings)

    loaded = loader.load_enabled(plugins_dir=plugins_dir)
    by_id = {p.id: p for p in loaded}
    assert set(by_id) == {"alpha", "bravo"}

    requested_orgs = {org for _, org in calls}
    assert "autonomy" in requested_orgs
    assert "anchore" in requested_orgs
    # The unscoped sweep that caused the original bug must not happen.
    assert None not in requested_orgs, (
        f"_read_plugin_settings was called with org=None: {calls}"
    )


def test_loader_handles_missing_org_db(tmp_path, monkeypatch):
    """Manifest declaring an org with no DB / no rows falls through to
    ``default_enabled`` (existing v1 bootstrap rule).
    """
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    _write_plugin(plugins_dir, "ghost", _min_manifest_yaml("ghost", org="nonexistent"))

    # ``read_set`` against a missing org-DB returns no rows → empty dict.
    monkeypatch.setattr(loader, "_read_plugin_settings", lambda org=None: {})

    loaded = loader.load_enabled(plugins_dir=plugins_dir)
    # Bootstrap default (dir name does not start with ``_``) → enabled.
    assert {p.id for p in loaded} == {"ghost"}


def test_setting_payload_org_overrides_manifest_org(tmp_path, monkeypatch):
    """Toggle row ``{enabled: true, org: anchore}`` causes
    ``LoadedPlugin.effective_org`` to be ``anchore`` regardless of
    ``manifest.org: autonomy``.
    """
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    _write_plugin(plugins_dir, "switched", _min_manifest_yaml("switched", org="autonomy"))

    def fake_settings(org=None):
        if org == "autonomy":
            return {"switched": {"enabled": True, "org": "anchore"}}
        return {}

    monkeypatch.setattr(loader, "_read_plugin_settings", fake_settings)
    loaded = loader.load_enabled(plugins_dir=plugins_dir)
    by_id = {p.id: p for p in loaded}
    assert by_id["switched"].effective_org == "anchore"


def test_setting_without_org_keeps_manifest_org(tmp_path, monkeypatch):
    """Toggle row ``{enabled: true}`` (no ``org`` key) keeps
    ``effective_org = manifest.org``.
    """
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    _write_plugin(plugins_dir, "stable", _min_manifest_yaml("stable", org="autonomy"))

    def fake_settings(org=None):
        if org == "autonomy":
            return {"stable": {"enabled": True}}
        return {}

    monkeypatch.setattr(loader, "_read_plugin_settings", fake_settings)
    loaded = loader.load_enabled(plugins_dir=plugins_dir)
    by_id = {p.id: p for p in loaded}
    assert by_id["stable"].effective_org == "autonomy"


# ── Plugin-owned graph Settings ─────────────────────────────────────


def test_plugin_declared_setting_installs_and_tracks_owner(tmp_path, org_graph):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    _write_setting_plugin(plugins_dir, payload=_agent_action_payload())

    loaded = loader.load_all(plugins_dir=plugins_dir)
    results = loader.reconcile_declared_settings(loaded)

    assert results[0]["action"] == "installed"
    action = graph_ops.read_set(
        "dashboard.agent-actions",
        org="autonomy",
        peers=[],
    ).to_dict()["design.refresh-preview"]
    assert action.payload["asset_type"] == "design"

    owner = graph_ops.read_set(
        PLUGIN_OWNED_SETTING_SET_ID,
        org="autonomy",
        peers=[],
    ).to_dict()["settingplug:dashboard.agent-actions#2:design.refresh-preview"]
    assert owner.payload["plugin_id"] == "settingplug"
    assert owner.payload["status"] == "managed"
    assert owner.payload["setting_id"] == action.id
    assert owner.payload["installed_payload_hash"]


def test_plugin_declared_setting_update_preserves_ownership(tmp_path, org_graph):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    pdir = _write_setting_plugin(
        plugins_dir,
        payload=_agent_action_payload("Original Primer"),
    )
    loaded = loader.load_all(plugins_dir=plugins_dir)
    loader.reconcile_declared_settings(loaded)

    new_payload = _agent_action_payload("Updated Primer")
    (pdir / "agent_actions" / "refresh_preview.json").write_text(json.dumps(new_payload))
    loaded = loader.load_all(plugins_dir=plugins_dir)
    results = loader.reconcile_declared_settings(loaded)

    assert results[0]["action"] == "updated"
    action = graph_ops.read_set(
        "dashboard.agent-actions",
        org="autonomy",
        peers=[],
    ).to_dict()["design.refresh-preview"]
    assert action.payload["label"] == "Updated Primer"
    owner = graph_ops.read_set(
        PLUGIN_OWNED_SETTING_SET_ID,
        org="autonomy",
        peers=[],
    ).to_dict()["settingplug:dashboard.agent-actions#2:design.refresh-preview"]
    assert owner.payload["status"] == "managed"
    assert owner.payload["plugin_payload_hash"] == owner.payload["installed_payload_hash"]


def test_plugin_declared_setting_drift_is_not_overwritten(tmp_path, org_graph):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    pdir = _write_setting_plugin(
        plugins_dir,
        payload=_agent_action_payload("Plugin Primer"),
    )
    loaded = loader.load_all(plugins_dir=plugins_dir)
    loader.reconcile_declared_settings(loaded)

    operator_payload = _agent_action_payload("Operator Primer")
    graph_ops.upsert_by_key(
        "dashboard.agent-actions",
        2,
        "design.refresh-preview",
        operator_payload,
        state="canonical",
        org="autonomy",
    )
    plugin_update = _agent_action_payload("Plugin Update")
    (pdir / "agent_actions" / "refresh_preview.json").write_text(json.dumps(plugin_update))
    loaded = loader.load_all(plugins_dir=plugins_dir)
    results = loader.reconcile_declared_settings(loaded)

    assert results[0]["action"] == "drifted"
    action = graph_ops.read_set(
        "dashboard.agent-actions",
        org="autonomy",
        peers=[],
    ).to_dict()["design.refresh-preview"]
    assert action.payload["label"] == "Operator Primer"
    owner = graph_ops.read_set(
        PLUGIN_OWNED_SETTING_SET_ID,
        org="autonomy",
        peers=[],
    ).to_dict()["settingplug:dashboard.agent-actions#2:design.refresh-preview"]
    assert owner.payload["status"] == "drifted"


def test_plugin_declared_setting_force_overwrites_drift(tmp_path, org_graph):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    pdir = _write_setting_plugin(
        plugins_dir,
        payload=_agent_action_payload("Plugin Primer"),
    )
    loaded = loader.load_all(plugins_dir=plugins_dir)
    loader.reconcile_declared_settings(loaded)

    operator_payload = _agent_action_payload("Operator Primer")
    graph_ops.upsert_by_key(
        "dashboard.agent-actions",
        2,
        "design.refresh-preview",
        operator_payload,
        state="canonical",
        org="autonomy",
    )
    plugin_update = _agent_action_payload("Plugin Update")
    (pdir / "agent_actions" / "refresh_preview.json").write_text(json.dumps(plugin_update))
    loaded = loader.load_all(plugins_dir=plugins_dir)
    results = loader.reconcile_declared_settings(loaded, force=True)

    assert results[0]["action"] == "forced"
    action = graph_ops.read_set(
        "dashboard.agent-actions",
        org="autonomy",
        peers=[],
    ).to_dict()["design.refresh-preview"]
    assert action.payload["label"] == "Plugin Update"
    owner = graph_ops.read_set(
        PLUGIN_OWNED_SETTING_SET_ID,
        org="autonomy",
        peers=[],
    ).to_dict()["settingplug:dashboard.agent-actions#2:design.refresh-preview"]
    assert owner.payload["status"] == "managed"
    assert owner.payload["plugin_payload_hash"] == owner.payload["installed_payload_hash"]
    assert owner.payload["current_payload_hash"] == owner.payload["installed_payload_hash"]


def test_plugin_declared_setting_disabled_deprecates_unchanged_row(
    tmp_path,
    org_graph,
):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    _write_setting_plugin(plugins_dir, payload=_agent_action_payload())
    loaded = loader.load_all(plugins_dir=plugins_dir)
    loader.reconcile_declared_settings(loaded)

    graph_ops.upsert_by_key(
        "dashboard.plugin",
        1,
        "settingplug",
        {"enabled": False},
        state="canonical",
        org="autonomy",
    )
    results = loader.reconcile_declared_settings(loaded)

    assert results[0]["action"] == "deprecated"
    assert "design.refresh-preview" not in graph_ops.read_set(
        "dashboard.agent-actions",
        org="autonomy",
        peers=[],
    ).to_dict()
    owner = graph_ops.read_set(
        PLUGIN_OWNED_SETTING_SET_ID,
        org="autonomy",
        peers=[],
    ).to_dict()["settingplug:dashboard.agent-actions#2:design.refresh-preview"]
    assert owner.payload["status"] == "uninstalled"
