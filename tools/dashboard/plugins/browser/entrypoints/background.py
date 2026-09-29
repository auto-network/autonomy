"""The browser plugin's background task: the lease reconciler.

The substrate starts plugin tasks at worker startup, but the reconciler
takes the lease epoch, which fences every other writer. Under the
zero-downtime hand-off the predecessor worker is still serving leases at
that point, so the task waits for this worker's activation (the
predecessor has exited) before it takes the epoch.
"""
from __future__ import annotations

import asyncio
import os

ACTIVATION_POLL_S = 1.0


def _activated() -> bool:
    import sys

    server = sys.modules.get("tools.dashboard.server")
    return bool(server is not None and getattr(server, "_worker_activated", False))


async def _reconciler() -> None:
    if os.environ.get("DASHBOARD_MOCK"):
        await asyncio.Event().wait()     # the mock has no Docker; stay idle
    while not _activated():
        await asyncio.sleep(ACTIVATION_POLL_S)
    from tools.dashboard.plugins.browser import reconciler

    await reconciler.run_forever()


def tasks():
    return [_reconciler]
