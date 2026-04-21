"""API tests for the Settings primitive endpoints.

Exercises every route exposed by the dashboard server, plus the structured
400 from validation failures. Spec: graph://0d3f750f-f9c.
"""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from tools.graph import ops, schemas
from tools.graph.schemas.registry import SCHEMAS, UPCONVERTERS, SchemaValidationError


@pytest.fixture
def graph_db_env(tmp_path, monkeypatch):
    db_path = tmp_path / "graph.db"
    monkeypatch.setenv("GRAPH_DB", str(db_path))
    monkeypatch.delenv("GRAPH_API", raising=False)
    yield db_path


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
def example_schema():
    class V1(schemas.SettingSchema):
        set_id = "autonomy.test.api"
        schema_revision = 1
    schemas.register_schema("autonomy.test.api", 1, V1)
    return V1


@pytest.fixture
def strict_schema():
    class StrictV1(schemas.SettingSchema):
        set_id = "autonomy.test.strict"
        schema_revision = 1

        @classmethod
        def validate(cls, payload):
            super().validate(payload)
            if "name" not in payload:
                raise SchemaValidationError("name required")
    schemas.register_schema("autonomy.test.strict", 1, StrictV1)
    return StrictV1


@pytest.fixture
def client(test_app):
    with TestClient(test_app) as c:
        yield c


# ── Read endpoints ─────────────────────────────────────────


def test_get_settings_list_empty(graph_db_env, example_schema, client):
    r = client.get("/api/graph/settings/autonomy.test.api")
    assert r.status_code == 200
    body = r.json()
    assert body["members"] == []
    assert "dropped" in body


def test_get_settings_list_with_rows(graph_db_env, example_schema, client):
    sid = ops.add_setting("autonomy.test.api", 1, "k", {"x": 1})
    r = client.get("/api/graph/settings/autonomy.test.api")
    assert r.status_code == 200
    body = r.json()
    assert len(body["members"]) == 1
    assert body["members"][0]["id"] == sid
    assert body["members"][0]["payload"] == {"x": 1}


def test_get_setting_by_key(graph_db_env, example_schema, client):
    ops.add_setting("autonomy.test.api", 1, "alpha", {"x": 1})
    r = client.get("/api/graph/settings/autonomy.test.api/alpha")
    assert r.status_code == 200
    assert r.json()["key"] == "alpha"


def test_get_setting_by_key_404(graph_db_env, example_schema, client):
    r = client.get("/api/graph/settings/autonomy.test.api/missing")
    assert r.status_code == 404


def test_get_setting_by_id(graph_db_env, example_schema, client):
    sid = ops.add_setting("autonomy.test.api", 1, "k", {"x": 1})
    r = client.get(f"/api/graph/setting/{sid}")
    assert r.status_code == 200
    assert r.json()["id"] == sid


def test_get_setting_by_id_404(graph_db_env, example_schema, client):
    r = client.get("/api/graph/setting/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404


def test_get_set_ids(graph_db_env, example_schema, client):
    ops.add_setting("autonomy.test.api", 1, "k", {"x": 1})
    r = client.get("/api/graph/sets")
    assert r.status_code == 200
    assert "autonomy.test.api" in r.json()["set_ids"]


# ── Read flag plumbing ─────────────────────────────────────


def test_get_settings_target_revision(graph_db_env, client):
    class V1(schemas.SettingSchema):
        set_id = "autonomy.test.api"
        schema_revision = 1
    class V2(schemas.SettingSchema):
        set_id = "autonomy.test.api"
        schema_revision = 2
    schemas.register_schema("autonomy.test.api", 1, V1)
    schemas.register_schema("autonomy.test.api", 2, V2,
                            upconvert_from_prev=lambda p: {**p, "v2": True})

    ops.add_setting("autonomy.test.api", 1, "k", {"x": 1})
    r = client.get("/api/graph/settings/autonomy.test.api?target_revision=2")
    assert r.status_code == 200
    member = r.json()["members"][0]
    assert member["payload"] == {"x": 1, "v2": True}
    assert member["target_revision"] == 2


def test_get_settings_min_revision(graph_db_env, client):
    class V1(schemas.SettingSchema):
        set_id = "autonomy.test.api"
        schema_revision = 1
    class V2(schemas.SettingSchema):
        set_id = "autonomy.test.api"
        schema_revision = 2
    schemas.register_schema("autonomy.test.api", 1, V1)
    schemas.register_schema("autonomy.test.api", 2, V2)
    ops.add_setting("autonomy.test.api", 1, "k1", {"x": 1})
    ops.add_setting("autonomy.test.api", 2, "k2", {"x": 2})
    r = client.get("/api/graph/settings/autonomy.test.api?min_revision=2")
    assert r.status_code == 200
    body = r.json()
    keys = {m["key"] for m in body["members"]}
    assert keys == {"k2"}
    assert body["dropped"]["below_min_revision"] == 1


def test_get_settings_invalid_revision_returns_400(graph_db_env, client):
    r = client.get("/api/graph/settings/autonomy.test.api?target_revision=abc")
    assert r.status_code == 400
    assert "target_revision" in r.json()["error"]


# ── Write endpoints ────────────────────────────────────────


def test_post_setting_creates(graph_db_env, example_schema, client):
    r = client.post("/api/graph/setting", json={
        "set_id": "autonomy.test.api",
        "schema_revision": 1,
        "key": "alpha",
        "payload": {"x": 1},
    })
    assert r.status_code == 201
    sid = r.json()["id"]
    got = ops.get_setting(sid)
    assert got is not None and got.payload == {"x": 1}


def test_post_setting_missing_field_returns_400(graph_db_env, example_schema, client):
    r = client.post("/api/graph/setting", json={
        "set_id": "autonomy.test.api",
        "schema_revision": 1,
        # missing key + payload
    })
    assert r.status_code == 400
    assert "missing fields" in r.json()["error"]


def test_post_setting_validation_failure_structured_400(graph_db_env, strict_schema, client):
    r = client.post("/api/graph/setting", json={
        "set_id": "autonomy.test.strict",
        "schema_revision": 1,
        "key": "k",
        "payload": {"x": 1},  # missing 'name'
    })
    assert r.status_code == 400
    body = r.json()
    assert body["error"] == "schema validation failed"
    assert "name" in body["detail"]


def test_post_override(graph_db_env, example_schema, client):
    base = ops.add_setting("autonomy.test.api", 1, "k", {"a": 1, "b": 2})
    r = client.post(f"/api/graph/setting/{base}/override", json={
        "payload": {"b": 99},
    })
    assert r.status_code == 201
    members = ops.read_set("autonomy.test.api").members
    assert members[0].payload == {"a": 1, "b": 99}


def test_post_override_missing_target_404(graph_db_env, example_schema, client):
    r = client.post("/api/graph/setting/00000000-0000-0000-0000-000000000000/override",
                    json={"payload": {"x": 1}})
    assert r.status_code == 404


def test_post_exclude(graph_db_env, example_schema, client):
    base = ops.add_setting("autonomy.test.api", 1, "k", {"x": 1},
                           state="canonical")
    r = client.post(f"/api/graph/setting/{base}/exclude", json={})
    assert r.status_code == 201
    assert ops.read_set("autonomy.test.api").members == []


def test_post_promote(graph_db_env, example_schema, client):
    sid = ops.add_setting("autonomy.test.api", 1, "k", {"x": 1})
    r = client.post(f"/api/graph/setting/{sid}/promote", json={"to_state": "canonical"})
    assert r.status_code == 200
    assert ops.get_setting(sid).publication_state == "canonical"


def test_post_promote_invalid_state_400(graph_db_env, example_schema, client):
    sid = ops.add_setting("autonomy.test.api", 1, "k", {"x": 1})
    r = client.post(f"/api/graph/setting/{sid}/promote", json={"to_state": "garbage"})
    assert r.status_code == 400


def test_post_promote_missing_to_state_400(graph_db_env, example_schema, client):
    sid = ops.add_setting("autonomy.test.api", 1, "k", {"x": 1})
    r = client.post(f"/api/graph/setting/{sid}/promote", json={})
    assert r.status_code == 400


def test_post_deprecate(graph_db_env, example_schema, client):
    sid = ops.add_setting("autonomy.test.api", 1, "k", {"x": 1})
    r = client.post(f"/api/graph/setting/{sid}/deprecate", json={})
    assert r.status_code == 200
    assert ops.get_setting(sid).deprecated is True


def test_delete_setting(graph_db_env, example_schema, client):
    sid = ops.add_setting("autonomy.test.api", 1, "k", {"x": 1})
    r = client.delete(f"/api/graph/setting/{sid}")
    assert r.status_code == 200
    assert ops.get_setting(sid) is None


def test_delete_canonical_blocked_400(graph_db_env, example_schema, client):
    sid = ops.add_setting("autonomy.test.api", 1, "k", {"x": 1},
                           state="canonical")
    r = client.delete(f"/api/graph/setting/{sid}")
    assert r.status_code == 400
