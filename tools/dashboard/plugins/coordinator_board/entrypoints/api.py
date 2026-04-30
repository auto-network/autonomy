"""Backend routes for the coordinator-board plugin.

Two endpoints exposed:

* ``GET  /api/coordinator/board``    — full board payload for the
  current operator. Canvas + operatorMessage come from graph Settings;
  tiles, threads, and the supplemental tracking lists ship as v1
  defaults until follow-up beads wire them to live sources (see bead
  description "Out of scope" — multi-coordinator, SSE updates, icon-rail).
* ``POST /api/coordinator/message``  — operator → coordinator message.
  Body: ``{text}``. Writes to the
  ``dashboard.operator-message-to-coordinator`` Setting and best-effort
  ``tmux send`` to a coordinator-role session.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from starlette.responses import JSONResponse
from starlette.routing import Route

from .schemas import (
    COORDINATOR_CANVAS_SET_ID,
    OPERATOR_MESSAGE_SET_ID,
    SCHEMA_REVISION,
)


logger = logging.getLogger(__name__)


# ── v1 defaults — stand-in until follow-up beads wire live sources ───
#
# The bead acceptance criteria boil down to: canvas + operator-message
# round-trip via Settings, and the rest of the board renders. The
# tile/thread/beads-landed/decisions/follow-up content lives in graph
# notes today (graph://81e126c8-75e App spec, graph://0ac8e52c-2de
# Operator protocol). v1 ships a static editorial snapshot so the page
# is not blank; live wiring is captured in the bead's "Out of scope"
# section as separate follow-up work.

_DEFAULT_CANVAS = {
    "ageMin": 0,
    "question": "No coordinator canvas published yet.",
    "context": (
        "When a coordinator session writes to "
        "[dashboard.coordinator-canvas](/graph/81e126c8-75e), it shows up "
        "here within one refresh."
    ),
    "quickReplies": [],
}

_DEFAULT_OPERATOR_MESSAGE = {"text": "", "sentAt": None}

_DEFAULT_DOCS = {
    "coordMap": "f1bd5424-6f2",
    "walkthrough": "78b421c2-50c",
}


# ── Helpers ──────────────────────────────────────────────────────────


def _caller_org(request) -> str | None:
    return request.headers.get("X-Graph-Org") or None


def _read_settings_member(set_id: str, *, org: str | None) -> dict | None:
    """Return the most recently-updated member payload for *set_id*.

    Reads the canonical/own-org rows, picks the entry whose
    ``updated_at`` is highest. None when the set is empty or read
    fails — the caller falls back to its default.
    """
    if os.environ.get("DASHBOARD_MOCK"):
        try:
            from tools.dashboard.dao import mock as dao_mock
            members = dao_mock.get_settings_members(set_id, org=org)
        except Exception:
            logger.exception("[coordinator-board] mock read_set failed for %s", set_id)
            return None
        if not members:
            return None
        # Mock fixtures don't expose updated_at reliably; the test
        # fixture pushes a single canonical row, so picking last is
        # equivalent to last-writer-wins.
        return dict(members[-1].get("payload") or {})
    try:
        from tools.graph import settings_ops
        members = settings_ops.read_set(set_id, org=org)
    except Exception:
        logger.exception("[coordinator-board] read_set failed for %s", set_id)
        return None
    rows = list(members.members)
    if not rows:
        return None
    rows.sort(key=lambda m: getattr(m, "updated_at", "") or "", reverse=True)
    payload = rows[0].payload
    return dict(payload) if isinstance(payload, dict) else None


def _list_session_status_rows() -> list[dict]:
    """Return ``/api/dao/session_status``-shape rows; empty on any failure."""
    if os.environ.get("DASHBOARD_MOCK"):
        try:
            from tools.dashboard.dao import mock as dao_mock
            data = dao_mock._load() if hasattr(dao_mock, "_load") else {}
        except Exception:
            return []
        return list(data.get("active_sessions") or [])
    try:
        from tools.dashboard.dao import sessions as dao_sessions
        return dao_sessions.get_session_status_rows(None)
    except Exception:
        logger.exception("[coordinator-board] session_status_rows failed")
        return []


def _age_min(epoch_seconds: float | int | None) -> int:
    if not epoch_seconds:
        return 0
    try:
        delta = datetime.now(timezone.utc).timestamp() - float(epoch_seconds)
    except (TypeError, ValueError):
        return 0
    return max(0, int(delta // 60))


def _tiles_from_sessions(rows: list[dict]) -> list[dict]:
    """Project session rows into tile dicts the page knows how to render.

    Tiles are the coordinator's per-session perspective cards. v1 only
    has access to label/role/age — the editorial ``thing`` and ``asks``
    fields live in graph notes / canvas content, so we mark them as
    ``fyi`` and use the latest message as the thing. Real wiring (bead
    description "Out of scope") will project tile content from a
    Setting once the projection schema is settled.
    """
    out: list[dict] = []
    for row in rows:
        tmux = row.get("tmux_name") or row.get("session_id") or ""
        if not tmux:
            continue
        label = row.get("label") or row.get("project") or tmux
        role = row.get("role") or row.get("type") or "session"
        last_activity = row.get("last_activity") or row.get("last_activity_ts")
        thing = (row.get("last_message") or "").strip() or "(no recent activity)"
        out.append({
            "session": tmux,
            "role": role,
            "label": label,
            "thing": thing[:280],
            "asks": "fyi",
            "ageMin": _age_min(last_activity),
            "updateKind": "refresh",
        })
    return out


def _coordinator_session_name(rows: list[dict]) -> str | None:
    """Return the first active coordinator-role session name, or None."""
    for row in rows:
        if (row.get("role") or "").strip() == "coordinator":
            tmux = (row.get("tmux_name") or "").strip()
            if tmux:
                return tmux
    return None


# ── Route handlers ───────────────────────────────────────────────────


async def api_coordinator_board(request):
    """Return the live board payload for ``/coordinator``.

    Shape mirrors the design's ``data`` literal — see the design
    revision ``f710c702`` of design ``59dd05c1-...`` for the full
    contract. Tabs ``primary`` and ``tracking`` rely on ``canvas``,
    ``operatorMessage``, ``tiles``, ``threads``, ``beads``,
    ``convergentDecisions``, ``openFollowups``, and ``docs``.
    """
    org = _caller_org(request)

    canvas = _read_settings_member(COORDINATOR_CANVAS_SET_ID, org=org) or _DEFAULT_CANVAS
    operator_message = (
        _read_settings_member(OPERATOR_MESSAGE_SET_ID, org=org)
        or dict(_DEFAULT_OPERATOR_MESSAGE)
    )

    rows = _list_session_status_rows()
    tiles = _tiles_from_sessions(rows)

    payload = {
        "snapshotTime": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "broadcastPlaceholder": "Message me…",
        "canvas": canvas,
        "operatorMessage": operator_message,
        "tiles": tiles,
        "threads": [],
        "beads": [],
        "convergentDecisions": [],
        "openFollowups": [],
        "docs": _DEFAULT_DOCS,
    }
    return JSONResponse(payload)


async def api_coordinator_message(request):
    """Persist the operator's latest message to coordinator + tmux send.

    Body: ``{text: str}``. Writes a new
    ``dashboard.operator-message-to-coordinator`` Setting member and,
    when a coordinator-role tmux session is live, sends the same
    message via the existing ``tmux_send`` machinery.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    text = (body.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "text is required"}, status_code=400)

    org = _caller_org(request)
    sent_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    rows = _list_session_status_rows()
    coordinator_tmux = _coordinator_session_name(rows)
    setting_key = coordinator_tmux or "default"

    payload = {"text": text, "sentAt": sent_at}
    persisted = False
    persist_error: str | None = None

    if os.environ.get("DASHBOARD_MOCK"):
        # Mock mode: poke the fixture file so subsequent reads see the
        # message; the DAO re-parses on every request so we have to
        # round-trip through disk, not memory.
        try:
            import json as _json
            from pathlib import Path as _Path
            fixture_path = _Path(os.environ["DASHBOARD_MOCK"])
            data = _json.loads(fixture_path.read_text())
            block = data.setdefault("settings", {})
            row = block.setdefault(OPERATOR_MESSAGE_SET_ID, {})
            all_list = row.setdefault("_all", [])
            all_list[:] = [m for m in all_list if m.get("key") != setting_key]
            all_list.append({"key": setting_key, "payload": payload})
            fixture_path.write_text(_json.dumps(data, indent=2))
            persisted = True
        except Exception as exc:
            persist_error = f"{type(exc).__name__}: {exc}"
            logger.exception("[coordinator-board] mock persist failed")
    else:
        try:
            from tools.graph import settings_ops
            settings_ops.add_setting(
                OPERATOR_MESSAGE_SET_ID,
                SCHEMA_REVISION,
                setting_key,
                payload,
                org=org,
            )
            persisted = True
        except Exception as exc:
            persist_error = f"{type(exc).__name__}: {exc}"
            logger.exception("[coordinator-board] add_setting failed")

    tmux_sent = False
    tmux_error: str | None = None
    if coordinator_tmux and not os.environ.get("DASHBOARD_MOCK"):
        try:
            from tools.dashboard.tmux_send import tmux_send
            await tmux_send(coordinator_tmux, text)
            tmux_sent = True
        except FileNotFoundError as exc:
            tmux_error = "tmux not available"
            logger.warning("[coordinator-board] tmux_send unavailable: %s", exc)
        except Exception as exc:
            tmux_error = f"{type(exc).__name__}: {exc}"
            logger.exception("[coordinator-board] tmux_send failed")

    return JSONResponse({
        "ok": persisted,
        "persisted": persisted,
        "persist_error": persist_error,
        "tmux_session": coordinator_tmux,
        "tmux_sent": tmux_sent,
        "tmux_error": tmux_error,
        "sentAt": sent_at,
    })


# ── Substrate entrypoint ─────────────────────────────────────────────

routes = [
    Route("/api/coordinator/board", api_coordinator_board, methods=["GET"]),
    Route("/api/coordinator/message", api_coordinator_message, methods=["POST"]),
]
