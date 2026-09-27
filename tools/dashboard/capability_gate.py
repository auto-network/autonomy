"""Session-bearer identity and capability checks shared by broker callers.

Scope comes exclusively from launcher-stamped session/workspace state. Tokens
and enable Settings are resolved on every request, so revocation is immediate.
These blocking reads belong off the dashboard event loop.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class CallerScope:
    org: str
    workspace: str
    session: str


class CapabilityRefused(RuntimeError):
    """A caller-facing refusal; never includes the bearer token."""

    def __init__(self, detail: str, *, status: int):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def resolve_caller(authorization: str | None) -> CallerScope:
    """Resolve trusted scope without granting any capability."""
    auth = authorization or ""
    if not auth.startswith("Bearer ") or not auth[7:]:
        raise CapabilityRefused(
            "missing bearer token: send Authorization: Bearer "
            "$CROSSTALK_TOKEN", status=401)

    from tools.dashboard.dao import auth_db, dashboard_db
    # Reuse the standalone REPL's TTL/miss refresh: that process does not
    # receive the dashboard's setting.changed cache invalidation events.
    from tools.connectors.repl_auth import _fresh_workspace

    resolved = auth_db.resolve_token(hashlib.sha256(auth[7:].encode()).hexdigest())
    if resolved is None:
        raise CapabilityRefused("invalid or revoked token", status=401)
    session, _token_org = resolved
    row = dashboard_db.get_session(session)
    project = ((row or {}).get("project") or "").strip()
    if not project:
        raise CapabilityRefused(
            f"session {session!r} does not map to a workspace", status=403)
    try:
        workspace = _fresh_workspace(project)
    except KeyError as exc:
        raise CapabilityRefused(
            f"session {session!r} maps to unknown workspace {project!r}",
            status=403) from exc
    return CallerScope(org=workspace.graph_project, workspace=workspace.id,
                       session=session)


def require_capability(authorization: str | None, capability: str) -> CallerScope:
    """Resolve the caller and require a fresh, explicitly enabled grant."""
    scope = resolve_caller(authorization)
    from agents.workspace_settings import WORKSPACE_CAPABILITY_ENABLE_SET_ID
    from tools.graph import ops as graph_ops

    members = graph_ops.read_set(
        WORKSPACE_CAPABILITY_ENABLE_SET_ID, org=scope.org, peers=[])
    wanted = f"{scope.workspace}:{capability}"
    for member in members.members:
        if member.key == wanted:
            if member.payload.get("enabled", True) is True:
                return scope
            break
    raise CapabilityRefused(
        f"workspace {scope.workspace!r} does not enable {capability}", status=403)
