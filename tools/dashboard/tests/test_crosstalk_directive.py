"""Tests for the :class:`CrosstalkDirective` family base.

Covers the four substrate guarantees the family rides on:

* The base class establishes the ``dashboard.session.crosstalk``
  namespace root with no ``schema_revision`` (abstract — no concrete
  schema row is registered for the root).
* A concrete subclass with ``set_id_suffix = "test"`` and
  ``schema_revision = 1`` composes to
  ``dashboard.session.crosstalk.test`` and registers in
  :data:`tools.graph.schemas.registry.SCHEMAS`.
* A write to a subclass's set fires the inherited :func:`deliver`
  action via the settings-mediator dispatch, which calls
  ``services.session_send(target_session, body)``.
* A subclass that re-applies ``@action`` to ``deliver`` shadows the
  inherited handler — only the override fires for that subclass.
"""
from __future__ import annotations

import asyncio

import pytest

from tools.graph import ops, schemas, settings_ops
from tools.graph.schemas.registry import SCHEMAS, UPCONVERTERS

from tools.dashboard import settings_mediator
from tools.dashboard.crosstalk_directive import (
    CROSSTALK_DIRECTIVE_NAMESPACE,
    CrosstalkDirective,
)
from tools.dashboard.event_bus import EventBus
from tools.dashboard.settings_mediator import (
    Row,
    Services,
    start_action_loop,
    stop_action_loop,
)
from tools.dashboard.settings_mediator.loop import _HANDLERS


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
    snap = {k: list(v) for k, v in _HANDLERS.items()}
    try:
        yield _HANDLERS
    finally:
        _HANDLERS.clear()
        for k, v in snap.items():
            _HANDLERS[k] = list(v)


@pytest.fixture(autouse=True)
def _clear_emit_hook():
    settings_ops.set_emit_hook(None)
    yield
    settings_ops.set_emit_hook(None)


@pytest.fixture
def graph_db_env(tmp_path, monkeypatch):
    db_path = tmp_path / "graph.db"
    monkeypatch.setenv("GRAPH_DB", str(db_path))
    monkeypatch.delenv("GRAPH_API", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    yield db_path


# ── 1. Namespace root is abstract ─────────────────────────────


def test_namespace_root_abstract_no_concrete_registration():
    """The base class establishes the namespace root, with ``set_id`` set
    but no ``schema_revision`` of its own. No concrete schema row at the
    root lands in :data:`SCHEMAS`."""
    assert CrosstalkDirective.set_id == "dashboard.session.crosstalk"
    assert CROSSTALK_DIRECTIVE_NAMESPACE == "dashboard.session.crosstalk"
    # Abstract: schema_revision is not declared in the base's own __dict__.
    assert "schema_revision" not in CrosstalkDirective.__dict__
    # And no concrete revision row at the root namespace lands in SCHEMAS.
    for sk in SCHEMAS:
        ns, _, _ = sk.partition("#")
        assert ns != "dashboard.session.crosstalk", (
            f"unexpected concrete registration at root namespace: {sk}"
        )


# ── 2. Subclass composition + registration ────────────────────


def test_subclass_set_id_suffix_composes_and_registers():
    """A concrete subclass declaring ``set_id_suffix = "test"`` and
    ``schema_revision = 1`` composes to
    ``dashboard.session.crosstalk.test`` and registers in
    :data:`SCHEMAS`."""

    class TestDirectiveV1(CrosstalkDirective):
        set_id_suffix = "test"
        schema_revision = 1

    assert TestDirectiveV1.set_id == "dashboard.session.crosstalk.test"
    assert SCHEMAS.get("dashboard.session.crosstalk.test#1") is TestDirectiveV1


# ── 3. Inherited deliver fires session_send ───────────────────


@pytest.mark.asyncio
async def test_inherited_deliver_calls_session_send():
    """A write to a CrosstalkDirective subclass's set, with
    ``target_session`` and ``body`` payload fields, triggers the
    inherited :func:`deliver` action — which calls
    ``services.session_send(target_session, body)``."""

    class DeliverDirectiveV1(CrosstalkDirective):
        set_id_suffix = "deliver-fixture"
        schema_revision = 1

    actions = _HANDLERS.get("dashboard.session.crosstalk.deliver-fixture", [])
    assert len(actions) == 1, (
        f"expected exactly one inherited handler, got "
        f"{[a.name for a in actions]}"
    )
    assert actions[0].name == "DeliverDirectiveV1.deliver"

    sent: list[tuple[str, str]] = []

    async def session_send(session, text):
        sent.append((session, text))

    services = Services(session_send=session_send)
    row = Row(
        id="r-1",
        set_id="dashboard.session.crosstalk.deliver-fixture",
        key="evt-1",
        payload={
            "target_session": "auto-x123",
            "body": "rebase against master",
            "sender": "dashboard-ui",
        },
        created_at="",
        updated_at="",
    )
    await actions[0].fn(row, services)
    assert sent == [("auto-x123", "rebase against master")]


@pytest.mark.asyncio
async def test_inherited_deliver_dispatched_via_settings_mediator(
    graph_db_env,
):
    """Wire the same path the production dashboard uses — settings_ops
    write → emit_hook → EventBus → settings_mediator dispatch — and
    confirm the inherited :func:`deliver` action runs end-to-end on a
    real Setting write.
    """

    class DispatchDirectiveV1(CrosstalkDirective):
        set_id_suffix = "dispatch-fixture"
        schema_revision = 1

    bus = EventBus()

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

    sent: list[tuple[str, str]] = []
    delivered = asyncio.Event()

    async def session_send(session, text):
        sent.append((session, text))
        delivered.set()

    services = Services(session_send=session_send)
    start_action_loop(services, event_bus=bus)
    try:
        await asyncio.sleep(0.05)
        ops.add_setting(
            "dashboard.session.crosstalk.dispatch-fixture",
            1,
            "evt-1",
            {
                "target_session": "auto-y456",
                "body": "ping the agent",
                "sender": "dashboard-ui",
            },
            org=ops.CALLER_ORG,
        )
        await asyncio.wait_for(delivered.wait(), timeout=2.0)
        assert sent == [("auto-y456", "ping the agent")]
    finally:
        await stop_action_loop()


# ── 4. Override shadows inherited deliver ─────────────────────


@pytest.mark.asyncio
async def test_subclass_override_shadows_inherited_deliver():
    """A subclass that overrides :func:`deliver` with ``@action`` runs
    its own override — the inherited handler is NOT also registered for
    the subclass's set_id."""
    custom_calls: list[tuple[str, str]] = []

    class CustomDirectiveV1(CrosstalkDirective):
        set_id_suffix = "custom-fixture"
        schema_revision = 1

        @schemas.action
        async def deliver(row, svc):
            custom_calls.append((row["target_session"], row["body"].upper()))

    actions = _HANDLERS.get("dashboard.session.crosstalk.custom-fixture", [])
    assert len(actions) == 1, (
        f"expected exactly one override handler, got "
        f"{[a.name for a in actions]}"
    )
    assert actions[0].name == "CustomDirectiveV1.deliver"

    sent: list[tuple[str, str]] = []

    async def session_send(session, text):
        sent.append((session, text))

    services = Services(session_send=session_send)
    row = Row(
        id="r-2",
        set_id="dashboard.session.crosstalk.custom-fixture",
        key="evt-2",
        payload={
            "target_session": "auto-z789",
            "body": "shout",
            "sender": "dashboard-ui",
        },
        created_at="",
        updated_at="",
    )
    await actions[0].fn(row, services)
    assert custom_calls == [("auto-z789", "SHOUT")]
    # The inherited (passthrough) handler must NOT also have fired —
    # only the override is registered for this subclass's set_id.
    assert sent == []
