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
