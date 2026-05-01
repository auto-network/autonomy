"""Tests for the typed-field declaration shape on ``SettingSchema``.

Covers ``field()`` + the ``__init_subclass__`` derivation that turns
typed annotations into ``_field_metadata`` entries. The legacy direct
``_field_metadata`` dict assignment continues to work; tests below
verify both shapes and their precedence rules.
"""

from __future__ import annotations

import logging

import pytest

from tools.graph.schemas.registry import (
    SCHEMAS,
    UPCONVERTERS,
    SettingSchema,
    field,
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


# ── Typed-field shape ────────────────────────────────────────


def test_typed_field_required_no_default_marks_required():
    class V1(SettingSchema):
        set_id = "x.y"
        schema_revision = 1
        name: str = field(description="A name")

    assert V1._field_metadata == {
        "name": {
            "type": "string",
            "description": "A name",
            "required": True,
        },
    }
    # _FieldSpec must not leak as a class attribute.
    assert "name" not in V1.__dict__


def test_typed_field_default_marks_optional_and_replaces_attribute():
    class V1(SettingSchema):
        set_id = "x.y"
        schema_revision = 1
        harness: str = field(default="claude", description="Agent CLI")

    assert V1._field_metadata == {
        "harness": {
            "type": "string",
            "description": "Agent CLI",
            "default": "claude",
        },
    }
    # The class attribute now carries the actual default value.
    assert V1.harness == "claude"


def test_typed_field_default_factory_invokes_at_class_creation():
    counter = {"calls": 0}

    def make_default():
        counter["calls"] += 1
        return ["always", "fresh"]

    class V1(SettingSchema):
        set_id = "x.y"
        schema_revision = 1
        tags: list = field(default_factory=make_default, description="Tags")

    assert counter["calls"] == 1
    assert V1.tags == ["always", "fresh"]
    meta = V1._field_metadata["tags"]
    assert meta["type"] == "array"
    assert "required" not in meta  # default_factory present → optional


def test_typed_field_explicit_required_false_overrides_inference():
    class V1(SettingSchema):
        set_id = "x.y"
        schema_revision = 1
        # No default but explicit required=False — must NOT be required.
        note: str = field(required=False, description="Optional note")

    meta = V1._field_metadata["note"]
    assert "required" not in meta


def test_typed_field_enum_flows_through():
    class V1(SettingSchema):
        set_id = "x.y"
        schema_revision = 1
        harness: str = field(
            default="claude",
            enum=["claude", "codex"],
            description="Agent CLI",
        )

    assert V1._field_metadata["harness"]["enum"] == ["claude", "codex"]


def test_typed_field_element_normalizes_python_type():
    class V1(SettingSchema):
        set_id = "x.y"
        schema_revision = 1
        tags: list = field(element=str, description="Tag names")

    assert V1._field_metadata["tags"]["element"] == "string"


def test_typed_field_element_normalizes_dict_of_types():
    class V1(SettingSchema):
        set_id = "x.y"
        schema_revision = 1
        repos: list = field(
            element={"url": str, "mount": str, "writable": bool},
            description="Repo mounts",
        )

    elt = V1._field_metadata["repos"]["element"]
    assert elt == {"url": "string", "mount": "string", "writable": "boolean"}


def test_typed_field_export_json_schema_round_trips():
    class V1(SettingSchema):
        set_id = "x.y"
        schema_revision = 1
        name: str = field(required=True, description="Workspace identifier")
        harness: str = field(
            default="claude",
            enum=["claude", "codex"],
            description="Agent CLI",
        )

    js = V1.export_json_schema()
    assert js["set_id"] == "x.y"
    assert js["schema_revision"] == 1
    assert js["type"] == "object"
    assert js["required"] == ["name"]
    assert js["properties"]["name"]["description"] == "Workspace identifier"
    assert js["properties"]["harness"]["enum"] == ["claude", "codex"]
    assert js["properties"]["harness"]["default"] == "claude"


# ── Coexistence with the legacy dict form ────────────────────


def test_legacy_field_metadata_dict_form_still_works():
    class V1(SettingSchema):
        set_id = "x.y"
        schema_revision = 1
        _field_metadata: dict[str, dict] = {
            "name": {
                "type": "string",
                "required": True,
                "description": "Name",
            },
        }

    assert V1._field_metadata == {
        "name": {
            "type": "string",
            "required": True,
            "description": "Name",
        },
    }


def test_typed_fields_take_precedence_over_dict_form():
    class V1(SettingSchema):
        set_id = "x.y"
        schema_revision = 1
        _field_metadata: dict[str, dict] = {
            "name": {
                "type": "string",
                "description": "Old description",
            },
            "extra": {
                "type": "string",
                "description": "Only in dict",
            },
        }
        name: str = field(required=True, description="New description")

    assert V1._field_metadata["name"] == {
        "type": "string",
        "required": True,
        "description": "New description",
    }
    # dict-only entries persist alongside typed-derived ones.
    assert V1._field_metadata["extra"] == {
        "type": "string",
        "description": "Only in dict",
    }


def test_typed_fields_inherit_across_multiple_levels():
    class Base(SettingSchema):
        set_id = "x.y"
        schema_revision = 1
        tile_id: str = field(description="Base field")

    class Child(Base):
        extra: str = field(description="Child field")

    class Grandchild(Child):
        final: bool = field(description="Grandchild field")

    assert set(Base._field_metadata) == {"tile_id"}
    assert set(Child._field_metadata) == {"tile_id", "extra"}
    assert set(Grandchild._field_metadata) == {"tile_id", "extra", "final"}


def test_child_dict_form_composes_with_inherited_typed_fields():
    class Base(SettingSchema):
        set_id = "x.y"
        schema_revision = 1
        tile_id: str = field(description="Base field")

    class Child(Base):
        _field_metadata = {
            "status": {
                "type": "string",
                "description": "Child dict-form field",
            },
        }

    assert Child._field_metadata["tile_id"] == {
        "type": "string",
        "description": "Base field",
        "required": True,
    }
    assert Child._field_metadata["status"] == {
        "type": "string",
        "description": "Child dict-form field",
    }


def test_child_typed_field_override_wins_over_parent_typed_field():
    class Base(SettingSchema):
        set_id = "x.y"
        schema_revision = 1
        name: str = field(required=True, description="Base description")

    class Child(Base):
        name: str = field(default="child", description="Child description")

    assert Base._field_metadata["name"] == {
        "type": "string",
        "required": True,
        "description": "Base description",
    }
    assert Child._field_metadata["name"] == {
        "type": "string",
        "description": "Child description",
        "default": "child",
    }
    assert Child.name == "child"


def test_get_type_hints_failure_logs_warning_and_falls_back_to_string(caplog):
    with caplog.at_level(logging.WARNING):
        class V1(SettingSchema):
            set_id = "x.y"
            schema_revision = 1
            ok: int = field(description="Should be integer when hints resolve")
            ref: MissingType = field(description="Missing forward ref")

    assert "get_type_hints(" in caplog.text
    assert "V1" in caplog.text
    assert "typed annotations fall back to raw strings" in caplog.text
    assert V1._field_metadata["ok"] == {
        "type": "string",
        "description": "Should be integer when hints resolve",
        "required": True,
    }
    assert V1._field_metadata["ref"] == {
        "type": "string",
        "description": "Missing forward ref",
        "required": True,
    }


# ── Hygiene ──────────────────────────────────────────────────


def test_no_annotations_is_fine():
    """A subclass without typed annotations doesn't error during init."""
    class V1(SettingSchema):
        set_id = "x.y"
        schema_revision = 1

    assert V1._field_metadata == {}


def test_set_id_and_schema_revision_annotations_are_ignored():
    """Subclasses that re-annotate set_id / schema_revision (e.g. for
    type checkers) don't accidentally land in _field_metadata."""
    class V1(SettingSchema):
        set_id: str = "x.y"
        schema_revision: int = 1
        name: str = field(required=True)

    assert "set_id" not in V1._field_metadata
    assert "schema_revision" not in V1._field_metadata
    assert "name" in V1._field_metadata


def test_private_annotations_are_ignored():
    """Subclasses using underscore-prefixed annotations for internal
    state don't get those annotations turned into field metadata."""
    class V1(SettingSchema):
        set_id = "x.y"
        schema_revision = 1
        _internal: dict = {}
        name: str = field(required=True)

    assert "_internal" not in V1._field_metadata
    assert "name" in V1._field_metadata


def test_unannotated_field_without_spec_is_ignored():
    """Plain class attributes without a field() spec don't pollute
    metadata even when typed-annotated."""
    class V1(SettingSchema):
        set_id = "x.y"
        schema_revision = 1
        unrelated: str = "just-a-class-attr"
        name: str = field(required=True)

    assert "unrelated" not in V1._field_metadata
    assert "name" in V1._field_metadata
    assert V1.unrelated == "just-a-class-attr"
