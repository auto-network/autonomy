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


routes = [
    Route("/api/plugins/fleet/view", fleet_view, methods=["GET"]),
]
