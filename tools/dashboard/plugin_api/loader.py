"""Plugin discovery, validation, enable filtering, and entrypoint loading.

Two entry points:

* :func:`discover` globs ``plugins/*/plugin.yaml``, validates each, and
  returns a list of :class:`DiscoveredPlugin` (manifest + directory).
  Validation errors are logged and the offending plugin is skipped —
  discovery never raises. Duplicate ids cause both claimants to be
  dropped.

* :func:`load_enabled` discovers, filters by the ``dashboard.plugin#1``
  Setting, then resolves any ``entrypoints.*`` ``module:attr`` strings
  to live Python objects. Import failures downgrade the offending
  plugin to disabled and the substrate keeps booting.

A plugin without a Setting row falls back to a bootstrap rule: plugin
directories whose name starts with ``_`` (e.g. the shipped sample
``_example/``) default to disabled, so they don't appear in production
until an operator opts in. Other plugins default to enabled.
"""
from __future__ import annotations

import importlib
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml
from pydantic import ValidationError

# Importing schema at the top wires ``dashboard.plugin#1`` into the
# registry before the loader's first ``read_set`` call (see "Schema
# registration order" risk in the bead).
from . import schema as _plugin_schema  # noqa: F401 — registers DashboardPluginV1
from .manifest import PluginManifest, SUBSTRATE_API_VERSION
from .schema import PLUGIN_SET_ID


logger = logging.getLogger(__name__)


DEFAULT_PLUGINS_DIR = Path(__file__).resolve().parent.parent / "plugins"


@dataclass
class DiscoveredPlugin:
    """A validated manifest paired with the directory it came from."""
    manifest: PluginManifest
    plugin_dir: Path


@dataclass
class LoadedPlugin:
    """A plugin that survived enable filtering + entrypoint resolution.

    ``routes`` / ``badge_counter`` / ``session_contributions`` / ``schemas``
    are populated only when
    the corresponding entrypoint is declared and imported successfully.

    ``effective_org`` is the org slug the substrate uses to scope this
    plugin's runtime: the manifest's ``org`` by default, overridden by
    the toggle row's ``payload.org`` when present. ``load_all`` (which
    skips Setting reads) leaves this equal to ``manifest.org``;
    ``load_enabled`` resolves the override.
    """
    id: str
    paths: list[str]
    nav_label: str
    alpine_root: str
    template: str
    script: str
    style: str | None
    plugin_dir: Path
    manifest: PluginManifest
    effective_org: str = ""
    routes: list = field(default_factory=list)
    badge_counter: Callable[[], Any] | None = None
    session_contributions: Callable[[list[str], Any], Any] | None = None
    #: Callable returning coroutine factories; lifecycle belongs to the
    #: PluginBackgroundSupervisor (plugin_api.background), never the plugin.
    background: Callable[[], Any] | None = None
    schemas: list = field(default_factory=list)
    actions: list[str] = field(default_factory=list)


# ── Discovery ────────────────────────────────────────────────────────


def discover(plugins_dir: Path | None = None) -> list[DiscoveredPlugin]:
    """Glob plugin manifests, validate each, return a list of survivors.

    Validation errors and duplicate ids are logged at WARNING; the
    offending plugin(s) are dropped. The substrate keeps booting in all
    failure modes.
    """
    base = Path(plugins_dir) if plugins_dir is not None else DEFAULT_PLUGINS_DIR
    if not base.exists():
        return []

    candidates: list[DiscoveredPlugin] = []
    seen_ids: dict[str, list[Path]] = {}

    for manifest_path in sorted(base.glob("*/plugin.yaml")):
        plugin_dir = manifest_path.parent
        try:
            with manifest_path.open() as f:
                raw = yaml.safe_load(f)
        except yaml.YAMLError as exc:
            logger.warning(
                "[plugin_loader] skipping %s: invalid YAML — %s",
                plugin_dir.name, exc,
            )
            continue
        except OSError as exc:
            logger.warning(
                "[plugin_loader] skipping %s: cannot read plugin.yaml — %s",
                plugin_dir.name, exc,
            )
            continue

        if not isinstance(raw, dict):
            logger.warning(
                "[plugin_loader] skipping %s: plugin.yaml is not a mapping",
                plugin_dir.name,
            )
            continue

        try:
            manifest = PluginManifest.model_validate(raw)
        except ValidationError as exc:
            logger.warning(
                "[plugin_loader] skipping %s: manifest validation failed — %s",
                plugin_dir.name, exc,
            )
            continue

        if manifest.api_version != SUBSTRATE_API_VERSION:
            logger.warning(
                "[plugin_loader] skipping %s: api_version %d "
                "(substrate supports %d)",
                plugin_dir.name, manifest.api_version, SUBSTRATE_API_VERSION,
            )
            continue

        seen_ids.setdefault(manifest.id, []).append(plugin_dir)
        candidates.append(DiscoveredPlugin(manifest=manifest, plugin_dir=plugin_dir))

    duplicates = {pid for pid, dirs in seen_ids.items() if len(dirs) > 1}
    if duplicates:
        for pid in duplicates:
            dirs = seen_ids[pid]
            logger.warning(
                "[plugin_loader] dropping plugin id %r: claimed by %d "
                "directories (%s)",
                pid, len(dirs), ", ".join(d.name for d in dirs),
            )
        candidates = [c for c in candidates if c.manifest.id not in duplicates]

    return candidates


# ── Enable filtering ─────────────────────────────────────────────────


def _bootstrap_default_enabled(
    plugin_dir: Path,
    manifest: PluginManifest | None = None,
) -> bool:
    """Resolve the no-Setting bootstrap default for a plugin.

    Precedence:

    1. ``manifest.default_enabled`` if explicitly set in ``plugin.yaml``.
       Plugins that ship dormant (operator opts in) declare
       ``default_enabled: false``.
    2. Directory-name convention: dirs starting with ``_``
       (e.g. ``_example/``) default disabled; everything else defaults
       enabled. This keeps existing samples / scaffolds dormant without
       requiring a manifest field.
    """
    if manifest is not None and manifest.default_enabled is not None:
        return manifest.default_enabled
    return not plugin_dir.name.startswith("_")


def is_enabled(
    plugin_id: str,
    plugin_dir: Path,
    settings: dict[str, dict],
    *,
    manifest: PluginManifest | None = None,
) -> bool:
    """Resolve enable state for a single plugin from the Setting map."""
    payload = settings.get(plugin_id)
    if payload is None:
        return _bootstrap_default_enabled(plugin_dir, manifest)
    return bool(payload.get("enabled", True))


def _read_plugin_settings(org: str | None = None) -> dict[str, dict]:
    """Return ``{plugin_id: payload}`` for ``dashboard.plugin#1`` rows.

    Mock-mode (``DASHBOARD_MOCK`` set) reads from the dashboard mock DAO
    so behavioral tests can drive plugin state through fixture data.
    Failures fall through to an empty dict; the bootstrap defaults take
    over so the substrate never blocks startup on a Settings glitch.
    """
    if os.environ.get("DASHBOARD_MOCK"):
        try:
            from tools.dashboard.dao import mock as dao_mock
            members = dao_mock.get_settings_members(PLUGIN_SET_ID, org=org)
            return {m["key"]: m.get("payload") or {} for m in members}
        except Exception:
            logger.exception(
                "[plugin_loader] mock get_settings_members failed; "
                "falling back to bootstrap defaults"
            )
            return {}
    try:
        from tools.graph import ops as graph_ops
        members = graph_ops.read_set(PLUGIN_SET_ID, org=org)
        return {m.key: dict(m.payload) for m in members}
    except Exception:
        logger.exception(
            "[plugin_loader] read_set(%s) failed; falling back to "
            "bootstrap defaults", PLUGIN_SET_ID,
        )
        return {}


# ── Entrypoint resolution ────────────────────────────────────────────


def _resolve_attr(spec: str) -> Any:
    """Resolve a ``module:attr`` string to a Python object.

    Raises ``ImportError`` (re-raised through the import machinery) or
    ``AttributeError`` if the lookup fails.
    """
    if ":" not in spec:
        raise ImportError(f"entrypoint spec {spec!r} missing ':' separator")
    module_name, attr = spec.split(":", 1)
    module = importlib.import_module(module_name)
    return getattr(module, attr)


def _resolve_entrypoints(
    discovered: DiscoveredPlugin,
    *,
    effective_org: str | None = None,
) -> LoadedPlugin | None:
    """Resolve ``module:attr`` strings to live objects.

    *effective_org* is the install scope the substrate will use for this
    plugin — ``manifest.org`` by default, overridden by the toggle row's
    ``payload.org`` when present. It's bound onto the
    :data:`tools.dashboard.settings_mediator.loop._loading_plugin_org`
    contextvar across the action import so every ``register_action(...)``
    inside the imported module stamps the right org onto its
    ``RegisteredAction``. ``None`` (default) keeps the legacy scopeless
    behaviour for callers (``load_all``) that don't yet know the override.

    Returns ``None`` when any entrypoint fails to import — the substrate
    treats this as "downgrade to disabled" + log.
    """
    manifest = discovered.manifest
    ep = manifest.entrypoints
    resolved_org = effective_org if effective_org is not None else manifest.org

    routes: list = []
    badge_counter: Callable[[], Any] | None = None
    session_contributions: Callable[[list[str], Any], Any] | None = None
    background: Callable[[], Any] | None = None
    schemas: list = []
    actions: list[str] = []

    try:
        if ep.api:
            resolved = _resolve_attr(ep.api)
            if not isinstance(resolved, list):
                raise TypeError(
                    f"entrypoints.api ({ep.api!r}) must resolve to a list of "
                    f"Route objects, got {type(resolved).__name__}"
                )
            routes = resolved
        if ep.badge_counter:
            badge_counter = _resolve_attr(ep.badge_counter)
        if ep.session_contributions:
            session_contributions = _resolve_attr(ep.session_contributions)
            if not callable(session_contributions):
                raise TypeError(
                    f"entrypoints.session_contributions "
                    f"({ep.session_contributions!r}) must resolve to a callable"
                )
        if ep.background:
            background = _resolve_attr(ep.background)
            if not callable(background):
                raise TypeError(
                    f"entrypoints.background ({ep.background!r}) must "
                    f"resolve to a callable returning coroutine factories"
                )
        if ep.schemas:
            schemas = [_resolve_attr(s) for s in ep.schemas]
        if ep.actions:
            # Bare module paths — importing fires register_action(...) at
            # module top-level. We don't keep handles to the imported
            # modules; the side effect lives in settings_mediator's
            # internal handler registry.
            #
            # Bind ``_loading_plugin_org`` for the duration of the import
            # so each register_action call inside the plugin module
            # stamps the right org onto its RegisteredAction. The
            # contextvar is reset on the way out (including on exception)
            # so unrelated registrations elsewhere stay scopeless.
            from tools.dashboard.settings_mediator.loop import (
                _loading_plugin_org,
            )
            token = _loading_plugin_org.set(resolved_org)
            try:
                for spec in ep.actions:
                    importlib.import_module(spec)
            finally:
                _loading_plugin_org.reset(token)
            actions = list(ep.actions)
    except (ImportError, AttributeError, TypeError) as exc:
        logger.warning(
            "[plugin_loader] downgrading plugin %r to disabled: "
            "entrypoint resolution failed — %s",
            manifest.id, exc,
        )
        return None

    return LoadedPlugin(
        id=manifest.id,
        paths=list(manifest.paths),
        nav_label=manifest.nav.label,
        alpine_root=manifest.frontend.alpine_root,
        template=manifest.assets.template,
        script=manifest.assets.script,
        style=manifest.assets.style,
        plugin_dir=discovered.plugin_dir,
        manifest=manifest,
        effective_org=resolved_org,
        routes=routes,
        badge_counter=badge_counter,
        session_contributions=session_contributions,
        background=background,
        schemas=schemas,
        actions=actions,
    )


# ── Public load_enabled ──────────────────────────────────────────────


def load_enabled(
    *,
    plugins_dir: Path | None = None,
) -> list[LoadedPlugin]:
    """Discover, filter by Setting, resolve entrypoints. Returns the
    surviving ``LoadedPlugin`` list.

    Each plugin's enable state is read from *its own* ``manifest.org``'s
    DB — never from the caller's org or an unscoped sweep. Reads are
    cached per-org for the duration of a single ``load_enabled`` call so
    plugins sharing an install scope don't double-fetch.

    ``LoadedPlugin.effective_org`` resolves to the toggle row's
    ``payload.org`` override when present, else ``manifest.org``.
    """
    discovered = discover(plugins_dir=plugins_dir)
    cache: dict[str, dict[str, dict]] = {}

    loaded: list[LoadedPlugin] = []
    for d in discovered:
        manifest_org = d.manifest.org
        if manifest_org not in cache:
            cache[manifest_org] = _read_plugin_settings(org=manifest_org)
        settings = cache[manifest_org]
        if not is_enabled(
            d.manifest.id, d.plugin_dir, settings, manifest=d.manifest,
        ):
            continue
        # Resolve effective_org BEFORE entrypoint import so any toggle
        # row override propagates into the action contextvar — otherwise
        # the actions would register under manifest.org and the mediator
        # would read from the wrong DB.
        payload = settings.get(d.manifest.id) or {}
        override = payload.get("org")
        if isinstance(override, str) and override:
            effective_org = override
        else:
            effective_org = manifest_org
        result = _resolve_entrypoints(d, effective_org=effective_org)
        if result is None:
            continue
        loaded.append(result)
    return loaded


def load_all(
    *,
    plugins_dir: Path | None = None,
) -> list[LoadedPlugin]:
    """Discover and resolve entrypoints for every valid plugin, ignoring
    Setting state.

    The dashboard server uses this so route registration covers plugins
    that may be toggled on at runtime via the ``dashboard.plugin#1``
    Setting. Per-request handlers still gate behind ``is_enabled``;
    plugins disabled at request time return 404 / are excluded from
    ``/api/plugins``.
    """
    discovered = discover(plugins_dir=plugins_dir)
    out: list[LoadedPlugin] = []
    for d in discovered:
        result = _resolve_entrypoints(d)
        if result is None:
            continue
        out.append(result)
    return out


def reconcile_declared_settings(
    plugins: list[LoadedPlugin],
    *,
    force: bool = False,
) -> list[dict[str, Any]]:
    """Reconcile plugin-declared graph Settings for the current enable state.

    Enabled plugins install/update/adopt their declared Settings. Explicitly
    disabled plugins uninstall their owned Settings according to each
    declaration's uninstall policy. Mock mode is intentionally read-only.
    ``force`` overwrites drifted plugin-owned Settings with the manifest
    payload; normal reconciliation remains conservative.
    """
    if os.environ.get("DASHBOARD_MOCK"):
        return []
    from . import settings as plugin_settings

    cache: dict[str, dict[str, dict]] = {}
    results: list[dict[str, Any]] = []
    for plugin in plugins:
        if not plugin.manifest.settings:
            continue
        manifest_org = plugin.manifest.org
        if manifest_org not in cache:
            cache[manifest_org] = _read_plugin_settings(org=manifest_org)
        settings = cache[manifest_org]
        payload = settings.get(plugin.id)
        override = (payload or {}).get("org") if isinstance(payload, dict) else None
        effective_org = (
            override if isinstance(override, str) and override else manifest_org
        )
        enabled = is_enabled(
            plugin.id,
            plugin.plugin_dir,
            settings,
            manifest=plugin.manifest,
        )
        try:
            if enabled:
                results.extend(plugin_settings.reconcile_plugin_settings(
                    plugin,
                    effective_org=effective_org,
                    force=force,
                ))
            elif payload is not None:
                results.extend(plugin_settings.uninstall_plugin_settings(
                    plugin,
                    effective_org=effective_org,
                ))
        except Exception:
            logger.exception(
                "[plugin_loader] failed reconciling declared settings for %s",
                plugin.id,
            )
    return results
