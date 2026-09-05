"""Schema for the fleet ORG roster's per-organization entries.

The machine roster (``autonomy.fleet.roster``) records which MACHINES hold the
operator's personal root. This is its sibling for ORGANIZATIONS: one
personal-root-signed statement per organization the operator belongs to — an
enrol or a kick. It answers "which orgs am I a member of" as synced, signed
state, instead of implying it from whichever ``data/orgs/*.db`` files happen to
exist locally (which never crossed to a fresh fleet member — the bootstrap
chicken-and-egg).

Like the machine roster, entries live in personal.db (``@home("personal")``)
pinned to ``raw`` band (``@publication_band(max="raw")``): at
``published``/``canonical`` a personal row is visible to every organization's
read on the operator's machines, which would leak fleet topology into org
surfaces. ``raw`` keeps it local; the personal-store sync carries it across the
fleet. Each machine reads the resolved roster and materialises the
``orgs/<slug>.db`` stub (with the recorded ``org_id``) so
``discover_org_sync_scopes`` finds it and the existing org-DB sync fills it.

The key is the deterministic content id (the hash of the signed binding), so
enrol/kick about one org coexist for the OR-set merge in
:mod:`tools.network.fleet_org_roster`, which owns the signature + merge
semantics; this schema only pins the stored shape and the band.
"""

from __future__ import annotations

from typing import Any

from tools.graph.schemas.registry import (
    SchemaValidationError,
    SettingSchema,
    field,
    home,
    keyed_per_entity,
    publication_band,
)

FLEET_ORG_ROSTER_SET_ID = "autonomy.fleet.org-roster"
FLEET_ORG_ROSTER_REVISION = 1

#: ``graph set find`` synopsis. Personal-scoped, not an organization set.
SYNOPSIS = {
    "summary": (
        "The fleet ORG roster — one personal-root-signed row per organization "
        "the operator belongs to (enroll / kick). Lives in personal.db at raw "
        "band; the OR-set merge that resolves it is "
        "tools.network.fleet_org_roster. Each machine materialises the org DB "
        "stub from it so the org databases synchronize across the fleet."
    ),
    "nouns": [
        "fleet", "org roster", "organization membership", "org sync",
        "personal root", "org bootstrap", "fleet organizations",
    ],
    "related_set_ids": ["autonomy.fleet.roster"],
}


@home("personal")
@publication_band(max="raw")
@keyed_per_entity(key_strategy="org_roster_entry_id")
class FleetOrgRosterEntryV1(SettingSchema):
    """One organization's roster statement, personal-root-signed.

    Key strategy ``org_roster_entry_id``: the key is the entry's own content id
    (``fleet_org_roster.OrgRosterEntry.entry_id``, the SHA-256 of its signed
    body) — content-addressed, so enrol and kick about one org are distinct
    entries with distinct ids that must all coexist for the OR-set merge.
    """

    set_id = FLEET_ORG_ROSTER_SET_ID
    schema_revision = FLEET_ORG_ROSTER_REVISION

    org_slug: str = field(
        required=True,
        description=(
            "The organization slug — the sync SCOPE name (org DB is "
            "data/orgs/<slug>.db). Merge identity for this roster."
        ),
    )
    org_id: str = field(
        required=True,
        description=(
            "The stable orgs.id UUID (the ledger genesis org label). A member "
            "materialises the stub with THIS id so both machines are the same "
            "org, not just the same slug."
        ),
    )
    kind: str = field(
        required=True, enum=["enroll", "kick"],
        description="enroll = a member org; kick = removed (an absorbing tombstone).",
    )
    seq: int = field(
        required=True,
        description="Per-org sequence. NOT a roster-wide counter — there is none.",
    )
    issued_at: int = field(
        required=True,
        description="Unix ms, informational. Never a merge input.",
    )
    supersedes: str | None = field(
        required=False, default=None,
        description=(
            "For a re-enrol only: the entry id of the kick it supersedes. A "
            "kick never cites one."
        ),
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            raise SchemaValidationError("fleet org roster entry must be an object")
        for key in ("org_slug", "org_id"):
            if not isinstance(payload.get(key), str) or not payload.get(key):
                raise SchemaValidationError(f"{key!r} must be a non-empty string")
        if payload.get("kind") == "kick" and payload.get("supersedes") is not None:
            raise SchemaValidationError("a kick tombstone does not cite a supersedes")
