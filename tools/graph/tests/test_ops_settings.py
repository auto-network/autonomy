"""Unit tests for the Settings ops layer.

Covers add/read/override/exclude/promote/deprecate/remove/migrate; covers
JSON-merge-patch override semantics; covers exclusion drop; covers
precedence ordering. Spec: graph://0d3f750f-f9c.
"""

from __future__ import annotations

import json

import pytest

from tools.graph import ops, schemas, settings_ops
from tools.graph.db import GraphDB
from tools.graph.schemas.registry import (
    SCHEMAS,
    UPCONVERTERS,
    SchemaValidationError,
    field,
)
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


def test_upsert_by_key_can_preserve_imported_source_timestamps(
    graph_db_env, example_schema,
):
    ops.upsert_by_key(
        "autonomy.test.example", 1, "imported", {"x": 1, "name": "armor"},
        org=ops.CALLER_ORG,
        _source_created_at="2026-08-20T01:02:03Z",
        _source_updated_at="2026-08-24T04:05:06Z",
    )
    member = ops.read_set(
        "autonomy.test.example", org=ops.CALLER_ORG,
    ).members[0]
    assert member.created_at == "2026-08-20T01:02:03Z"
    assert member.updated_at == "2026-08-24T04:05:06Z"


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


# test_precedence_tiebreak_by_recency — RETIRED (settings-owner ruling,
# 2026-08-13): it built two same-publication_state base rows to assert
# the recency tiebreak, a state idx_settings_one_base (f5c20ebd) now
# forbids at insert and, on a pre-index DB, at open (bare CREATE UNIQUE
# INDEX, no dedupe migration) — so the tiebreak is unreachable in any
# openable DB. Cross-state precedence (canonical>published>curated>raw)
# stays covered by the tests above.


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


def test_undeprecate_clears_flag_and_successor(graph_db_env, example_schema):
    a = ops.add_setting("autonomy.test.example", 1, "k1", {"v": 1}, org=ops.CALLER_ORG)
    b = ops.add_setting("autonomy.test.example", 1, "k2", {"v": 2}, org=ops.CALLER_ORG)
    ops.deprecate_setting(a, successor_id=b, org=ops.CALLER_ORG)
    ops.undeprecate_setting(a, org=ops.CALLER_ORG)
    got = ops.get_setting(a, org=ops.CALLER_ORG)
    assert got.deprecated is False
    assert got.successor_id is None


def test_undeprecate_missing_setting_raises(graph_db_env):
    with pytest.raises(LookupError):
        ops.undeprecate_setting("nope", org=ops.CALLER_ORG)


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


def test_org_param_routes_to_the_orgs_own_db(tmp_path, monkeypatch, example_schema):
    """The org parameter ROUTES — it stopped being a no-op when per-org
    resolution landed (23d9m): an explicit-org write lands in that org's
    own DB in the orgs tree, and a read with the same org finds it there.
    (The old test asserted the parameters were accepted-but-ignored under
    a GRAPH_DB pin — the pin-collapse tautology this sweep retires.)"""
    from tools.graph.db import GraphDB

    orgs = tmp_path / "orgs"
    orgs.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)
    GraphDB.close_all_pooled()
    GraphDB.create_org_db("autonomy").close()

    sid = ops.add_setting(
        "autonomy.test.example", 1, "k", {"v": 1}, org="autonomy",
    )
    got = ops.get_setting(sid, org="autonomy", peers=["anchore"])
    assert got is not None
    members = ops.read_set("autonomy.test.example", org="autonomy", peers=[])
    assert len(members.members) == 1
    assert (orgs / "autonomy.db").exists()
    GraphDB.close_all_pooled()

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


# ── exact-key narrowing (read_set key_equals) — auto-x0xsu ──────────
#
# key_equals is the private substrate selector that lets a single-key read
# seek idx_settings_set (set_id, key) instead of materializing the whole set.
# It must change ONLY candidate cardinality: for the selected key it returns
# the byte-identical resolved member the full read-then-filter would, because
# every base/override/exclusion layer for that key shares the key and so still
# reaches the unchanged resolver. Spec: graph://7d588dfa-429 §4.


def _ser(members):
    """Serialize a member list the way the wire and read_set_key would see
    it — stable across the full-read-then-filter and the narrowed read."""
    return json.dumps(
        [m.to_dict() for m in members], sort_keys=True, default=str,
    )


def _full_then_filter(set_id, key, *, org, **kw):
    """The reference: resolve the WHOLE set, then keep the one key."""
    full = ops.read_set(set_id, org=org, **kw)
    return [m for m in full.members if m.key == key]


class _RecordingConn:
    """Delegating proxy that records the RAW (pre-binding) SQL and params of
    every ``execute``, so a test can assert what a query builder emitted.

    ``set_trace_callback`` expands bound parameters into the statement text,
    which would erase the ``?`` placeholders this test exists to see — so the
    capture has to sit in front of ``execute`` instead.
    """

    def __init__(self, real, sink):
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_sink", sink)

    def execute(self, sql, params=()):
        self._sink.append((sql, tuple(params)))
        return self._real.execute(sql, params)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_real"), name)


def test_key_equals_matches_full_read_local_base(graph_db_env, example_schema):
    ops.add_setting(
        "autonomy.test.example", 1, "foo", {"x": 1, "name": "bar"},
        org=ops.CALLER_ORG)
    ops.add_setting(
        "autonomy.test.example", 1, "other", {"x": 2}, org=ops.CALLER_ORG)

    narrowed = ops.read_set(
        "autonomy.test.example", org=ops.CALLER_ORG, key_equals="foo")
    assert len(narrowed.members) == 1
    assert narrowed.members[0].key == "foo"
    assert _ser(narrowed.members) == _ser(
        _full_then_filter("autonomy.test.example", "foo", org=ops.CALLER_ORG))


def test_key_equals_matches_full_read_matching_override(
    graph_db_env, example_schema,
):
    base = ops.add_setting(
        "autonomy.test.example", 1, "foo",
        {"name": "Alice", "color": "blue", "limit": 10}, org=ops.CALLER_ORG)
    ops.override_setting(base, {"color": "red"}, org=ops.CALLER_ORG)
    ops.add_setting(
        "autonomy.test.example", 1, "other", {"name": "Z"}, org=ops.CALLER_ORG)

    narrowed = ops.read_set(
        "autonomy.test.example", org=ops.CALLER_ORG, key_equals="foo")
    assert narrowed.members[0].payload == {
        "name": "Alice", "color": "red", "limit": 10}
    assert _ser(narrowed.members) == _ser(
        _full_then_filter("autonomy.test.example", "foo", org=ops.CALLER_ORG))


def test_key_equals_matches_full_read_exclusion(graph_db_env, example_schema):
    canonical = ops.add_setting(
        "autonomy.test.example", 1, "foo", {"name": "X"},
        state="canonical", org=ops.CALLER_ORG)
    ops.add_setting(
        "autonomy.test.example", 1, "bar", {"name": "Y"},
        state="canonical", org=ops.CALLER_ORG)
    ops.exclude_setting(canonical, org=ops.CALLER_ORG)

    narrowed = ops.read_set(
        "autonomy.test.example", org=ops.CALLER_ORG, key_equals="foo")
    # The excluded key resolves to nothing — same as the filtered full read.
    assert narrowed.members == []
    assert _ser(narrowed.members) == _ser(
        _full_then_filter("autonomy.test.example", "foo", org=ops.CALLER_ORG))


def test_key_equals_matches_full_read_target_revision(graph_db_env):
    class V1(schemas.SettingSchema):
        set_id = "autonomy.test.rev"
        schema_revision = 1

    class V2(schemas.SettingSchema):
        set_id = "autonomy.test.rev"
        schema_revision = 2

    schemas.register_schema("autonomy.test.rev", 1, V1)
    schemas.register_schema(
        "autonomy.test.rev", 2, V2,
        upconvert_from_prev=lambda p: {**p, "v2": True})
    ops.add_setting("autonomy.test.rev", 1, "foo", {"x": 1}, org=ops.CALLER_ORG)
    ops.add_setting("autonomy.test.rev", 1, "other", {"x": 9}, org=ops.CALLER_ORG)

    narrowed = ops.read_set(
        "autonomy.test.rev", org=ops.CALLER_ORG,
        target_revision=2, key_equals="foo")
    assert narrowed.members[0].payload == {"x": 1, "v2": True}
    assert narrowed.members[0].target_revision == 2
    assert _ser(narrowed.members) == _ser(_full_then_filter(
        "autonomy.test.rev", "foo", org=ops.CALLER_ORG, target_revision=2))


def test_key_equals_matches_full_read_min_revision(graph_db_env):
    class V1(schemas.SettingSchema):
        set_id = "autonomy.test.rev"
        schema_revision = 1

    class V2(schemas.SettingSchema):
        set_id = "autonomy.test.rev"
        schema_revision = 2

    schemas.register_schema("autonomy.test.rev", 1, V1)
    schemas.register_schema("autonomy.test.rev", 2, V2)
    ops.add_setting("autonomy.test.rev", 1, "foo", {"x": 1}, org=ops.CALLER_ORG)
    ops.add_setting("autonomy.test.rev", 2, "foo2", {"x": 2}, org=ops.CALLER_ORG)

    # A key dropped by the floor resolves to nothing under both paths.
    narrowed = ops.read_set(
        "autonomy.test.rev", org=ops.CALLER_ORG,
        min_revision=2, key_equals="foo")
    assert narrowed.members == []
    assert _ser(narrowed.members) == _ser(_full_then_filter(
        "autonomy.test.rev", "foo", org=ops.CALLER_ORG, min_revision=2))


def test_key_equals_matches_full_read_declared_defaults(graph_db_env):
    class DefV1(schemas.SettingSchema):
        set_id = "autonomy.test.defaults"
        schema_revision = 1
        color: str = field(default="blue")

    schemas.register_schema("autonomy.test.defaults", 1, DefV1)
    ops.add_setting("autonomy.test.defaults", 1, "foo", {}, org=ops.CALLER_ORG)

    narrowed = ops.read_set(
        "autonomy.test.defaults", org=ops.CALLER_ORG, key_equals="foo")
    # The declared default is filled by the resolver, not the selector.
    assert narrowed.members[0].payload == {"color": "blue"}
    assert _ser(narrowed.members) == _ser(
        _full_then_filter("autonomy.test.defaults", "foo", org=ops.CALLER_ORG))


def test_key_equals_matches_full_read_vault_refusal(graph_db_env, tmp_path):
    """A vault member that does not open resolves to a NAMED refusal, not to
    absence — and the narrowed read must carry it exactly as the full read
    does (payload None + vault_error), because step six runs unchanged."""
    from tools.graph.tests.vault_read_harness import VaultWorld, clear_seams

    vset = "autonomy.test.exactkey-vault"

    @schemas.vaulted("audited")
    class VaultedV1(schemas.SettingSchema):
        set_id = vset
        schema_revision = 1

    schemas.register_schema(vset, 1, VaultedV1)
    clear_seams()
    world = VaultWorld(tmp_path / "vault").register()
    try:
        ops.add_setting(
            vset, 1, "default", {"access_token": "sk-must-not-leak"},
            org=ops.CALLER_ORG)
        ops.add_setting(
            vset, 1, "other", {"access_token": "sk-other"}, org=ops.CALLER_ORG)
        settings_ops.set_vault_key_holder(None)

        narrowed = ops.read_set(vset, org=None, key_equals="default")
        assert len(narrowed.members) == 1
        m = narrowed.members[0]
        assert m.payload is None
        assert m.vault_error.reason == settings_ops.VAULT_NO_KEY_HOLDER
        assert "sk-must-not-leak" not in json.dumps(m.to_dict())
        assert _ser(narrowed.members) == _ser(
            _full_then_filter(vset, "default", org=None))
    finally:
        world.close()
        clear_seams()


def test_key_equals_no_selector_reproduces_full_query(graph_db_env, example_schema):
    """No selector => the byte-identical prior result and shape."""
    ops.add_setting("autonomy.test.example", 1, "a", {"x": 1}, org=ops.CALLER_ORG)
    ops.add_setting("autonomy.test.example", 1, "b", {"x": 2}, org=ops.CALLER_ORG)
    with_none = ops.read_set(
        "autonomy.test.example", org=ops.CALLER_ORG, key_equals=None)
    plain = ops.read_set("autonomy.test.example", org=ops.CALLER_ORG)
    assert _ser(with_none.members) == _ser(plain.members)
    assert with_none.dropped.to_dict() == plain.dropped.to_dict()


def test_read_set_key_matches_full_read_then_filter(graph_db_env, example_schema):
    """read_set_key (now narrowed) returns the byte-identical ROW contract a
    full read-then-filter would build: base identity + resolved payload."""
    base = ops.add_setting(
        "autonomy.test.example", 1, "foo",
        {"name": "Alice", "color": "blue"}, org=ops.CALLER_ORG)
    ops.override_setting(base, {"color": "red"}, org=ops.CALLER_ORG)
    ops.add_setting(
        "autonomy.test.example", 1, "other", {"name": "Z"}, org=ops.CALLER_ORG)

    row = settings_ops.read_set_key(
        "autonomy.test.example", "foo", org=ops.CALLER_ORG)
    assert row is not None
    assert row["id"] == base  # base identity, not the override id
    assert row["payload"] == {"name": "Alice", "color": "red"}  # resolved

    # Reference: resolve the full set, filter, fetch the base row, splice the
    # resolved payload — exactly what read_set_key did before it narrowed.
    # _fetch_setting_any_org takes a RESOLVED org, not the CALLER_ORG sentinel,
    # which read_set_key resolves via _resolve_org_arg before calling it.
    resolved_org = settings_ops._resolve_org_arg(ops.CALLER_ORG)
    m = next(
        mm for mm in ops.read_set("autonomy.test.example", org=ops.CALLER_ORG).members
        if mm.key == "foo")
    ref = settings_ops._fetch_setting_any_org(
        m.id, resolved_org, "autonomy.test.example")
    ref["payload"] = m.payload
    assert json.dumps(row, sort_keys=True) == json.dumps(ref, sort_keys=True)


def test_read_set_key_missing_returns_none(graph_db_env, example_schema):
    ops.add_setting("autonomy.test.example", 1, "foo", {"x": 1}, org=ops.CALLER_ORG)
    assert settings_ops.read_set_key(
        "autonomy.test.example", "nope", org=ops.CALLER_ORG) is None


def test_key_equals_narrows_both_builders_and_uses_index(
    tmp_path, monkeypatch, example_schema,
):
    """Both the owned and the peer SELECT must carry the parameterized
    ``AND key = ?``, and the plan must be an index SEARCH on idx_settings_set
    — a full scan on either builder is the regression this guards."""
    import sqlite3

    from tools.graph import cross_org

    orgs = tmp_path / "orgs"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    GraphDB.close_all_pooled()
    for slug in ("autonomy", "anchore", "personal"):
        GraphDB.create_org_db(slug).close()

    # Peer (autonomy) canonical beats owned (anchore) raw under the same key.
    ops.add_setting(
        "autonomy.test.example", 1, "foo", {"k": "peer"},
        org="autonomy", state="canonical")
    ops.add_setting(
        "autonomy.test.example", 1, "foo", {"k": "own"},
        org="anchore", state="raw")

    owned_sql: list = []
    peer_sql: list = []
    real_open_read = settings_ops._open_read
    real_open_peer = cross_org.open_peer_db

    def spy_open_read(*a, **k):
        db = real_open_read(*a, **k)
        db.conn = _RecordingConn(db.conn, owned_sql)
        return db

    def spy_open_peer(*a, **k):
        db = real_open_peer(*a, **k)
        if db is not None:
            db.conn = _RecordingConn(db.conn, peer_sql)
        return db

    monkeypatch.setattr(settings_ops, "_open_read", spy_open_read)
    monkeypatch.setattr(cross_org, "open_peer_db", spy_open_peer)

    result = ops.read_set(
        "autonomy.test.example", org="anchore", key_equals="foo")
    assert len(result.members) == 1
    assert result.members[0].payload == {"k": "peer"}

    def _selects(sink):
        return [
            (s, p) for (s, p) in sink
            if "FROM settings" in s and "rowid AS _rowid" in s
        ]

    owned_sel = _selects(owned_sql)
    peer_sel = _selects(peer_sql)
    assert owned_sel, "owned builder never ran"
    assert peer_sel, "peer builder never ran"
    assert all("AND key = ?" in s for (s, _) in owned_sel)
    assert all("AND key = ?" in s for (s, _) in peer_sel)
    # The key is a bound parameter, never interpolated into the SQL text.
    assert all("foo" in p for (_, p) in owned_sel)
    assert all("foo" in p for (_, p) in peer_sel)

    sql, params = owned_sel[0]
    conn = sqlite3.connect(orgs / "anchore.db")
    try:
        plan = "\n".join(
            r[-1] for r in conn.execute("EXPLAIN QUERY PLAN " + sql, params))
    finally:
        conn.close()
    assert "SEARCH settings USING INDEX idx_settings_set" in plan
    GraphDB.close_all_pooled()
