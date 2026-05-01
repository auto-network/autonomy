"""Settings-mediator: turn Setting writes into code-defined side effects.

Plugins (and any module that imports this) call :func:`register_action`
at import time to attach a handler to ``(set_id, predicate)``. Once the
dashboard's lifespan has started the loop via :func:`start_action_loop`,
each new row in a registered set fires every matching handler once and
only once — restart-resume + per-(row, handler) idempotency are
guaranteed by the cursor + marker Settings declared in :mod:`.schemas`.

The substrate is single-host, single-process, in-memory registry.
Cross-process or cross-host action workers are explicitly out of scope
(see bead auto-f93wj). When SSE-on-Settings lands, the loop's
``read_set`` + ``sleep`` will be replaced with a subscription; the
public surface stays unchanged.
"""
from __future__ import annotations

# Importing schemas for side effects: register_schema(...) at import
# time wires the cursor + state schemas into the registry before any
# write goes through. ``server.py`` imports this package from its
# lifespan startup, which guarantees the schemas are present before
# the first read_set / add_setting call.
from . import schemas as _schemas  # noqa: F401
from .loop import (
    DEFAULT_POLL_SECONDS,
    HEALTH,
    REGISTRY,
    MediatorHealth,
    RegisteredAction,
    Row,
    Services,
    clear_registry,
    iterate_once,
    register_action,
    register_action_decorator,
    reset_health,
    start_action_loop,
    stop_action_loop,
)
from .schemas import (
    CURSOR_SET_ID,
    CURSOR_REVISION,
    STATE_SET_ID,
    STATE_REVISION,
)


__all__ = [
    "DEFAULT_POLL_SECONDS",
    "HEALTH",
    "REGISTRY",
    "MediatorHealth",
    "RegisteredAction",
    "Row",
    "Services",
    "clear_registry",
    "iterate_once",
    "register_action",
    "register_action_decorator",
    "reset_health",
    "start_action_loop",
    "stop_action_loop",
    "CURSOR_SET_ID",
    "CURSOR_REVISION",
    "STATE_SET_ID",
    "STATE_REVISION",
]
