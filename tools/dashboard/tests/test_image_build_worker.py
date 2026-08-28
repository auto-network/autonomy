"""image_build_worker — wake-on-provision-write, quiet otherwise."""

from __future__ import annotations

import asyncio

import pytest

from tools.dashboard import image_build_worker
from tools.dashboard.event_bus import event_bus


@pytest.mark.asyncio
async def test_provision_write_triggers_sweep(monkeypatch):
    sweeps = []

    def sweep(*, repo_root):
        sweeps.append(repo_root)
        return []

    monkeypatch.setattr(image_build_worker.image_builder, "sweep", sweep)
    await image_build_worker.start_worker()
    try:
        for _ in range(50):        # startup reconcile sweep
            if sweeps:
                break
            await asyncio.sleep(0.02)
        assert len(sweeps) == 1

        event_bus.broadcast_sync("setting.changed", {
            "set_id": image_build_worker.image_builder.PROVISION_SET_ID,
            "key": "ai-recon", "org": "autonomy",
            "schema_revision": 1, "publication_state": "raw",
            "deprecated": False, "operation": "upsert",
        }, dedup=False)
        for _ in range(50):
            if len(sweeps) >= 2:
                break
            await asyncio.sleep(0.02)
        assert len(sweeps) == 2

        # an unrelated setting write does NOT wake the builder
        event_bus.broadcast_sync("setting.changed", {
            "set_id": "dashboard.feature_flags", "key": "x", "org": "autonomy",
            "schema_revision": 1, "publication_state": "raw",
            "deprecated": False, "operation": "upsert",
        }, dedup=False)
        await asyncio.sleep(0.2)
        assert len(sweeps) == 2
    finally:
        await image_build_worker.stop_worker()
