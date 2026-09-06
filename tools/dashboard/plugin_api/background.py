"""Lifespan-owned background tasks for plugins (auto-jjqct).

A plugin declares ``entrypoints.background`` — a callable returning an
iterable of zero-argument coroutine factories. This supervisor owns
every lifecycle decision, so a plugin can never leak a task past its
own enablement or the process:

- **Enablement is live.** Plugins toggle via the ``dashboard.plugin``
  Setting without a restart, so the supervisor RECONCILES rather than
  starts-once: each pass compares running tasks against the currently
  enabled set, starting the newly enabled and cancelling the newly
  disabled. ``load_all`` keeps disabled plugins in the registry for
  route gating; their tasks must not run.
- **Crashes restart with bounded backoff.** A factory's coroutine
  returning or raising is a plugin bug the substrate contains: log with
  the plugin id, wait (doubling from BACKOFF_BASE_S to BACKOFF_CAP_S),
  run it again. A run that survives HEALTHY_AFTER_S resets the backoff.
  A permanently crashing task therefore costs one log line per few
  minutes, never a spin, and never the lifespan.
- **Shutdown is a cancel + bounded wait.** Tasks must tolerate
  ``CancelledError``; one that ignores it is abandoned after
  SHUTDOWN_GRACE_S with a warning rather than wedging shutdown.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)

BACKOFF_BASE_S = 5.0
BACKOFF_CAP_S = 300.0
HEALTHY_AFTER_S = 60.0
SHUTDOWN_GRACE_S = 10.0
RECONCILE_INTERVAL_S = 30.0


async def _supervise(plugin_id: str, index: int,
                     factory: Callable[[], Any]) -> None:
    """Run one coroutine factory forever, containing its failures."""
    backoff = BACKOFF_BASE_S
    loop = asyncio.get_running_loop()
    while True:
        started = loop.time()
        try:
            await factory()
            logger.warning(
                "[plugin_background] %s task %d returned; restarting",
                plugin_id, index,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "[plugin_background] %s task %d crashed; restarting in %.0fs",
                plugin_id, index, backoff,
            )
        if loop.time() - started >= HEALTHY_AFTER_S:
            backoff = BACKOFF_BASE_S
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, BACKOFF_CAP_S)


class PluginBackgroundSupervisor:
    """Owns every plugin background task in the process."""

    def __init__(
        self,
        plugins: Iterable[Any],
        enabled: Callable[[], dict],
    ) -> None:
        #: plugins with a background entrypoint, by id.
        self._plugins = {
            p.id: p for p in plugins if getattr(p, "background", None)
        }
        self._enabled = enabled
        self._tasks: dict[str, list[asyncio.Task]] = {}
        self._reconcile_task: asyncio.Task | None = None

    def reconcile_once(self) -> None:
        """Align running tasks with the live enabled map."""
        try:
            enabled_map = self._enabled() or {}
        except Exception:
            logger.exception("[plugin_background] enable-map read failed; "
                             "keeping current task set")
            return
        for plugin_id, plugin in self._plugins.items():
            want = bool(enabled_map.get(plugin_id))
            have = plugin_id in self._tasks
            if want and not have:
                self._start(plugin_id, plugin)
            elif not want and have:
                self._cancel(plugin_id)
        # Tasks that died terminally (cancelled externally) drop from
        # the book so a re-enable restarts them.
        for plugin_id, tasks in list(self._tasks.items()):
            if all(task.done() for task in tasks):
                del self._tasks[plugin_id]

    def _start(self, plugin_id: str, plugin: Any) -> None:
        try:
            factories = list(plugin.background() or [])
        except Exception:
            logger.exception(
                "[plugin_background] %s background() failed; no tasks started",
                plugin_id,
            )
            return
        tasks = []
        for index, factory in enumerate(factories):
            if not callable(factory):
                logger.warning(
                    "[plugin_background] %s factory %d is not callable; "
                    "skipped", plugin_id, index,
                )
                continue
            tasks.append(asyncio.create_task(
                _supervise(plugin_id, index, factory),
                name=f"plugin-background:{plugin_id}:{index}",
            ))
        if tasks:
            self._tasks[plugin_id] = tasks
            logger.info("[plugin_background] %s: %d task(s) started",
                        plugin_id, len(tasks))

    def _cancel(self, plugin_id: str) -> None:
        for task in self._tasks.pop(plugin_id, []):
            task.cancel()
        logger.info("[plugin_background] %s: tasks cancelled (disabled)",
                    plugin_id)

    async def start(self) -> None:
        self.reconcile_once()

        async def loop() -> None:
            while True:
                await asyncio.sleep(RECONCILE_INTERVAL_S)
                self.reconcile_once()

        if self._plugins:
            self._reconcile_task = asyncio.create_task(
                loop(), name="plugin-background:reconcile")

    async def stop(self) -> None:
        if self._reconcile_task is not None:
            self._reconcile_task.cancel()
            self._reconcile_task = None
        pending = [t for tasks in self._tasks.values() for t in tasks]
        self._tasks.clear()
        for task in pending:
            task.cancel()
        if not pending:
            return
        done, alive = await asyncio.wait(pending, timeout=SHUTDOWN_GRACE_S)
        for task in alive:
            logger.warning(
                "[plugin_background] %s ignored cancellation for %.0fs; "
                "abandoned", task.get_name(), SHUTDOWN_GRACE_S,
            )
        for task in done:
            if not task.cancelled() and task.exception() is not None:
                logger.warning("[plugin_background] %s died at shutdown: %s",
                               task.get_name(), task.exception())
