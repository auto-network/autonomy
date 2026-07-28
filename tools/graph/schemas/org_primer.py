"""``autonomy.org.primer#1`` — org-scoped workspace primer overlay.

Markdown folded into the rendered workspace primer for every workspace
an org owns, under a generated ``## Org Conventions ({org})`` heading.

This is the graph-native home for content that previously lived at
``agents/orgs/<org>/primer.md`` inside the autonomy platform repo. That
repo is open source and has no per-org boundary, so storing one org's
operational content there published it. A Setting row lives in the org's
own database, which is the isolation boundary the rest of the platform
already respects — the renderer reads it with ``peers=[]`` so the
content never crosses into another org.

Keyed by org slug. The key is redundant with the database the row lives
in, but keeping it explicit matches ``autonomy.workspace.turn_correction#1``
and makes a misfiled row obvious on inspection rather than silently
rendering into the wrong org.
"""

from __future__ import annotations

from typing import Any

from .registry import (
    SchemaValidationError,
    SettingSchema,
    keyed_per_entity,
)


SET_ID = "autonomy.org.primer"
SCHEMA_REVISION = 1


SYNOPSIS = {
    "summary": (
        "Org-scoped markdown overlay folded into the workspace primer "
        "for every workspace the org owns. Graph-native replacement for "
        "agents/orgs/<org>/primer.md."
    ),
    "nouns": [
        "org primer", "org overlay", "org conventions",
        "primer overlay", "workspace primer",
    ],
    "related_set_ids": [
        "autonomy.workspace.primer#1",
        "autonomy.workspace#1",
        "autonomy.org#1",
    ],
}


@keyed_per_entity
class OrgPrimerV1(SettingSchema):
    """Shape of an ``autonomy.org.primer#1`` payload."""

    set_id = SET_ID
    schema_revision = SCHEMA_REVISION

    _field_metadata: dict[str, dict] = {
        "markdown": {
            "type": "string",
            "description": (
                "Markdown body inlined under the generated "
                "``## Org Conventions ({org})`` heading. Supply section "
                "headings at ``###`` or deeper so they nest under it."
            ),
        },
        "enabled": {
            "type": "boolean",
            "description": (
                "When false the overlay is skipped without deleting the "
                "row, so content can be parked without losing it."
            ),
            "default": True,
        },
    }

    @classmethod
    def validate(cls, payload: Any) -> None:
        _validate_primer_payload(cls, payload)


def _validate_primer_payload(cls: type, payload: Any) -> None:
    """Shared validation for the org and workspace primer overlays."""
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
    if "markdown" in payload and not isinstance(payload["markdown"], str):
        raise SchemaValidationError(
            f"{cls.__name__}: 'markdown' must be a string"
        )
    if "enabled" in payload and not isinstance(payload["enabled"], bool):
        raise SchemaValidationError(
            f"{cls.__name__}: 'enabled' must be a bool"
        )


def resolve_markdown(payload: dict | None) -> str:
    """Return the overlay body for ``payload``, or ``""`` when absent.

    A row that is present but disabled, or whose ``markdown`` is missing
    or blank, resolves the same as no row at all — the renderer omits the
    section rather than emitting an empty heading.
    """
    if not payload or not isinstance(payload, dict):
        return ""
    if not payload.get("enabled", True):
        return ""
    body = payload.get("markdown") or ""
    return body if isinstance(body, str) else ""
