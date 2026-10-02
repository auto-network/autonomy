"""@payload_union: one set, several typed payload shapes told apart by a
discriminator field (auto-raepo). Enforcement picks the shape from the
payload; export and typegen carry the union separately from ``variants``;
a malformed declaration fails at import; shapes never register as sets."""

from __future__ import annotations

from typing import Literal

import pytest

from tools.graph.schemas import registry
from tools.graph.schemas.registry import (
    SchemaValidationError,
    SettingSchema,
    declared_union,
    enforce_declared_fields,
    field,
    keyed_per_entity,
    payload_union,
    validate_payload,
)

SET_ID = "probe.payload-union"


class _Common(SettingSchema):
    account_id: str = field(required=True, description="the account")
    alias: str = field(required=False, description="a label")


class _Claude(_Common):
    harness: Literal["claude"] = field(required=True, description="which harness")
    email: str = field(required=False, description="sign-in email")
    scopes: list[str] = field(required=False, description="granted scopes")


class _Grok(_Common):
    harness: Literal["grok"] = field(required=True, description="which harness")


@pytest.fixture(scope="module")
def union_set():
    @payload_union(discriminator="harness", shapes=(_Claude, _Grok))
    @keyed_per_entity(key_strategy="harness:account_id")
    class ProbeUnion(SettingSchema):
        set_id = SET_ID
        schema_revision = 1

    yield ProbeUnion
    registry.unregister_schema(SET_ID, 1)


def test_each_shape_is_enforced_by_its_discriminator(union_set):
    validate_payload(SET_ID, 1, {"harness": "claude", "account_id": "a",
                                 "email": "x@y", "scopes": ["s"]})
    validate_payload(SET_ID, 1, {"harness": "grok", "account_id": "g", "alias": None})


def test_a_field_of_another_shape_is_undeclared(union_set):
    with pytest.raises(SchemaValidationError, match="undeclared field.*email"):
        validate_payload(SET_ID, 1, {"harness": "grok", "account_id": "g", "email": "x"})


@pytest.mark.parametrize("payload", [
    {"account_id": "a"},                          # missing discriminator
    {"harness": "codex", "account_id": "a"},      # unknown value
    {"harness": None, "account_id": "a"},
    {"harness": 1, "account_id": "a"},
])
def test_a_wrong_or_missing_discriminator_is_refused(union_set, payload):
    with pytest.raises(SchemaValidationError, match="'harness' must be one of"):
        validate_payload(SET_ID, 1, payload)


def test_a_missing_required_field_is_refused(union_set):
    with pytest.raises(SchemaValidationError, match="missing required field 'account_id'"):
        validate_payload(SET_ID, 1, {"harness": "claude"})


def test_types_are_enforced_inside_the_shape(union_set):
    with pytest.raises(SchemaValidationError, match="'scopes' must be array"):
        enforce_declared_fields(union_set, {"harness": "claude", "account_id": "a",
                                            "scopes": "s"})


def test_shapes_never_register_as_sets(union_set):
    registered = {cls for cls in registry.SCHEMAS.values()}
    assert _Claude not in registered and _Grok not in registered and _Common not in registered


def test_declared_union_names_the_shapes(union_set):
    assert declared_union(SET_ID) == ("harness", {"claude": _Claude, "grok": _Grok})
    assert declared_union("autonomy.vault.audited") is None


def test_export_carries_the_union_apart_from_variants(union_set):
    exported = union_set.export_json_schema()
    assert exported["variants"] == {}
    assert exported["properties"] == {}
    union = exported["union"]
    assert union["discriminator"] == "harness"
    assert set(union["shapes"]) == {"claude", "grok"}
    claude = union["shapes"]["claude"]
    assert claude["properties"]["harness"]["enum"] == ["claude"]
    assert set(claude["required"]) == {"account_id", "harness"}
    assert "email" not in union["shapes"]["grok"]["properties"]


def test_typegen_emits_a_discriminated_union(union_set):
    from tools.graph.typegen_cmd import _render_schema_block

    ts = _render_schema_block("ProbeUnion", union_set.export_json_schema())
    assert "export interface ProbeUnionClaude {" in ts
    assert "export interface ProbeUnionGrok {" in ts
    assert "harness: 'claude';" in ts and "harness: 'grok';" in ts
    assert "export type ProbeUnion =\n  | ProbeUnionClaude\n  | ProbeUnionGrok;" in ts


# ── a malformed union fails at import ──────────────────────


class _NoLiteral(SettingSchema):
    harness: str = field(required=True, description="not a Literal")


class _Undeclared(SettingSchema):
    other: str = field(required=True, description="no discriminator")


class _GrokAgain(SettingSchema):
    harness: Literal["grok"] = field(required=True, description="duplicate value")


class _WithSetId(SettingSchema):
    set_id = "probe.payload-union.shape-as-set"
    harness: Literal["x"] = field(required=True, description="a set, not a shape")


@pytest.mark.parametrize("shapes, message", [
    ((_NoLiteral,), "must declare 'harness' as Literal"),
    ((_Undeclared,), "does not declare the discriminator"),
    ((_Grok, _GrokAgain), "both claim harness='grok'"),
    ((_WithSetId,), "declares a set_id"),
    ((dict,), "is not a SettingSchema"),
])
def test_a_malformed_union_fails_at_import(shapes, message):
    with pytest.raises(TypeError, match=message):
        @payload_union(discriminator="harness", shapes=shapes)
        class Bad(SettingSchema):
            pass


def test_a_union_set_declares_no_fields_of_its_own():
    with pytest.raises(TypeError, match="declares no fields of its own"):
        @payload_union(discriminator="harness", shapes=(_Grok,))
        class Bad(SettingSchema):
            extra: str = field(required=True, description="belongs on a shape")
