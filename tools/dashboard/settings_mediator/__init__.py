"""Settings-mediator: turn ``setting.changed`` events into handler calls.

Plugins (and any module that imports this) call :func:`register_action`
or :func:`register_action_decorator` at import time to attach a coroutine
to a ``set_id``. Once the dashboard's lifespan starts the loop via
:func:`start_action_loop`, every ``setting.changed`` bus event for a
registered ``set_id`` resolves the row and fans out to all matching
handlers.

The substrate is in-process, single-host, at-most-once across process
restart: events fired while the dashboard is down are lost. Cross-process
or cross-host action workers are explicitly out of scope (see bead
auto-f93wj).
"""
from __future__ import annotations

from .loop import (
    HEALTH,
    MediatorHealth,
    RegisteredAction,
    Row,
    Services,
    clear_registry,
    register_action,
    register_action_decorator,
    reset_health,
    start_action_loop,
    stop_action_loop,
)


__all__ = [
    "HEALTH",
    "MediatorHealth",
    "RegisteredAction",
    "Row",
    "Services",
    "clear_registry",
    "register_action",
    "register_action_decorator",
    "reset_health",
    "start_action_loop",
    "stop_action_loop",
]
