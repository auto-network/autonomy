"""Host-side workspace image builder worker.

Watches ``autonomy.workspace.provision`` writes on the process EventBus
(``setting.changed``, emitted post-commit by the settings hook) and runs
:func:`agents.image_builder.sweep` — a fallback sweep also runs every
15 minutes so a build missed while the process was down still happens.
Builds are content-hash gated inside the sweep, so a quiet pass costs a
few settings reads and no docker calls.

The docker work runs in a thread; one sweep at a time — a change
arriving mid-sweep sets the wake flag again and triggers a fresh pass,
which the hash gate makes cheap for everything already built.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from agents import image_builder
from tools.dashboard.event_bus import event_bus

logger = logging.getLogger(__name__)

_SWEEP_FALLBACK_SECONDS = 900
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_task: asyncio.Task | None = None
_stop = asyncio.Event()


async def _run() -> None:
    queue = event_bus.subscribe(client_id="image-build-worker")
    wake = True   # one sweep at startup reconciles anything missed
    try:
        while not _stop.is_set():
            if wake:
                wake = False
                try:
                    results = await asyncio.to_thread(
                        image_builder.sweep, repo_root=_REPO_ROOT,
                    )
                    for r in results:
                        if r.action == "skipped":
                            continue
                        log = logger.info if r.action == "built" \
                            else logger.warning
                        log("workspace image %s: %s %s",
                            r.action, r.image, r.detail[:200])
                except Exception:
                    logger.warning("workspace image sweep failed",
                                   exc_info=True)
            try:
                topic, data, _seq = await asyncio.wait_for(
                    queue.get(), timeout=_SWEEP_FALLBACK_SECONDS,
                )
            except asyncio.TimeoutError:
                wake = True
                continue
            if topic == "setting.changed" and isinstance(data, dict) \
                    and data.get("set_id") == image_builder.PROVISION_SET_ID:
                wake = True
    finally:
        event_bus.unsubscribe(queue)


async def start_worker() -> None:
    global _task
    if _task is not None and not _task.done():
        return
    _stop.clear()
    _task = asyncio.create_task(_run(), name="workspace-image-builder")


async def stop_worker() -> None:
    global _task
    if _task is None:
        return
    _stop.set()
    _task.cancel()
    try:
        await _task
    except (asyncio.CancelledError, Exception):
        pass
    _task = None
