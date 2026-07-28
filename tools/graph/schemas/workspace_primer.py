"""``autonomy.workspace.primer#1`` — workspace-scoped primer overlay.

Markdown folded into the rendered primer for exactly one workspace,
inlined bare between the org overlay and the commit-policy block. The
body supplies its own headings; unlike the org overlay, the template
wraps it in nothing.

This is the graph-native home for content that previously lived at
``agents/projects/<workspace-id>/primer.md`` inside the autonomy
platform repo. That repo is open source and has no per-org boundary, so
an external org's workspace content stored there was published. A
Setting row lives in the owning org's database and is read with
``peers=[]``, so it never crosses an org boundary.

Keyed by workspace id, matching ``autonomy.workspace.turn_correction#1``.
"""

from __future__ import annotations

from typing import Any

from .org_primer import _validate_primer_payload, resolve_markdown  # noqa: F401
from .registry import SettingSchema, keyed_per_entity


SET_ID = "autonomy.workspace.primer"
SCHEMA_REVISION = 1


SYNOPSIS = {
    "summary": (
        "Workspace-scoped markdown overlay inlined into one workspace's "
        "primer. Graph-native replacement for "
        "agents/projects/<workspace-id>/primer.md."
    ),
    "nouns": [
        "workspace primer", "workspace overlay", "primer overlay",
        "runbooks", "per-workspace instructions",
    ],
    "related_set_ids": [
        "autonomy.org.primer#1",
        "autonomy.workspace#1",
        "autonomy.workspace.turn_correction#1",
    ],
}


@keyed_per_entity
class WorkspacePrimerV1(SettingSchema):
    """Shape of an ``autonomy.workspace.primer#1`` payload."""

    set_id = SET_ID
    schema_revision = SCHEMA_REVISION

    _field_metadata: dict[str, dict] = {
        "markdown": {
            "type": "string",
            "description": (
                "Markdown body inlined verbatim, with no wrapping "
                "heading. Open it with your own ``##`` heading or the "
                "content merges into the preceding section."
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
