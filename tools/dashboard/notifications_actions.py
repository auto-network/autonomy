"""Refresh-request → CrossTalk source-session ping bridge.

Registered via the settings_mediator's ``@register_action_decorator``
substrate. Fires on every AskRefreshRequestV1 ``setting.changed`` event;
each click bumps ``target_revision`` via :func:`upsert_by_key`, which
fires a fresh event, which invokes this handler. Repeat clicks fire
repeat pings — that's the desired behavior.

``ask_id`` == source session id by the
:class:`SessionAskV1` ``@keyed_per_entity(key="session_id")``
convention; the handler resolves the SessionAsk row to inline a
truncated preview of the current ask text into the CrossTalk envelope.

Provenance:

* Original (workaround) bead: auto-r92kc — direct bus subscriber for
  the ``upsert_by_key`` UPDATE gap in the pre-collapse mediator.
* Migration bead: auto-tdlhq — collapsed onto thin mediator registry
  after auto-rc27t closed the UPDATE gap (every ``setting.changed``
  event reaches every registered handler regardless of INSERT vs
  UPDATE).
* Substrate: auto-5u8zb (notifications schemas).
* Hook seam: auto-5mz65 (``set_emit_hook`` → ``event_bus`` broadcast).
"""
from __future__ import annotations

import asyncio
import logging

from tools.dashboard.notifications_settings import (
    ASK_REFRESH_SET_ID,
    SESSION_ASK_SET_ID,
)
from tools.dashboard.settings_mediator import register_action_decorator
from tools.graph import settings_ops


logger = logging.getLogger("notifications_actions")


_TEXT_PREVIEW_CHARS = 80
_FROM_ID_FALLBACK = "operator:notifications"
_ENVELOPE_KIND = "ask-refresh"


def _resolve_session_ask(ask_id: str, *, org: str | None):
    """Return the SessionAsk row keyed by *ask_id* or None.

    ``org`` is threaded from the originating refresh-row's org so the
    dependent SessionAsk lookup stays scoped to the same DB as the
    event that triggered it. A scopeless refresh row (``org=None``)
    reads scopelessly; an org-scoped refresh row reads only that org's
    DB. Mixing these (e.g. hardcoding ``org=None``) would silently
    cross-pollinate ask previews across orgs in any future multi-org
    deployment — see auto-dcegc.
    """
    members = settings_ops.read_set(SESSION_ASK_SET_ID, org=org, peers=[])
    for m in members.members:
        if m.key == ask_id:
            return m
    return None


def _format_ping_body(ask_row, refresh_payload: dict) -> dict:
    """Build the structured CrossTalk body for a refresh ping."""
    text = (ask_row.payload.get("text") or "").strip().replace("\n", " ")
    if len(text) > _TEXT_PREVIEW_CHARS:
        text = text[:_TEXT_PREVIEW_CHARS] + "…"
    rev = refresh_payload.get("target_revision", 0)
    requested_by = refresh_payload.get("requested_by") or _FROM_ID_FALLBACK
    return {
        "from": requested_by,
        "attrs": {
            "ask_id": refresh_payload.get("ask_id", ""),
            "target_revision": str(rev),
            "requested_by": refresh_payload.get("requested_by") or "",
        },
        "text": f"↻ Refresh requested at revision {rev}: {text}",
    }


@register_action_decorator(
    ASK_REFRESH_SET_ID,
    name="notifications.refresh_ping",
)
async def deliver_refresh_ping(row, services) -> None:
    ask_id = row.key
    ask_row = await asyncio.to_thread(
        _resolve_session_ask, ask_id, org=row.org,
    )
    if ask_row is None:
        logger.warning(
            "[ask_refresh] SessionAsk row missing for ask_id=%s "
            "org=%s (refresh row exists, ask row gone) — skip ping",
            ask_id, row.org,
        )
        return
    body = _format_ping_body(ask_row, row.payload)
    await services.crosstalk.send(
        target=ask_id,
        kind=_ENVELOPE_KIND,
        body=body,
    )
