"""Plugin-declared graph Setting reconciliation.

Plugins declare ordinary graph Settings in ``plugin.yaml``. This module is the
lifecycle layer: install/update those Settings with normal graph APIs and track
which plugin owns each declaration so uninstall can safely deprecate unchanged
rows without deleting operator-edited Settings.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from tools.dashboard.plugin_api.manifest import PluginSettingDeclaration
from tools.dashboard.plugin_api.schema import (
    PLUGIN_OWNED_SETTING_SCHEMA_REVISION,
    PLUGIN_OWNED_SETTING_SET_ID,
)
from tools.graph import ops as graph_ops
from tools.graph import schemas as graph_schemas


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedPluginSetting:
    plugin_id: str
    org: str
    plugin_dir: Path
    set_id: str
    schema_revision: int
    key: str
    state: str
    payload: dict[str, Any]
    payload_hash: str
    resource: str
    uninstall: str

    @property
    def owner_key(self) -> str:
        return f"{self.plugin_id}:{self.set_id}#{self.schema_revision}:{self.key}"


def canonical_payload_hash(payload: dict[str, Any]) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _load_payload(
    plugin_dir: Path,
    decl: PluginSettingDeclaration,
) -> tuple[dict[str, Any], str]:
    if decl.payload is not None:
        return dict(decl.payload), "inline"
    rel = decl.payload_file or ""
    candidate = (plugin_dir / rel).resolve()
    base = plugin_dir.resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"payload_file escapes plugin directory: {rel!r}") from exc
    if not candidate.is_file():
        raise FileNotFoundError(f"plugin payload_file not found: {rel!r}")
    raw = candidate.read_text(encoding="utf-8")
    if candidate.suffix.lower() in {".yaml", ".yml"}:
        data = yaml.safe_load(raw)
    else:
        data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"plugin payload_file must contain an object: {rel!r}")
    return data, rel


def resolve_setting_declarations(
    plugin,
    *,
    effective_org: str | None = None,
) -> list[ResolvedPluginSetting]:
    org = effective_org or getattr(plugin, "effective_org", "") or plugin.manifest.org
    out: list[ResolvedPluginSetting] = []
    for decl in plugin.manifest.settings:
        payload, resource = _load_payload(plugin.plugin_dir, decl)
        graph_schemas.validate_payload(decl.set_id, decl.schema_revision, payload)
        out.append(ResolvedPluginSetting(
            plugin_id=plugin.id,
            org=org,
            plugin_dir=plugin.plugin_dir,
            set_id=decl.set_id,
            schema_revision=int(decl.schema_revision),
            key=decl.key,
            state=decl.state,
            payload=payload,
            payload_hash=canonical_payload_hash(payload),
            resource=resource,
            uninstall=decl.uninstall,
        ))
    return out


def _read_member(set_id: str, *, key: str, schema_revision: int, org: str):
    try:
        members = graph_ops.read_set(set_id, org=org, peers=[]).members
    except Exception:
        logger.exception("plugin settings: read_set(%s) failed org=%s", set_id, org)
        return None
    for member in members:
        if member.key == key and int(member.stored_revision) == int(schema_revision):
            return member
    return None


def _read_owner(owner_key: str, *, org: str):
    try:
        members = graph_ops.read_set(
            PLUGIN_OWNED_SETTING_SET_ID,
            org=org,
            peers=[],
        ).members
    except Exception:
        logger.exception(
            "plugin settings: read_set(%s) failed org=%s",
            PLUGIN_OWNED_SETTING_SET_ID,
            org,
        )
        return None
    for member in members:
        if member.key == owner_key:
            return member
    return None


def _owner_payload(
    decl: ResolvedPluginSetting,
    *,
    status: str,
    setting_id: str | None,
    plugin_payload_hash: str | None = None,
    installed_payload_hash: str | None = None,
    current_payload_hash: str | None = None,
) -> dict[str, Any]:
    return {
        "plugin_id": decl.plugin_id,
        "org": decl.org,
        "set_id": decl.set_id,
        "schema_revision": decl.schema_revision,
        "key": decl.key,
        "setting_id": setting_id or "",
        "status": status,
        "plugin_payload_hash": plugin_payload_hash or decl.payload_hash,
        "installed_payload_hash": installed_payload_hash or "",
        "current_payload_hash": current_payload_hash or "",
        "resource": decl.resource,
        "uninstall": decl.uninstall,
    }


def _write_owner(decl: ResolvedPluginSetting, payload: dict[str, Any]) -> str:
    existing = _read_owner(decl.owner_key, org=decl.org)
    if existing is not None and dict(existing.payload) == payload:
        return existing.id
    return graph_ops.upsert_by_key(
        PLUGIN_OWNED_SETTING_SET_ID,
        PLUGIN_OWNED_SETTING_SCHEMA_REVISION,
        decl.owner_key,
        payload,
        state="canonical",
        org=decl.org,
    )


def reconcile_plugin_settings(
    plugin,
    *,
    effective_org: str | None = None,
) -> list[dict[str, Any]]:
    """Install/update settings declared by one enabled plugin."""
    results: list[dict[str, Any]] = []
    for decl in resolve_setting_declarations(plugin, effective_org=effective_org):
        current = _read_member(
            decl.set_id,
            key=decl.key,
            schema_revision=decl.schema_revision,
            org=decl.org,
        )
        owner = _read_owner(decl.owner_key, org=decl.org)
        current_hash = canonical_payload_hash(current.payload) if current is not None else ""
        owner_payload = dict(owner.payload) if owner is not None else {}
        last_installed_hash = str(owner_payload.get("installed_payload_hash") or "")

        should_write = False
        status = "managed"
        action = "adopted"

        if owner is None:
            if current is None:
                should_write = True
                action = "installed"
            elif current_hash == decl.payload_hash:
                action = "adopted"
            else:
                status = "drifted"
                action = "conflict"
        else:
            if current is None:
                should_write = True
                action = "reinstalled"
            elif current_hash == last_installed_hash or current_hash == decl.payload_hash:
                if current_hash != decl.payload_hash:
                    should_write = True
                    action = "updated"
                else:
                    action = "unchanged"
            else:
                status = "drifted"
                action = "drifted"

        if should_write:
            sid = graph_ops.upsert_by_key(
                decl.set_id,
                decl.schema_revision,
                decl.key,
                decl.payload,
                state=decl.state,
                org=decl.org,
            )
            current_hash = decl.payload_hash
            setting_id = sid
            installed_hash = decl.payload_hash
            status = "managed"
        else:
            setting_id = (
                current.id
                if current is not None
                else str(owner_payload.get("setting_id") or "")
            )
            installed_hash = (
                decl.payload_hash
                if status == "managed" and current_hash == decl.payload_hash
                else last_installed_hash
            )

        _write_owner(decl, _owner_payload(
            decl,
            status=status,
            setting_id=setting_id,
            installed_payload_hash=installed_hash,
            current_payload_hash=current_hash,
        ))
        results.append({
            "plugin_id": decl.plugin_id,
            "org": decl.org,
            "set_id": decl.set_id,
            "key": decl.key,
            "status": status,
            "action": action,
        })
    return results


def uninstall_plugin_settings(
    plugin,
    *,
    effective_org: str | None = None,
) -> list[dict[str, Any]]:
    """Deprecate unchanged settings declared by one disabled/uninstalled plugin."""
    results: list[dict[str, Any]] = []
    for decl in resolve_setting_declarations(plugin, effective_org=effective_org):
        owner = _read_owner(decl.owner_key, org=decl.org)
        if owner is None:
            continue
        owner_payload = dict(owner.payload)
        if owner_payload.get("status") not in {"managed", "drifted"}:
            continue
        current = _read_member(
            decl.set_id,
            key=decl.key,
            schema_revision=decl.schema_revision,
            org=decl.org,
        )
        current_hash = canonical_payload_hash(current.payload) if current is not None else ""
        installed_hash = str(owner_payload.get("installed_payload_hash") or "")
        if (
            decl.uninstall == "deprecate_if_unchanged"
            and current is not None
            and current_hash
            and current_hash == installed_hash
        ):
            graph_ops.deprecate_setting(current.id, org=decl.org)
            status = "uninstalled"
            action = "deprecated"
            setting_id = current.id
        else:
            status = "orphaned"
            action = "left_in_place"
            setting_id = (
                current.id
                if current is not None
                else str(owner_payload.get("setting_id") or "")
            )
        _write_owner(decl, _owner_payload(
            decl,
            status=status,
            setting_id=setting_id,
            installed_payload_hash=installed_hash,
            current_payload_hash=current_hash,
        ))
        results.append({
            "plugin_id": decl.plugin_id,
            "org": decl.org,
            "set_id": decl.set_id,
            "key": decl.key,
            "status": status,
            "action": action,
        })
    return results
