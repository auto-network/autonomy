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
#: Current schema revision. Revision 3 adds portable organization icons
#: (``icon_attachment_id`` + ``icon_data_uri``). This constant names the
#: CURRENT revision only; it must never be assigned as the ``schema_revision``
#: of an older class, which would relabel a landed revision (auto-j1y0z).
ORG_REVISION = 3

VALID_ORG_TYPES = ("shared", "personal")

#: The bounded compact icon derivative is a 64x64 WebP encoded as a
#: ``data:image/webp;base64,...`` URI. The processor holds the byte bound
#: (16 KiB encoded); the schema bounds the whole URI string so a single
#: org-list row a consumer loads cannot grow without limit. 24,000 chars is
#: comfortably above a 16-KiB base64 payload (~21,848 chars) plus prefix.
ORG_ICON_DATA_URI_MAX_CHARS = 24_000


SYNOPSIS = {
    "summary": (
        "Org identity: display name, byline, brand color, favicon, icon, type"
    ),
    "nouns": [
        "org", "organization", "identity", "branding",
        "rename org", "byline", "favicon", "icon", "avatar",
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
    # Frozen literal, NOT ``ORG_REVISION``: this class is revision 1 forever.
    # Binding it to the current-revision constant would silently relabel a
    # landed revision the day ORG_REVISION advances (auto-j1y0z).
    schema_revision = 1

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


class OrgV2(OrgV1):
    """Revision 2 adds the optional ``description`` — the org's long-form
    charter text, shown on the Charter screen and anywhere a full
    introduction of the org belongs. Everything else is revision 1
    unchanged; a revision-1 row is a valid revision-2 row as-is.
    """

    set_id = ORG_SET_ID
    schema_revision = 2

    _optional_types = {**OrgV1._optional_types, "description": str}

    _field_metadata = {
        **OrgV1._field_metadata,
        "description": {
            "type": "string",
            # Long-form, but bounded: 4000 characters is several paragraphs,
            # and an unbounded field invites pasted documents into a row
            # every org-list consumer loads.
            "max_length": 4000,
            "description": (
                "Long-form charter text: what the org is, in the org's own "
                "words. Optional; rendered on the Charter screen."
            ),
        },
    }

    @classmethod
    def upconvert_from_prev(cls, payload: dict) -> dict:
        # description is optional; a rev-1 payload is a valid rev-2 payload.
        return dict(payload)


class OrgV3(OrgV2):
    """Revision 3 adds a portable organization icon.

    Two server-owned fields, both optional:

    * ``icon_attachment_id`` — the UUID of the canonical 512x512 sRGB WebP
      stored in this organization's own attachment store (durable, immutable
      bytes). This is what a machine serves through the same-origin
      ``/api/attachment`` route.
    * ``icon_data_uri`` — an exact 64x64 WebP derivative encoded as a
      ``data:image/webp;base64,...`` URI, bounded so it can travel inline in
      the org identity row and be shown before any attachment fetch (the
      portable compact presentation, e.g. the pre-consent invitation tile).

    Both are written and cleared as a pair by the authority-checked icon
    routes; the Charter writer never accepts them from a client body. A
    revision-1 or revision-2 row (which carry neither, and may carry the
    legacy ``favicon`` path/URL) is a valid revision-3 row as-is — upconversion
    preserves ``favicon`` untouched.
    """

    set_id = ORG_SET_ID
    schema_revision = ORG_REVISION

    _optional_types = {
        **OrgV2._optional_types,
        "icon_attachment_id": str,
        "icon_data_uri": str,
    }

    _field_metadata = {
        **OrgV2._field_metadata,
        "icon_attachment_id": {
            "type": "string",
            "description": (
                "UUID of the canonical 512x512 WebP in the org's attachment "
                "store; server-owned, set only by the org icon routes"
            ),
        },
        "icon_data_uri": {
            "type": "string",
            "max_length": ORG_ICON_DATA_URI_MAX_CHARS,
            "description": (
                "Bounded 64x64 WebP data: URI — the portable compact icon "
                "shown inline; server-owned, set only by the org icon routes"
            ),
        },
    }

    @classmethod
    def upconvert_from_prev(cls, payload: dict) -> dict:
        # The icon fields are optional and server-owned; a rev-2 payload
        # (with or without the legacy `favicon`) is a valid rev-3 payload,
        # and `favicon` is preserved untouched.
        return dict(payload)
