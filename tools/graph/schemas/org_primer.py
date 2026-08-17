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

A layer may be split across several rows: the bare key ``<org>`` plus any
number of named blocks keyed ``<org>:<block-name>``. Every matching row
renders, concatenated in ``(order, key)`` sequence. Each block carries its
own ``enabled`` flag, which is the point of the split — a block can be
switched off without touching the rest of the layer and without deleting
the content.
"""

from __future__ import annotations

from typing import Any

from .registry import (
    home,
    home,
    SchemaValidationError,
    SettingSchema,
    keyed_per_entity,
)


SET_ID = "autonomy.org.primer"
SCHEMA_REVISION = 1

#: Sort position assumed for a row that does not set ``order``.
DEFAULT_ORDER = 100


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


#: Not forced into any one store. This records that the question was
#: ASKED -- must this live in the operator's own database, or on
#: this machine alone? -- and answered no, which is different
#: from nobody having considered it.
#:
#: It is not a prohibition. The operator owns workspaces, so
#: their database is the organizational home of their own
#: things; reading this as "anywhere but personal" refuses
#: writes that are correct.
@home("organization")
@keyed_per_entity(key_strategy="org_slug[:block_name]")
class OrgPrimerV1(SettingSchema):
    """Shape of an ``autonomy.org.primer#1`` payload."""

    set_id = SET_ID
    schema_revision = SCHEMA_REVISION

    _field_metadata: dict[str, dict] = {
        "markdown": {
            "type": "string",
            "required": True,
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
        "order": {
            "type": "integer",
            "description": (
                "Sort position among the blocks sharing this layer. "
                "Lower renders earlier; ties break on key, so the "
                "unsuffixed row always leads. Default 100 leaves room "
                "on both sides without renumbering."
            ),
            "default": DEFAULT_ORDER,
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
    if "markdown" not in payload:
        raise SchemaValidationError(
            f"{cls.__name__}: 'markdown' is required — a row with no body "
            f"renders nothing. To park content, keep the markdown and set "
            f"'enabled' to false."
        )
    if not isinstance(payload["markdown"], str):
        raise SchemaValidationError(
            f"{cls.__name__}: 'markdown' must be a string"
        )
    if "enabled" in payload and not isinstance(payload["enabled"], bool):
        raise SchemaValidationError(
            f"{cls.__name__}: 'enabled' must be a bool"
        )
    # bool is a subclass of int; reject it explicitly so a mistyped
    # 'order': true reads as an error rather than sorting at position 1.
    if "order" in payload and (
        isinstance(payload["order"], bool)
        or not isinstance(payload["order"], int)
    ):
        raise SchemaValidationError(
            f"{cls.__name__}: 'order' must be an int"
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


def resolve_order(payload: dict | None) -> int:
    """Return the sort position for ``payload``, or the default.

    A row with no ``order`` sorts at :data:`DEFAULT_ORDER`, so blocks
    added without thinking about placement land together in key order
    rather than jumping to the front.
    """
    if not payload or not isinstance(payload, dict):
        return DEFAULT_ORDER
    order = payload.get("order", DEFAULT_ORDER)
    if isinstance(order, bool) or not isinstance(order, int):
        return DEFAULT_ORDER
    return order
