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

from .registry import SettingSchema, SchemaValidationError, keyed_per_entity, publication_band
from .registry import home


ORG_SET_ID = "autonomy.org"
ORG_REVISION = 1

VALID_ORG_TYPES = ("shared", "personal")


SYNOPSIS = {
    "summary": (
        "Org identity: display name, byline, brand color, favicon, type"
    ),
    "nouns": [
        "org", "organization", "identity", "branding",
        "rename org", "byline", "favicon",
    ],
    "related_set_ids": [
        "autonomy.org.peer-subscription#1",
        "autonomy.org.capability.install#1",
    ],
}


#: Not forced into any one store. This records that the question was
#: ASKED -- must this live in the operator's own database, or on
#: this machine alone? -- and answered no, which is different
#: from nobody having considered it.
#:
#: It is not a prohibition. The operator owns workspaces, so
#: their database is the organizational home of their own
#: things; reading this as "anywhere but personal" refuses
#: writes that are correct.
@publication_band(min="raw", max="canonical")
@home("organization")
@keyed_per_entity(key_strategy="org_slug")
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

    _field_metadata: dict[str, dict] = {
        "name": {
            "type": "string",
            "required": True,
            "description": "Display name of the org (the Setting key carries the slug)",
        },
        "byline": {
            "type": "string",
            # 60, because it sits under the org's name in a list and has to
            # stay one line on a phone. The three bylines that read well are
            # 12, 17 and 28 characters; the one that does not is 98 and wraps
            # to two lines, which is how a tagline turns into a description.
            "max_length": 60,
            "description": "Tagline shown on org cards / dashboard headers",
        },
        "color": {
            "type": "string",
            "description": "Brand color (hex string, e.g. #3366ff) for UI accents",
        },
        "favicon": {
            "type": "string",
            "description": "Path or URL to the favicon shown in dashboard tabs",
        },
        "type": {
            "type": "string",
            "description": (
                "Mirror of the bootstrap orgs row type so consumers can render "
                "a personal-vs-shared indicator without a second lookup"
            ),
            "enum": list(VALID_ORG_TYPES),
        },
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
