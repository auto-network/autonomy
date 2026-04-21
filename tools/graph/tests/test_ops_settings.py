"""Unit tests for the Settings ops layer.

Covers add/read/override/exclude/promote/deprecate/remove/migrate; covers
JSON-merge-patch override semantics; covers exclusion drop; covers
precedence ordering. Spec: graph://0d3f750f-f9c.
"""

from __future__ import annotations

import json

import pytest

from tools.graph import ops, schemas
from tools.graph.schemas.registry import SCHEMAS, UPCONVERTERS, SchemaValidationError
from tools.graph.settings_ops import json_merge_patch


# ── Fixtures ─────────────────────────────────────────────────


@pytest.fixture
def graph_db_env(tmp_path, monkeypatch):
    """Pin GRAPH_DB to a fresh tmp file for the test's duration."""
    db_path = tmp_path / "graph.db"
    monkeypatch.setenv("GRAPH_DB", str(db_path))
    monkeypatch.delenv("GRAPH_API", raising=False)
    yield db_path


@pytest.fixture(autouse=True)
def _isolate_schema_registry():
    """Snapshot + restore the global schema registry around each test."""
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
def example_schema():
    """Register a permissive schema for autonomy.test.example#1."""

    class ExampleV1(schemas.SettingSchema):
        set_id = "autonomy.test.example"
        schema_revision = 1

    schemas.register_schema("autonomy.test.example", 1, ExampleV1)
    return ExampleV1


@pytest.fixture
def strict_schema():
    """Register a schema that requires a 'name' string field."""

    class StrictV1(schemas.SettingSchema):
        set_id = "autonomy.test.strict"
        schema_revision = 1

        @classmethod
        def validate(cls, payload):
            super().validate(payload)
            if "name" not in payload or not isinstance(payload["name"], str):
                raise SchemaValidationError("name (str) required")

    schemas.register_schema("autonomy.test.strict", 1, StrictV1)
    return StrictV1


# ── JSON merge-patch ────────────────────────────────────────


def test_merge_patch_replaces_scalar():
    assert json_merge_patch({"a": 1}, {"a": 2}) == {"a": 2}


def test_merge_patch_recurses_into_dicts():
    assert json_merge_patch({"a": {"b": 1, "c": 2}}, {"a": {"b": 9}}) == {"a": {"b": 9, "c": 2}}


def test_merge_patch_replaces_lists_wholesale():
    assert json_merge_patch({"tags": ["a", "b"]}, {"tags": ["c"]}) == {"tags": ["c"]}


def test_merge_patch_null_removes_key():
    assert json_merge_patch({"a": 1, "b": 2}, {"a": None}) == {"b": 2}


def test_merge_patch_adds_new_key():
    assert json_merge_patch({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}


# ── add / read / get ────────────────────────────────────────


def test_add_and_read_round_trip(graph_db_env, example_schema):
    sid = ops.add_setting(
        "autonomy.test.example", 1, "foo", {"x": 1, "name": "bar"},
    )
    members = ops.read_set("autonomy.test.example")
    assert len(members.members) == 1
    m = members.members[0]
    assert m.id == sid
    assert m.key == "foo"
    assert m.payload == {"x": 1, "name": "bar"}
    assert m.publication_state == "raw"


def test_add_unknown_schema_raises(graph_db_env):
    with pytest.raises(SchemaValidationError):
        ops.add_setting("unregistered.set", 1, "k", {})


def test_add_strict_schema_rejects_invalid(graph_db_env, strict_schema):
    with pytest.raises(SchemaValidationError):
        ops.add_setting("autonomy.test.strict", 1, "k", {"x": 1})


def test_add_strict_schema_accepts_valid(graph_db_env, strict_schema):
    sid = ops.add_setting("autonomy.test.strict", 1, "k", {"name": "ok"})
    assert sid


def test_get_setting_returns_resolved(graph_db_env, example_schema):
    sid = ops.add_setting("autonomy.test.example", 1, "foo", {"a": 1})
    got = ops.get_setting(sid)
    assert got is not None
    assert got.id == sid
    assert got.payload == {"a": 1}


def test_get_setting_missing_returns_none(graph_db_env):
    assert ops.get_setting("does-not-exist") is None


def test_list_set_ids(graph_db_env, example_schema, strict_schema):
    ops.add_setting("autonomy.test.example", 1, "k1", {"x": 1})
    ops.add_setting("autonomy.test.strict", 1, "k2", {"name": "y"})
    ids = ops.list_set_ids()
    assert "autonomy.test.example" in ids
    assert "autonomy.test.strict" in ids


# ── override ────────────────────────────────────────────────


def test_override_merges_per_field(graph_db_env, example_schema):
    base = ops.add_setting(
        "autonomy.test.example", 1, "foo",
        {"name": "Alice", "color": "blue", "limit": 10},
    )
    ops.override_setting(base, {"color": "red"})
    members = ops.read_set("autonomy.test.example")
    assert len(members.members) == 1
    m = members.members[0]
    # Per-field merge: name and limit preserved; color overridden
    assert m.payload == {"name": "Alice", "color": "red", "limit": 10}


def test_override_chain_applies_all(graph_db_env, example_schema):
    base = ops.add_setting(
        "autonomy.test.example", 1, "foo",
        {"a": 1, "b": 2, "c": 3},
    )
    ops.override_setting(base, {"a": 10})
    ops.override_setting(base, {"b": 20})
    members = ops.read_set("autonomy.test.example")
    m = members.members[0]
    assert m.payload["a"] == 10
    assert m.payload["b"] == 20
    assert m.payload["c"] == 3


def test_override_missing_target_raises(graph_db_env, example_schema):
    with pytest.raises(LookupError):
        ops.override_setting("nope", {"x": 1})


# ── exclude ─────────────────────────────────────────────────


def test_exclude_drops_target(graph_db_env, example_schema):
    canonical = ops.add_setting(
        "autonomy.test.example", 1, "foo", {"name": "X"},
        state="canonical",
    )
    ops.add_setting(
        "autonomy.test.example", 1, "bar", {"name": "Y"},
        state="canonical",
    )
    ops.exclude_setting(canonical)
    members = ops.read_set("autonomy.test.example")
    keys = {m.key for m in members.members}
    assert "bar" in keys
    assert "foo" not in keys


def test_exclude_missing_target_raises(graph_db_env):
    with pytest.raises(LookupError):
        ops.exclude_setting("nope")


# ── precedence ──────────────────────────────────────────────


def test_precedence_canonical_beats_raw(graph_db_env, example_schema):
    raw_sid = ops.add_setting(
        "autonomy.test.example", 1, "k", {"name": "raw"}, state="raw",
    )
    can_sid = ops.add_setting(
        "autonomy.test.example", 1, "k", {"name": "canonical"}, state="canonical",
    )
    members = ops.read_set("autonomy.test.example")
    # Two bases with same key; canonical wins.
    assert len(members.members) == 1
    assert members.members[0].id == can_sid
    assert members.members[0].payload["name"] == "canonical"


def test_precedence_published_beats_curated(graph_db_env, example_schema):
    ops.add_setting(
        "autonomy.test.example", 1, "k", {"v": "curated"}, state="curated",
    )
    pub_sid = ops.add_setting(
        "autonomy.test.example", 1, "k", {"v": "published"}, state="published",
    )
    members = ops.read_set("autonomy.test.example")
    assert members.members[0].id == pub_sid


def test_precedence_tiebreak_by_recency(graph_db_env, example_schema):
    """Two rows at same precedence: most recent wins."""
    import time
    first = ops.add_setting(
        "autonomy.test.example", 1, "k", {"v": "first"}, state="raw",
    )
    time.sleep(1.1)  # ISO seconds-resolution timestamps need a real gap
    second = ops.add_setting(
        "autonomy.test.example", 1, "k", {"v": "second"}, state="raw",
    )
    members = ops.read_set("autonomy.test.example")
    assert members.members[0].id == second
    assert members.members[0].payload["v"] == "second"


# ── promote / deprecate / remove ────────────────────────────


def test_promote_changes_state(graph_db_env, example_schema):
    sid = ops.add_setting("autonomy.test.example", 1, "k", {"v": 1})
    ops.promote_setting(sid, "canonical")
    got = ops.get_setting(sid)
    assert got.publication_state == "canonical"


def test_promote_invalid_state_raises(graph_db_env, example_schema):
    sid = ops.add_setting("autonomy.test.example", 1, "k", {"v": 1})
    with pytest.raises(ValueError):
        ops.promote_setting(sid, "garbage")


def test_promote_missing_setting_raises(graph_db_env):
    with pytest.raises(LookupError):
        ops.promote_setting("nope", "canonical")


def test_deprecate_marks_flag_and_successor(graph_db_env, example_schema):
    a = ops.add_setting("autonomy.test.example", 1, "k1", {"v": 1})
    b = ops.add_setting("autonomy.test.example", 1, "k2", {"v": 2})
    ops.deprecate_setting(a, successor_id=b)
    got = ops.get_setting(a)
    assert got.deprecated is True
    assert got.successor_id == b


def test_remove_only_works_on_raw(graph_db_env, example_schema):
    sid = ops.add_setting("autonomy.test.example", 1, "k", {"v": 1},
                          state="canonical")
    with pytest.raises(ValueError):
        ops.remove_setting(sid)


def test_remove_raw_succeeds(graph_db_env, example_schema):
    sid = ops.add_setting("autonomy.test.example", 1, "k", {"v": 1})
    ops.remove_setting(sid)
    assert ops.get_setting(sid) is None


# ── caller_org / peers plumbing ─────────────────────────────


def test_caller_org_param_accepted(graph_db_env, example_schema):
    """caller_org / peers parameters are no-op today but must be accepted."""
    sid = ops.add_setting(
        "autonomy.test.example", 1, "k", {"v": 1}, caller_org="autonomy",
    )
    got = ops.get_setting(sid, caller_org="autonomy", peers=["anchore"])
    assert got is not None
    members = ops.read_set(
        "autonomy.test.example", caller_org="autonomy", peers=["anchore"],
    )
    assert len(members.members) == 1
