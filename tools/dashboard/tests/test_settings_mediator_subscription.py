"""Mediator wakeup tests — event-driven dispatch via the in-process EventBus.

After bead auto-rc27t collapsed the mediator into a thin handler
registry, the subscription path is the only path: every
``setting.changed`` event triggers an immediate dispatch, no polling
fallback. These tests cover what survives:

* A write produces a handler invocation in well under a second.
* Events for unrelated set_ids do not invoke any registered handler.
* Stop releases the bus subscription cleanly.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from tools.graph import ops, settings_ops
from tools.graph.schemas.registry import SCHEMAS, UPCONVERTERS

from tools.dashboard import settings_mediator
from tools.dashboard.event_bus import EventBus
from tools.dashboard.settings_mediator import (
    Services,
    register_action,
    start_action_loop,
    stop_action_loop,
)


TEST_SET_ID = "dashboard.test.subscription-fixture"
TEST_REVISION = 1


@pytest.fixture
def graph_db_env(tmp_path, monkeypatch):
    db_path = tmp_path / "graph.db"
    monkeypatch.setenv("GRAPH_DB", str(db_path))
    monkeypatch.delenv("GRAPH_API", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    yield db_path


@pytest.fixture(autouse=True)
def _isolate_schema_registry():
    snap = dict(SCHEMAS)
    upcon_snap = dict(UPCONVERTERS)
    try:
        yield
    finally:
        SCHEMAS.clear()
        SCHEMAS.update(snap)
        UPCONVERTERS.clear()
        UPCONVERTERS.update(upcon_snap)


@pytest.fixture(autouse=True)
def _isolate_action_registry():
    settings_mediator.clear_registry()
    yield
    settings_mediator.clear_registry()


@pytest.fixture(autouse=True)
def _clear_emit_hook():
    settings_ops.set_emit_hook(None)
    yield
    settings_ops.set_emit_hook(None)


@pytest.fixture
def fixture_schema():
    from tools.graph.schemas.registry import SettingSchema, register_schema

    class V1(SettingSchema):
        set_id = TEST_SET_ID
        schema_revision = TEST_REVISION

    register_schema(TEST_SET_ID, TEST_REVISION, V1)
    return V1


@pytest.fixture
def services():
    sent: list[tuple[str, str]] = []

    async def _send(session, text):
        sent.append((session, text))

    return Services(session_send=_send)


def _wire_bus_to_settings_ops(bus: EventBus):
    """Mirror ``server._settings_emit_hook`` against *bus*.

    Tests use this so the real wakeup path (settings_ops.add_setting →
    EventBus.broadcast_sync → mediator subscriber → handler) is what's
    being exercised.
    """
    def hook(*, operation, snapshot, org):
        bus.broadcast_sync("setting.changed", {
            "set_id": snapshot["set_id"],
            "schema_revision": snapshot["schema_revision"],
            "key": snapshot["key"],
            "org": org,
            "publication_state": snapshot["publication_state"],
            "deprecated": snapshot["deprecated"],
            "operation": operation,
        }, dedup=False)
    settings_ops.set_emit_hook(hook)


@pytest.mark.asyncio
async def test_event_driven_wakeup_under_one_second(
    graph_db_env, fixture_schema, services,
):
    """A write triggers handler invocation well under a second."""
    bus = EventBus()
    _wire_bus_to_settings_ops(bus)

    handler_invoked = asyncio.Event()
    invoked_at: list[float] = []

    async def handler(row, svc):
        invoked_at.append(time.monotonic())
        handler_invoked.set()

    register_action(TEST_SET_ID, handler, name="ev-driven")

    start_action_loop(services, event_bus=bus)
    try:
        await asyncio.sleep(0.05)
        write_at = time.monotonic()
        ops.add_setting(TEST_SET_ID, TEST_REVISION, "ev-key", {"x": 1})
        await asyncio.wait_for(handler_invoked.wait(), timeout=2.0)
        latency = invoked_at[0] - write_at
        assert latency < 1.0, (
            f"event-driven wakeup latency {latency:.3f}s exceeds 1s — "
            f"the bus wakeup path is broken or too slow"
        )
    finally:
        await stop_action_loop()


@pytest.mark.asyncio
async def test_event_for_unrelated_set_id_does_not_invoke_handler(
    graph_db_env, fixture_schema, services,
):
    """Events whose set_id isn't registered must not reach our handler."""
    bus = EventBus()
    _wire_bus_to_settings_ops(bus)

    fired = asyncio.Event()

    async def handler(row, svc):
        fired.set()

    register_action(TEST_SET_ID, handler, name="filter-test")

    start_action_loop(services, event_bus=bus)
    try:
        await asyncio.sleep(0.05)
        bus.broadcast_sync("setting.changed", {
            "set_id": "dashboard.test.unrelated",
            "schema_revision": 1,
            "key": "k",
            "org": None,
            "publication_state": "raw",
            "deprecated": False,
            "operation": "write",
        }, dedup=False)
        await asyncio.sleep(0.15)
        assert not fired.is_set()
    finally:
        await stop_action_loop()


@pytest.mark.asyncio
async def test_loop_unsubscribes_on_stop(
    graph_db_env, fixture_schema, services,
):
    """Stopping the loop releases the bus subscription queue."""
    bus = EventBus()

    async def handler(row, svc):
        pass

    register_action(TEST_SET_ID, handler, name="lifecycle")
    start_action_loop(services, event_bus=bus)
    try:
        await asyncio.sleep(0.05)
        assert bus.subscribers_count() == 1
    finally:
        await stop_action_loop()
    assert bus.subscribers_count() == 0
