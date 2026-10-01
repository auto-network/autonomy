"""Session-bearer identity and capability checks shared by broker callers.

Scope comes exclusively from launcher-stamped session/workspace state. Tokens
and enable Settings are resolved on every request, so revocation is immediate.
These blocking reads belong off the dashboard event loop.
"""

from __future__ import annotations

import hashlib
import time
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


#: How stale this process's workspace-id map may get before a rebuild.
WORKSPACE_MAP_TTL_S = 60.0
_workspace_map_refreshed_at = 0.0


def _fresh_workspace(project: str):
    """Workspace for *project*, tolerating a stale cache: on a miss (or past
    the TTL) invalidate and retry once before refusing."""
    from agents import workspace_settings

    global _workspace_map_refreshed_at
    now = time.monotonic()
    if now - _workspace_map_refreshed_at <= WORKSPACE_MAP_TTL_S:
        try:
            return workspace_settings.get_workspace(project)
        except KeyError:
            pass  # maybe added since the map was built — rebuild below
    workspace_settings.invalidate_caches()
    _workspace_map_refreshed_at = now
    return workspace_settings.get_workspace(project)


def resolve_caller(authorization: str | None) -> CallerScope:
    """Resolve trusted scope without granting any capability."""
    auth = authorization or ""
    if not auth.startswith("Bearer ") or not auth[7:]:
        raise CapabilityRefused(
            "missing bearer token: send Authorization: Bearer "
            "$CROSSTALK_TOKEN", status=401)

    from tools.dashboard.dao import auth_db, dashboard_db

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


def capability_enabled(org: str, workspace: str, capability: str) -> bool:
    """Whether the workspace's grant for *capability* is present and enabled,
    read fresh from ``autonomy.workspace.capability.enable``."""
    from agents.workspace_settings import WORKSPACE_CAPABILITY_ENABLE_SET_ID
    from tools.graph import ops as graph_ops

    members = graph_ops.read_set(WORKSPACE_CAPABILITY_ENABLE_SET_ID, org=org, peers=[])
    wanted = f"{workspace}:{capability}"
    for member in members.members:
        if member.key == wanted:
            return member.payload.get("enabled", True) is True
    return False


def require_capability(authorization: str | None, capability: str) -> CallerScope:
    """Resolve the caller and require a fresh, explicitly enabled grant."""
    scope = resolve_caller(authorization)
    if capability_enabled(scope.org, scope.workspace, capability):
        return scope
    raise CapabilityRefused(
        f"workspace {scope.workspace!r} does not enable {capability}", status=403)
