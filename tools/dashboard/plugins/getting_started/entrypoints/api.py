"""Getting Started plugin API: the harness sign-in scan.

Design of record graph://5f2f5a49-00d v12 FR7a: the first step in Getting
Started is the harness sign-in. A deterministic scan of the well-known
locations on the operator's machine finds each harness's token, brings it
into the system, and the session starts with it; no browser when a token
exists. The scan is ``graph credentials import`` run in-process.

* ``GET  /api/plugins/getting_started/harnesses`` — the scan without any
  write (what is on this machine, what is already imported).
* ``POST /api/plugins/getting_started/harnesses/import`` — the scan with
  the imports applied; idempotent, the operator's files are never touched.

Both answer ``{"harnesses": [{harness, status, detail, account, usable}],
"usable": [harness, ...], "home": path}``. ``usable`` names the harnesses a
session can launch with now. ``home`` is the operator's home as the operator
knows it (``AUTONOMY_HOST_HOME``, the host path mounted at ``/host-home``), so
a failed scan can say where it looked; ``None`` when the node has no such
mount.
"""
from __future__ import annotations

import asyncio
import os

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from tools.dashboard.api_auth import require_authenticated_api_caller
from tools.graph import credential_import


def _scan(dry_run: bool) -> dict:
    report = credential_import.run_import(
        credential_import.operator_home(), dry_run=dry_run,
    )
    out = credential_import.report_to_dict(report)
    out["home"] = os.environ.get("AUTONOMY_HOST_HOME") or None
    return out


async def harnesses(request: Request):
    auth_error = require_authenticated_api_caller(request)
    if auth_error is not None:
        return auth_error
    return JSONResponse(await asyncio.to_thread(_scan, True))


async def harnesses_import(request: Request):
    auth_error = require_authenticated_api_caller(request)
    if auth_error is not None:
        return auth_error
    return JSONResponse(await asyncio.to_thread(_scan, False))


routes: list[Route] = [
    Route("/api/plugins/getting_started/harnesses", harnesses, methods=["GET"]),
    Route("/api/plugins/getting_started/harnesses/import", harnesses_import, methods=["POST"]),
]
