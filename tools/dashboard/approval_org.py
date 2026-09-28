"""The organization a Central approval request acts in (auto-fkhq0.10a, .13, .9).

Central reserves ``org`` in a request body for routing selectors it derives
itself, so a kind whose request names an organization as application data
(the org whose link to publish, the org a secure setting is sealed for)
carries it as ``org_slug``. One rule settles it, so no kind can widen it:

- an organization session acts only in its own organization. A named
  ``org_slug`` must equal it; omitted means it;
- a local session (a host terminal, the operator's own session) names any
  organization this node holds.

The target is then resolved only in the settled organization.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tools.dashboard.approval_kind_registry import ApprovalPlanningContext


def settle_org(context: ApprovalPlanningContext, body: Mapping[str, Any]) -> tuple[str, dict]:
    """Return ``(org, body without org_slug)`` or raise ValueError."""
    body = dict(body)
    named = body.pop("org_slug", None)
    if context.requester_principal_kind == "org_session":
        if named not in (None, context.requester_org):
            raise ValueError("an organization session acts only in its own organization")
        org = context.requester_org
    else:
        org = named
    if not isinstance(org, str) or not org:
        raise ValueError("the request names its organization as org_slug")
    return org, body
