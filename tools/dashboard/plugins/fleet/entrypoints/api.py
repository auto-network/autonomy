"""Authenticated API for the Fleet Machines read-only projection."""
from __future__ import annotations

import asyncio
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from tools.dashboard import api_auth
from tools.dashboard.plugins.fleet.entrypoints import projection


logger = logging.getLogger(__name__)


async def fleet_view(request: Request) -> JSONResponse:
    refusal = api_auth.require_global_api_authority(request)
    if refusal is not None:
        return refusal
    try:
        view = await asyncio.to_thread(projection.build_view)
    except Exception:
        logger.exception("Fleet view projection failed")
        return JSONResponse(
            {"error": "Fleet state is unavailable"}, status_code=503
        )
    return JSONResponse(view)


async def rename_machine(request: Request) -> JSONResponse:
    """Set the human label for an authorized machine.

    The label is a personal-scoped Setting, deliberately outside the signed
    roster entry — renaming needs no ceremony and propagates by settings sync.
    """
    refusal = api_auth.require_global_api_authority(request)
    if refusal is not None:
        return refusal
    machine_id = request.path_params.get("machine_id") or ""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "body must be JSON"}, status_code=400)
    from tools.network import fleet_machine_profile, fleet_roster, fleet_tunnel_server

    root_pub = fleet_tunnel_server._personal_root_pub()
    if root_pub is None:
        return JSONResponse({"error": "no fleet is configured"}, status_code=409)
    active = fleet_roster.current_roster(root_pub, org=None)
    if not any(entry.machine_id == machine_id for entry in active.values()):
        return JSONResponse(
            {"error": "that machine is not on the active roster"},
            status_code=404,
        )
    try:
        await asyncio.to_thread(
            fleet_machine_profile.store, machine_id, body.get("display_name")
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True})


routes = [
    Route("/api/plugins/fleet/view", fleet_view, methods=["GET"]),
    Route(
        "/api/plugins/fleet/machines/{machine_id}/name",
        rename_machine,
        methods=["PATCH"],
    ),
]
