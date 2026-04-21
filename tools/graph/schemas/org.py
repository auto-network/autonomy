"""``autonomy.org#1`` — org identity Setting.

An org's rich identity (display name, byline, color, favicon) lives as a
Setting in the org's own per-org DB, keyed by the slug. The bootstrap
row in ``orgs`` identifies which DB represents which org; this Setting
carries everything a consumer (dashboard, CLI) needs to *render* the
org. Spec: graph://d970d946-f95 (Org Registry), graph://0d3f750f-f9c
(Setting Primitive), graph://497cdc20-d43 (Identity asset cascade).

Notes on shape:

* No ``slug`` or ``org`` field — the key is already the slug, and adding
  an ``org`` field would trip the cross-DB reference scanner in
  ``org_ops.find_references``.
* ``type`` mirrors the bootstrap ``orgs`` row type so downstream readers
  can render a personal-vs-shared indicator without a second lookup.
  It's optional so callers that already know the type can skip it.
"""

from __future__ import annotations

from typing import Any

from .registry import SettingSchema, SchemaValidationError, register_schema


ORG_SET_ID = "autonomy.org"
ORG_REVISION = 1

VALID_ORG_TYPES = ("shared", "personal")


class OrgV1(SettingSchema):
    """Shape of an ``autonomy.org#1`` Setting payload.

    Required: ``name``.
    Optional: ``byline``, ``color``, ``favicon``, ``type``.
    """

    set_id = ORG_SET_ID
    schema_revision = ORG_REVISION

    _required = ("name",)
    _optional_types: dict[str, type | tuple[type, ...]] = {
        "byline": str,
        "color": str,
        "favicon": str,
        "type": str,
    }

    @classmethod
    def validate(cls, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise SchemaValidationError(
                f"{cls.__name__}: payload must be a dict, "
                f"got {type(payload).__name__}"
            )
        for key in cls._required:
            if key not in payload:
                raise SchemaValidationError(
                    f"{cls.__name__}: missing required field {key!r}"
                )
            val = payload[key]
            if not isinstance(val, str) or not val:
                raise SchemaValidationError(
                    f"{cls.__name__}: {key!r} must be a non-empty string"
                )
        allowed = set(cls._required) | set(cls._optional_types)
        extra = set(payload) - allowed
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown field(s): {sorted(extra)}"
            )
        for key, want in cls._optional_types.items():
            if key not in payload:
                continue
            val = payload[key]
            if not isinstance(val, want):
                raise SchemaValidationError(
                    f"{cls.__name__}: {key!r} must be "
                    f"{want.__name__ if isinstance(want, type) else want}, "
                    f"got {type(val).__name__}"
                )
            if isinstance(val, str) and not val:
                raise SchemaValidationError(
                    f"{cls.__name__}: {key!r} must be a non-empty string"
                )
        if "type" in payload and payload["type"] not in VALID_ORG_TYPES:
            raise SchemaValidationError(
                f"{cls.__name__}: 'type' must be one of {VALID_ORG_TYPES}, "
                f"got {payload['type']!r}"
            )


register_schema(ORG_SET_ID, ORG_REVISION, OrgV1)
