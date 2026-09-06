"""Plain-language names for the ledger scopes the platform enforces.

A scope is an opaque string on the authority ledger (``invite:member``,
``role:grant:admin``, ``link:publish``). People do not read those, so the
Roles editor and the invitation mint form render every scope through this
catalog and never show the raw string in a headline view (roles design of
record graph://d1b3db8f-879, decision R-S4).

The catalog lists ONLY scopes something actually checks — the fold, a
dashboard route, or a registry acceptance — so a role built from it is a
real grant of authority, never a label. A scope nothing enforces (``org:read``,
``content:write``) is added by the bead that enforces it, not here.

The JavaScript twin ``static/js/scope-catalog.js`` carries the identical
table between ``CATALOG-BEGIN`` / ``CATALOG-END`` markers as a JSON literal;
``tests/test_scope_catalog.py`` proves the two never drift.
"""

from __future__ import annotations

import re
from typing import Optional

#: One entry per enforced scope or scope family, in display order. ``pattern``
#: is the exact scope, or a family ending in ``:*`` whose members take a role
#: name (``{role}`` in the label/sentence). ``family`` groups entries in the
#: editor; ``enforced_by`` names where the check lives, for the reader who
#: wants to verify the claim.
CATALOG: list[dict] = [
    {
        "pattern": "*",
        "family": "everything",
        "label": "Everything",
        "sentence": "Every authority the organization can confer, now and later.",
        "enforced_by": "ledger fold (UNIVERSE)",
    },
    {
        "pattern": "role:define",
        "family": "roles",
        "label": "Define roles",
        "sentence": "Define or revise roles, within the authority you may delegate.",
        "enforced_by": "ledger fold _h_role_define",
    },
    {
        "pattern": "role:grant:*",
        "family": "roles",
        "label": "Approve and grant any role",
        "sentence": "Approve joiners for, and grant or revoke, every role.",
        "enforced_by": "ledger fold _h_role_grant / claim approvals",
    },
    {
        "pattern": "role:grant:{role}",
        "family": "roles",
        "label": "Approve and grant {role}",
        "sentence": "Approve joiners for {role}, and grant or revoke it.",
        "enforced_by": "ledger fold _h_role_grant / claim approvals",
    },
    {
        "pattern": "invite:*",
        "family": "invitations",
        "label": "Invite people as any role",
        "sentence": "Mint invitations for every role.",
        "enforced_by": "ledger fold _h_invite; POST /api/network/ledger/invite",
    },
    {
        "pattern": "invite:{role}",
        "family": "invitations",
        "label": "Invite people as {role}",
        "sentence": "Mint invitations that admit people as {role}.",
        "enforced_by": "ledger fold _h_invite; POST /api/network/ledger/invite",
    },
    {
        "pattern": "link:publish",
        "family": "share links",
        "label": "Publish share links",
        "sentence": "Publish organization content behind a share link.",
        "enforced_by": "link_approvals (org_authority.authorize)",
    },
    {
        "pattern": "link:revoke",
        "family": "share links",
        "label": "Revoke share links",
        "sentence": "Take a published share link down.",
        "enforced_by": "link_approvals (org_authority.authorize)",
    },
    {
        "pattern": "link:read",
        "family": "share links",
        "label": "Read share links",
        "sentence": "Read organization content served behind share links.",
        "enforced_by": "link serving grants",
    },
    {
        "pattern": "membership:checkpoint",
        "family": "membership",
        "label": "Sign membership checkpoints",
        "sentence": "Sign the committed-membership checkpoints the registry trusts.",
        "enforced_by": "membership_commitment (CHECKPOINT_SCOPE)",
    },
]

_ROLE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def catalog() -> list[dict]:
    """The table, copied, for JSON responses."""
    return [dict(entry) for entry in CATALOG]


def _fill(entry: dict, role: Optional[str]) -> dict:
    out = {
        "scope": entry["pattern"] if role is None else entry["pattern"].replace("{role}", role),
        "family": entry["family"],
        "label": entry["label"],
        "sentence": entry["sentence"],
        "enforced_by": entry["enforced_by"],
        "unknown": False,
    }
    if role is not None:
        out["label"] = entry["label"].replace("{role}", role)
        out["sentence"] = entry["sentence"].replace("{role}", role)
        out["role"] = role
    return out


def describe_scope(scope: str) -> dict:
    """Resolve one concrete scope or pattern to its catalog entry.

    Exact patterns match first (``*``, ``role:grant:*``, ``invite:*``); then
    the templated families (``role:grant:<role>``, ``invite:<role>``) with the
    role name substituted. Anything else is ``{"unknown": True, "scope": raw}``
    so a caller renders it as a mono chip rather than hiding it.
    """
    if not isinstance(scope, str) or not scope:
        return {"unknown": True, "scope": scope}
    for entry in CATALOG:
        if "{role}" not in entry["pattern"] and entry["pattern"] == scope:
            return _fill(entry, None)
    for entry in CATALOG:
        pattern = entry["pattern"]
        if "{role}" not in pattern:
            continue
        prefix = pattern.split("{role}", 1)[0]
        if scope.startswith(prefix):
            role = scope[len(prefix):]
            if _ROLE_NAME.match(role):
                return _fill(entry, role)
    return {"unknown": True, "scope": scope}


def describe_scope_set(scopes) -> list[dict]:
    """Describe every scope of a set, preserving order."""
    return [describe_scope(scope) for scope in scopes]
