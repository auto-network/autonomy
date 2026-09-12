"""Tests for the ``@indexed_payload`` schema decorator and its helpers.

Covers the Tier 3 declaration surface (graph://7d588dfa-429 §6, current-code
mapping graph://0ee72ca9-99c@4):

* ``@indexed_payload(*fields)`` rejects an empty call, requires the identifier
  field shape, and requires each field to exist in the merged field metadata;
* declarations append across inheritance and stacked decorators and deduplicate
  in first-declaration order, stored as an immutable tuple;
* ``export_json_schema`` surfaces the declaration as ``indexed_payload``;
* the one canonical JSON-path helper both the WHERE clause and index DDL use
  produces the exact ``json_extract(payload, '$.<field>')`` text and refuses a
  field name it cannot represent safely;
* cross-revision declarations collapse to one ``(set_id, field)`` identity, and
  the deterministic index name is the specified SHA-256 digest.
"""

from __future__ import annotations

import hashlib

import pytest

from tools.graph.schemas.registry import (
    SCHEMAS,
    UPCONVERTERS,
    SchemaValidationError,
    SettingSchema,
    field,
    indexed_payload,
    indexed_payload_declarations,
    payload_json_extract_sql,
    _payload_index_name,
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


# ── Canonical JSON-path helper ───────────────────────────────


def test_payload_json_extract_sql_canonical_text():
    # The exact text the ``where_payload`` clause builder emits, so SQLite
    # treats the index expression and the predicate as textually identical.
    assert payload_json_extract_sql("run_id") == "json_extract(payload, '$.run_id')"


@pytest.mark.parametrize("bad", ["a.b", "a-b", "a b", "", "1a", "a'b", "a[0]", "$.a"])
def test_payload_json_extract_sql_rejects_unsafe_field(bad):
    with pytest.raises(SchemaValidationError):
        payload_json_extract_sql(bad)


# ── Decorator validation ─────────────────────────────────────


def test_indexed_payload_rejects_empty_call():
    with pytest.raises(SchemaValidationError):
        indexed_payload()


def test_indexed_payload_rejects_unsafe_field_shape():
    # The shape check runs before the class is inspected.
    with pytest.raises(SchemaValidationError):
        indexed_payload("bad.field")


def test_indexed_payload_rejects_undeclared_field():
    with pytest.raises(SchemaValidationError):
        @indexed_payload("ghost")
        class V1(SettingSchema):
            set_id = "x.y"
            schema_revision = 1
            real: str = field(description="declared")


def test_indexed_payload_stores_immutable_tuple():
    @indexed_payload("run_id")
    class V1(SettingSchema):
        set_id = "x.y"
        schema_revision = 1
        run_id: str = field(description="id")

    assert V1._indexed_payload_fields == ("run_id",)
    assert isinstance(V1._indexed_payload_fields, tuple)


def test_indexed_payload_accepts_legacy_dict_metadata():
    @indexed_payload("a")
    class V1(SettingSchema):
        set_id = "x.y"
        schema_revision = 1
        _field_metadata = {"a": {"type": "string"}, "b": {"type": "string"}}

    assert V1._indexed_payload_fields == ("a",)


# ── Inheritance and stacking ─────────────────────────────────


def test_indexed_payload_inherited_by_subclass():
    @indexed_payload("a")
    class Base(SettingSchema):
        set_id = "x.base"
        schema_revision = 1
        a: str = field(description="a")
        b: str = field(description="b")

    @indexed_payload("b")
    class Child(Base):
        set_id = "x.child"
        schema_revision = 1

    # Inherited "a" first, then the subclass's own "b".
    assert Child._indexed_payload_fields == ("a", "b")


def test_indexed_payload_stacked_decorators_append():
    @indexed_payload("a")
    @indexed_payload("b")
    class V1(SettingSchema):
        set_id = "x.y"
        schema_revision = 1
        a: str = field(description="a")
        b: str = field(description="b")

    # Decorators apply bottom-up: "b" is declared first.
    assert V1._indexed_payload_fields == ("b", "a")


def test_indexed_payload_deduplicates_first_declaration_order():
    @indexed_payload("a")
    class Base(SettingSchema):
        set_id = "x.base"
        schema_revision = 1
        a: str = field(description="a")
        b: str = field(description="b")

    # Subclass re-declares the inherited "a" plus a new "b": the inherited
    # "a" wins its position and the duplicate is dropped.
    @indexed_payload("a", "b")
    class Child(Base):
        set_id = "x.child"
        schema_revision = 1

    assert Child._indexed_payload_fields == ("a", "b")


# ── Export ───────────────────────────────────────────────────


def test_export_json_schema_surfaces_indexed_payload():
    @indexed_payload("run_id")
    class V1(SettingSchema):
        set_id = "x.y"
        schema_revision = 1
        run_id: str = field(description="id")

    exported = V1.export_json_schema()
    assert exported["indexed_payload"] == ["run_id"]


def test_export_json_schema_omits_indexed_payload_when_undeclared():
    class V1(SettingSchema):
        set_id = "x.y"
        schema_revision = 1
        run_id: str = field(description="id")

    assert "indexed_payload" not in V1.export_json_schema()


# ── Declaration collapse across revisions ────────────────────


def test_declarations_collapse_across_revisions():
    @indexed_payload("run_id")
    class V1(SettingSchema):
        set_id = "x.obs"
        schema_revision = 1
        run_id: str = field(description="id")

    @indexed_payload("run_id")
    class V2(SettingSchema):
        set_id = "x.obs"
        schema_revision = 2
        run_id: str = field(description="id")

    decls = indexed_payload_declarations()
    assert decls.count(("x.obs", "run_id")) == 1


def test_declarations_sorted_deterministically():
    @indexed_payload("b", "a")
    class V1(SettingSchema):
        set_id = "x.two"
        schema_revision = 1
        a: str = field(description="a")
        b: str = field(description="b")

    decls = [d for d in indexed_payload_declarations() if d[0] == "x.two"]
    assert decls == [("x.two", "a"), ("x.two", "b")]


# ── Index name digest ────────────────────────────────────────


def test_payload_index_name_matches_sha256_contract():
    set_id = "dashboard.testing.test-observation"
    field_name = "run_id"
    expected_digest = hashlib.sha256(
        f"{set_id}:{field_name}".encode("utf-8")
    ).hexdigest()[:16]
    name = _payload_index_name(set_id, field_name)
    assert name == f"idx_settings_payload_{expected_digest}"
    # 16 lowercase hex characters.
    digest = name.rsplit("_", 1)[-1]
    assert len(digest) == 16
    assert digest == digest.lower()
    assert all(c in "0123456789abcdef" for c in digest)
