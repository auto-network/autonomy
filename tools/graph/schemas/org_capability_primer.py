"""``autonomy.org.capability.primer#1`` — org capability guidance.

Additive Markdown composed after a capability implementation's code-owned
``primer.md`` / ``SKILL.md`` for sessions in the owning organization.  This
is the home for provider-instance details such as Jira custom-field ids and
organization-specific workflow transitions; the implementation package stays
the source of truth for portable commands and behavior.

Keys are implementation ids (for example ``autonomy/jira``) plus optional
named blocks such as ``autonomy/jira:enterprise-workflow``.  Matching blocks
compose in ``(order, key)`` order and may be parked with ``enabled: false``.
"""

from __future__ import annotations

from typing import Any

from .org_primer import (
    DEFAULT_ORDER,
    _validate_primer_payload,
    resolve_markdown,
    resolve_order,
)
from .registry import SettingSchema, keyed_per_entity, publication_band
from .registry import home


SET_ID = "autonomy.org.capability.primer"
SCHEMA_REVISION = 1


SYNOPSIS = {
    "summary": (
        "Organization-specific Markdown appended to an enabled capability's "
        "code-owned primer and skill"
    ),
    "nouns": [
        "capability primer", "capability guidance", "skill supplement",
        "organization capability", "provider workflow",
    ],
    "related_set_ids": [
        "autonomy.capability.impl#1",
        "autonomy.org.capability.install#1",
        "autonomy.org.primer#1",
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
@keyed_per_entity(key_strategy="implementation_name[:block_name]")
class OrgCapabilityPrimerV1(SettingSchema):
    """Shape of an ``autonomy.org.capability.primer#1`` payload."""

    set_id = SET_ID
    schema_revision = SCHEMA_REVISION

    _field_metadata: dict[str, dict] = {
        "markdown": {
            "type": "string",
            "required": True,
            "description": (
                "Markdown appended beneath a generated organization-specific "
                "guidance heading in the capability primer and skill."
            ),
        },
        "enabled": {
            "type": "boolean",
            "description": (
                "When false this block is skipped without deleting its content."
            ),
            "default": True,
        },
        "order": {
            "type": "integer",
            "description": (
                "Sort position among blocks for the same implementation; "
                "lower renders earlier and ties break on key."
            ),
            "default": DEFAULT_ORDER,
        },
    }

    @classmethod
    def validate(cls, payload: Any) -> None:
        _validate_primer_payload(cls, payload)


__all__ = [
    "SET_ID",
    "SCHEMA_REVISION",
    "OrgCapabilityPrimerV1",
    "resolve_markdown",
    "resolve_order",
]
