"""Background tasks of the ``backup`` plugin (entrypoints.background).

One task: the deriver loop — ingest run reports, derive
failure/staleness conditions, publish them to Central attention. The
substrate's PluginBackgroundSupervisor owns its lifecycle (started
while the plugin is enabled, cancelled on disable/shutdown, restarted
with backoff if it ever escapes its own containment).
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


async def _deriver_loop() -> None:
    from tools.dashboard.plugins.backup import deriver

    while True:
        try:
            outcome = await asyncio.to_thread(deriver.run_cycle)
            if outcome.get("published") or outcome.get("errors"):
                logger.info("backup deriver cycle: %s", outcome)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Contained here so the cycle cadence stays steady; the
            # supervisor's backoff is the second line of defense.
            logger.exception("backup deriver cycle failed")
        await asyncio.sleep(deriver.DERIVE_INTERVAL_S)


def tasks():
    """entrypoints.background — coroutine factories for the supervisor."""
    return [_deriver_loop]
