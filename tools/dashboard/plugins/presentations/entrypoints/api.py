"""Present plugin backend API."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from html.parser import HTMLParser

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from tools.dashboard.plugins.presentations.entrypoints.schemas import (
    PRESENTATION_DECK_SET_ID,
    SCHEMA_REVISION,
)


def _caller_org(request: Request) -> str:
    return request.headers.get("X-Graph-Org") or "autonomy"


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _variant_html(design: dict) -> str:
    variants = design.get("variants") or []
    if not variants:
        return ""
    selected = [v for v in variants if v.get("selected")]
    variant = (selected or variants)[-1]
    return variant.get("html") or ""


class _SlideCounter(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.matches = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {k: (v or "") for k, v in attrs}
        classes = set((attrs_dict.get("class") or "").split())
        if (
            tag in {"section", "article"}
            or attrs_dict.get("data-slide") is not None
            or "slide" in classes
            or "present-slide" in classes
        ):
            self.matches += 1


def _detect_slide_count(html: str) -> int:
    parser = _SlideCounter()
    try:
        parser.feed(html or "")
    except Exception:
        return 1
    return max(parser.matches, 1)


def _slide_ids(count: int) -> list[str]:
    return [f"slide-{idx + 1}" for idx in range(max(count, 1))]


def _read_deck_members(org: str) -> list[dict]:
    if os.environ.get("DASHBOARD_MOCK"):
        from tools.dashboard.dao import mock as dao_mock
        return dao_mock.get_settings_members(PRESENTATION_DECK_SET_ID, org=org)
    from tools.graph import ops as graph_ops
    members = graph_ops.read_set(PRESENTATION_DECK_SET_ID, org=org, peers=[])
    return [m.to_dict() for m in members]


def _active_sessions() -> list[dict]:
    if os.environ.get("DASHBOARD_MOCK"):
        from tools.dashboard.dao import mock as dao_sessions
        return dao_sessions.get_active_sessions()
    from tools.dashboard.dao import sessions as dao_sessions
    return dao_sessions.get_active_sessions()


def _session_matches(row: dict, session_id: str) -> bool:
    if not session_id:
        return False
    candidates = {
        row.get("session_id"),
        row.get("tmux_session"),
        row.get("session_uuid"),
        row.get("graph_source_id"),
    }
    return session_id in {str(c) for c in candidates if c}


def _owner_presence(deck: dict) -> dict | None:
    surface_id = f"presentations:{deck.get('design_id') or ''}"
    owner_id = (
        deck.get("author_session_id")
        or deck.get("creator_session_id")
        or ""
    )
    if not owner_id:
        return {
            "surface_id": surface_id,
            "participant_kind": "agent",
            "participant_id": f"{surface_id}:unowned",
            "participant_label": "No owner session",
            "display_initial": "?",
            "accepts_pings": False,
            "state": "present",
            "position_kind": "label",
            "position_value": "deck-owner",
            "intent": "no active dictation listener is registered",
            "heartbeat_at": "",
            "last_ping_id": "",
            "is_owner": True,
            "is_live": False,
            "is_active": False,
            "session": None,
        }
    owner_label = (
        deck.get("author_session_label")
        or deck.get("creator_session_label")
        or owner_id
    )
    matched = next((s for s in _active_sessions() if _session_matches(s, owner_id)), None)
    age = matched.get("age_seconds") if matched else None
    active = bool(
        matched
        and (
            matched.get("active") is True
            or matched.get("activity_state") in {"active", "working", "streaming"}
            or (isinstance(age, (int, float)) and age < 60)
        )
    )
    live = bool(matched and matched.get("is_live", True))
    label = (matched or {}).get("label") or owner_label
    return {
        "surface_id": surface_id,
        "participant_kind": "agent",
        "participant_id": owner_id,
        "participant_label": label,
        "accepts_pings": bool(matched),
        "state": "working" if active else "present",
        "position_kind": "label",
        "position_value": "deck-owner",
        "intent": "listening for slide dictation" if live else "owner session not live",
        "heartbeat_at": "",
        "last_ping_id": "",
        "is_owner": True,
        "is_live": live,
        "is_active": active,
        "session": matched or None,
    }


def _upsert_deck(key: str, payload: dict, org: str) -> None:
    if os.environ.get("DASHBOARD_MOCK"):
        from tools.dashboard.dao import mock as dao_mock
        dao_mock.add_setting_member(PRESENTATION_DECK_SET_ID, key, payload, org=org)
        return
    from tools.graph import ops as graph_ops
    graph_ops.upsert_by_key(
        PRESENTATION_DECK_SET_ID,
        SCHEMA_REVISION,
        key,
        payload,
        org=org,
        state="published",
    )


def _get_design_by_revision_or_design_id(raw_id: str) -> dict | None:
    if os.environ.get("DASHBOARD_MOCK"):
        from tools.dashboard.dao import mock as dao_mock
        design = dao_mock.get_design(raw_id)
        if design:
            return design
        matches = [d for d in dao_mock._designs() if (d.get("design_id") or d.get("id")) == raw_id]
        if not matches:
            return None
        return sorted(matches, key=lambda d: int(d.get("revision_seq") or 0))[-1]

    from agents.design_db import get_design, _get_conn
    design = get_design(raw_id)
    if design:
        # If the caller passed the stable design id and it points at an
        # older revision, return the latest sibling for presentation.
        revisions = design.get("revisions") or []
        if (design.get("design_id") == raw_id or design.get("id") == raw_id) and revisions:
            latest = revisions[-1]
            if latest != design.get("id"):
                return get_design(latest) or design
        return design
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT id FROM designs WHERE design_id = ? ORDER BY revision_seq DESC LIMIT 1",
            (raw_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return get_design(row["id"])


def _deck_payload(design: dict, *, last_shown_at: str | None = None) -> dict:
    html = _variant_html(design)
    count = _detect_slide_count(html)
    design_id = design.get("design_id") or design.get("id") or ""
    creator_session_id = design.get("creator_session_id") or ""
    creator_session_label = design.get("creator_session_label") or ""
    return {
        "design_id": design_id,
        "latest_revision_id": design.get("id") or design_id,
        "name": design.get("title") or "Untitled deck",
        "subtitle": design.get("description") or "",
        "created_at": design.get("created_at") or "",
        "last_shown_at": last_shown_at or "",
        "creator_session_id": creator_session_id,
        "creator_session_label": creator_session_label,
        "author_session_id": creator_session_id,
        "author_session_label": creator_session_label,
        "slide_count": count,
        "slide_ids": _slide_ids(count),
    }


async def list_decks(request: Request) -> JSONResponse:
    org = _caller_org(request)
    rows = _read_deck_members(org)
    decks = []
    for row in rows:
        payload = row.get("payload") or {}
        deck = dict(payload)
        deck["key"] = row.get("key") or deck.get("design_id") or ""
        deck["updated_at"] = row.get("updated_at") or ""
        decks.append(deck)
    decks.sort(key=lambda d: d.get("last_shown_at") or d.get("created_at") or "", reverse=True)
    return JSONResponse({"decks": decks})


async def get_deck(request: Request) -> JSONResponse:
    raw_id = request.path_params["design_id"]
    design = _get_design_by_revision_or_design_id(raw_id)
    if not design:
        return JSONResponse({"error": "design not found"}, status_code=404)
    deck = _deck_payload(design)
    return JSONResponse({
        "deck": deck,
        "design": design,
        "owner_presence": _owner_presence(deck),
    })


async def mark_shown(request: Request) -> JSONResponse:
    org = _caller_org(request)
    raw_id = request.path_params["design_id"]
    design = _get_design_by_revision_or_design_id(raw_id)
    if not design:
        return JSONResponse({"error": "design not found"}, status_code=404)
    payload = _deck_payload(design, last_shown_at=_iso_now())
    _upsert_deck(payload["design_id"], payload, org)
    return JSONResponse({"ok": True, "deck": payload})


routes: list[Route] = [
    Route("/api/presentations/decks", list_decks, methods=["GET"]),
    Route("/api/presentations/deck/{design_id}", get_deck, methods=["GET"]),
    Route("/api/presentations/deck/{design_id}/shown", mark_shown, methods=["POST"]),
]
