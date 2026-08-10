"""Host-side caller authentication for the stealth REPL.

The REPL binds to 127.0.0.1, but agent containers on this host run with
``--network=host`` — reachability is not a boundary. Every request must
therefore present the caller's per-session bearer token
(``Authorization: Bearer $CROSSTALK_TOKEN``). The launcher stamps
``sha256(token) -> tmux_name`` into the dashboard's auth DB at container
start; we resolve the hash there, then derive the workspace host-side
from the session row the trusted launcher wrote — the caller supplies no
session name and no workspace, so there is nothing to assert or forge,
and revoking the token cuts access immediately.

ACCEPTED COUPLING — write-down, not an accident: ``CROSSTALK_TOKEN`` was
minted as the container's messaging identity; this module additionally
makes it the container's credential-decryption identity. That is the
same trust boundary (the container env), but anyone widening CrossTalk's
token distribution must know that browser-login access to provisioned
credentials rides on it.

Authorization then requires the derived workspace to enable the
``repl_login`` contract in ``autonomy.workspace.capability.enable``,
read fresh from that workspace's own org on every request (matching
``tools/dashboard/jira_routes.py::_workspace_overrides``) so a revoked
grant takes effect without a REPL restart. The slow-changing
workspace-id -> workspace map is cached by ``agents.workspace_settings``
and refreshed here on a short TTL, since the REPL process never receives
the dashboard's ``setting.changed`` invalidation hook.
"""

from __future__ import annotations

import hashlib
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPL_LOGIN_CONTRACT = "repl_login"

#: How stale the workspace-id map may get in this process before we force
#: a rebuild. The capability-enable Setting itself is read fresh on every
#: request — only the id -> org/project mapping rides this TTL.
WORKSPACE_MAP_TTL_S = 60.0

_workspace_map_refreshed_at = 0.0


class ReplAuthError(RuntimeError):
    """Refusal with an HTTP status. Everything here fails closed."""

    def __init__(self, message: str, *, status: int = 403):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class ReplCaller:
    """An authenticated caller: session from the bearer token, workspace
    and org derived host-side from launcher-stamped state."""

    session: str
    workspace_id: str
    org: str


def _ensure_root(autonomy_root: Path) -> None:
    root = str(autonomy_root.resolve())
    if root not in sys.path:
        sys.path.insert(0, root)


def _fresh_workspace(project: str):
    """Workspace for *project*, tolerating this process's stale cache: on
    a miss (or past the TTL) invalidate and retry once before refusing."""
    from agents import workspace_settings

    global _workspace_map_refreshed_at
    now = time.monotonic()
    stale = now - _workspace_map_refreshed_at > WORKSPACE_MAP_TTL_S
    if not stale:
        try:
            return workspace_settings.get_workspace(project)
        except KeyError:
            pass  # maybe added since the map was built — rebuild below
    workspace_settings.invalidate_caches()
    _workspace_map_refreshed_at = now
    return workspace_settings.get_workspace(project)


def authenticate(*, autonomy_root: Path | None,
                 authorization: str | None) -> ReplCaller:
    """Resolve and authorize the caller, or raise :class:`ReplAuthError`.

    Fail-closed ladder: missing/malformed bearer -> 401; unknown or
    revoked token -> 401; session without a stamped workspace, unknown
    workspace, or workspace not enabling ``repl_login`` -> 403.
    """
    if autonomy_root is None:
        raise ReplAuthError(
            "caller authentication requires --autonomy-root", status=503)
    _ensure_root(autonomy_root)

    auth = authorization or ""
    if not auth.startswith("Bearer ") or not auth[7:]:
        raise ReplAuthError(
            "missing bearer token: send Authorization: Bearer "
            "$CROSSTALK_TOKEN", status=401)
    token_hash = hashlib.sha256(auth[7:].encode()).hexdigest()

    from tools.dashboard.dao import auth_db, dashboard_db

    resolved = auth_db.resolve_token(token_hash)
    if resolved is None:
        raise ReplAuthError("invalid or revoked token", status=401)
    # resolve_token now returns (session, org); the REPL gates on the derived
    # WORKSPACE (below), not the token's org, so the org is intentionally
    # unused here.
    session, _org = resolved

    row = dashboard_db.get_session(session)
    project = ((row or {}).get("project") or "").strip()
    if not project:
        raise ReplAuthError(
            f"session {session!r} does not map to a workspace")
    try:
        ws = _fresh_workspace(project)
    except KeyError:
        raise ReplAuthError(
            f"session {session!r} maps to unknown workspace {project!r}")

    from agents.workspace_settings import WORKSPACE_CAPABILITY_ENABLE_SET_ID
    from tools.graph import ops as graph_ops

    members = graph_ops.read_set(
        WORKSPACE_CAPABILITY_ENABLE_SET_ID, org=ws.graph_project, peers=[])
    wanted = f"{ws.id}:{REPL_LOGIN_CONTRACT}"
    for member in members.members:
        if member.key == wanted:
            if member.payload.get("enabled", True) is True:
                return ReplCaller(session=session, workspace_id=ws.id,
                                  org=ws.graph_project)
            break
    raise ReplAuthError(
        f"workspace {ws.id!r} does not enable {REPL_LOGIN_CONTRACT}")
