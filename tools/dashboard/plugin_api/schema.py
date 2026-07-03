"""``dashboard.plugin#1`` — operator toggle for dashboard plugins.

Each Setting member is keyed by plugin id and carries ``{enabled: bool}``.
The loader consults this set to filter the discovered manifests before
resolving entrypoints. A plugin with no Setting row falls back to a
bootstrap default (see ``loader._bootstrap_default_enabled``).
"""
from __future__ import annotations

from typing import Any

from tools.graph.schemas.registry import (
    SchemaValidationError,
    SettingSchema,
)


PLUGIN_SET_ID = "dashboard.plugin"
PLUGIN_SCHEMA_REVISION = 1
PLUGIN_OWNED_SETTING_SET_ID = "dashboard.plugin-owned-setting"
PLUGIN_OWNED_SETTING_SCHEMA_REVISION = 1


class DashboardPluginV1(SettingSchema):
    """Payload shape for ``dashboard.plugin#1`` Settings.

    ``{enabled: bool, org?: str}``. The optional ``org`` field is an
    operator override of the manifest's declared install scope —
    flipping it switches the plugin to a different org-DB at runtime
    without a reinstall.
    """

    set_id = PLUGIN_SET_ID
    schema_revision = PLUGIN_SCHEMA_REVISION

    @classmethod
    def validate(cls, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise SchemaValidationError(
                f"{cls.__name__}: payload must be a dict, "
                f"got {type(payload).__name__}"
            )
        if "enabled" not in payload:
            raise SchemaValidationError(
                f"{cls.__name__}: missing required field 'enabled'"
            )
        if not isinstance(payload["enabled"], bool):
            raise SchemaValidationError(
                f"{cls.__name__}: 'enabled' must be a bool"
            )
        if "org" in payload and not isinstance(payload["org"], str):
            raise SchemaValidationError(
                f"{cls.__name__}: 'org' must be a string when present"
            )
        extra = set(payload) - {"enabled", "org"}
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown field(s): {sorted(extra)}"
            )


class DashboardPluginOwnedSettingV1(SettingSchema):
    """Tracks graph Settings installed from plugin declarations.

    The row key is ``<plugin_id>:<set_id>#<schema_revision>:<setting_key>``.
    The payload stores the current ownership/reconciliation state.
    """

    set_id = PLUGIN_OWNED_SETTING_SET_ID
    schema_revision = PLUGIN_OWNED_SETTING_SCHEMA_REVISION

    @classmethod
    def validate(cls, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise SchemaValidationError(
                f"{cls.__name__}: payload must be a dict, "
                f"got {type(payload).__name__}"
            )
        required_strings = {
            "plugin_id", "org", "set_id", "key", "status",
            "plugin_payload_hash", "installed_payload_hash",
            "current_payload_hash", "resource", "uninstall",
        }
        for key in required_strings:
            v = payload.get(key)
            if not isinstance(v, str):
                raise SchemaValidationError(
                    f"{cls.__name__}: {key!r} must be a string"
                )
        if not isinstance(payload.get("schema_revision"), int):
            raise SchemaValidationError(
                f"{cls.__name__}: 'schema_revision' must be an integer"
            )
        if not isinstance(payload.get("setting_id"), str):
            raise SchemaValidationError(
                f"{cls.__name__}: 'setting_id' must be a string"
            )
        if payload["status"] not in {
            "managed", "drifted", "orphaned", "uninstalled",
        }:
            raise SchemaValidationError(
                f"{cls.__name__}: invalid status {payload['status']!r}"
            )
        if payload["uninstall"] not in {"deprecate_if_unchanged", "leave"}:
            raise SchemaValidationError(
                f"{cls.__name__}: invalid uninstall {payload['uninstall']!r}"
            )
        extra = set(payload) - {
            "plugin_id", "org", "set_id", "schema_revision", "key",
            "setting_id", "status", "plugin_payload_hash",
            "installed_payload_hash", "current_payload_hash", "resource",
            "uninstall",
        }
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown field(s): {sorted(extra)}"
            )
