"""Authenticated organization-scoped persistence for Voice Notes."""
from __future__ import annotations

from datetime import datetime, timezone
import re

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from tools.dashboard.api_auth import (
    organization_scope_from_request,
    require_authenticated_api_caller,
)
from tools.dashboard.plugins.voice_notes.entrypoints.schemas import (
    SCHEMA_REVISION,
    VOICE_NOTE_SET_ID,
)
from tools.graph import settings_ops


_NOTE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")


def _scope(request: Request):
    refusal = require_authenticated_api_caller(request)
    if refusal is not None:
        return None, refusal
    organization = organization_scope_from_request(request)
    if not organization:
        return None, JSONResponse(
            {"error": "organization scope required"}, status_code=400,
        )
    return organization, None


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z",
    )


async def list_notes(request: Request) -> JSONResponse:
    organization, refusal = _scope(request)
    if refusal is not None:
        return refusal
    members = settings_ops.read_owned_set(
        VOICE_NOTE_SET_ID, org=organization,
    ).members
    notes = [dict(member.payload) for member in members]
    notes.sort(key=lambda note: note.get("updated_at", ""), reverse=True)
    return JSONResponse({"notes": notes})


async def put_note(request: Request) -> JSONResponse:
    organization, refusal = _scope(request)
    if refusal is not None:
        return refusal
    note_id = str(request.path_params.get("note_id") or "")
    if not _NOTE_ID_RE.fullmatch(note_id):
        return JSONResponse({"error": "invalid note id"}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "body must be JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be an object"}, status_code=400)

    title = body.get("title", "")
    content = body.get("body", "")
    if not isinstance(title, str) or not isinstance(content, str):
        return JSONResponse(
            {"error": "title and body must be strings"}, status_code=400,
        )
    if len(title) > 240 or len(content) > 200_000:
        return JSONResponse({"error": "note is too large"}, status_code=400)

    existing = settings_ops.read_set_key(
        VOICE_NOTE_SET_ID, note_id, org=organization, peers=[],
    )
    now = _utc_now()
    payload = {
        "note_id": note_id,
        "title": title.strip(),
        "body": content,
        "created_at": (
            (existing or {}).get("payload", {}).get("created_at") or now
        ),
        "updated_at": now,
    }
    try:
        settings_ops.upsert_by_key(
            VOICE_NOTE_SET_ID,
            SCHEMA_REVISION,
            note_id,
            payload,
            org=organization,
            state="raw",
        )
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "note": payload})


routes: list[Route] = [
    Route("/api/plugins/voice-notes/notes", list_notes, methods=["GET"]),
    Route(
        "/api/plugins/voice-notes/notes/{note_id}",
        put_note,
        methods=["PUT"],
    ),
]
