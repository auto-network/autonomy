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
