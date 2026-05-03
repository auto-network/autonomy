"""Refresh-request → CrossTalk source-session ping bridge.

When an operator clicks "↻ Refresh" on a SessionAsk row in the
Activity tab's Notifications panel, the substrate writes an
:class:`tools.dashboard.notifications_settings.AskRefreshRequestV1`
row pinned to the SessionAsk's current ``revision_seq``. Without
this module, the source session has no way to know — the
"requested" state pins forever waiting for a response from a
session that doesn't know to respond. See the refresh state
machine in graph://75c03f1d-4cd.

This module subscribes to the in-process EventBus' ``setting.changed``
channel (auto-5mz65). On each event whose ``set_id`` is
``dashboard.activity.ask_refresh``, the dispatcher:

1. Reads the AskRefreshRequest row by key (= ``ask_id``, which is the
   source session id by the SessionAsk ``@keyed_per_entity(key=
   "session_id")`` convention — see ``activity.js`` "ask_id (Setting
   key, == session_id)" comment).
2. Reads the SessionAsk row to inline an 80-char preview of the
   current ask text into the CrossTalk envelope.
3. Compares ``target_revision`` against an in-process tracker of the
   last-delivered ping for this ask. Skips if equal-or-lower.
4. Reserves the new ``target_revision`` in the tracker BEFORE sending
   so a transient ``send_fn`` failure doesn't latch into a retry
   loop. The tracker is in-memory only; restarts forget it, which is
   acceptable per the bead spec — operators re-engage when the source
   session comes back.
5. Sends a CrossTalk envelope to the source session via the same
   ``tmux_send`` plumbing as :class:`tools.dashboard.surface_actions.
   CrosstalkService`.

The mediator pattern (used by surface_actions.py) is deliberately
NOT used here: it advances per-set cursors by ``created_at``, so an
``upsert_by_key`` UPDATE that bumps ``target_revision`` on the same
row id is invisible to it. Direct EventBus subscription sees every
write, including updates — which is exactly what bumping
``target_revision`` requires.

Provenance:

* Bead: auto-r92kc — refresh-request → source-session ping
* Substrate: auto-5u8zb (notifications schemas)
* Hook seam: auto-5mz65 (``set_emit_hook`` → ``event_bus`` broadcast)
* Envelope helper: auto-9gxo8 (``surface_actions.build_envelope``)
* Design: graph://75c03f1d-4cd (refresh state machine)
"""
from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from tools.dashboard.event_bus import event_bus as _default_event_bus
from tools.dashboard.notifications_settings import (
    ASK_REFRESH_SET_ID,
    SESSION_ASK_SET_ID,
)
from tools.dashboard.surface_actions import build_envelope
from tools.graph import settings_ops


logger = logging.getLogger("notifications_actions")


# Inline preview cap on the CrossTalk body's quoted ask text. The
# substrate's hard cap on SessionAskV1.text is 2KB; 80 chars is enough
# for the source session to identify which ask is being refreshed
# without flooding the recipient's terminal.
_TEXT_PREVIEW_CHARS = 80


# Sender id baked into the envelope's ``from="..."`` attribute when the
# row's ``requested_by`` is missing or empty. The dispatcher always
# carries the operator id when present.
_FROM_ID_FALLBACK = "operator:notifications"


# Envelope ``kind=`` for ask-refresh messages. Stable string —
# receivers may key on it.
_ENVELOPE_KIND = "ask-refresh-request"


SendFn = Callable[[str, str], Awaitable[None]]


# In-process idempotency tracker. Key: (org_or_empty, ask_id). Value:
# the last ``target_revision`` for which a CrossTalk ping was attempted
# (success OR failure). Updated under :data:`_DELIVERED_LOCK` to keep
# the check-then-set atomic in the face of concurrent dispatcher ticks.
#
# Rationale for "even on failure": the bead spec calls out
# "doesn't loop" — a sticky reservation prevents a flaky source
# session from triggering a ping storm if multiple updates land for the
# same ``target_revision``.
_DELIVERED: dict[tuple[str, str], int] = {}
_DELIVERED_LOCK = threading.Lock()


def reset_delivered_tracker() -> None:
    """Test helper — clear in-memory dedup state."""
    with _DELIVERED_LOCK:
        _DELIVERED.clear()


# ── Helpers ─────────────────────────────────────────────────────────


def _read_payload(
    set_id: str, key: str, *, org: str | None,
) -> dict | None:
    """Return the latest base payload for ``(set_id, key)`` or None.

    ``peers=[]`` keeps the read local to *org* — the refresh row and
    its target SessionAsk both live in the same per-org DB.
    """
    members = settings_ops.read_set(set_id, org=org, peers=[])
    for m in members.members:
        if m.key == key:
            payload = m.payload if isinstance(m.payload, dict) else {}
            return dict(payload)
    return None


def _format_ping_body(
    *, target_revision: int, ask_text: str,
) -> str:
    """Render the CrossTalk envelope body per the bead spec.

    The literal trailing ``"..."`` is intentional even when ``ask_text``
    is short — it matches the body template in the bead description and
    signals to the recipient that text was abbreviated.
    """
    preview = (ask_text or "")[:_TEXT_PREVIEW_CHARS]
    return (
        f"Operator requested refresh on your ask at "
        f"rev={target_revision}.\n"
        f"Ask text: \"{preview}...\"\n"
        f"Respond by writing a newer ask revision (revision_seq++) "
        f"or removing the ask. The refresh button stays \"requested\" "
        f"until you respond."
    )


# ── Delivery ────────────────────────────────────────────────────────


async def deliver_refresh_ping(
    *,
    ask_id: str,
    org: str | None,
    send_fn: SendFn,
) -> bool:
    """Deliver a CrossTalk refresh ping for *ask_id*.

    Returns ``True`` if a ping was sent (or attempted — the tracker
    advances on attempt, not success), ``False`` if skipped because:

    * the AskRefreshRequest row is missing (raced with a write),
    * ``target_revision`` is malformed,
    * the SessionAsk row is missing,
    * the row's ``target_revision`` has already been delivered
      (or surpassed — defensive).

    The function is safe to call concurrently for distinct ``ask_id``s
    and is idempotent for repeated calls at the same
    ``target_revision``.
    """
    refresh = _read_payload(ASK_REFRESH_SET_ID, ask_id, org=org)
    if refresh is None:
        # Race window: event fired but row resolved away (excluded /
        # promoted). No ping to send.
        logger.debug(
            "[ask_refresh] no row for ask_id=%s org=%s — skip",
            ask_id, org,
        )
        return False

    target_rev = refresh.get("target_revision")
    # bool is a subclass of int — exclude it explicitly so a payload
    # that happens to deserialise ``True`` as a revision doesn't slip
    # through and get arithmetic-compared to an int tracker entry.
    if isinstance(target_rev, bool) or not isinstance(target_rev, int):
        logger.debug(
            "[ask_refresh] bad target_revision (%r) for ask_id=%s — skip",
            target_rev, ask_id,
        )
        return False

    tracker_key = (org or "", ask_id)
    with _DELIVERED_LOCK:
        last = _DELIVERED.get(tracker_key)
        if last is not None and last >= target_rev:
            return False
        # Reserve BEFORE sending. A subsequent event at the SAME
        # target_revision will short-circuit; only a strictly higher
        # target_revision retries delivery. This is the "doesn't loop"
        # invariant called out in the bead spec.
        _DELIVERED[tracker_key] = target_rev

    # Resolve source session id + ask text from the SessionAsk row.
    # ``ask_id`` IS the session_id by the SessionAskV1
    # ``@keyed_per_entity(key="session_id")`` convention. We still
    # prefer the explicit ``session_id`` field on the SessionAsk
    # payload over the key in case a writer keys differently in the
    # future.
    ask_payload = _read_payload(SESSION_ASK_SET_ID, ask_id, org=org)
    if ask_payload is None:
        logger.warning(
            "[ask_refresh] SessionAsk row missing for ask_id=%s "
            "(refresh row exists, ask row gone) — skip ping",
            ask_id,
        )
        return False

    source_session_id = ask_payload.get("session_id") or ask_id
    ask_text = ask_payload.get("text") or ""
    requested_by = refresh.get("requested_by") or ""

    body = _format_ping_body(
        target_revision=target_rev, ask_text=ask_text,
    )
    envelope = build_envelope(
        from_id=requested_by or _FROM_ID_FALLBACK,
        kind=_ENVELOPE_KIND,
        extra={
            "ask_id": ask_id,
            "target_revision": str(target_rev),
            "requested_by": requested_by,
        },
        body=body,
    )

    try:
        await send_fn(source_session_id, envelope)
    except Exception:
        logger.warning(
            "[ask_refresh] CrossTalk send failed for source session=%s "
            "ask_id=%s target_revision=%d (suppressed; tracker pinned "
            "to prevent retry loop)",
            source_session_id, ask_id, target_rev, exc_info=True,
        )
        return False
    return True


# ── Dispatcher loop ─────────────────────────────────────────────────


@dataclass
class DispatcherHandle:
    """Returned by :func:`start_notifications_dispatcher`."""
    task: asyncio.Task
    stop_event: asyncio.Event
    queue: asyncio.Queue


_running: DispatcherHandle | None = None


async def _dispatcher_main(
    queue: asyncio.Queue,
    stop_event: asyncio.Event,
    send_fn: SendFn,
) -> None:
    """Pump events off the bus queue and dispatch ASK_REFRESH writes.

    Mirrors the structure of
    :func:`tools.dashboard.settings_mediator.loop._bus_demuxer` —
    skips the initial cached-state replay (``seq == 0``), filters by
    ``set_id``, and tolerates per-event handler failures so one bad
    row doesn't kill the loop.
    """
    while not stop_event.is_set():
        try:
            item = await queue.get()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "[notifications_actions] queue read failed; continuing"
            )
            await asyncio.sleep(0.05)
            continue
        try:
            topic, data, seq = item
        except (TypeError, ValueError):
            continue
        if topic != "setting.changed":
            continue
        if seq == 0:
            # Cached-state replay on subscribe — not a live event.
            continue
        if not isinstance(data, dict):
            continue
        if data.get("set_id") != ASK_REFRESH_SET_ID:
            continue
        ask_id = data.get("key")
        if not isinstance(ask_id, str) or not ask_id:
            continue
        org_value = data.get("org")
        org = org_value if isinstance(org_value, str) else None
        try:
            await deliver_refresh_ping(
                ask_id=ask_id, org=org, send_fn=send_fn,
            )
        except Exception:
            logger.exception(
                "[notifications_actions] deliver_refresh_ping raised "
                "for ask_id=%s org=%s",
                ask_id, org,
            )


def start_notifications_dispatcher(
    *,
    send_fn: SendFn,
    bus: Any | None = None,
) -> DispatcherHandle:
    """Start the dispatcher task on the running event loop.

    Idempotent — a second call while the dispatcher is already running
    logs a warning and returns the existing handle.
    """
    global _running
    if _running is not None and not _running.task.done():
        logger.warning(
            "[notifications_actions] start_notifications_dispatcher() "
            "called while dispatcher already running; returning "
            "existing handle"
        )
        return _running
    bus_obj = bus if bus is not None else _default_event_bus
    queue = bus_obj.subscribe()
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        _dispatcher_main(queue, stop_event, send_fn),
        name="notifications_actions.dispatcher",
    )
    _running = DispatcherHandle(
        task=task, stop_event=stop_event, queue=queue,
    )
    return _running


async def stop_notifications_dispatcher(
    *, drain_timeout: float = 5.0, bus: Any | None = None,
) -> None:
    """Signal stop, drain the in-flight handler, unsubscribe from bus.

    No-op when the dispatcher isn't running. ``drain_timeout`` is a
    backstop on a hung handler — past the deadline the task is
    cancelled so shutdown doesn't block forever.
    """
    global _running
    handle = _running
    if handle is None:
        return
    handle.stop_event.set()
    handle.task.cancel()
    try:
        await asyncio.wait_for(handle.task, timeout=drain_timeout)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass
    except Exception:
        logger.exception(
            "[notifications_actions] dispatcher raised during shutdown"
        )
    bus_obj = bus if bus is not None else _default_event_bus
    try:
        bus_obj.unsubscribe(handle.queue)
    except Exception:
        logger.exception(
            "[notifications_actions] event_bus.unsubscribe() failed; "
            "continuing shutdown"
        )
    _running = None
