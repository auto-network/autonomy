"""Tests for the Setting schema registry.

Covers ``register_schema``/``register_upconverter`` registration + lookup,
plus chain composition + identity/missing edge cases. Spec:
graph://0d3f750f-f9c § Schema versioning.
"""

from __future__ import annotations

import pytest

from tools.graph import schemas
from tools.graph.schemas.registry import (
    SCHEMAS, UPCONVERTERS, SchemaValidationError,
)


@pytest.fixture(autouse=True)
def _isolate_registry():
    schemas_snap = dict(SCHEMAS)
    upcon_snap = dict(UPCONVERTERS)
    try:
        yield
    finally:
        SCHEMAS.clear()
        SCHEMAS.update(schemas_snap)
        UPCONVERTERS.clear()
        UPCONVERTERS.update(upcon_snap)


def test_register_and_get_schema():
    class V1(schemas.SettingSchema):
        set_id = "x.y"
        schema_revision = 1

    schemas.register_schema("x.y", 1, V1)
    assert schemas.get_schema("x.y", 1) is V1


def test_unknown_schema_returns_none():
    assert schemas.get_schema("nope", 99) is None


def test_register_schema_with_inline_upconverter_chain():
    class V1(schemas.SettingSchema):
        set_id = "x.y"
        schema_revision = 1

    class V2(schemas.SettingSchema):
        set_id = "x.y"
        schema_revision = 2

    schemas.register_schema("x.y", 1, V1)
    schemas.register_schema("x.y", 2, V2,
                            upconvert_from_prev=lambda p: {**p, "v2": True})
    chain = schemas.upconvert_chain("x.y", 1, 2)
    assert chain is not None
    assert len(chain) == 1


def test_register_upconverter_must_be_single_step():
    with pytest.raises(ValueError):
        schemas.register_upconverter("x.y", 1, 3, lambda p: p)


def test_validate_payload_unknown_schema_raises():
    with pytest.raises(SchemaValidationError):
        schemas.validate_payload("not.registered", 1, {})


def test_validate_payload_default_accepts_dict():
    class V1(schemas.SettingSchema):
        set_id = "x.y"
        schema_revision = 1

    schemas.register_schema("x.y", 1, V1)
    schemas.validate_payload("x.y", 1, {"any": "value"})  # no raise


def test_validate_payload_default_rejects_non_dict():
    class V1(schemas.SettingSchema):
        set_id = "x.y"
        schema_revision = 1

    schemas.register_schema("x.y", 1, V1)
    with pytest.raises(SchemaValidationError):
        schemas.validate_payload("x.y", 1, "not a dict")


def test_list_registered_set_ids_dedups_revisions():
    class V1(schemas.SettingSchema):
        set_id = "a.b"
        schema_revision = 1

    class V2(schemas.SettingSchema):
        set_id = "a.b"
        schema_revision = 2

    class V1c(schemas.SettingSchema):
        set_id = "c.d"
        schema_revision = 1

    schemas.register_schema("a.b", 1, V1)
    schemas.register_schema("a.b", 2, V2)
    schemas.register_schema("c.d", 1, V1c)
    ids = schemas.list_registered_set_ids()
    assert "a.b" in ids and "c.d" in ids


def test_schema_key_format():
    assert schemas.schema_key("x.y", 1) == "x.y#1"


def test_upconvert_payload_returns_none_on_gap():
    class V1(schemas.SettingSchema):
        set_id = "g.g"
        schema_revision = 1

    class V3(schemas.SettingSchema):
        set_id = "g.g"
        schema_revision = 3

    schemas.register_schema("g.g", 1, V1)
    schemas.register_schema("g.g", 3, V3)  # no 2; no chain
    assert schemas.upconvert_payload("g.g", 1, 3, {"x": 1}) is None


# ── Auto-registration via __init_subclass__ ──────────────────


def test_auto_register_subclass_with_set_id_and_revision():
    """A subclass declaring both ``set_id`` and ``schema_revision`` is
    discoverable via ``get_schema`` without an explicit ``register_schema``
    call."""

    class V1(schemas.SettingSchema):
        set_id = "auto.reg"
        schema_revision = 1

    assert schemas.get_schema("auto.reg", 1) is V1


def test_auto_register_skipped_when_revision_missing():
    """A subclass with only ``set_id`` (no ``schema_revision``) does NOT
    register — that's an abstract intermediate base."""

    class AbstractBase(schemas.SettingSchema):
        set_id = "auto.abstract"
        # schema_revision intentionally omitted

    assert schemas.get_schema("auto.abstract", 0) is None
    assert schemas.get_schema("auto.abstract", 1) is None


def test_auto_register_skipped_when_set_id_missing():
    """A subclass with only ``schema_revision`` (no ``set_id``) does NOT
    register — that's an abstract intermediate base."""

    class AbstractBase(schemas.SettingSchema):
        schema_revision = 1
        # set_id intentionally omitted

    # Nothing landed under the empty set_id + revision 1 combination.
    assert schemas.get_schema("", 1) is None


def test_auto_register_picks_up_classmethod_upconvert_from_prev():
    """A subclass defining an ``upconvert_from_prev`` classmethod
    auto-registers the upconverter alongside the schema."""

    class V1(schemas.SettingSchema):
        set_id = "auto.up"
        schema_revision = 1

    class V2(schemas.SettingSchema):
        set_id = "auto.up"
        schema_revision = 2

        @classmethod
        def upconvert_from_prev(cls, payload: dict) -> dict:
            return {**payload, "v2": True}

    chain = schemas.upconvert_chain("auto.up", 1, 2)
    assert chain is not None
    assert len(chain) == 1
    assert chain[0]({"x": 1}) == {"x": 1, "v2": True}


def test_auto_register_idempotent_with_explicit_call():
    """Explicit ``register_schema`` on the same triple is idempotent
    after auto-registration — re-registration overwrites with the same
    class."""

    class V1(schemas.SettingSchema):
        set_id = "auto.idem"
        schema_revision = 1

    schemas.register_schema("auto.idem", 1, V1)
    assert schemas.get_schema("auto.idem", 1) is V1


def test_auto_register_inherited_set_id_does_not_register_subclass():
    """Variant subclasses inheriting ``set_id`` / ``schema_revision`` from
    a parent (not declaring them in their own ``__dict__``) do NOT
    re-register under the parent's key."""

    class Parent(schemas.SettingSchema):
        set_id = "auto.var"
        schema_revision = 1

    class Variant(Parent):
        pass

    # The lookup still resolves to the parent — the subclass's
    # auto-registration was skipped because it didn't declare its own
    # set_id/schema_revision.
    assert schemas.get_schema("auto.var", 1) is Parent


# ── set_id_suffix composition (auto-uqdkk) ────────────────────


def test_set_id_suffix_three_level_composition():
    """A 3-level ``set_id_suffix`` chain composes left-to-right, with each
    level appending one dotted leaf to the running prefix written by the
    previous level."""

    class Parent(schemas.SettingSchema):
        set_id = "compose.root"

    class Child(Parent):
        set_id_suffix = "a"

    class Grandchild(Child):
        set_id_suffix = "b"

    assert Parent.set_id == "compose.root"
    assert Child.set_id == "compose.root.a"
    assert Grandchild.set_id == "compose.root.a.b"
    # The composed value lives in the subclass's own __dict__ so that
    # descendants find it via their MRO walk.
    assert Child.__dict__["set_id"] == "compose.root.a"
    assert Grandchild.__dict__["set_id"] == "compose.root.a.b"


def test_set_id_suffix_without_namespace_ancestor_raises():
    """Declaring ``set_id_suffix`` without an ancestor that provides a
    namespace ``set_id`` raises ``TypeError`` at class definition."""

    with pytest.raises(TypeError, match="set_id_suffix"):
        class Orphan(schemas.SettingSchema):  # noqa: F841
            set_id_suffix = "lost"
            schema_revision = 1


def test_collision_on_duplicate_composition_raises():
    """Two registered subclasses that compose to the same ``(set_id,
    schema_revision)`` raise ``TypeError`` at the second class definition."""

    class Parent(schemas.SettingSchema):
        set_id = "collide.root"

    class A(Parent):  # noqa: F841 — registers compose.root.x#1 to A
        set_id_suffix = "x"
        schema_revision = 1

    with pytest.raises(TypeError, match="collision|already registered"):
        class B(Parent):  # noqa: F841
            set_id_suffix = "x"
            schema_revision = 1


def test_prefix_matching_finds_namespace_descendants():
    """``list_registered_set_ids()`` plus a prefix filter surfaces every
    schema descended from an intermediate namespace — the substrate-level
    "all descendants of <namespace>" query."""

    class Root(schemas.SettingSchema):
        set_id = "prefix.root"

    class Middle(Root):
        # No schema_revision — namespace intermediate, doesn't itself
        # register, but its composed set_id provides the prefix for
        # descendants below.
        set_id_suffix = "ns"

    class Leaf1(Middle):  # noqa: F841
        set_id_suffix = "x"
        schema_revision = 1

    class Leaf2(Middle):  # noqa: F841
        set_id_suffix = "y"
        schema_revision = 1

    descendants = [
        s for s in schemas.list_registered_set_ids()
        if s.startswith("prefix.root.ns.")
    ]
    assert "prefix.root.ns.x" in descendants
    assert "prefix.root.ns.y" in descendants
    # The intermediate namespace itself didn't register.
    assert "prefix.root.ns" not in schemas.list_registered_set_ids()


# ── @action / @on_kind marker decorators ─────────────────────


@pytest.fixture
def _isolate_action_registry():
    """Snapshot + restore the settings-mediator handler registry.

    ``__init_subclass__`` action discovery registers entries in the
    substrate's ``_HANDLERS`` dict via ``register_action_decorator``;
    tests need their entries to land + be cleaned up without affecting
    sibling tests.
    """
    from tools.dashboard.settings_mediator.loop import _HANDLERS
    snap = {k: list(v) for k, v in _HANDLERS.items()}
    try:
        yield _HANDLERS
    finally:
        _HANDLERS.clear()
        for k, v in snap.items():
            _HANDLERS[k] = list(v)


def test_action_marker_registers_default_handler(_isolate_action_registry):
    """A schema with ``@action async def deliver(row, svc)`` registers a
    default action under ``f"{cls.__name__}.deliver"`` with no predicate
    on the schema's ``set_id``."""
    handlers = _isolate_action_registry

    class WidgetV1(schemas.SettingSchema):
        set_id = "act.widget"
        schema_revision = 1

        @schemas.action
        async def deliver(row, svc):
            pass

    actions = handlers.get("act.widget", [])
    assert len(actions) == 1
    assert actions[0].name == "WidgetV1.deliver"
    # Default action: dispatch fires on every row (no predicate filter).
    assert actions[0].fn is WidgetV1.deliver


@pytest.mark.asyncio
async def test_on_kind_marker_registers_predicate_per_kind(
    _isolate_action_registry,
):
    """A schema with multiple ``@on_kind("...")`` methods registers one
    predicate-filtered action per kind, each firing only when
    ``row.get("kind")`` matches its declared kind."""
    handlers = _isolate_action_registry
    fired: list[tuple[str, str]] = []

    class DecisionV1(schemas.SettingSchema):
        set_id = "act.decision"
        schema_revision = 1

        @schemas.on_kind("thumb_yes")
        async def thumb_yes(row, svc):
            fired.append(("thumb_yes", row["tile_id"]))

        @schemas.on_kind("thumb_no")
        async def thumb_no(row, svc):
            fired.append(("thumb_no", row["tile_id"]))

        @schemas.on_kind("choice")
        async def choice(row, svc):
            fired.append(("choice", row["choice"]))

    actions = handlers.get("act.decision", [])
    assert len(actions) == 3
    names = {a.name for a in actions}
    assert names == {
        "DecisionV1.thumb_yes",
        "DecisionV1.thumb_no",
        "DecisionV1.choice",
    }

    from tools.dashboard.settings_mediator.loop import Row

    def _row(payload: dict) -> Row:
        return Row(
            id="r", set_id="act.decision", key="k",
            payload=payload, created_at="", updated_at="",
        )

    # Each registered handler is wrapped in a predicate. A row whose
    # ``kind`` doesn't match a given handler should be skipped by that
    # handler — only the matching one fires.
    for action in actions:
        await action.fn(_row({"kind": "thumb_yes", "tile_id": "t1"}), None)
    assert fired == [("thumb_yes", "t1")]
    fired.clear()

    for action in actions:
        await action.fn(_row({
            "kind": "choice", "tile_id": "t2", "choice": "yep",
        }), None)
    assert fired == [("choice", "yep")]


def test_marker_on_non_callable_silently_ignored(_isolate_action_registry):
    """Marker attributes on non-callable values are silently skipped — only
    callable methods register as actions."""
    handlers = _isolate_action_registry

    class _Sentinel:
        """Class-attribute shim that accepts arbitrary attribute writes."""

    sentinel = _Sentinel()
    sentinel._is_substrate_action = True  # type: ignore[attr-defined]
    sentinel_kind = _Sentinel()
    sentinel_kind._action_kind = "ignored_kind"  # type: ignore[attr-defined]

    class DodgyV1(schemas.SettingSchema):
        set_id = "act.dodgy"
        schema_revision = 1

        # Markers stamped on non-callable instances must not register —
        # discovery filters by ``callable(value)`` first.
        not_a_method = sentinel
        also_not = sentinel_kind

        @schemas.action
        async def deliver(row, svc):
            pass

    actions = handlers.get("act.dodgy", [])
    assert len(actions) == 1, (
        f"only the callable-marked method should register; got {[a.name for a in actions]}"
    )
    assert actions[0].name == "DodgyV1.deliver"


@pytest.mark.asyncio
async def test_on_kind_migration_parity_with_free_function_form(
    _isolate_action_registry,
):
    """Migration test: rewriting the coordinator-board free-function
    handlers as ``@on_kind`` methods on a single schema produces the same
    set of registered actions (count + dispatch behavior) the existing
    ``@register_action_decorator`` form produces.

    This mirrors the six handlers currently in
    ``tools.dashboard.plugins.coordinator_board.entrypoints.actions``
    (``thumb_yes``, ``thumb_no``, ``choice``, ``custom_reply``,
    ``sitrep_request``, ``refresh_request``) on a per-test schema so
    we can observe registration + dispatch end-to-end without reloading
    the production module.
    """
    handlers = _isolate_action_registry
    sent: list[tuple[str, str]] = []

    class _SvcStub:
        async def session_send(self, target: str, text: str) -> None:
            sent.append((target, text))

    svc = _SvcStub()

    class CoordinatorDecisionTestV1(schemas.SettingSchema):
        set_id = "act.coord-decision"
        schema_revision = 1

        @schemas.on_kind("thumb_yes")
        async def thumb_yes(row, svc):
            await svc.session_send(
                row["target_session"],
                f"COORDINATOR: Operator: thumb yes on {row['tile_id']}.",
            )

        @schemas.on_kind("thumb_no")
        async def thumb_no(row, svc):
            await svc.session_send(
                row["target_session"],
                f"COORDINATOR: Operator: thumb no on {row['tile_id']}.",
            )

        @schemas.on_kind("choice")
        async def choice(row, svc):
            await svc.session_send(
                row["target_session"],
                f"COORDINATOR: Operator chose: {row['choice']} on {row['tile_id']}.",
            )

        @schemas.on_kind("custom")
        async def custom_reply(row, svc):
            await svc.session_send(
                row["target_session"],
                f"COORDINATOR: {row['choice']}",
            )

        @schemas.on_kind("sitrep_request")
        async def sitrep_request(row, svc):
            await svc.session_send(
                row["target_session"],
                f"COORDINATOR: Operator requests a sitrep on {row['tile_id']}.",
            )

        @schemas.on_kind("refresh_request")
        async def refresh_request(row, svc):
            await svc.session_send(
                row["target_session"],
                f"COORDINATOR: Operator requests a refresh on {row['tile_id']}.",
            )

    actions = handlers.get("act.coord-decision", [])
    # Same count as the production free-function form (six decision kinds).
    assert len(actions) == 6
    expected_names = {
        "CoordinatorDecisionTestV1.thumb_yes",
        "CoordinatorDecisionTestV1.thumb_no",
        "CoordinatorDecisionTestV1.choice",
        "CoordinatorDecisionTestV1.custom_reply",
        "CoordinatorDecisionTestV1.sitrep_request",
        "CoordinatorDecisionTestV1.refresh_request",
    }
    assert {a.name for a in actions} == expected_names

    from tools.dashboard.settings_mediator.loop import Row

    def _row(payload: dict) -> Row:
        return Row(
            id="r", set_id="act.coord-decision", key="k",
            payload=payload, created_at="", updated_at="",
        )

    # Dispatch behavior parity: each kind triggers exactly its handler's
    # synthesized text. We invoke every registered action against a row
    # carrying that ``kind`` and verify only the matching handler fires.
    cases = [
        (
            {"kind": "thumb_yes", "tile_id": "t1", "target_session": "auto-foo"},
            ("auto-foo", "COORDINATOR: Operator: thumb yes on t1."),
        ),
        (
            {"kind": "thumb_no", "tile_id": "t2", "target_session": "auto-bar"},
            ("auto-bar", "COORDINATOR: Operator: thumb no on t2."),
        ),
        (
            {"kind": "choice", "tile_id": "t3", "choice": "ship it",
             "target_session": "auto-baz"},
            ("auto-baz", "COORDINATOR: Operator chose: ship it on t3."),
        ),
        (
            {"kind": "custom", "tile_id": "t4", "choice": "hold off",
             "target_session": "auto-quux"},
            ("auto-quux", "COORDINATOR: hold off"),
        ),
        (
            {"kind": "sitrep_request", "tile_id": "t5",
             "target_session": "auto-foo"},
            ("auto-foo", "COORDINATOR: Operator requests a sitrep on t5."),
        ),
        (
            {"kind": "refresh_request", "tile_id": "t6",
             "target_session": "auto-foo"},
            ("auto-foo", "COORDINATOR: Operator requests a refresh on t6."),
        ),
    ]
    for payload, expected in cases:
        sent.clear()
        for action in actions:
            await action.fn(_row(payload), svc)
        assert sent == [expected], (
            f"kind={payload['kind']}: expected one fire {expected}, got {sent}"
        )
