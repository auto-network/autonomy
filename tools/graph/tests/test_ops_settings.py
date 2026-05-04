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
     org=ops.CALLER_ORG)
    members = ops.read_set("autonomy.test.example", org=ops.CALLER_ORG)
    assert len(members.members) == 1
    m = members.members[0]
    assert m.id == sid
    assert m.key == "foo"
    assert m.payload == {"x": 1, "name": "bar"}
    assert m.state == "raw"


def test_upsert_by_key_updates_existing_row_in_place(graph_db_env, example_schema):
    sid = ops.upsert_by_key(
        "autonomy.test.example", 1, "foo", {"x": 1, "name": "bar"},
     org=ops.CALLER_ORG)
    sid_again = ops.upsert_by_key(
        "autonomy.test.example", 1, "foo", {"x": 2, "name": "baz"},
     org=ops.CALLER_ORG)
    members = ops.read_set("autonomy.test.example", org=ops.CALLER_ORG)
    assert sid_again == sid
    assert len(members.members) == 1
    assert members.members[0].id == sid
    assert members.members[0].payload == {"x": 2, "name": "baz"}


def test_add_unknown_schema_raises(graph_db_env):
    with pytest.raises(SchemaValidationError):
        ops.add_setting("unregistered.set", 1, "k", {}, org=ops.CALLER_ORG)


def test_add_strict_schema_rejects_invalid(graph_db_env, strict_schema):
    with pytest.raises(SchemaValidationError):
        ops.add_setting("autonomy.test.strict", 1, "k", {"x": 1}, org=ops.CALLER_ORG)


def test_add_strict_schema_accepts_valid(graph_db_env, strict_schema):
    sid = ops.add_setting("autonomy.test.strict", 1, "k", {"name": "ok"}, org=ops.CALLER_ORG)
    assert sid


def test_get_setting_returns_resolved(graph_db_env, example_schema):
    sid = ops.add_setting("autonomy.test.example", 1, "foo", {"a": 1}, org=ops.CALLER_ORG)
    got = ops.get_setting(sid, org=ops.CALLER_ORG)
    assert got is not None
    assert got.id == sid
    assert got.payload == {"a": 1}


def test_get_setting_missing_returns_none(graph_db_env):
    assert ops.get_setting("does-not-exist", org=ops.CALLER_ORG) is None


def test_list_set_ids(graph_db_env, example_schema, strict_schema):
    ops.add_setting("autonomy.test.example", 1, "k1", {"x": 1}, org=ops.CALLER_ORG)
    ops.add_setting("autonomy.test.strict", 1, "k2", {"name": "y"}, org=ops.CALLER_ORG)
    ids = ops.list_set_ids(org=ops.CALLER_ORG)
    assert "autonomy.test.example" in ids
    assert "autonomy.test.strict" in ids


# ── override ────────────────────────────────────────────────


def test_override_merges_per_field(graph_db_env, example_schema):
    base = ops.add_setting(
        "autonomy.test.example", 1, "foo",
        {"name": "Alice", "color": "blue", "limit": 10},
     org=ops.CALLER_ORG)
    ops.override_setting(base, {"color": "red"}, org=ops.CALLER_ORG)
    members = ops.read_set("autonomy.test.example", org=ops.CALLER_ORG)
    assert len(members.members) == 1
    m = members.members[0]
    # Per-field merge: name and limit preserved; color overridden
    assert m.payload == {"name": "Alice", "color": "red", "limit": 10}


def test_override_chain_applies_all(graph_db_env, example_schema):
    base = ops.add_setting(
        "autonomy.test.example", 1, "foo",
        {"a": 1, "b": 2, "c": 3},
     org=ops.CALLER_ORG)
    ops.override_setting(base, {"a": 10}, org=ops.CALLER_ORG)
    ops.override_setting(base, {"b": 20}, org=ops.CALLER_ORG)
    members = ops.read_set("autonomy.test.example", org=ops.CALLER_ORG)
    m = members.members[0]
    assert m.payload["a"] == 10
    assert m.payload["b"] == 20
    assert m.payload["c"] == 3


def test_override_missing_target_raises(graph_db_env, example_schema):
    with pytest.raises(LookupError):
        ops.override_setting("nope", {"x": 1}, org=ops.CALLER_ORG)


# ── exclude ─────────────────────────────────────────────────


def test_exclude_drops_target(graph_db_env, example_schema):
    canonical = ops.add_setting(
        "autonomy.test.example", 1, "foo", {"name": "X"},
        state="canonical",
     org=ops.CALLER_ORG)
    ops.add_setting(
        "autonomy.test.example", 1, "bar", {"name": "Y"},
        state="canonical",
     org=ops.CALLER_ORG)
    ops.exclude_setting(canonical, org=ops.CALLER_ORG)
    members = ops.read_set("autonomy.test.example", org=ops.CALLER_ORG)
    keys = {m.key for m in members.members}
    assert "bar" in keys
    assert "foo" not in keys


def test_exclude_missing_target_raises(graph_db_env):
    with pytest.raises(LookupError):
        ops.exclude_setting("nope", org=ops.CALLER_ORG)


# ── precedence ──────────────────────────────────────────────


def test_precedence_canonical_beats_raw(graph_db_env, example_schema):
    raw_sid = ops.add_setting(
        "autonomy.test.example", 1, "k", {"name": "raw"}, state="raw",
     org=ops.CALLER_ORG)
    can_sid = ops.add_setting(
        "autonomy.test.example", 1, "k", {"name": "canonical"}, state="canonical",
     org=ops.CALLER_ORG)
    members = ops.read_set("autonomy.test.example", org=ops.CALLER_ORG)
    # Two bases with same key; canonical wins.
    assert len(members.members) == 1
    assert members.members[0].id == can_sid
    assert members.members[0].payload["name"] == "canonical"


def test_precedence_published_beats_curated(graph_db_env, example_schema):
    ops.add_setting(
        "autonomy.test.example", 1, "k", {"v": "curated"}, state="curated",
     org=ops.CALLER_ORG)
    pub_sid = ops.add_setting(
        "autonomy.test.example", 1, "k", {"v": "published"}, state="published",
     org=ops.CALLER_ORG)
    members = ops.read_set("autonomy.test.example", org=ops.CALLER_ORG)
    assert members.members[0].id == pub_sid


def test_precedence_tiebreak_by_recency(graph_db_env, example_schema):
    """Two rows at same precedence: most recent wins."""
    import time
    first = ops.add_setting(
        "autonomy.test.example", 1, "k", {"v": "first"}, state="raw",
     org=ops.CALLER_ORG)
    time.sleep(1.1)  # ISO seconds-resolution timestamps need a real gap
    second = ops.add_setting(
        "autonomy.test.example", 1, "k", {"v": "second"}, state="raw",
     org=ops.CALLER_ORG)
    members = ops.read_set("autonomy.test.example", org=ops.CALLER_ORG)
    assert members.members[0].id == second
    assert members.members[0].payload["v"] == "second"


# ── promote / deprecate / remove ────────────────────────────


def test_promote_changes_state(graph_db_env, example_schema):
    sid = ops.add_setting("autonomy.test.example", 1, "k", {"v": 1}, org=ops.CALLER_ORG)
    ops.promote_setting(sid, "canonical", org=ops.CALLER_ORG)
    got = ops.get_setting(sid, org=ops.CALLER_ORG)
    assert got.state == "canonical"


def test_promote_invalid_state_raises(graph_db_env, example_schema):
    sid = ops.add_setting("autonomy.test.example", 1, "k", {"v": 1}, org=ops.CALLER_ORG)
    with pytest.raises(ValueError):
        ops.promote_setting(sid, "garbage", org=ops.CALLER_ORG)


def test_promote_missing_setting_raises(graph_db_env):
    with pytest.raises(LookupError):
        ops.promote_setting("nope", "canonical", org=ops.CALLER_ORG)


def test_deprecate_marks_flag_and_successor(graph_db_env, example_schema):
    a = ops.add_setting("autonomy.test.example", 1, "k1", {"v": 1}, org=ops.CALLER_ORG)
    b = ops.add_setting("autonomy.test.example", 1, "k2", {"v": 2}, org=ops.CALLER_ORG)
    ops.deprecate_setting(a, successor_id=b, org=ops.CALLER_ORG)
    got = ops.get_setting(a, org=ops.CALLER_ORG)
    assert got.deprecated is True
    assert got.successor_id == b


def test_remove_only_works_on_raw(graph_db_env, example_schema):
    sid = ops.add_setting("autonomy.test.example", 1, "k", {"v": 1},
                          state="canonical", org=ops.CALLER_ORG)
    with pytest.raises(ValueError):
        ops.remove_setting(sid, org=ops.CALLER_ORG)


def test_remove_raw_succeeds(graph_db_env, example_schema):
    sid = ops.add_setting("autonomy.test.example", 1, "k", {"v": 1}, org=ops.CALLER_ORG)
    ops.remove_setting(sid, org=ops.CALLER_ORG)
    assert ops.get_setting(sid, org=ops.CALLER_ORG) is None


# ── org / peers plumbing ─────────────────────────────


def test_org_param_accepted(graph_db_env, example_schema):
    """org / peers parameters are no-op today but must be accepted."""
    sid = ops.add_setting(
        "autonomy.test.example", 1, "k", {"v": 1}, org="autonomy",
    )
    got = ops.get_setting(sid, org="autonomy", peers=["anchore"])
    assert got is not None
    members = ops.read_set(
        "autonomy.test.example", org="autonomy", peers=["anchore"],
    )
    assert len(members.members) == 1


# ── deprecated filter ───────────────────────────────────────


def test_read_set_skips_deprecated_rows(graph_db_env, example_schema):
    """A row with deprecated=1 must not surface from read_set, even if
    publication_state is canonical. Regression for the universal.send-to
    duplicate that surfaced in the dashboard agent-actions dropdown
    (Round 7g rename → 7i fix)."""
    ops.add_setting(
        "autonomy.test.example", 1, "active.key",
        {"name": "active"}, state="canonical",
     org=ops.CALLER_ORG)
    retired = ops.add_setting(
        "autonomy.test.example", 1, "retired.key",
        {"name": "retired"}, state="canonical",
     org=ops.CALLER_ORG)
    ops.deprecate_setting(retired, org=ops.CALLER_ORG)

    members = ops.read_set("autonomy.test.example", org=ops.CALLER_ORG)
    keys = {m.key for m in members.members}
    assert "active.key" in keys
    assert "retired.key" not in keys
    assert members.dropped.deprecated_filtered == 1


def test_read_set_deprecated_filter_counts_zero_when_none(
    graph_db_env, example_schema,
):
    """Without any deprecated rows the counter stays at 0 (no false pos)."""
    ops.add_setting(
        "autonomy.test.example", 1, "active.key",
        {"name": "active"}, state="canonical",
     org=ops.CALLER_ORG)
    members = ops.read_set("autonomy.test.example", org=ops.CALLER_ORG)
    assert members.dropped.deprecated_filtered == 0
