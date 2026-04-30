"""Async dispatch loop for the settings-mediator substrate.

The loop polls each registered ``set_id`` every ``poll_seconds``, walks
new rows in ``(created_at, id)`` order, and invokes registered handlers
once per (row, handler). Cursor + marker Settings provide
restart-resume and per-row idempotency.

Exposed:

* :func:`start_action_loop` — schedule the loop on the running event
  loop. Idempotent.
* :func:`stop_action_loop` — flag stop, await the in-flight tick to
  finish naturally (graceful drain).
* :func:`iterate_once` — single-pass driver. Tests use this to step the
  loop deterministically without sleeping.

Reads/writes go through :mod:`tools.graph.settings_ops` for cursor /
marker storage and :func:`settings_ops.read_set` for member discovery.
The loop tolerates per-action failures (marker records ``failed``, no
auto-retry) and per-set read failures (logs and retries on next tick;
cursor never advances past unread rows).
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from uuid import uuid4

from tools.graph import settings_ops
from tools.graph.settings_ops import ResolvedSetting

from .schemas import (
    CURSOR_SET_ID,
    CURSOR_REVISION,
    STATE_SET_ID,
    STATE_REVISION,
)


logger = logging.getLogger("settings_mediator")


# Fallback watchdog interval. The primary wakeup path is the EventBus
# ``setting.changed`` subscription; this timer covers missed events on
# bus reconnect, in-process coalescing, or subscriber-disconnect windows
# during shutdown. 30s gives a comfortable safety margin (6× the prior
# 5s poll cadence) without being so slow as to feel broken on the
# fail-over path. See bead auto-5mz65 § "Watchdog interval".
DEFAULT_POLL_SECONDS = 30.0


# ── Row / Services / RegisteredAction ────────────────────────────────


@dataclass
class Row:
    """A single Setting row passed to action handlers and predicates.

    The dataclass exposes typed metadata (``id``, ``set_id``, ``key``,
    ``created_at``, ``updated_at``) plus the parsed ``payload`` dict.
    Mapping-style access (``row['kind']`` / ``'kind' in row`` /
    ``row.get('kind')``) reads from ``payload`` so predicates can be
    written as ``lambda row: row['kind'] == 'foo'`` against the payload
    body.
    """
    id: str
    set_id: str
    key: str
    payload: dict
    created_at: str
    updated_at: str

    def __getitem__(self, k: str) -> Any:
        return self.payload[k]

    def __contains__(self, k: object) -> bool:
        return k in self.payload

    def get(self, k: str, default: Any = None) -> Any:
        return self.payload.get(k, default)


@dataclass
class Services:
    """Cross-plugin primitives an action handler may call.

    Wired by :func:`tools.dashboard.server` at lifespan startup. Adding a
    primitive requires a second consumer that needs it — keep this
    surface tight.
    """
    session_send: Callable[[str, str], Awaitable[None]]
    find_session_by_role: Callable[[str], Awaitable[str | None]]
    log: logging.Logger = field(default_factory=lambda: logger)


@dataclass
class RegisteredAction:
    """One entry in the action registry."""
    set_id: str
    fn: Callable[[Row, Services], Awaitable[None]]
    name: str
    predicate: Callable[[Row], bool] | None = None


# ── Module-level state ───────────────────────────────────────────────

# Sticky across the process: register_action calls populate REGISTRY at
# import time of plugin code; start_action_loop owns the asyncio.Task.
REGISTRY: list[RegisteredAction] = []

_loop_task: asyncio.Task | None = None
_stop_event: asyncio.Event | None = None
_iterating_lock: asyncio.Lock | None = None


# ── Registration ─────────────────────────────────────────────────────


def register_action(
    set_id: str,
    fn: Callable[[Row, Services], Awaitable[None]],
    *,
    predicate: Callable[[Row], bool] | None = None,
    name: str | None = None,
) -> None:
    """Register an action handler for new rows in *set_id*.

    *predicate* filters rows before invoking *fn*; if absent, every new
    row is dispatched. *name* defaults to ``fn.__qualname__`` and shows
    up in idempotency markers + logs — choose distinct names if a
    plugin registers multiple handlers against the same ``set_id``.
    """
    resolved_name = name or getattr(fn, "__qualname__", None) or repr(fn)
    REGISTRY.append(
        RegisteredAction(
            set_id=set_id,
            fn=fn,
            name=resolved_name,
            predicate=predicate,
        )
    )


def register_action_decorator(
    set_id: str,
    *,
    predicate: Callable[[Row], bool] | None = None,
    name: str | None = None,
) -> Callable[
    [Callable[[Row, Services], Awaitable[None]]],
    Callable[[Row, Services], Awaitable[None]],
]:
    """Decorator sugar over :func:`register_action`.

    .. code-block:: python

        @register_action_decorator("dashboard.coordinator-decision",
                                   predicate=lambda r: r['kind'] == 'thumb_yes')
        async def my_handler(row, svc): ...
    """
    def _wrap(fn: Callable[[Row, Services], Awaitable[None]]):
        register_action(set_id, fn, predicate=predicate, name=name)
        return fn
    return _wrap


def clear_registry() -> None:
    """Test helper — reset the in-process registry."""
    REGISTRY.clear()


# ── Cursor + marker persistence ──────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_cursor(set_id: str) -> tuple[str, str] | None:
    """Return ``(lastSeenAt, lastRowId)`` or ``None`` if no cursor stored.

    Reads the latest base member of ``dashboard.action-registry-cursor#1``
    keyed by *set_id*. ``read_set`` already returns one row per key
    (latest by precedence/created_at), so we just look up the key.
    """
    members = settings_ops.read_set(CURSOR_SET_ID)
    for m in members.members:
        if m.key == set_id:
            payload = m.payload if isinstance(m.payload, dict) else {}
            last_row = payload.get("lastRowId") or ""
            last_seen = payload.get("lastSeenAt") or ""
            if last_row and last_seen:
                return last_seen, last_row
            return None
    return None


def _write_cursor(set_id: str, last_row_id: str, last_seen_at: str) -> None:
    """Upsert the cursor for *set_id* directly via SQL.

    We bypass ``add_setting`` to avoid bloat: cursor advancement happens
    once per processed row and we only ever care about the latest value.
    A direct UPDATE-or-INSERT keyed on (set_id, key) keeps one row
    per cursor for the lifetime of the substrate.
    """
    payload = {"lastRowId": last_row_id, "lastSeenAt": last_seen_at}
    serialized = json.dumps(payload, sort_keys=True)
    now = _now_iso()
    db = settings_ops._open(None)
    try:
        row = db.conn.execute(
            "SELECT id FROM settings WHERE set_id = ? AND key = ? "
            "  AND supersedes IS NULL AND excludes IS NULL",
            (CURSOR_SET_ID, set_id),
        ).fetchone()
        if row is not None:
            db.conn.execute(
                "UPDATE settings SET payload = ?, updated_at = ? WHERE id = ?",
                (serialized, now, row["id"]),
            )
        else:
            db.conn.execute(
                "INSERT INTO settings(id, set_id, schema_revision, key, "
                "payload, publication_state, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (str(uuid4()), CURSOR_SET_ID, int(CURSOR_REVISION),
                 set_id, serialized, "canonical", now, now),
            )
        db.conn.commit()
    finally:
        db.close()


def _marker_key(set_id: str, row_id: str, action_name: str) -> str:
    return f"{set_id}:{row_id}:{action_name}"


def _marker_exists(marker_key: str) -> bool:
    """Return True if a state marker for *marker_key* already exists.

    A direct SQL probe keeps the per-row hot path cheap — we don't need
    the resolved-set machinery, just an indexed lookup.
    """
    db = settings_ops._open(None)
    try:
        row = db.conn.execute(
            "SELECT 1 FROM settings WHERE set_id = ? AND key = ? "
            "  AND supersedes IS NULL AND excludes IS NULL "
            "  LIMIT 1",
            (STATE_SET_ID, marker_key),
        ).fetchone()
        return row is not None
    finally:
        db.close()


def _write_marker(
    marker_key: str,
    *,
    status: str,
    error: str | None = None,
) -> None:
    """Insert (or replace) the marker for *marker_key*.

    Idempotency relies on a single base row per ``(set_id, key)`` pair,
    so we upsert just like the cursor. ``status='filtered'`` is used
    when a predicate rejected the row — the marker still lands so the
    loop doesn't re-evaluate the predicate every tick.
    """
    payload: dict[str, Any] = {
        "processedAt": _now_iso(),
        "status": status,
    }
    if error is not None:
        payload["error"] = error
    serialized = json.dumps(payload, sort_keys=True)
    now = _now_iso()
    db = settings_ops._open(None)
    try:
        existing = db.conn.execute(
            "SELECT id FROM settings WHERE set_id = ? AND key = ? "
            "  AND supersedes IS NULL AND excludes IS NULL",
            (STATE_SET_ID, marker_key),
        ).fetchone()
        if existing is not None:
            db.conn.execute(
                "UPDATE settings SET payload = ?, updated_at = ? WHERE id = ?",
                (serialized, now, existing["id"]),
            )
        else:
            db.conn.execute(
                "INSERT INTO settings(id, set_id, schema_revision, key, "
                "payload, publication_state, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (str(uuid4()), STATE_SET_ID, int(STATE_REVISION),
                 marker_key, serialized, "canonical", now, now),
            )
        db.conn.commit()
    finally:
        db.close()


# ── Member iteration ─────────────────────────────────────────────────


def _resolved_to_row(m: ResolvedSetting) -> Row:
    payload = m.payload if isinstance(m.payload, dict) else {}
    return Row(
        id=m.id,
        set_id=m.set_id,
        key=m.key,
        payload=dict(payload),
        created_at=m.created_at or "",
        updated_at=m.updated_at or "",
    )


def _members_since(
    set_id: str,
    cursor: tuple[str, str] | None,
) -> list[ResolvedSetting]:
    """Return resolved members of *set_id* strictly after *cursor*.

    ``read_set`` deduplicates by key (one base row per key per the
    resolution algorithm), which is exactly what action handlers expect:
    the substrate fires once per logical Setting member. Sort order is
    ``(created_at, id)`` so re-polls produce a stable sequence.
    """
    members = settings_ops.read_set(set_id)
    rows = sorted(
        members.members,
        key=lambda m: (m.created_at or "", m.id),
    )
    if cursor is None:
        return rows
    last_seen_at, last_row_id = cursor
    cutoff = (last_seen_at, last_row_id)
    return [m for m in rows if (m.created_at or "", m.id) > cutoff]


# ── Loop body ────────────────────────────────────────────────────────


async def iterate_once(services: Services) -> None:
    """Walk each registered set once, dispatching new rows.

    Public so tests can drive the loop deterministically without
    sleeping. Production callers go through :func:`start_action_loop`.
    """
    # Group actions by set_id so we read each set once per tick. Order
    # within a set is registration order — multi-handler fan-out is
    # not concurrent; one failure does not block siblings.
    by_set: dict[str, list[RegisteredAction]] = {}
    for action in REGISTRY:
        by_set.setdefault(action.set_id, []).append(action)

    for set_id, actions in by_set.items():
        try:
            cursor = _read_cursor(set_id)
            new_rows = _members_since(set_id, cursor)
        except Exception:
            logger.exception(
                "[settings_mediator] read failure for set_id=%s; "
                "will retry on next tick",
                set_id,
            )
            continue

        for resolved in new_rows:
            row = _resolved_to_row(resolved)
            for action in actions:
                marker = _marker_key(set_id, resolved.id, action.name)
                try:
                    if _marker_exists(marker):
                        continue
                except Exception:
                    logger.exception(
                        "[settings_mediator] marker-read failed for %s; "
                        "skipping this handler this tick",
                        marker,
                    )
                    continue

                if action.predicate is not None:
                    try:
                        accepted = bool(action.predicate(row))
                    except Exception as exc:
                        logger.exception(
                            "[settings_mediator] predicate raised for "
                            "set=%s action=%s row=%s",
                            set_id, action.name, resolved.id,
                        )
                        _safe_write_marker(
                            marker, status="failed",
                            error=f"predicate: {type(exc).__name__}: {exc}",
                        )
                        continue
                    if not accepted:
                        _safe_write_marker(marker, status="filtered")
                        continue

                try:
                    await action.fn(row, services)
                    _safe_write_marker(marker, status="ok")
                except Exception as exc:
                    logger.exception(
                        "[settings_mediator] handler %s raised on "
                        "set=%s row=%s",
                        action.name, set_id, resolved.id,
                    )
                    _safe_write_marker(
                        marker, status="failed",
                        error=f"{type(exc).__name__}: {exc}",
                    )

            # Cursor advances only after every registered handler has
            # been considered — keeps multi-handler fan-out atomic per
            # row, so a process crash mid-fan-out re-invokes only the
            # missing handlers (each guarded by its own marker).
            try:
                _write_cursor(
                    set_id, resolved.id, resolved.created_at or "",
                )
            except Exception:
                logger.exception(
                    "[settings_mediator] cursor write failed for "
                    "set=%s row=%s; will retry next tick",
                    set_id, resolved.id,
                )
                # Don't try further rows in this set — the next tick
                # will re-evaluate from the still-old cursor and
                # marker checks will skip already-processed (row,
                # handler) pairs.
                break


def _safe_write_marker(
    marker: str,
    *,
    status: str,
    error: str | None = None,
) -> None:
    """Best-effort marker write — surface logger noise but never raise."""
    try:
        _write_marker(marker, status=status, error=error)
    except Exception:
        logger.exception(
            "[settings_mediator] marker write failed for %s "
            "(status=%s)", marker, status,
        )


async def _bus_demuxer(
    bus_queue: asyncio.Queue,
    wakeup_event: asyncio.Event,
    stop_event: asyncio.Event,
) -> None:
    """Read the EventBus subscription queue and trigger ``wakeup_event``.

    Filters incoming ``setting.changed`` events by ``set_id`` against
    the live :data:`REGISTRY` so unrelated traffic doesn't wake the
    loop. The bus replays cached topic state on subscribe with
    ``seq=0`` — those replays are not "new events", so we skip them
    and rely on the loop's startup tick + fallback watchdog instead.

    Cancellation (driven by the loop's ``finally`` block at shutdown)
    surfaces as :class:`asyncio.CancelledError` from
    ``bus_queue.get()`` and exits the task cleanly.
    """
    while not stop_event.is_set():
        try:
            entry = await bus_queue.get()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "[settings_mediator] bus queue read failed; "
                "demuxer will continue"
            )
            await asyncio.sleep(0.05)
            continue
        try:
            topic, data, seq = entry
        except (TypeError, ValueError):
            continue
        if topic != "setting.changed":
            continue
        if seq == 0:
            # Cached-state replay on subscribe — not a new event.
            continue
        if not isinstance(data, dict):
            continue
        set_id = data.get("set_id")
        if not set_id:
            continue
        if any(a.set_id == set_id for a in REGISTRY):
            wakeup_event.set()


async def _loop_main(
    services: Services,
    fallback_seconds: float,
    stop_event: asyncio.Event,
    iterating_lock: asyncio.Lock,
    event_bus: Any | None,
) -> None:
    """Tick until *stop_event* is set; drain in-flight tick on shutdown.

    Wakeup signals (descending priority):

    * ``stop_event`` — bounded shutdown.
    * ``wakeup_event`` — set by :func:`_bus_demuxer` when a relevant
      ``setting.changed`` arrives. Primary wakeup path.
    * ``fallback_seconds`` watchdog — wakes ``iterate_once`` for all
      registered sets unconditionally. Covers missed events on bus
      reconnect or in-process coalescing; never the primary path.

    The lock is held across the entire iteration so
    :func:`stop_action_loop` can await it and observe the tick is fully
    drained before returning.
    """
    wakeup_event = asyncio.Event()
    bus_queue: asyncio.Queue | None = None
    demuxer_task: asyncio.Task | None = None
    if event_bus is not None:
        try:
            bus_queue = event_bus.subscribe()
            demuxer_task = asyncio.create_task(
                _bus_demuxer(bus_queue, wakeup_event, stop_event),
                name="settings_mediator.demuxer",
            )
        except Exception:
            logger.exception(
                "[settings_mediator] event_bus.subscribe() failed; "
                "falling back to watchdog-only wakeup"
            )
            bus_queue = None
            demuxer_task = None

    logger.info(
        "[settings_mediator] loop started — %d action(s) registered, "
        "fallback=%.1fs%s",
        len(REGISTRY),
        fallback_seconds,
        " (event-driven)" if demuxer_task is not None else " (poll-only)",
    )
    try:
        while not stop_event.is_set():
            async with iterating_lock:
                try:
                    await iterate_once(services)
                except Exception:
                    logger.exception(
                        "[settings_mediator] iterate_once raised; "
                        "swallowing and continuing"
                    )
            if stop_event.is_set():
                break
            # Wait for: stop OR a relevant setting.changed wakeup OR the
            # fallback watchdog timeout. Whichever fires first wins.
            wakeup_task = asyncio.create_task(wakeup_event.wait())
            stop_task = asyncio.create_task(stop_event.wait())
            try:
                done, pending = await asyncio.wait(
                    {wakeup_task, stop_task},
                    timeout=fallback_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for t in (wakeup_task, stop_task):
                    if not t.done():
                        t.cancel()
            wakeup_event.clear()
    finally:
        if demuxer_task is not None:
            demuxer_task.cancel()
            try:
                await demuxer_task
            except (asyncio.CancelledError, Exception):
                pass
        if bus_queue is not None and event_bus is not None:
            try:
                event_bus.unsubscribe(bus_queue)
            except Exception:
                logger.exception(
                    "[settings_mediator] event_bus.unsubscribe() failed; "
                    "continuing shutdown"
                )
        logger.info("[settings_mediator] loop stopped")


def start_action_loop(
    services: Services,
    *,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    event_bus: Any | None = None,
) -> asyncio.Task:
    """Start the dispatch loop on the running event loop.

    Wakeup is event-driven when ``event_bus`` is provided: the loop
    subscribes to it at start, demuxes ``setting.changed`` events for
    registered ``set_id``s, and ticks immediately on receipt. The
    ``poll_seconds`` argument is the fallback watchdog interval (kept
    under that name for compatibility with existing tests that pass
    sub-second values for fast iteration).

    Idempotent: a second call while the loop is already running is a
    logged no-op and returns the existing task.
    """
    global _loop_task, _stop_event, _iterating_lock
    if _loop_task is not None and not _loop_task.done():
        logger.warning(
            "[settings_mediator] start_action_loop() called while loop "
            "already running; returning existing task"
        )
        return _loop_task
    _stop_event = asyncio.Event()
    _iterating_lock = asyncio.Lock()
    _loop_task = asyncio.create_task(
        _loop_main(
            services, poll_seconds, _stop_event, _iterating_lock, event_bus,
        ),
        name="settings_mediator.loop",
    )
    return _loop_task


async def stop_action_loop(*, drain_timeout: float = 30.0) -> None:
    """Signal stop; await the in-flight tick to finish naturally.

    Per acceptance #7 we don't ``cancel()`` the loop — we let it
    observe the stop flag at the next tick boundary so any handler
    currently awaiting completes. ``drain_timeout`` is a backstop:
    if a handler hangs, we cancel after the deadline so shutdown
    doesn't block forever.
    """
    global _loop_task, _stop_event, _iterating_lock
    task = _loop_task
    stop = _stop_event
    if task is None or stop is None:
        return
    stop.set()
    try:
        await asyncio.wait_for(task, timeout=drain_timeout)
    except asyncio.TimeoutError:
        logger.warning(
            "[settings_mediator] drain timeout (%.1fs) exceeded; "
            "cancelling loop task", drain_timeout,
        )
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    except asyncio.CancelledError:
        pass
    finally:
        _loop_task = None
        _stop_event = None
        _iterating_lock = None
