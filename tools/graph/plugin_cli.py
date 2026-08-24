"""Dynamic per-plugin CLI registration for the ``graph`` command.

A dashboard plugin may declare ``entrypoints.cli`` in its ``plugin.yaml``
— a ``module:attr`` spec resolving to a ``register(subparsers)`` callable,
the same shape the built-in command modules use.  When the plugin is
enabled for the caller's org, its command tree mounts into ``graph``;
when it is disabled or absent, it contributes nothing, so ``graph --help``
only ever advertises commands that will actually work.

Deliberately independent of ``tools.dashboard.plugin_api.loader``: that
module imports pydantic to validate manifests, which is the right cost at
dashboard startup and the wrong cost on every ``graph`` invocation.  Here
a manifest is read as plain YAML and only three facts are taken from it —
plugin id, owning org, and the cli entrypoint spec.  Validation stays the
dashboard's job; a manifest the dashboard would reject simply fails to
mount here and is skipped.

Enablement mirrors ``plugin_api.loader``'s rules on the same Setting
(``dashboard.plugin#1``, keyed by plugin id, read from the plugin's own
org): an explicit payload wins; without a row, ``default_enabled`` from
the manifest wins; without either, underscore-prefixed directories stay
dormant and everything else defaults on.  An optional ``cli_workspaces``
allow-list in the Setting payload additionally gates mounting to named
workspaces (matched against ``$AUTONOMY_WORKSPACE`` when both exist).
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

#: Same Setting the dashboard's enable filter reads.
PLUGIN_SET_ID = "dashboard.plugin"


def _plugins_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "dashboard" / "plugins"


def discover_cli_plugins(plugins_dir: Path | None = None) -> list[dict]:
    """Manifest scan: every plugin declaring a cli entrypoint.

    Returns ``[{id, org, spec, default_enabled, dirname}]``.  Anything
    unreadable is skipped silently — a broken manifest must never take
    the whole CLI down with it.
    """
    root = plugins_dir or _plugins_dir()
    found: list[dict] = []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return found
    for child in entries:
        manifest = child / "plugin.yaml"
        if not manifest.is_file():
            continue
        try:
            import yaml
            data = yaml.safe_load(manifest.read_text()) or {}
        except Exception:
            continue
        spec = ((data.get("entrypoints") or {}).get("cli"))
        if not isinstance(spec, str) or ":" not in spec:
            continue
        pid = data.get("id")
        if not isinstance(pid, str) or not pid:
            continue
        found.append({
            "id": pid,
            "org": data.get("org") if isinstance(data.get("org"), str) else None,
            "spec": spec,
            "default_enabled": data.get("default_enabled"),
            "dirname": child.name,
        })
    return found


def _is_enabled(plugin: dict, payloads: dict[str, dict]) -> bool:
    payload = payloads.get(plugin["id"])
    if payload is None:
        if plugin["default_enabled"] is not None:
            return bool(plugin["default_enabled"])
        return not plugin["dirname"].startswith("_")
    if not bool(payload.get("enabled", True)):
        return False
    allow = payload.get("cli_workspaces")
    if isinstance(allow, list) and allow:
        ws = os.environ.get("AUTONOMY_WORKSPACE")
        return ws in allow
    return True


def _read_payloads(org: str | None) -> dict[str, dict]:
    try:
        from tools.graph import ops as graph_ops
        members = graph_ops.read_set(PLUGIN_SET_ID, org=org)
        return {m.key: dict(m.payload) for m in members}
    except Exception:
        return {}


def register_plugin_commands(sub, plugins_dir: Path | None = None,
                             payload_reader=None) -> list[str]:
    """Mount every enabled plugin's command tree.  Returns mounted ids.

    A plugin whose module fails to import, whose ``register`` raises, or
    whose verb collides with an existing command is skipped with a note
    on stderr — the rest of the CLI stays healthy.
    """
    reader = payload_reader or _read_payloads
    mounted: list[str] = []
    payload_cache: dict[str | None, dict[str, dict]] = {}
    for plugin in discover_cli_plugins(plugins_dir):
        org = plugin["org"]
        if org not in payload_cache:
            payload_cache[org] = reader(org)
        if not _is_enabled(plugin, payload_cache[org]):
            continue
        mod_path, _, attr = plugin["spec"].partition(":")
        before = set(getattr(sub, "choices", {}) or {})
        try:
            register = getattr(importlib.import_module(mod_path), attr)
            register(sub)
        except Exception as exc:  # noqa: BLE001 — plugin faults stay contained
            print(f"graph: plugin cli {plugin['id']!r} failed to mount: {exc}",
                  file=sys.stderr)
            continue
        added = set(getattr(sub, "choices", {}) or {}) - before
        mounted.append(plugin["id"])
        del added  # collision detection lives in argparse itself: add_parser
        # raises on a duplicate verb, which the except above contains.
    return mounted
