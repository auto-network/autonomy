"""Tests for the coordinator-board action handlers (bead auto-e5vus).

The handlers themselves are stateless one-liners — the bulk of the work
(predicate routing, fan-out, exception isolation) lives in the
settings-mediator substrate, covered by ``test_handler_registry.py``.
What we verify here:

* Each ``coordinator-decision`` ``kind`` produces the synthesized text
  the design's operator protocol calls for.
* ``operator-message`` resolves the bound coordinator session from
  ``dashboard.coordinator`` and passes the body verbatim.
* ``operator-message`` with no coordinator binding resolved logs +
  drops the row (no exception, no ``session_send`` call).
* Importing the actions module registers all seven handlers under the
  expected ``set_id``s — the manifest's import-side-effect contract.
* End-to-end through the substrate's dispatch path: a synthesized
  ``setting.changed`` event resolves the matching row and fires only
  the handler whose wrapped predicate accepts the row's ``kind``.
"""
from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from tools.dashboard import settings_mediator
from tools.dashboard.settings_mediator import Row, Services
from tools.dashboard.settings_mediator.loop import _HANDLERS, _dispatch_event
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
    """Hermetic per-org tree for coordinator-board action tests.

    A GRAPH_DB pin collapses every org to one file and, under the
    fail-loud resolver, conflicts with the explicit-org reads these
    actions make. Use the orgs tree: no pin, both org DBs created, no
    GRAPH_ORG override; decisions and operator messages are written and
    dispatched with an explicit org (the sets are organization-homed, so
    ambient writes are refused under the no-default-scope law).
    """
    from tools.graph.db import GraphDB
    orgs = tmp_path / "orgs"
    orgs.mkdir()
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    GraphDB.close_all_pooled()
    for slug, kind in (("autonomy", "shared"), ("personal", "personal")):
        GraphDB.create_org_db(slug, type_=kind, path=orgs / f"{slug}.db").close()
    GraphDB.close_all_pooled()
    yield orgs / "personal.db"
    GraphDB.close_all_pooled()


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

    assert services_capture._sent == [("auto-foo", "COORDINATOR: Operator: thumb yes on t1.")]


@pytest.mark.asyncio
async def test_thumb_no_synthesizes_text(services_capture):
    from tools.dashboard.plugins.coordinator_board.entrypoints import actions

    row = _make_row({
        "kind": "thumb_no",
        "tile_id": "t2",
        "target_session": "auto-bar",
    })
    await actions.thumb_no(row, services_capture)

    assert services_capture._sent == [("auto-bar", "COORDINATOR: Operator: thumb no on t2.")]


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
        ("auto-baz", "COORDINATOR: Operator chose: ship it on t3."),
    ]


@pytest.mark.asyncio
async def test_custom_passes_through_verbatim(services_capture):
    """``custom`` keeps the body text but adds the board prefix."""
    from tools.dashboard.plugins.coordinator_board.entrypoints import actions

    row = _make_row({
        "kind": "custom",
        "tile_id": "t4",
        "choice": "hold off until Friday — legal still reviewing",
        "target_session": "auto-quux",
    })
    await actions.custom_reply(row, services_capture)

    assert services_capture._sent == [
        ("auto-quux", "COORDINATOR: hold off until Friday — legal still reviewing"),
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
        ("auto-foo", "COORDINATOR: Operator requests a sitrep on t5."),
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
        ("auto-foo", "COORDINATOR: Operator requests a refresh on t6."),
    ]


# ── operator-message handler ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_operator_message_routes_to_coordinator(
    graph_db_env, services_capture,
):
    """Resolves the bound coordinator session and adds the board prefix."""
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
        org="autonomy",  # actions read the binding at COORDINATOR_ORG
    )

    await actions.operator_message(row, services_capture)
    assert services_capture._sent == [
        ("auto-coord-9", "COORDINATOR: ack — sequencing approved"),
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
    """The module's ``register_action_decorator`` calls populate the registry.

    Seven handlers total: six on the decision set (one per ``kind``)
    plus one on the operator-message set. Predicates are wrapped INSIDE
    the registered handler now (bead auto-rc27t), so we no longer
    inspect them via ``RegisteredAction.predicate`` — instead the
    end-to-end dispatch tests below verify only the matching ``kind``
    triggers its handler.
    """
    # Force a fresh import so registration fires under the cleared
    # registry (the autouse fixture clears between tests, but the module
    # is import-cached after the first test).
    import importlib
    from tools.dashboard.plugins.coordinator_board.entrypoints import actions
    importlib.reload(actions)

    decision_actions = _HANDLERS.get(COORDINATOR_DECISION_SET_ID, [])
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

    op_actions = _HANDLERS.get(OPERATOR_MESSAGE_SET_ID, [])
    assert len(op_actions) == 1
    assert op_actions[0].name == "coordinator_board.operator_message"


# ── End-to-end via the substrate's dispatch path ─────────────────────


def _register_permissive_schemas() -> None:
    """Register no-op SettingSchemas for the two coordinator-board sets.

    The full validators live in ``entrypoints/schemas.py`` (operator
    message) and will land via ``auto-lffg5`` (decision). Tests need
    ``add_setting`` to work without being coupled to those validators —
    a permissive override keeps the test focused on dispatch behavior.
    """
    # The real validators (entrypoints/schemas.py) auto-register when their
    # module is imported — which happens transitively whenever a sibling test
    # module that pulls in the coordinator entrypoints is collected on this
    # xdist worker. Defining a SettingSchema subclass auto-registers it, and
    # register_schema refuses to overwrite a different class, so drop any
    # existing registration for these keys BEFORE defining the stubs (the
    # auto-register on class definition would otherwise collide). The autouse
    # _isolate_schema_registry fixture restores the real schemas after the test.
    for _sid in (COORDINATOR_SET_ID, COORDINATOR_DECISION_SET_ID, OPERATOR_MESSAGE_SET_ID):
        SCHEMAS.pop(f"{_sid}#1", None)

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


def _decision_event(key: str) -> dict:
    return {
        "set_id": COORDINATOR_DECISION_SET_ID,
        "schema_revision": 1,
        "key": key,
        "org": "autonomy",
        "publication_state": "raw",
        "deprecated": False,
        "operation": "write",
    }


def _operator_message_event(key: str) -> dict:
    return {
        "set_id": OPERATOR_MESSAGE_SET_ID,
        "schema_revision": 1,
        "key": key,
        "org": "autonomy",
        "publication_state": "raw",
        "deprecated": False,
        "operation": "write",
    }


@pytest.mark.asyncio
async def test_dispatch_thumb_yes_end_to_end(
    graph_db_env, services_capture,
):
    """Acceptance #1 — writing a decision row + setting.changed produces a session_send.

    Covers registration + set membership + predicate match + handler
    invocation in one path through the new dispatcher.
    """
    import importlib
    from tools.dashboard.plugins.coordinator_board.entrypoints import actions
    importlib.reload(actions)

    _register_permissive_schemas()

    from tools.graph import ops
    key = "thumb-yes-1"
    ops.add_setting(
        COORDINATOR_DECISION_SET_ID, 1, key,
        {
            "kind": "thumb_yes",
            "tile_id": "t1",
            "target_session": "auto-foo",
        },
        org="autonomy",
    )

    await _dispatch_event(_decision_event(key), services_capture)

    assert services_capture._sent == [
        ("auto-foo", "COORDINATOR: Operator: thumb yes on t1."),
    ], f"expected one session_send; got {services_capture._sent}"


@pytest.mark.asyncio
async def test_dispatch_only_matching_predicate_handler_fires(
    graph_db_env, services_capture,
):
    """A ``choice``-kind row triggers only the choice handler, not thumb_yes."""
    import importlib
    from tools.dashboard.plugins.coordinator_board.entrypoints import actions
    importlib.reload(actions)

    _register_permissive_schemas()

    from tools.graph import ops
    key = "choice-1"
    ops.add_setting(
        COORDINATOR_DECISION_SET_ID, 1, key,
        {
            "kind": "choice",
            "tile_id": "tile-7",
            "choice": "approve",
            "target_session": "auto-target",
        },
        org="autonomy",
    )

    await _dispatch_event(_decision_event(key), services_capture)

    assert services_capture._sent == [
        ("auto-target", "COORDINATOR: Operator chose: approve on tile-7."),
    ]


@pytest.mark.asyncio
async def test_dispatch_operator_message_routes_to_bound_coordinator(
    graph_db_env, services_capture,
):
    """``operator-message`` uses ``dashboard.coordinator``."""
    import importlib
    from tools.dashboard.plugins.coordinator_board.entrypoints import actions
    importlib.reload(actions)

    _register_permissive_schemas()

    from tools.graph import ops
    ops.add_setting(
        COORDINATOR_SET_ID, 1, "default",
        {"session_id": "auto-bound-coord"},
        org="autonomy",  # actions read the binding at COORDINATOR_ORG
    )
    ops.add_setting(
        OPERATOR_MESSAGE_SET_ID, 1,
        "default",
        {"text": "are you there", "sentAt": None},
        org="autonomy",
    )

    await _dispatch_event(_operator_message_event("default"), services_capture)

    assert services_capture._sent == [
        ("auto-bound-coord", "COORDINATOR: are you there"),
    ]


@pytest.mark.asyncio
async def test_dispatch_operator_message_no_coordinator_drops(
    graph_db_env, services_capture,
):
    """Acceptance #5 — operator-message with no coordinator binding never raises."""
    import importlib
    from tools.dashboard.plugins.coordinator_board.entrypoints import actions
    importlib.reload(actions)

    _register_permissive_schemas()

    from tools.graph import ops
    ops.add_setting(
        OPERATOR_MESSAGE_SET_ID, 1,
        "default",
        {"text": "are you there", "sentAt": None},
        org="autonomy",
    )

    await _dispatch_event(_operator_message_event("default"), services_capture)

    assert services_capture._sent == [], (
        "with no dashboard.coordinator binding, handler must not call session_send"
    )
