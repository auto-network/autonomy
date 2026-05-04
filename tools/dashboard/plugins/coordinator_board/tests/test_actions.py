"""Tests for the coordinator-board action handlers (bead auto-e5vus).

The handlers themselves are stateless one-liners — the bulk of the work
(idempotency, restart-resume, predicate filtering) lives in the
settings-mediator substrate, covered by ``test_settings_mediator.py``.
What we verify here:

* Each ``coordinator-decision`` ``kind`` produces the synthesized text
  the design's operator protocol calls for.
* ``operator-message`` resolves the bound coordinator session from
  ``dashboard.coordinator`` and passes the body verbatim.
* ``operator-message`` with no coordinator binding resolved logs +
  drops the row (no exception, no ``session_send`` call).
* Importing the actions module registers all seven handlers in
  ``settings_mediator.REGISTRY`` with the correct predicates and
  set_ids — the manifest's import-side-effect contract.
* End-to-end through ``iterate_once``: writing a decision row dispatches
  exactly the matching handler, and re-iterating is a no-op (substrate
  idempotency).
"""
from __future__ import annotations

import logging
import time
from unittest.mock import MagicMock

import pytest

from tools.dashboard import settings_mediator
from tools.dashboard.settings_mediator import (
    Row,
    Services,
    iterate_once,
)
from tools.graph.schemas.registry import SCHEMAS, UPCONVERTERS, register_schema, SettingSchema


COORDINATOR_SET_ID = "dashboard.coordinator"
COORDINATOR_DECISION_SET_ID = "dashboard.coordinator-decision"
OPERATOR_MESSAGE_SET_ID = "dashboard.operator-message-to-coordinator"


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_action_registry():
    """Reset the in-process action registry around each test.

    The actions module is normally imported once at process startup;
    importing the same module again is a no-op (Python caches it). We
    snapshot + restore the registry so tests stay independent of import
    order even when the module has already been loaded by another test.
    """
    settings_mediator.clear_registry()
    yield
    settings_mediator.clear_registry()


@pytest.fixture(autouse=True)
def _isolate_schema_registry():
    """Snapshot + restore the global schema registry around each test.

    Tests register permissive schemas for the two coordinator-board
    Settings; without isolation they'd leak into sibling tests that
    expect the real validators.
    """
    schemas_snap = dict(SCHEMAS)
    upcon_snap = dict(UPCONVERTERS)
    try:
        yield
    finally:
        SCHEMAS.clear()
        SCHEMAS.update(schemas_snap)
        UPCONVERTERS.clear()
        UPCONVERTERS.update(upcon_snap)


@pytest.fixture
def graph_db_env(tmp_path, monkeypatch):
    """Pin GRAPH_DB to a fresh tmp file for the test's duration."""
    db_path = tmp_path / "graph.db"
    monkeypatch.setenv("GRAPH_DB", str(db_path))
    monkeypatch.delenv("GRAPH_API", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    yield db_path


@pytest.fixture
def services_capture():
    """Stub :class:`Services` recording every ``session_send`` call."""
    sent: list[tuple[str, str]] = []

    async def _send(session: str, text: str) -> None:
        sent.append((session, text))

    svc = Services(
        session_send=_send,
        log=logging.getLogger("settings_mediator.test"),
    )
    svc._sent = sent
    return svc


def _make_row(payload: dict, *, set_id: str = COORDINATOR_DECISION_SET_ID,
              row_id: str = "row-1", key: str = "k-1") -> Row:
    """Construct a Row for direct handler unit tests."""
    return Row(
        id=row_id,
        set_id=set_id,
        key=key,
        payload=payload,
        created_at="2026-04-30T00:00:00Z",
        updated_at="2026-04-30T00:00:00Z",
    )


# ── Per-handler unit tests ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_thumb_yes_synthesizes_text(services_capture):
    from tools.dashboard.plugins.coordinator_board.entrypoints import actions

    row = _make_row({
        "kind": "thumb_yes",
        "tile_id": "t1",
        "target_session": "auto-foo",
    })
    await actions.thumb_yes(row, services_capture)

    assert services_capture._sent == [("auto-foo", "Operator: thumb yes on t1.")]


@pytest.mark.asyncio
async def test_thumb_no_synthesizes_text(services_capture):
    from tools.dashboard.plugins.coordinator_board.entrypoints import actions

    row = _make_row({
        "kind": "thumb_no",
        "tile_id": "t2",
        "target_session": "auto-bar",
    })
    await actions.thumb_no(row, services_capture)

    assert services_capture._sent == [("auto-bar", "Operator: thumb no on t2.")]


@pytest.mark.asyncio
async def test_choice_synthesizes_text(services_capture):
    from tools.dashboard.plugins.coordinator_board.entrypoints import actions

    row = _make_row({
        "kind": "choice",
        "tile_id": "t3",
        "choice": "ship it",
        "target_session": "auto-baz",
    })
    await actions.choice(row, services_capture)

    assert services_capture._sent == [
        ("auto-baz", "Operator chose: ship it on t3."),
    ]


@pytest.mark.asyncio
async def test_custom_passes_through_verbatim(services_capture):
    """``custom`` is operator free text — no synthesis, body unmodified."""
    from tools.dashboard.plugins.coordinator_board.entrypoints import actions

    row = _make_row({
        "kind": "custom",
        "tile_id": "t4",
        "choice": "hold off until Friday — legal still reviewing",
        "target_session": "auto-quux",
    })
    await actions.custom_reply(row, services_capture)

    assert services_capture._sent == [
        ("auto-quux", "hold off until Friday — legal still reviewing"),
    ]


@pytest.mark.asyncio
async def test_sitrep_request_synthesizes_text(services_capture):
    from tools.dashboard.plugins.coordinator_board.entrypoints import actions

    row = _make_row({
        "kind": "sitrep_request",
        "tile_id": "t5",
        "target_session": "auto-foo",
    })
    await actions.sitrep_request(row, services_capture)

    assert services_capture._sent == [
        ("auto-foo", "Operator requests a sitrep on t5."),
    ]


@pytest.mark.asyncio
async def test_refresh_request_synthesizes_text(services_capture):
    from tools.dashboard.plugins.coordinator_board.entrypoints import actions

    row = _make_row({
        "kind": "refresh_request",
        "tile_id": "t6",
        "target_session": "auto-foo",
    })
    await actions.refresh_request(row, services_capture)

    assert services_capture._sent == [
        ("auto-foo", "Operator requests a refresh on t6."),
    ]


# ── operator-message handler ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_operator_message_routes_to_coordinator(
    graph_db_env, services_capture,
):
    """Resolves the bound coordinator session, sends body verbatim."""
    from tools.dashboard.plugins.coordinator_board.entrypoints import actions

    _register_permissive_schemas()

    row = _make_row(
        {"text": "ack — sequencing approved", "sentAt": None},
        set_id=OPERATOR_MESSAGE_SET_ID,
    )
    from tools.graph import ops
    ops.add_setting(
        COORDINATOR_SET_ID, 1, "default",
        {"session_id": "auto-coord-9"},
    )

    await actions.operator_message(row, services_capture)
    assert services_capture._sent == [
        ("auto-coord-9", "ack — sequencing approved"),
    ]


@pytest.mark.asyncio
async def test_operator_message_drops_when_no_coordinator(
    graph_db_env, services_capture, caplog,
):
    """No binding resolved → log + drop, no send."""
    from tools.dashboard.plugins.coordinator_board.entrypoints import actions

    _register_permissive_schemas()

    row = _make_row(
        {"text": "still no one home", "sentAt": None},
        set_id=OPERATOR_MESSAGE_SET_ID,
        row_id="orphan-row",
    )
    with caplog.at_level(logging.WARNING, logger="settings_mediator.test"):
        await actions.operator_message(row, services_capture)

    assert services_capture._sent == [], (
        "no coordinator binding resolved → handler must not call session_send"
    )
    assert any("no dashboard.coordinator session resolved" in r.getMessage()
               for r in caplog.records), (
        f"expected warning log, got: {[r.getMessage() for r in caplog.records]}"
    )


# ── Registration contract ────────────────────────────────────────────


def test_importing_actions_module_registers_all_handlers():
    """The module's ``register_action_decorator`` calls populate REGISTRY.

    Seven handlers total: six on the decision set (one per ``kind``)
    plus one on the operator-message set. Each decision handler carries
    a predicate; the operator-message handler does not.
    """
    # Force a fresh import so registration fires under the cleared
    # registry (the autouse fixture clears between tests, but the module
    # is import-cached after the first test). We re-run the registration
    # by importing the module's symbols and calling them through the
    # registry directly — but the cleanest way is to re-execute the
    # module.
    import importlib
    from tools.dashboard.plugins.coordinator_board.entrypoints import actions
    importlib.reload(actions)

    by_set: dict[str, list] = {}
    for entry in settings_mediator.REGISTRY:
        by_set.setdefault(entry.set_id, []).append(entry)

    # Decision set: six predicate-gated handlers.
    decision_actions = by_set.get(COORDINATOR_DECISION_SET_ID, [])
    assert len(decision_actions) == 6, (
        f"expected 6 decision handlers; got {len(decision_actions)}: "
        f"{[a.name for a in decision_actions]}"
    )
    decision_names = {a.name for a in decision_actions}
    assert decision_names == {
        "coordinator_board.thumb_yes",
        "coordinator_board.thumb_no",
        "coordinator_board.choice",
        "coordinator_board.custom_reply",
        "coordinator_board.sitrep_request",
        "coordinator_board.refresh_request",
    }
    for entry in decision_actions:
        assert entry.predicate is not None, (
            f"decision handler {entry.name!r} must have a predicate"
        )

    # Operator-message set: one unfiltered handler.
    op_actions = by_set.get(OPERATOR_MESSAGE_SET_ID, [])
    assert len(op_actions) == 1
    assert op_actions[0].name == "coordinator_board.operator_message"
    assert op_actions[0].predicate is None


def test_predicates_route_decision_kinds_to_their_handlers():
    """Each decision handler's predicate matches its ``kind`` and rejects others."""
    import importlib
    from tools.dashboard.plugins.coordinator_board.entrypoints import actions
    importlib.reload(actions)

    by_name = {a.name: a for a in settings_mediator.REGISTRY
               if a.set_id == COORDINATOR_DECISION_SET_ID}

    cases = {
        "coordinator_board.thumb_yes": "thumb_yes",
        "coordinator_board.thumb_no": "thumb_no",
        "coordinator_board.choice": "choice",
        "coordinator_board.custom_reply": "custom",
        "coordinator_board.sitrep_request": "sitrep_request",
        "coordinator_board.refresh_request": "refresh_request",
    }
    for handler_name, expected_kind in cases.items():
        action = by_name[handler_name]
        # Matches its own kind.
        match_row = _make_row({
            "kind": expected_kind,
            "tile_id": "t",
            "target_session": "auto-x",
            "choice": "c",
        })
        assert action.predicate(match_row), (
            f"predicate for {handler_name!r} should accept kind={expected_kind!r}"
        )
        # Rejects every other kind in the table.
        for other_kind in set(cases.values()) - {expected_kind}:
            other_row = _make_row({
                "kind": other_kind,
                "tile_id": "t",
                "target_session": "auto-x",
                "choice": "c",
            })
            assert not action.predicate(other_row), (
                f"predicate for {handler_name!r} should reject "
                f"kind={other_kind!r}"
            )
        # Predicates are payload-shape tolerant — missing kind reads as
        # absent, not an exception.
        empty_row = _make_row({})
        assert not action.predicate(empty_row)


# ── End-to-end via iterate_once ──────────────────────────────────────


def _register_permissive_schemas() -> None:
    """Register no-op SettingSchemas for the two coordinator-board sets.

    The full validators live in ``entrypoints/schemas.py`` (operator
    message) and will land via ``auto-lffg5`` (decision). Tests need
    ``add_setting`` to work without being coupled to those validators —
    a permissive override keeps the test focused on dispatch behavior.
    """
    class _DecisionV1(SettingSchema):
        set_id = COORDINATOR_DECISION_SET_ID
        schema_revision = 1

    class _CoordinatorV1(SettingSchema):
        set_id = COORDINATOR_SET_ID
        schema_revision = 1

    class _OperatorMsgV1(SettingSchema):
        set_id = OPERATOR_MESSAGE_SET_ID
        schema_revision = 1

    register_schema(COORDINATOR_SET_ID, 1, _CoordinatorV1)
    register_schema(COORDINATOR_DECISION_SET_ID, 1, _DecisionV1)
    register_schema(OPERATOR_MESSAGE_SET_ID, 1, _OperatorMsgV1)


@pytest.mark.asyncio
async def test_iterate_once_dispatches_thumb_yes_end_to_end(
    graph_db_env, services_capture,
):
    """Acceptance #1 — writing a decision row produces a session_send.

    Drives the substrate's ``iterate_once`` so we cover registration +
    set membership + predicate match + handler invocation in one go.
    """
    import importlib
    from tools.dashboard.plugins.coordinator_board.entrypoints import actions
    importlib.reload(actions)

    _register_permissive_schemas()

    from tools.graph import ops
    ops.add_setting(
        COORDINATOR_DECISION_SET_ID, 1,
        f"k-{int(time.time_ns())}",
        {
            "kind": "thumb_yes",
            "tile_id": "t1",
            "target_session": "auto-foo",
        },
    )

    await iterate_once(services_capture)

    assert services_capture._sent == [
        ("auto-foo", "Operator: thumb yes on t1."),
    ], f"expected one session_send; got {services_capture._sent}"


@pytest.mark.asyncio
async def test_iterate_once_idempotent_on_repoll(
    graph_db_env, services_capture,
):
    """Acceptance #4 — re-iterating produces NO second send (substrate idempotency)."""
    import importlib
    from tools.dashboard.plugins.coordinator_board.entrypoints import actions
    importlib.reload(actions)

    _register_permissive_schemas()

    from tools.graph import ops
    ops.add_setting(
        COORDINATOR_DECISION_SET_ID, 1,
        f"k-{int(time.time_ns())}",
        {
            "kind": "choice",
            "tile_id": "tile-7",
            "choice": "approve",
            "target_session": "auto-target",
        },
    )

    await iterate_once(services_capture)
    assert len(services_capture._sent) == 1
    assert services_capture._sent[0] == (
        "auto-target", "Operator chose: approve on tile-7.",
    )

    await iterate_once(services_capture)
    assert len(services_capture._sent) == 1, (
        "re-poll must not re-dispatch the same row"
    )


@pytest.mark.asyncio
async def test_iterate_once_operator_message_routes_to_bound_coordinator(
    graph_db_env, services_capture,
):
    """Acceptance — operator-message uses ``dashboard.coordinator``."""
    import importlib
    from tools.dashboard.plugins.coordinator_board.entrypoints import actions
    importlib.reload(actions)

    _register_permissive_schemas()

    from tools.graph import ops
    ops.add_setting(
        COORDINATOR_SET_ID, 1, "default",
        {"session_id": "auto-bound-coord"},
    )
    ops.add_setting(
        OPERATOR_MESSAGE_SET_ID, 1,
        "default",
        {"text": "are you there", "sentAt": None},
    )

    await iterate_once(services_capture)

    assert services_capture._sent == [
        ("auto-bound-coord", "are you there"),
    ]


@pytest.mark.asyncio
async def test_iterate_once_operator_message_no_coordinator_drops(
    graph_db_env, services_capture,
):
    """Acceptance #5 — operator-message row with no coordinator binding
    leaves a marker (so it isn't retried) but never raises."""
    import importlib
    from tools.dashboard.plugins.coordinator_board.entrypoints import actions
    importlib.reload(actions)

    _register_permissive_schemas()

    from tools.graph import ops
    ops.add_setting(
        OPERATOR_MESSAGE_SET_ID, 1,
        "default",
        {"text": "are you there", "sentAt": None},
    )

    await iterate_once(services_capture)

    assert services_capture._sent == [], (
        "with no dashboard.coordinator binding, handler must not call session_send"
    )
