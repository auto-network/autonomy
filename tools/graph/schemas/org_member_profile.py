"""``autonomy.org.member-profile`` — the org member directory.

The simple, direct, org-scoped profile row the platform's identity
design calls for (the Aug-7 record: "the persona→person binding lives
in the join record and the org's own member directory"): each member
publishes how they want to be presented **in this organization** —
starting with the display name they chose for this membership.

Deliberately presentation-only. Authority is never read from Settings
(register D18): who is a member, their roles, and their keys stay in
the authority ledger and its projections. This set carries what a
screen shows beside a member's acts — nothing a verifier consults.

SCOPE — organization: a member's chosen presentation in that org is the
org's shared fact, synced to members' replicas via publication state.
CARDINALITY — one row per member. KEY — the member's ORG-SCOPED
persona public key (autonomy.network.persona: HKDF-derived from the
personal root and this org's genesis — deterministic within the org,
unlinkable across orgs). Founders have one too, written at the found
ceremony. The personal root public key NEVER keys or appears in org
data: that would link one person's memberships across organizations,
the exact linkage persona derivation exists to remove.
"""
from __future__ import annotations

from tools.graph.schemas.registry import (
    publication_band,
    SettingSchema,
    field,
    home,
    keyed_per_entity,
)

MEMBER_PROFILE_SET_ID = "autonomy.org.member-profile"
MEMBER_PROFILE_REVISION = 1

SYNOPSIS = {
    "summary": (
        "Org member directory: one presentation row per member (display "
        "name chosen for this org, optional avatar/color/byline), keyed by the "
        "member's org-scoped persona public key (autonomy.network.persona; "
        "never the cross-org-linkable personal root). "
        "Presentation only — authority stays in the ledger."
    ),
    "nouns": [
        "member", "profile", "display name", "directory", "identity",
        "attribution", "org member", "roster presentation",
    ],
    "related_set_ids": [
        "autonomy.org", "autonomy.network.ledger-projection",
        "autonomy.identity.personal",
    ],
}


@publication_band(min="raw", max="published")
@home("organization")
@keyed_per_entity(key_strategy="member_public_key")
class OrgMemberProfileV1(SettingSchema):
    """One member's chosen presentation in one organization."""

    set_id = MEMBER_PROFILE_SET_ID
    schema_revision = MEMBER_PROFILE_REVISION

    display_name: str = field(
        required=True,
        description="The name this member chose for this organization; "
                    "what screens show beside their acts")
    avatar: str = field(
        default="",
        description="Optional icon: a graph attachment id (served at "
                    "/api/attachment/<id>) or an absolute URL")
    color: str = field(
        default="",
        description="Optional CSS color hint for this member's accents")
    byline: str = field(
        default="",
        description="Optional one-line role or tagline shown with the name")
