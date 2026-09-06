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


DRILL_CHECK_INTERVAL_S = 1800.0


def _drill_due() -> bool:
    """A scheduled drill is due when the newest recorded drill (any
    verdict) is older than the configured cadence. Cadence 0 disables
    scheduling; on-demand drills still count toward freshness."""
    from datetime import datetime, timezone

    from tools.dashboard.plugins.backup.entrypoints.api import (
        _read_config,
        _rows,
    )
    from tools.dashboard.plugins.backup.entrypoints.schemas import (
        DRILL_SET_ID,
    )

    cadence_days = float(_read_config().get("drill_cadence_days", 7.0))
    if cadence_days <= 0:
        return False
    drills = sorted(_rows(DRILL_SET_ID), key=lambda d: d.get("key", ""))
    if not drills:
        return True
    newest = drills[-1]
    raw = newest.get("started_at") or ""
    try:
        started = datetime.fromisoformat(raw)
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    age_days = (datetime.now(timezone.utc) - started).total_seconds() / 86400
    return age_days >= cadence_days


async def _drill_scheduler_loop() -> None:
    from tools.dashboard.plugins.backup import drill as drill_mod

    while True:
        try:
            if await asyncio.to_thread(_drill_due) \
                    and drill_mod.running_stamp() is None:
                logger.info("backup drill cadence due; starting scheduled drill")
                try:
                    await asyncio.to_thread(drill_mod.run_drill, "scheduled")
                except drill_mod.DrillAlreadyRunning:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("scheduled drill cycle failed")
        await asyncio.sleep(DRILL_CHECK_INTERVAL_S)


def tasks():
    """entrypoints.background — coroutine factories for the supervisor."""
    return [_deriver_loop, _drill_scheduler_loop]
