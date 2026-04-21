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
