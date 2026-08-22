"""Authenticated, organization-scoped API for the Testing plugin."""
from __future__ import annotations

import asyncio
import re
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from tools.dashboard.api_auth import (
    organization_scope_from_request,
    require_authenticated_api_caller,
)
from tools.dashboard.plugins.testing.entrypoints import store
from tools.dashboard.event_bus import event_bus


_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")


def _scope(request: Request) -> tuple[str | None, JSONResponse | None]:
    refusal = require_authenticated_api_caller(request)
    if refusal is not None:
        return None, refusal
    organization = organization_scope_from_request(request)
    if organization is None:
        return None, JSONResponse(
            {"error": "organization scope required"}, status_code=400,
        )
    return organization, None


async def _json(request: Request) -> tuple[dict[str, Any] | None, JSONResponse | None]:
    try:
        value = await request.json()
    except (ValueError, TypeError):
        return None, JSONResponse({"error": "JSON object required"}, status_code=400)
    if not isinstance(value, dict):
        return None, JSONResponse({"error": "JSON object required"}, status_code=400)
    return value, None


def _result(value: dict[str, Any]) -> JSONResponse:
    return JSONResponse(value, status_code=200 if value.get("ok") else 400)


async def telemetry(request: Request) -> JSONResponse:
    organization, refusal = _scope(request)
    if refusal is not None:
        return refusal
    body, error = await _json(request)
    if error is not None:
        return error
    assert organization is not None and body is not None
    action = body.get("action", "event")
    if action == "status":
        return _result(await asyncio.to_thread(store.telemetry_status, organization))
    session = str(body.get("session") or "")
    if not _SESSION_RE.fullmatch(session):
        return JSONResponse({"error": "valid session is required"}, status_code=400)
    version = str(body.get("agent_test_version") or "")[:40]
    if action == "error":
        phase = str(body.get("phase") or "")
        category = str(body.get("category") or "")
        message = str(body.get("message") or "")
        run_id = str(body.get("run_id") or "")
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", phase):
            return JSONResponse({"error": "valid error phase is required"}, status_code=400)
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", category):
            return JSONResponse({"error": "valid error category is required"}, status_code=400)
        if not message or len(message) > 1000 or len(run_id) > 200:
            return JSONResponse({"error": "bounded error message and run_id are required"}, status_code=400)
        return _result(await asyncio.to_thread(
            store.record_error,
            organization,
            session,
            run_id=run_id,
            phase=phase,
            category=category,
            message=message,
            agent_test_version=version,
        ))
    if action == "progress":
        run_id = str(body.get("run_id") or "")[:200]
        if not run_id:
            return JSONResponse({"error": "run_id is required"}, status_code=400)
        try:
            completed = max(0, int(body.get("completed", 0)))
            total = max(0, int(body.get("total", 0)))
            percent = max(0, min(100, int(body.get("percent", 0))))
        except (TypeError, ValueError):
            return JSONResponse({"error": "progress counts must be integers"}, status_code=400)
        await event_bus.broadcast("agent-test:progress", {
            "session_id": session,
            "run_id": run_id,
            "completed": completed,
            "total": total,
            "percent": percent,
            "status": str(body.get("status") or "running")[:32],
        })
        return JSONResponse({"ok": True})
    event = str(body.get("event") or "")
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", event):
        return JSONResponse({"error": "valid event is required"}, status_code=400)
    return _result(await asyncio.to_thread(
        store.record_event, organization, session, event, version,
    ))


async def runs(request: Request) -> JSONResponse:
    organization, refusal = _scope(request)
    if refusal is not None:
        return refusal
    body, error = await _json(request)
    if error is not None:
        return error
    assert organization is not None and body is not None
    run_id = str(body.get("run_id") or "")
    payload = body.get("run")
    if not isinstance(payload, dict):
        return JSONResponse({"error": "run object required"}, status_code=400)
    return _result(await asyncio.to_thread(
        store.record_run, organization, run_id, payload,
    ))


async def durations(request: Request) -> JSONResponse:
    organization, refusal = _scope(request)
    if refusal is not None:
        return refusal
    body, error = await _json(request)
    if error is not None:
        return error
    assert organization is not None and body is not None
    action = body.get("action")
    repository = str(body.get("repository") or "")
    selectors = body.get("selectors") or []
    if action == "record":
        return _result(await asyncio.to_thread(
            store.record_observations,
            organization,
            repository,
            str(body.get("run_id") or ""),
            body.get("observations"),
        ))
    if action == "history":
        return _result(await asyncio.to_thread(
            store.duration_history,
            organization,
            repository,
            selectors,
            limit_tests=body.get("limit_tests", 10),
        ))
    if action == "estimate":
        return _result(await asyncio.to_thread(
            store.estimate_duration,
            organization,
            repository,
            selectors,
            parallelism=body.get("parallelism", 1),
        ))
    return JSONResponse({"error": "action must be record, history, or estimate"}, status_code=400)


async def summary(request: Request) -> JSONResponse:
    organization, refusal = _scope(request)
    if refusal is not None:
        return refusal
    assert organization is not None
    try:
        value = await asyncio.to_thread(
            store.dashboard_summary,
            organization,
            request.query_params.get("repository", ""),
            recent_limit=int(request.query_params.get("recent_limit", "20")),
            ranked_limit=int(request.query_params.get("ranked_limit", "10")),
        )
    except ValueError:
        return JSONResponse({"error": "limits must be integers"}, status_code=400)
    return _result(value)


routes: list[Route] = [
    Route("/api/plugins/testing/telemetry", telemetry, methods=["POST"]),
    Route("/api/plugins/testing/runs", runs, methods=["POST"]),
    Route("/api/plugins/testing/durations", durations, methods=["POST"]),
    Route("/api/plugins/testing/summary", summary, methods=["GET"]),
]
