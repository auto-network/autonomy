"""Settings-mediator: thin handler registry over the in-process EventBus.

A registered action is a coroutine ``(row, services) -> None``. The loop
subscribes to the dashboard's ``EventBus``, filters to ``setting.changed``
events for registered ``set_id``s, resolves the row from
``settings_ops.read_set``, and fans out to every matching handler.

The substrate is at-most-once across process restart: events fired while
the dashboard is down are lost. Every current handler is a tmux /
CrossTalk send — operator-visible, idempotent in practice — so the
trade-off is "worst case the operator sees a duplicate prompt."

Cursor / marker / predicate / 30s watchdog scaffolding (~700 LOC) was
collapsed away in bead auto-rc27t. The audit (this session) showed those
were defending against drop modes that don't exist for an in-process
consumer: bus subscriber queues are unbounded, the mediator and
dashboard share a process, and the chronological replay buffer is for
SSE clients reconnecting over the network — not in-process consumers.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from tools.graph import settings_ops
from tools.graph.settings_ops import ResolvedSetting


logger = logging.getLogger("settings_mediator")


# ── Row / Services / RegisteredAction ────────────────────────────────


@dataclass
class Row:
    """A single Setting row passed to action handlers.

    Mapping-style access (``row['kind']`` / ``'kind' in row`` /
    ``row.get('kind')``) reads from ``payload`` so handlers can read
    payload fields without unwrapping.

    ``org`` is the originating DB's org slug (``None`` for the scopeless
    DB). Handlers that perform a follow-on Settings lookup keyed by data
    in the row payload (e.g. refresh-ping resolving a SessionAsk by
    ``ask_id``) must thread this through the lookup so the dependent
    read stays scoped to the same org as the triggering event — see
    auto-dcegc.
    """
    id: str
    set_id: str
    key: str
    payload: dict
    created_at: str
    updated_at: str
    org: str | None = None

    def __getitem__(self, k: str) -> Any:
        return self.payload[k]

    def __contains__(self, k: object) -> bool:
        return k in self.payload

    def get(self, k: str, default: Any = None) -> Any:
        return self.payload.get(k, default)


class _UnboundCrosstalk:
    """Default placeholder for :attr:`Services.crosstalk` — raises on use.

    Production wires a real :class:`tools.dashboard.surface_actions.CrosstalkService`
    in ``server._build_settings_mediator_services``. Test fixtures that
    don't exercise CrosstalkService leave this in place; calling it from
    such a test surfaces the missing wiring instead of silently no-oping.
    """
    async def send(self, *args, **kwargs):  # pragma: no cover — defensive
        raise RuntimeError(
            "Services.crosstalk is unbound; wire a CrosstalkService "
            "instance before invoking handlers that send CrossTalk."
        )


@dataclass
class Services:
    """Cross-plugin primitives an action handler may call.

    Wired by :func:`tools.dashboard.server` at lifespan startup. Adding a
    primitive requires a second consumer that needs it — keep this
    surface tight.
    """
    session_send: Callable[[str, str], Awaitable[None]]
    log: logging.Logger = field(default_factory=lambda: logger)
    crosstalk: Any = field(default_factory=_UnboundCrosstalk)


@dataclass
class RegisteredAction:
    """One entry in the handler registry.

    ``org`` is the install scope this handler reads from. The plugin
    loader sets it via the :data:`_loading_plugin_org` contextvar at
    import time so handlers declared inside an ``entrypoints.actions``
    module see their owning plugin's ``manifest.effective_org``. Direct
    ``register_action`` callers outside the loader leave it ``None``
    (scopeless — fires on every org).
    """
    set_id: str
    name: str
    fn: Callable[[Row, Services], Awaitable[None]]
    org: str | None = None


# ── Module-level state ───────────────────────────────────────────────

_HANDLERS: dict[str, list[RegisteredAction]] = {}

# Active during a plugin's ``entrypoints.actions`` import: the loader
# binds this to ``manifest.effective_org`` so every register_action call
# inside the imported module stamps the right org onto its
# RegisteredAction. Default ``None`` keeps non-plugin callers scopeless.
_loading_plugin_org: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "settings_mediator._loading_plugin_org", default=None,
)

_loop_task: asyncio.Task | None = None
_stop_event: asyncio.Event | None = None


# ── Heartbeat ────────────────────────────────────────────────────────


@dataclass
class MediatorHealth:
    """Heartbeat surface mutated by the loop on every documented seam.

    Read on demand by ``GET /api/diag/settings_mediator``. Per-tick log
    lines are explicitly out of scope; this is the on-demand alternative
    — no log noise, but enough state to answer "is the loop alive?" /
    "did this handler fire?" when the mediator looks wedged.
    """
    last_tick_at: float = 0.0
    events_received_count: int = 0
    last_handler_fired_at: dict[str, float] = field(default_factory=dict)
    last_handler_succeeded_at: dict[str, float] = field(default_factory=dict)
    last_handler_error: dict[str, str] = field(default_factory=dict)
    handlers_fired_count: dict[str, int] = field(default_factory=dict)
    loop_started_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Snapshot the heartbeat as a JSON-friendly dict.

        Includes a derived ``last_tick_age_s`` so callers can spot a
        stale loop without computing it themselves.
        """
        now = time.time()
        last_tick = self.last_tick_at or None
        registered = [
            {"set_id": a.set_id, "name": a.name}
            for actions in _HANDLERS.values()
            for a in actions
        ]
        return {
            "loop_started_at": self.loop_started_at,
            "last_tick_at": last_tick,
            "last_tick_age_s": (now - last_tick) if last_tick else None,
            "events_received_count": self.events_received_count,
            "last_handler_fired_at": dict(self.last_handler_fired_at),
            "last_handler_succeeded_at": dict(self.last_handler_succeeded_at),
            "last_handler_error": dict(self.last_handler_error),
            "handlers_fired_count": dict(self.handlers_fired_count),
            "registered_actions": registered,
            "now": now,
        }


HEALTH = MediatorHealth()


def reset_health() -> None:
    """Test helper — clear all heartbeat state in place."""
    HEALTH.last_tick_at = 0.0
    HEALTH.events_received_count = 0
    HEALTH.last_handler_fired_at.clear()
    HEALTH.last_handler_succeeded_at.clear()
    HEALTH.last_handler_error.clear()
    HEALTH.handlers_fired_count.clear()
    HEALTH.loop_started_at = None


# ── Registration ─────────────────────────────────────────────────────


def register_action(
    set_id: str,
    fn: Callable[[Row, Services], Awaitable[None]],
    *,
    name: str | None = None,
    org: str | None = None,
) -> None:
    """Register *fn* as a handler for ``setting.changed`` on *set_id*.

    *name* defaults to ``fn.__qualname__`` and shows up in heartbeat
    logs — distinct names matter when a single ``set_id`` carries
    multiple handlers (e.g. predicate-routed coordinator-board kinds).
    *org* defaults to the value of :data:`_loading_plugin_org`, set by
    the plugin loader during plugin-actions import.
    """
    resolved_name = name or getattr(fn, "__qualname__", None) or repr(fn)
    resolved_org = org if org is not None else _loading_plugin_org.get()
    _HANDLERS.setdefault(set_id, []).append(
        RegisteredAction(
            set_id=set_id,
            name=resolved_name,
            fn=fn,
            org=resolved_org,
        )
    )


def register_action_decorator(
    set_id: str,
    *,
    predicate: Callable[[Row], bool] | None = None,
    name: str | None = None,
    org: str | None = None,
) -> Callable[
    [Callable[[Row, Services], Awaitable[None]]],
    Callable[[Row, Services], Awaitable[None]],
]:
    """Decorator sugar over :func:`register_action`.

    *predicate* is wrapped INSIDE the registered handler instead of
    being evaluated by the substrate — the public decorator signature
    is unchanged so existing handlers don't need migration.

    .. code-block:: python

        @register_action_decorator("dashboard.coordinator-decision",
                                   predicate=lambda r: r['kind'] == 'thumb_yes')
        async def my_handler(row, svc): ...
    """
    def _wrap(fn: Callable[[Row, Services], Awaitable[None]]):
        if predicate is None:
            register_action(set_id, fn, name=name, org=org)
            return fn
        async def _filtered(row: Row, svc: Services) -> None:
            if not predicate(row):
                return
            await fn(row, svc)
        # Preserve the original handler's qualname for the registry
        # entry so health snapshots show the operator-meaningful name.
        _filtered.__qualname__ = getattr(fn, "__qualname__", _filtered.__qualname__)
        register_action(set_id, _filtered, name=name, org=org)
        return fn
    return _wrap


def clear_registry() -> None:
    """Test helper — reset the in-process registry."""
    _HANDLERS.clear()


# ── Row resolution ───────────────────────────────────────────────────


def _resolved_to_row(m: ResolvedSetting) -> Row:
    payload = m.payload if isinstance(m.payload, dict) else {}
    return Row(
        id=m.id,
        set_id=m.set_id,
        key=m.key,
        payload=dict(payload),
        created_at=m.created_at or "",
        updated_at=m.updated_at or "",
        org=m.org,
    )


def _resolve_row_or_none(set_id: str, key: str, org: str | None) -> Row | None:
    """Return the resolved Row for ``(set_id, key)`` in *org* or None.

    ``peers=[]`` keeps the read scoped to *org* alone. None is returned
    when the row was deleted between commit and event delivery, or when
    the event represents a delete operation — handlers don't fire on
    deletes (matches the pre-collapse mediator's behavior).
    """
    members = settings_ops.read_set(set_id, org=org, peers=[])
    for m in members.members:
        if m.key == key:
            return _resolved_to_row(m)
    return None


# ── Event dispatch ───────────────────────────────────────────────────


async def _dispatch_event(data: Any, services: Services) -> None:
    """Fan a single ``setting.changed`` payload out to every matching handler.

    Extracted from :func:`_loop_main` so tests can exercise the dispatch
    surface without spinning the asyncio loop. Handler exceptions are
    logged and swallowed so a sibling handler's failure cannot block
    other handlers on the same event.
    """
    if not isinstance(data, dict):
        return
    set_id = data.get("set_id")
    if not set_id:
        return
    actions = _HANDLERS.get(set_id)
    if not actions:
        return
    org_filter = data.get("org")
    key = data.get("key")
    row: Row | None = None
    row_resolved = False
    for action in actions:
        if action.org is not None and action.org != org_filter:
            continue
        if not row_resolved:
            try:
                row = await asyncio.to_thread(
                    _resolve_row_or_none, set_id, key, org_filter,
                )
            except Exception:
                logger.exception(
                    "settings_mediator: row resolve failed for "
                    "set=%s key=%s org=%s",
                    set_id, key, org_filter,
                )
                return
            row_resolved = True
        if row is None:
            # Row was deleted between commit and event delivery (or the
            # event represents a delete operation). Match the pre-collapse
            # mediator: deletes don't fire handlers.
            return
        fire_ts = time.time()
        HEALTH.last_handler_fired_at[action.name] = fire_ts
        HEALTH.handlers_fired_count[action.name] = (
            HEALTH.handlers_fired_count.get(action.name, 0) + 1
        )
        try:
            await action.fn(row, services)
            HEALTH.last_handler_succeeded_at[action.name] = time.time()
        except Exception as exc:
            HEALTH.last_handler_error[action.name] = (
                f"{type(exc).__name__}: {exc}"
            )
            logger.exception(
                "settings_mediator: handler %s raised on set=%s key=%s",
                action.name, set_id, key,
            )


async def _loop_main(
    services: Services,
    stop_event: asyncio.Event,
    event_bus: Any,
) -> None:
    """Subscribe to the bus and dispatch ``setting.changed`` events.

    The 1-second ``wait_for`` timeout is a polling-shutdown shape, not a
    polling-for-events shape — it only exists so ``stop_event`` can fire
    promptly during shutdown. Real events arrive on the bus queue with
    no polling delay.
    """
    queue = event_bus.subscribe()
    HEALTH.loop_started_at = time.time()
    logger.info(
        "settings_mediator: loop started — %d set(s) registered",
        len(_HANDLERS),
    )
    try:
        while not stop_event.is_set():
            try:
                topic, data, seq = await asyncio.wait_for(
                    queue.get(), timeout=1.0,
                )
            except asyncio.TimeoutError:
                HEALTH.last_tick_at = time.time()
                continue
            HEALTH.last_tick_at = time.time()
            if topic != "setting.changed":
                continue
            if seq == 0:
                # Cached-state replay on subscribe — not a new event.
                continue
            HEALTH.events_received_count += 1
            await _dispatch_event(data, services)
    finally:
        try:
            event_bus.unsubscribe(queue)
        except Exception:
            logger.exception("settings_mediator: unsubscribe failed")
        logger.info("settings_mediator: loop stopped")


def start_action_loop(
    services: Services,
    *,
    event_bus: Any,
) -> asyncio.Task:
    """Start the dispatch loop on the running event loop.

    Idempotent: a second call while the loop is already running is a
    logged no-op and returns the existing task.
    """
    global _loop_task, _stop_event
    if _loop_task is not None and not _loop_task.done():
        logger.warning(
            "settings_mediator: start_action_loop() called while loop "
            "already running; returning existing task"
        )
        return _loop_task
    _stop_event = asyncio.Event()
    _loop_task = asyncio.create_task(
        _loop_main(services, _stop_event, event_bus),
        name="settings_mediator.loop",
    )
    return _loop_task


async def stop_action_loop(*, drain_timeout: float = 30.0) -> None:
    """Signal stop and await the loop task.

    The loop observes ``stop_event`` at the next 1s wakeup boundary so
    any handler currently awaiting completes naturally. ``drain_timeout``
    cancels the task if a handler hangs past the deadline.
    """
    global _loop_task, _stop_event
    task = _loop_task
    stop = _stop_event
    if task is None or stop is None:
        return
    stop.set()
    try:
        await asyncio.wait_for(task, timeout=drain_timeout)
    except asyncio.TimeoutError:
        logger.warning(
            "settings_mediator: drain timeout (%.1fs) exceeded; "
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
