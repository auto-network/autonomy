"""Tests for the thin handler-registry settings-mediator (bead auto-rc27t).

The substrate collapsed away cursor / marker / predicate / 30s watchdog
scaffolding into a registry that subscribes to the in-process EventBus
and fans events out to registered callbacks. These tests cover:

* Registration shape — ``register_action`` and the decorator sugar both
  populate the registry with the expected ``RegisteredAction`` entries.
* Org filtering — handlers registered with ``org="X"`` only fire on
  events whose org matches; ``org=None`` handlers fire on every org.
* Predicate wrapping — the decorator's ``predicate=`` filter is invoked
  inside the handler, not on the registry's ``RegisteredAction``.
* Dispatch behavior — handlers fire once per matching event, sibling
  handlers are insulated from each other's exceptions, and deleted rows
  do not invoke any handler.
* Health surface — the documented HEALTH fields populate as events
  arrive and as handlers run.
* Loop lifecycle — ``start_action_loop`` subscribes to the bus,
  ``stop_action_loop`` unsubscribes cleanly.
* Plugin loader integration — handlers imported under the loader's
  ``_loading_plugin_org`` contextvar register with the right org.
"""
from __future__ import annotations

import asyncio

import pytest

from tools.graph import ops, settings_ops
from tools.graph.schemas.registry import (
    SCHEMAS, UPCONVERTERS, SettingSchema, register_schema,
)

from tools.dashboard import settings_mediator
from tools.dashboard.event_bus import EventBus
from tools.dashboard.settings_mediator import (
    HEALTH,
    Row,
    Services,
    register_action,
    register_action_decorator,
    start_action_loop,
    stop_action_loop,
)
from tools.dashboard.settings_mediator.loop import (
    _HANDLERS,
    _dispatch_event,
    _loading_plugin_org,
)


TEST_SET_ID = "dashboard.test.handler-registry"
TEST_REVISION = 1


# ── Fixtures ─────────────────────────────────────────────────────────


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
    settings_mediator.reset_health()
    yield
    settings_mediator.clear_registry()
    settings_mediator.reset_health()


@pytest.fixture(autouse=True)
def _clear_emit_hook():
    settings_ops.set_emit_hook(None)
    yield
    settings_ops.set_emit_hook(None)


@pytest.fixture
def fixture_schema():
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

    svc = Services(session_send=_send)
    svc._sent = sent
    return svc


def _wire_bus_to_settings_ops(bus: EventBus):
    """Mirror ``server._settings_emit_hook`` against *bus*.

    Used by lifecycle tests that drive the loop end-to-end through the
    real bus. Per-test clear-emit-hook fixture removes it on teardown.
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


def _event(set_id=TEST_SET_ID, key="k1", org=None, operation="write") -> dict:
    return {
        "set_id": set_id,
        "schema_revision": TEST_REVISION,
        "key": key,
        "org": org,
        "publication_state": "raw",
        "deprecated": False,
        "operation": operation,
    }


# ── Registration ────────────────────────────────────────────────────


def test_register_action_adds_to_registry():
    async def h(row, svc): ...
    register_action(TEST_SET_ID, h, name="foo")
    assert TEST_SET_ID in _HANDLERS
    assert len(_HANDLERS[TEST_SET_ID]) == 1
    entry = _HANDLERS[TEST_SET_ID][0]
    assert entry.name == "foo"
    assert entry.set_id == TEST_SET_ID
    assert entry.fn is h
    assert entry.org is None


def test_decorator_form_attaches_handler_without_predicate():
    @register_action_decorator(TEST_SET_ID, name="bare")
    async def h(row, svc): ...
    assert len(_HANDLERS[TEST_SET_ID]) == 1
    entry = _HANDLERS[TEST_SET_ID][0]
    assert entry.name == "bare"
    # No predicate: registered fn is the original.
    assert entry.fn is h


def test_decorator_form_attaches_handler_with_predicate_wrapper():
    """``predicate`` is wrapped INSIDE the handler, not stored on the entry."""
    @register_action_decorator(
        TEST_SET_ID,
        predicate=lambda r: r.get("kind") == "yes",
        name="filtered",
    )
    async def h(row, svc): ...

    assert len(_HANDLERS[TEST_SET_ID]) == 1
    entry = _HANDLERS[TEST_SET_ID][0]
    assert entry.name == "filtered"
    # Wrapped function — not the original.
    assert entry.fn is not h
    # RegisteredAction has no `predicate` field anymore.
    assert not hasattr(entry, "predicate")


# ── Dispatch ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_loop_invokes_all_handlers_for_matching_set_id(
    graph_db_env, fixture_schema, services,
):
    """Two handlers on the same set both fire for one event."""
    invocations: list[str] = []

    async def h1(row, svc):
        invocations.append("h1")

    async def h2(row, svc):
        invocations.append("h2")

    register_action(TEST_SET_ID, h1, name="h1")
    register_action(TEST_SET_ID, h2, name="h2")

    ops.add_setting(TEST_SET_ID, TEST_REVISION, "k1", {"x": 1}, org=ops.CALLER_ORG)
    await _dispatch_event(_event(key="k1"), services)

    assert invocations == ["h1", "h2"]


@pytest.mark.asyncio
async def test_loop_skips_handlers_for_non_matching_set_id(
    graph_db_env, fixture_schema, services,
):
    """Events for unrelated set_ids never reach the registered handler."""
    fired = asyncio.Event()

    async def h(row, svc):
        fired.set()

    register_action(TEST_SET_ID, h, name="lonely")

    await _dispatch_event(_event(set_id="dashboard.unrelated", key="k1"), services)
    assert not fired.is_set()


@pytest.mark.asyncio
async def test_loop_skips_org_scoped_handler_when_org_filter_mismatches(
    graph_db_env, fixture_schema, services,
):
    """``org="autonomy"`` handler does NOT fire on org=personal events."""
    fired = asyncio.Event()

    async def h(row, svc):
        fired.set()

    register_action(TEST_SET_ID, h, name="org-scoped", org="autonomy")

    await _dispatch_event(_event(key="k1", org="personal"), services)
    assert not fired.is_set()


@pytest.mark.asyncio
async def test_loop_invokes_scopeless_handler_for_any_org(
    graph_db_env, fixture_schema, services,
):
    """``org=None`` handler fires regardless of event's org field."""
    invoked: list[str | None] = []

    async def h(row, svc):
        invoked.append(row.payload.get("origin"))

    register_action(TEST_SET_ID, h, name="scopeless", org=None)

    ops.add_setting(TEST_SET_ID, TEST_REVISION, "any-org", {"origin": "test"}, org=ops.CALLER_ORG)
    await _dispatch_event(_event(key="any-org", org="autonomy"), services)
    await _dispatch_event(_event(key="any-org", org="personal"), services)

    assert invoked == ["test", "test"]


@pytest.mark.asyncio
async def test_handler_exception_does_not_block_siblings(
    graph_db_env, fixture_schema, services,
):
    """One handler raising must not prevent the next handler from firing."""
    fired_after_raise = asyncio.Event()

    async def raises(row, svc):
        raise RuntimeError("boom")

    async def runs_after(row, svc):
        fired_after_raise.set()

    register_action(TEST_SET_ID, raises, name="raises")
    register_action(TEST_SET_ID, runs_after, name="runs_after")

    ops.add_setting(TEST_SET_ID, TEST_REVISION, "k1", {"x": 1}, org=ops.CALLER_ORG)
    await _dispatch_event(_event(key="k1"), services)

    assert fired_after_raise.is_set()


@pytest.mark.asyncio
async def test_handler_exception_logged_and_swallowed_with_health_recorded(
    graph_db_env, fixture_schema, services, caplog,
):
    """Exception from a handler lands in HEALTH.last_handler_error and logs."""
    import logging

    async def boom(row, svc):
        raise ValueError("kaboom")

    register_action(TEST_SET_ID, boom, name="boom-handler")

    ops.add_setting(TEST_SET_ID, TEST_REVISION, "k1", {"x": 1}, org=ops.CALLER_ORG)
    with caplog.at_level(logging.ERROR, logger="settings_mediator"):
        await _dispatch_event(_event(key="k1"), services)

    assert "boom-handler" in HEALTH.last_handler_error
    assert "ValueError" in HEALTH.last_handler_error["boom-handler"]
    assert any(
        "boom-handler" in r.getMessage() for r in caplog.records
    ), "expected handler exception to be logged"


@pytest.mark.asyncio
async def test_dispatch_skips_when_row_not_found(
    graph_db_env, fixture_schema, services,
):
    """Event for a row that never landed (or was deleted) is a no-op.

    Pre-collapse the mediator's cursor/marker scaffolding skipped delete
    operations the same way; preserve that behavior so handlers don't
    need to defensively check for missing payloads.
    """
    fired = asyncio.Event()

    async def h(row, svc):
        fired.set()

    register_action(TEST_SET_ID, h, name="ghost")
    # No add_setting — the row does not exist.
    await _dispatch_event(_event(key="ghost-key"), services)
    assert not fired.is_set()


@pytest.mark.asyncio
async def test_predicate_filter_skips_handler_when_predicate_returns_false(
    graph_db_env, fixture_schema, services,
):
    """Decorator's predicate gates the inner handler at call time."""
    invocations: list[str] = []

    @register_action_decorator(
        TEST_SET_ID,
        predicate=lambda r: r.get("kind") == "yes",
        name="kind-yes",
    )
    async def h(row, svc):
        invocations.append(row["kind"])

    ops.add_setting(TEST_SET_ID, TEST_REVISION, "match", {"kind": "yes"}, org=ops.CALLER_ORG)
    ops.add_setting(TEST_SET_ID, TEST_REVISION, "mismatch", {"kind": "no"}, org=ops.CALLER_ORG)

    await _dispatch_event(_event(key="match"), services)
    await _dispatch_event(_event(key="mismatch"), services)

    assert invocations == ["yes"]


@pytest.mark.asyncio
async def test_two_plugins_registering_same_set_id_both_fire(
    graph_db_env, fixture_schema, services,
):
    """Multiple registrations on one set_id all fan out for one event.

    Regression for the spec's "two plugins registering the same set_id
    results in both handlers being called" — the registry stores a list
    per set_id, not a singleton.
    """
    invocations: list[str] = []

    async def plugin_a(row, svc):
        invocations.append("a")

    async def plugin_b(row, svc):
        invocations.append("b")

    # Simulate plugin A's import.
    token = _loading_plugin_org.set("autonomy")
    try:
        register_action(TEST_SET_ID, plugin_a, name="plugin-a")
    finally:
        _loading_plugin_org.reset(token)

    # Simulate plugin B's import.
    token = _loading_plugin_org.set("autonomy")
    try:
        register_action(TEST_SET_ID, plugin_b, name="plugin-b")
    finally:
        _loading_plugin_org.reset(token)

    ops.add_setting(TEST_SET_ID, TEST_REVISION, "k1", {"x": 1}, org=ops.CALLER_ORG)
    await _dispatch_event(_event(key="k1", org="autonomy"), services)

    assert invocations == ["a", "b"]


@pytest.mark.asyncio
async def test_plugin_loader_contextvar_registration_lands_in_registry(
    graph_db_env, fixture_schema, services,
):
    """A handler registered under ``_loading_plugin_org`` carries that org.

    Regression for the loader integration: ``loader._resolve_entrypoints``
    sets the contextvar before importing a plugin's actions module so
    every register_action call inside the import stamps the right org.
    """
    async def h(row, svc): ...

    token = _loading_plugin_org.set("autonomy")
    try:
        register_action(TEST_SET_ID, h, name="loaded")
    finally:
        _loading_plugin_org.reset(token)

    entry = _HANDLERS[TEST_SET_ID][0]
    assert entry.org == "autonomy"


# ── Health surface ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_fields_populated_on_event_arrival(
    graph_db_env, fixture_schema, services,
):
    """Each handler invocation records fired/succeeded counters and timestamps."""
    async def h(row, svc):
        pass

    register_action(TEST_SET_ID, h, name="counted")

    ops.add_setting(TEST_SET_ID, TEST_REVISION, "k1", {"x": 1}, org=ops.CALLER_ORG)
    await _dispatch_event(_event(key="k1"), services)

    assert HEALTH.handlers_fired_count.get("counted") == 1
    assert "counted" in HEALTH.last_handler_fired_at
    assert "counted" in HEALTH.last_handler_succeeded_at
    assert "counted" not in HEALTH.last_handler_error


@pytest.mark.asyncio
async def test_health_to_dict_exposes_documented_fields(
    graph_db_env, fixture_schema, services,
):
    """``/api/diag/settings_mediator`` JSON contract — all required keys present.

    The diag endpoint reads ``HEALTH.to_dict()`` directly; this is the
    backstop that the new mediator continues to surface every field
    operators rely on.
    """
    async def h(row, svc): ...
    register_action(TEST_SET_ID, h, name="diag-handler")

    ops.add_setting(TEST_SET_ID, TEST_REVISION, "k1", {"x": 1}, org=ops.CALLER_ORG)
    await _dispatch_event(_event(key="k1"), services)

    snap = HEALTH.to_dict()
    for key in (
        "loop_started_at", "last_tick_at", "last_tick_age_s",
        "events_received_count", "last_handler_fired_at",
        "last_handler_succeeded_at", "last_handler_error",
        "handlers_fired_count", "registered_actions", "now",
    ):
        assert key in snap, f"diag JSON missing {key!r}"


# ── Loop lifecycle ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_loop_unsubscribes_on_stop_event(
    graph_db_env, fixture_schema, services,
):
    """``stop_action_loop`` releases the bus subscription cleanly."""
    bus = EventBus()
    async def h(row, svc): ...
    register_action(TEST_SET_ID, h, name="lifecycle")

    start_action_loop(services, event_bus=bus)
    try:
        await asyncio.sleep(0.05)
        assert bus.subscribers_count() == 1
    finally:
        await stop_action_loop()
    assert bus.subscribers_count() == 0


@pytest.mark.asyncio
async def test_loop_dispatches_on_real_bus_event(
    graph_db_env, fixture_schema, services,
):
    """End-to-end: write a row → bus broadcast → handler fires.

    Drives the production wakeup path: ``settings_ops.add_setting`` →
    emit hook → ``EventBus.broadcast_sync`` → mediator subscriber →
    ``_dispatch_event`` → handler.
    """
    bus = EventBus()
    _wire_bus_to_settings_ops(bus)

    fired = asyncio.Event()

    async def h(row, svc):
        fired.set()

    register_action(TEST_SET_ID, h, name="bus-driven")

    start_action_loop(services, event_bus=bus)
    try:
        await asyncio.sleep(0.05)
        ops.add_setting(TEST_SET_ID, TEST_REVISION, "bus-key", {"x": 1}, org=ops.CALLER_ORG)
        await asyncio.wait_for(fired.wait(), timeout=2.0)
    finally:
        await stop_action_loop()


@pytest.mark.asyncio
async def test_loop_skips_seq_zero_replay(graph_db_env, fixture_schema, services):
    """Cached-state replay (seq=0) on subscribe must not fire handlers.

    The bus enqueues every cached topic with ``seq=0`` when a new
    subscriber joins. For ``setting.changed`` that means the most recent
    event re-arrives — re-running the handler would fire the operator a
    second prompt for an event the prior process already handled.
    """
    bus = EventBus()
    fired = asyncio.Event()

    async def h(row, svc):
        fired.set()

    register_action(TEST_SET_ID, h, name="replay-guard")

    # Pre-populate a cached event so subscribe replays it with seq=0.
    bus.broadcast_sync("setting.changed", _event(key="cached"), dedup=False)

    start_action_loop(services, event_bus=bus)
    try:
        await asyncio.sleep(0.15)
        assert not fired.is_set(), (
            "handler fired on cached-state replay (seq=0) — should be ignored"
        )
    finally:
        await stop_action_loop()
