"""Operator-action → CrossTalk source-session ping bridges.

Two handlers, both registered via the settings_mediator's
``@register_action_decorator`` substrate.

* :func:`deliver_refresh_ping` — fires on every
  :class:`AskRefreshRequestV1` ``setting.changed`` event. Each click
  bumps ``target_revision`` via :func:`upsert_by_key`, which fires a
  fresh event, which invokes this handler. Repeat clicks fire repeat
  pings — that's the desired behavior.

* :func:`deliver_vote_ping` — fires on every :class:`AskVoteV1`
  ``setting.changed`` event. The source session learns the operator's
  stance (👍 / 👎) by receiving a CrossTalk envelope identified as
  ``from="dashboard:<voter_id>"`` so the receiving session can tell a
  UI-driven vote from a peer-session message.

``ask_id`` == source session id by the
:class:`SessionAskV2` ``@keyed_per_entity(key="session_id")``
convention; both handlers resolve the SessionAsk row to inline a
truncated preview of the current ask body into the envelope.

Provenance:

* Original (workaround) bead: auto-r92kc — direct bus subscriber for
  the ``upsert_by_key`` UPDATE gap in the pre-collapse mediator.
* Migration bead: auto-tdlhq — collapsed onto thin mediator registry
  after auto-rc27t closed the UPDATE gap (every ``setting.changed``
  event reaches every registered handler regardless of INSERT vs
  UPDATE).
* Substrate: auto-5u8zb (notifications schemas).
* Hook seam: auto-5mz65 (``set_emit_hook`` → ``event_bus`` broadcast).
* Vote ping + v2 zoom fields: this commit (PUNCH list).
"""
from __future__ import annotations

import asyncio
import logging

from tools.dashboard.notifications_settings import (
    ASK_REFRESH_SET_ID,
    ASK_VOTE_SET_ID,
    SESSION_ASK_SET_ID,
)
from tools.dashboard.settings_mediator import register_action_decorator
from tools.graph import settings_ops


logger = logging.getLogger("notifications_actions")


_TEXT_PREVIEW_CHARS = 80
_FROM_ID_FALLBACK = "operator:notifications"
_VOTE_FROM_PREFIX = "dashboard:"
_REFRESH_ENVELOPE_KIND = "ask-refresh"
_VOTE_ENVELOPE_KIND = "ask-vote"
# Back-compat alias kept so external importers don't break.
_ENVELOPE_KIND = _REFRESH_ENVELOPE_KIND


def _resolve_session_ask(ask_id: str, *, org: str | None):
    """Return the SessionAsk row keyed by *ask_id* or None.

    ``org`` is threaded from the originating row's org so the
    dependent SessionAsk lookup stays scoped to the same DB as the
    event that triggered it. A scopeless event row (``org=None``)
    reads scopelessly; an org-scoped event row reads only that org's
    DB. Mixing these (e.g. hardcoding ``org=None``) would silently
    cross-pollinate ask previews across orgs in any future multi-org
    deployment — see auto-dcegc.
    """
    members = settings_ops.read_set(SESSION_ASK_SET_ID, org=org, peers=[])
    for m in members.members:
        if m.key == ask_id:
            return m
    return None


def _ask_preview(ask_row) -> str:
    """Pick the most informative body field and truncate to a preview.

    v2 rows expose ``compact`` / ``normal`` / ``expanded``; v1 rows are
    upconverted on read so ``normal`` carries the legacy ``text``.
    Prefer ``normal``, fall back to ``compact`` then ``expanded`` then
    the legacy ``text`` field for any row that bypassed the
    upconverter (defensive — should not happen in practice).
    """
    payload = ask_row.payload or {}
    body = (
        payload.get("normal")
        or payload.get("compact")
        or payload.get("expanded")
        or payload.get("text")
        or ""
    )
    body = body.strip().replace("\n", " ")
    if len(body) > _TEXT_PREVIEW_CHARS:
        body = body[:_TEXT_PREVIEW_CHARS] + "…"
    return body


def _format_refresh_body(ask_row, refresh_payload: dict) -> dict:
    """Build the structured CrossTalk body for a refresh ping."""
    preview = _ask_preview(ask_row)
    rev = refresh_payload.get("target_revision", 0)
    requested_by = refresh_payload.get("requested_by") or _FROM_ID_FALLBACK
    return {
        "from": requested_by,
        "attrs": {
            "ask_id": refresh_payload.get("ask_id", ""),
            "target_revision": str(rev),
            "requested_by": refresh_payload.get("requested_by") or "",
        },
        "text": f"↻ Refresh requested at revision {rev}: {preview}",
    }


# Back-compat alias for external test imports.
def _format_ping_body(ask_row, refresh_payload: dict) -> dict:
    return _format_refresh_body(ask_row, refresh_payload)


def _format_vote_body(ask_row, vote_payload: dict) -> dict:
    """Build the structured CrossTalk body for a vote ping.

    The ``from`` attribution stamps ``dashboard:<voter_id>`` so the
    receiving session can distinguish a UI-issued vote from a
    peer-session CrossTalk message. ``voter_id`` falls back to
    ``"operator"`` if the writer omitted it.
    """
    preview = _ask_preview(ask_row)
    direction = vote_payload.get("direction", "")
    voter_id = vote_payload.get("voter_id") or "operator"
    glyph = "👍" if direction == "up" else ("👎" if direction == "down" else "?")
    return {
        "from": f"{_VOTE_FROM_PREFIX}{voter_id}",
        "attrs": {
            "ask_id": vote_payload.get("ask_id", ""),
            "direction": direction,
            "voter_id": voter_id,
        },
        "text": f"{glyph} Operator vote ({direction}): {preview}",
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
    body = _format_refresh_body(ask_row, row.payload)
    await services.crosstalk.send(
        target=ask_id,
        kind=_REFRESH_ENVELOPE_KIND,
        body=body,
    )


@register_action_decorator(
    ASK_VOTE_SET_ID,
    name="notifications.vote_ping",
)
async def deliver_vote_ping(row, services) -> None:
    """Forward an operator vote (👍 / 👎) to the source session.

    Vote rows are keyed ``"<ask_id>:<voter_id>"``; the ask id is the
    source session id. We resolve the SessionAsk to inline a body
    preview, then deliver via the existing CrosstalkService — no new
    transport, no new endpoint.
    """
    ask_id = row.payload.get("ask_id") if row.payload else None
    if not ask_id:
        # Defensive: a vote row without ask_id has nowhere to deliver.
        logger.warning(
            "[ask_vote] payload missing ask_id (key=%s org=%s) — skip ping",
            row.key, row.org,
        )
        return
    ask_row = await asyncio.to_thread(
        _resolve_session_ask, ask_id, org=row.org,
    )
    if ask_row is None:
        logger.warning(
            "[ask_vote] SessionAsk row missing for ask_id=%s "
            "org=%s (vote row exists, ask row gone) — skip ping",
            ask_id, row.org,
        )
        return
    body = _format_vote_body(ask_row, row.payload)
    await services.crosstalk.send(
        target=ask_id,
        kind=_VOTE_ENVELOPE_KIND,
        body=body,
    )
