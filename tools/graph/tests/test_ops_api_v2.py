"""Acceptance tests for the ops API v2 refactor (auto-9knxj).

Covers:

* ``caller_org=`` → ``org=`` rename is live (no ``caller_org`` kwargs left
  in the ops surface).
* ``read_set(..., prefix=...)`` generates the SQL ``LIKE`` pattern with
  the ``:`` separator auto-appended.
* ``read_set(..., model=Foo)`` returns typed payloads and drops
  validation failures into ``dropped.schema_invalid`` with a WARN log.
* ``SetMembers.to_dict()`` returns a key-indexed mapping; iteration /
  ``len()`` still work.
* ``ResolvedSetting`` is generic on the payload type.
* ``get_setting(..., model=...)`` mirrors the same typing pipeline.
"""

from __future__ import annotations

import inspect
import logging

import pytest
from pydantic import BaseModel, Field

from tools.graph import ops, schemas
from tools.graph.schemas.registry import (
    SchemaValidationError,
    SettingSchema,
    SCHEMAS,
    UPCONVERTERS,
    register_schema,
)
from tools.graph.settings_ops import (
    DropAccounting,
    ResolvedSetting,
    SetMembers,
)


@pytest.fixture
def graph_db_env(tmp_path, monkeypatch):
    db = tmp_path / "graph.db"
    monkeypatch.setenv("GRAPH_DB", str(db))
    monkeypatch.delenv("GRAPH_API", raising=False)
    yield db


@pytest.fixture(autouse=True)
def _isolate_schema_registry():
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
def open_schema():
    class V1(SettingSchema):
        set_id = "autonomy.test.v2"
        schema_revision = 1
    register_schema("autonomy.test.v2", 1, V1)
    return V1


# ── caller_org → org rename ─────────────────────────────────


OPS_FUNCTIONS_TO_CHECK = [
    "read_set",
    "get_setting",
    "list_set_ids",
    "add_setting",
    "override_setting",
    "exclude_setting",
    "promote_setting",
    "deprecate_setting",
    "remove_setting",
    "migrate_setting_revisions",
]


@pytest.mark.parametrize("fn_name", OPS_FUNCTIONS_TO_CHECK)
def test_ops_no_longer_accepts_caller_org(fn_name):
    """Every Setting-facing ops function takes ``org=``, never ``caller_org=``."""
    fn = getattr(ops, fn_name)
    sig = inspect.signature(fn)
    assert "caller_org" not in sig.parameters, (
        f"ops.{fn_name} still has caller_org= parameter"
    )
    assert "org" in sig.parameters, f"ops.{fn_name} missing org= parameter"


# ── prefix= kwarg semantics ─────────────────────────────────


def test_read_set_prefix_filters_by_colon_boundary(graph_db_env, open_schema):
    ops.add_setting("autonomy.test.v2", 1, "enterprise-ng:alpha", {"x": 1})
    ops.add_setting("autonomy.test.v2", 1, "enterprise-ng:beta", {"x": 2})
    ops.add_setting("autonomy.test.v2", 1, "enterprise-v5:gamma", {"x": 3})

    got = ops.read_set("autonomy.test.v2", prefix="enterprise-ng")
    keys = {m.key for m in got.members}
    assert keys == {"enterprise-ng:alpha", "enterprise-ng:beta"}


def test_read_set_prefix_matches_only_colon_boundary(graph_db_env, open_schema):
    """``prefix="enterprise-ng"`` must not match ``enterprise-ng-alt:...``."""
    ops.add_setting("autonomy.test.v2", 1, "enterprise-ng:a", {"x": 1})
    ops.add_setting("autonomy.test.v2", 1, "enterprise-ng-alt:b", {"x": 2})

    got = ops.read_set("autonomy.test.v2", prefix="enterprise-ng")
    keys = {m.key for m in got.members}
    assert keys == {"enterprise-ng:a"}


def test_read_set_prefix_escapes_sql_wildcards(graph_db_env, open_schema):
    """A literal ``%`` in the prefix must not act as a wildcard."""
    ops.add_setting("autonomy.test.v2", 1, "100%:real", {"x": 1})
    ops.add_setting("autonomy.test.v2", 1, "100-other:fake", {"x": 2})

    got = ops.read_set("autonomy.test.v2", prefix="100%")
    keys = {m.key for m in got.members}
    assert keys == {"100%:real"}


def test_read_set_prefix_no_results(graph_db_env, open_schema):
    ops.add_setting("autonomy.test.v2", 1, "foo:bar", {"x": 1})
    got = ops.read_set("autonomy.test.v2", prefix="nothing")
    assert list(got.members) == []


# ── SetMembers shape ────────────────────────────────────────


def test_set_members_to_dict_maps_key_to_resolved(graph_db_env, open_schema):
    ops.add_setting("autonomy.test.v2", 1, "a", {"v": 1})
    ops.add_setting("autonomy.test.v2", 1, "b", {"v": 2})
    sm = ops.read_set("autonomy.test.v2")
    mapping = sm.to_dict()
    assert set(mapping.keys()) == {"a", "b"}
    for key, rs in mapping.items():
        assert isinstance(rs, ResolvedSetting)
        assert rs.key == key


def test_set_members_is_iterable_and_sized(graph_db_env, open_schema):
    ops.add_setting("autonomy.test.v2", 1, "a", {"v": 1})
    ops.add_setting("autonomy.test.v2", 1, "b", {"v": 2})
    sm = ops.read_set("autonomy.test.v2")
    assert len(sm) == 2
    assert {m.key for m in sm} == {"a", "b"}


def test_set_members_as_payload_is_json_serializable(graph_db_env, open_schema):
    """The dashboard API uses :meth:`as_payload` for ``JSONResponse``."""
    ops.add_setting("autonomy.test.v2", 1, "a", {"v": 1})
    sm = ops.read_set("autonomy.test.v2")
    payload = sm.as_payload()
    assert set(payload.keys()) == {"members", "dropped"}
    assert isinstance(payload["members"], list)
    assert isinstance(payload["dropped"], dict)


# ── model= kwarg + typed payloads ───────────────────────────


class _TestPayload(BaseModel):
    name: str
    value: int = Field(ge=0)


def test_read_set_model_returns_typed_payloads(graph_db_env, open_schema):
    ops.add_setting("autonomy.test.v2", 1, "a", {"name": "alpha", "value": 7})
    sm = ops.read_set("autonomy.test.v2", model=_TestPayload)
    assert len(sm.members) == 1
    m = sm.members[0]
    assert isinstance(m.payload, _TestPayload)
    assert m.payload.name == "alpha"
    assert m.payload.value == 7


def test_read_set_model_drops_invalid_rows(graph_db_env, open_schema, caplog):
    ops.add_setting("autonomy.test.v2", 1, "good", {"name": "ok", "value": 1})
    ops.add_setting("autonomy.test.v2", 1, "bad-missing", {"name": "nope"})
    ops.add_setting("autonomy.test.v2", 1, "bad-negative", {"name": "neg", "value": -5})

    with caplog.at_level(logging.WARNING, logger="tools.graph.settings_ops"):
        sm = ops.read_set("autonomy.test.v2", model=_TestPayload)

    surviving_keys = {m.key for m in sm.members}
    assert surviving_keys == {"good"}
    assert sm.dropped.schema_invalid == 2
    # At least one WARN was emitted per dropped row.
    assert caplog.text.count("payload validation failed") >= 2


def test_get_setting_model_returns_typed_payload(graph_db_env, open_schema):
    sid = ops.add_setting(
        "autonomy.test.v2", 1, "k", {"name": "alpha", "value": 1},
    )
    rs = ops.get_setting(sid, model=_TestPayload)
    assert rs is not None
    assert isinstance(rs.payload, _TestPayload)
    assert rs.payload.name == "alpha"


def test_get_setting_model_returns_none_on_invalid(graph_db_env, open_schema, caplog):
    sid = ops.add_setting("autonomy.test.v2", 1, "k", {"name": "x"})
    with caplog.at_level(logging.WARNING, logger="tools.graph.settings_ops"):
        assert ops.get_setting(sid, model=_TestPayload) is None
    assert "payload validation failed" in caplog.text


def test_read_set_without_model_keeps_payload_as_dict(graph_db_env, open_schema):
    """Callers that skip ``model=`` get ``dict`` payloads (back-compat)."""
    ops.add_setting("autonomy.test.v2", 1, "k", {"v": 1})
    sm = ops.read_set("autonomy.test.v2")
    assert isinstance(sm.members[0].payload, dict)


# ── DropAccounting back-compat ──────────────────────────────


def test_drop_accounting_exposes_new_field():
    da = DropAccounting()
    assert da.schema_invalid == 0
    da.schema_invalid = 3
    assert da.to_dict()["schema_invalid"] == 3


def test_drop_accounting_subscript_for_backcompat():
    """``members.dropped["below_min_revision"]`` still works."""
    da = DropAccounting(below_min_revision=2)
    assert da["below_min_revision"] == 2


def test_drop_accounting_values_for_backcompat():
    da = DropAccounting(no_upconvert_path=1)
    assert any(da.values())
