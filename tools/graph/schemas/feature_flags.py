"""Schema: ``dashboard.feature_flags#1``.

Operator-controlled boolean toggles for dashboard features. Keyed by
dotted flag name (e.g. ``voice.pipe_enabled``). Each downstream slice
that gates visible behavior on a flag seeds its own row when the slice
lands; this substrate ships zero rows.

Schema *shape* precedent: ``dashboard.plugin#1`` — keyed boolean toggles
with missing-row-as-default-false semantics (registered via the plugin
loader at ``tools/dashboard/plugin_api/loader.py``, not here).

Schema *file-location and registration boilerplate* precedent:
``tools/graph/schemas/claude_credentials.py``.

Spec: graph://40dd9d7a-23a.
"""

from __future__ import annotations

from typing import Any

from .registry import (
    publication_band,
    SchemaValidationError,
    SettingSchema,
    field,
    keyed_per_entity,
)


FEATURE_FLAGS_SET_ID = "dashboard.feature_flags"
FEATURE_FLAGS_REVISION = 1


SYNOPSIS = {
    "summary": (
        "Operator-controlled boolean feature flags. Keyed by dotted flag "
        "name. Missing rows read as False; absent flag does not throw."
    ),
    "nouns": [
        "feature flag",
        "flag",
        "toggle",
        "voice client enabled",
        "responsive collapse",
        "registry visibility",
        "librarian routing",
    ],
    "related_set_ids": [
        "dashboard.plugin#1",
    ],
}


@publication_band(min="raw", max="canonical")
@keyed_per_entity(key_strategy="flag_name")
class FeatureFlagV1(SettingSchema):
    """Per-flag boolean toggle.

    Key: dotted flag name (``voice.pipe_enabled``,
    ``inference.librarian_routing``, etc.). Owner-slice seeds the row
    when the downstream slice lands.
    """

    set_id = FEATURE_FLAGS_SET_ID
    schema_revision = FEATURE_FLAGS_REVISION

    enabled: bool = field(
        required=True,
        description=(
            "True if the flag is enabled. Absent rows read as False; "
            "consumers must not throw on missing rows."
        ),
    )
    description: str = field(
        required=True,
        description=(
            "Operator-facing description of what the flag gates. "
            "Required so every flag carries context for future readers."
        ),
    )
    owner: str = field(
        required=True,
        description=(
            "Slice that owns this flag (e.g. ``S3 voice pipe canary``). "
            "Required so each flag traces back to its introducing slice."
        ),
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise SchemaValidationError(
                f"{cls.__name__}: payload must be a dict, "
                f"got {type(payload).__name__}"
            )
        extra = set(payload) - set(cls._field_metadata)
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown field(s): {sorted(extra)}"
            )
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            raise SchemaValidationError(
                f"{cls.__name__}: 'enabled' must be a bool, "
                f"got {type(enabled).__name__}"
            )
        for required_str in ("description", "owner"):
            value = payload.get(required_str)
            if not isinstance(value, str) or not value:
                raise SchemaValidationError(
                    f"{cls.__name__}: missing or empty required field "
                    f"{required_str!r}"
                )
