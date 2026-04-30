"""Primers UI plugin — backend API.

Exposes two routes consumed by ``page.js`` to browse + render the
markdown primers that get mounted as ``~/.claude/CLAUDE.md`` at session
launch.

* ``GET /api/primers/workspaces`` — list every workspace
  ``agents.workspace_settings.load_workspaces()`` returns. Org-scoped
  metadata only; does NOT render primers eagerly.
* ``GET /api/primers/workspace/{workspace_id}`` — render the named
  workspace's primer via ``agents.primer_renderer.render_workspace_primer``
  and return the markdown plus a rough token estimate
  (``len(markdown) // 4``).

Both routes honour ``X-Graph-Org``: when the header carries a slug the
response is filtered to workspaces owned by that org. Without the
header (or with an empty slug) the caller sees every visible
workspace.

Errors from ``load_workspaces()`` (malformed Settings) or
``render_workspace_primer`` (template / overlay drift) are caught and
returned as ``{"error": "..."}`` so an internal traceback never leaks
to the browser. The server log keeps the full stack.
"""
from __future__ import annotations

import logging
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


logger = logging.getLogger(__name__)


def _caller_org(request: Request) -> str | None:
    """Return the ``X-Graph-Org`` slug or ``None`` if absent / empty."""
    org = request.headers.get("X-Graph-Org")
    return org if org else None


def _workspace_metadata(workspace) -> dict[str, Any]:
    """Trim a :class:`WorkspaceV1` to the fields the page needs.

    The full dataclass carries mounts, capabilities, and resolved
    artifacts — none of which the list view consumes. Returning the
    minimum reduces payload size and keeps the workspace-row schema
    stable across renderer revisions.
    """
    writable = any(getattr(r, "writable", False) for r in workspace.repos)
    return {
        "id": workspace.id,
        "name": workspace.name,
        "org": workspace.graph_project,
        "image": workspace.image,
        "writable": writable,
    }


async def list_workspaces(request: Request) -> JSONResponse:
    """``GET /api/primers/workspaces`` — workspace metadata for the rail.

    Returns ``{"workspaces": [...]}`` ordered by ``id``. Filters by
    ``X-Graph-Org`` when the caller stamps it; absent header returns
    every visible workspace. Errors during ``load_workspaces()`` are
    downgraded to an empty list with a logged warning so a malformed
    Settings row in one org cannot 500 the whole list.
    """
    from agents.workspace_settings import load_workspaces

    try:
        workspaces = load_workspaces()
    except Exception:
        logger.exception(
            "[primers] load_workspaces() raised; returning empty list",
        )
        return JSONResponse({
            "workspaces": [],
            "warning": "load_workspaces failed — see server log",
        })

    caller_org = _caller_org(request)
    metadata = []
    for ws in workspaces.values():
        if caller_org and ws.graph_project != caller_org:
            continue
        metadata.append(_workspace_metadata(ws))
    metadata.sort(key=lambda m: m["id"])
    return JSONResponse({"workspaces": metadata})


async def render_workspace(request: Request) -> JSONResponse:
    """``GET /api/primers/workspace/{workspace_id}`` — rendered primer.

    Calls ``render_workspace_primer`` for the requested workspace and
    returns ``{markdown, token_estimate, workspace}``. Unknown workspace
    ids return 404; render errors return 500 with a generic message.
    """
    from agents.primer_renderer import render_workspace_primer
    from agents.workspace_settings import load_workspaces

    workspace_id = request.path_params["workspace_id"]

    try:
        workspaces = load_workspaces()
    except Exception:
        logger.exception("[primers] load_workspaces() raised")
        return JSONResponse(
            {"error": "load_workspaces failed"},
            status_code=500,
        )

    workspace = workspaces.get(workspace_id)
    if workspace is None:
        return JSONResponse(
            {"error": f"unknown workspace: {workspace_id!r}"},
            status_code=404,
        )

    caller_org = _caller_org(request)
    if caller_org and workspace.graph_project != caller_org:
        # Treat out-of-scope workspaces as not-found — same shape as the
        # unknown-id branch so the page can render a single error path.
        return JSONResponse(
            {"error": f"unknown workspace: {workspace_id!r}"},
            status_code=404,
        )

    try:
        markdown = render_workspace_primer(workspace)
    except Exception:
        logger.exception(
            "[primers] render_workspace_primer raised for %r", workspace_id,
        )
        return JSONResponse(
            {"error": "rendering failed"},
            status_code=500,
        )

    return JSONResponse({
        "markdown": markdown,
        "token_estimate": len(markdown) // 4,
        "workspace": _workspace_metadata(workspace),
    })


routes: list[Route] = [
    Route("/api/primers/workspaces", list_workspaces, methods=["GET"]),
    Route(
        "/api/primers/workspace/{workspace_id}",
        render_workspace,
        methods=["GET"],
    ),
]
