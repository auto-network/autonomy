"""Tests for schema-level access-pattern decorators.

Covers ``@append_only_log`` / ``@singleton`` / ``@keyed_per_entity``
storing ``_access_pattern`` and ``_key_strategy`` on the decorated
schema class. Undecorated schemas leave both ``None`` (the substrate's
generic ``.write({key, payload})`` escape hatch).

Decorators compose with typed ``field()`` declarations and the legacy
``_field_metadata`` dict form; they are inherited by subclasses (subject
to override by re-decoration).
"""

from __future__ import annotations

import pytest

from tools.graph.schemas.registry import (
    SCHEMAS,
    UPCONVERTERS,
    SettingSchema,
    append_only_log,
    field,
    keyed_per_entity,
    singleton,
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


# ── @append_only_log ─────────────────────────────────────────


def test_append_only_log_bare_form():
    @append_only_log
    class V1(SettingSchema):
        set_id = "x.y"
        schema_revision = 1

    assert V1._access_pattern == "append_only_log"
    assert V1._key_strategy == "uuid_v4"


def test_append_only_log_parens_form():
    @append_only_log()
    class V1(SettingSchema):
        set_id = "x.y"
        schema_revision = 1

    assert V1._access_pattern == "append_only_log"
    assert V1._key_strategy == "uuid_v4"


def test_append_only_log_explicit_key_strategy():
    @append_only_log(key="snowflake_id")
    class V1(SettingSchema):
        set_id = "x.y"
        schema_revision = 1

    assert V1._access_pattern == "append_only_log"
    assert V1._key_strategy == "snowflake_id"


def test_append_only_log_callable_key_normalizes_to_name():
    """Design-note example writes ``@append_only_log(key=uuid_v4)`` with
    a callable reference; the decorator must normalize to the callable's
    ``__name__`` so ``_key_strategy`` always holds a string for
    serialization (regression coverage for auto-cvon6).
    """
    def uuid_v4():
        return "abc"

    @append_only_log(key=uuid_v4)
    class V1(SettingSchema):
        set_id = "x.y"
        schema_revision = 1

    assert V1._key_strategy == "uuid_v4"
    assert isinstance(V1._key_strategy, str)


def test_append_only_log_callable_without_name_falls_back_to_repr():
    """Lambdas and unnamed callables fall back to repr — defensive,
    keeps the contract that ``_key_strategy`` is always a string.
    """
    @append_only_log(key=lambda: "x")
    class V1(SettingSchema):
        set_id = "x.y"
        schema_revision = 1

    # lambdas have ``__name__ == '<lambda>'``, so we get that string.
    assert isinstance(V1._key_strategy, str)
    assert V1._key_strategy == "<lambda>"


# ── @singleton ───────────────────────────────────────────────


def test_singleton_bare_form_uses_default_key():
    @singleton
    class V1(SettingSchema):
        set_id = "x.y"
        schema_revision = 1

    assert V1._access_pattern == "singleton"
    assert V1._key_strategy == "fixed:default"


def test_singleton_parens_form_uses_default_key():
    @singleton()
    class V1(SettingSchema):
        set_id = "x.y"
        schema_revision = 1

    assert V1._key_strategy == "fixed:default"


def test_singleton_explicit_key():
    @singleton(key="canonical")
    class V1(SettingSchema):
        set_id = "x.y"
        schema_revision = 1

    assert V1._access_pattern == "singleton"
    assert V1._key_strategy == "fixed:canonical"


# ── @keyed_per_entity ────────────────────────────────────────


def test_keyed_per_entity_bare_form():
    @keyed_per_entity
    class V1(SettingSchema):
        set_id = "x.y"
        schema_revision = 1

    assert V1._access_pattern == "keyed_per_entity"
    assert V1._key_strategy == "natural"


def test_keyed_per_entity_explicit_strategy():
    @keyed_per_entity(key_strategy="composite")
    class V1(SettingSchema):
        set_id = "x.y"
        schema_revision = 1

    assert V1._key_strategy == "composite"


# ── Undecorated default ──────────────────────────────────────


def test_undecorated_schema_has_none_access_pattern():
    class V1(SettingSchema):
        set_id = "x.y"
        schema_revision = 1
        name: str = field(required=True)

    assert V1._access_pattern is None
    assert V1._key_strategy is None


# ── Composition with typed field() declarations ──────────────


def test_decorator_composes_with_typed_field_declarations():
    @append_only_log(key="uuid_v4")
    class V1(SettingSchema):
        set_id = "x.y"
        schema_revision = 1
        tile_id: str = field(required=True, description="Tile reference")

    assert V1._access_pattern == "append_only_log"
    assert V1._field_metadata == {
        "tile_id": {
            "type": "string",
            "description": "Tile reference",
            "required": True,
        },
    }


def test_decorator_composes_with_legacy_dict_form():
    @keyed_per_entity
    class V1(SettingSchema):
        set_id = "x.y"
        schema_revision = 1
        _field_metadata = {
            "name": {"type": "string", "required": True, "description": "N"},
        }

    assert V1._access_pattern == "keyed_per_entity"
    assert V1._field_metadata["name"]["description"] == "N"


# ── Inheritance ──────────────────────────────────────────────


def test_decorator_metadata_inherits_to_subclass():
    @append_only_log
    class Base(SettingSchema):
        set_id = "x.y"
        schema_revision = 1
        tile_id: str = field(description="Base field")

    class Child(Base):
        extra: str = field(description="Child field")

    assert Child._access_pattern == "append_only_log"
    assert Child._key_strategy == "uuid_v4"
    # Inheritance fix from auto-vumin: child also sees base's typed fields.
    assert set(Child._field_metadata) == {"tile_id", "extra"}


def test_subclass_redecorates_to_override_pattern():
    @append_only_log
    class Base(SettingSchema):
        set_id = "x.y"
        schema_revision = 1

    @keyed_per_entity
    class Child(Base):
        pass

    assert Base._access_pattern == "append_only_log"
    assert Child._access_pattern == "keyed_per_entity"
    assert Child._key_strategy == "natural"


# ── Hygiene ──────────────────────────────────────────────────


def test_decorators_return_the_class():
    """Each decorator must return the class so other decorators can stack."""
    @append_only_log
    class A(SettingSchema):
        set_id = "x.y"
        schema_revision = 1

    @singleton
    class B(SettingSchema):
        set_id = "x.z"
        schema_revision = 1

    @keyed_per_entity
    class C(SettingSchema):
        set_id = "x.w"
        schema_revision = 1

    assert isinstance(A, type) and issubclass(A, SettingSchema)
    assert isinstance(B, type) and issubclass(B, SettingSchema)
    assert isinstance(C, type) and issubclass(C, SettingSchema)


def test_decorator_does_not_affect_export_json_schema_payload():
    """1B stores access pattern as class attributes only; surfacing it
    in export_json_schema's payload is bead 1D's job. This test pins
    that 1B doesn't leak the new metadata into the existing payload
    shape (regression coverage for unchanged downstream consumers).
    """
    @append_only_log
    class V1(SettingSchema):
        set_id = "x.y"
        schema_revision = 1
        name: str = field(required=True, description="N")

    js = V1.export_json_schema()
    assert "access_pattern" not in js
    assert "key_strategy" not in js
    # Existing keys stay where they were.
    assert js["set_id"] == "x.y"
    assert js["schema_revision"] == 1
    assert js["type"] == "object"
    assert js["required"] == ["name"]
