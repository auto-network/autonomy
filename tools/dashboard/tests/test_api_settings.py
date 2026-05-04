"""API tests for the Settings primitive endpoints.

Exercises every route exposed by the dashboard server, plus the structured
400 from validation failures. Spec: graph://0d3f750f-f9c.
"""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from tools.graph import ops, schemas, settings_ops
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
    sid = ops.add_setting("autonomy.test.api", 1, "k", {"x": 1}, org=ops.CALLER_ORG)
    r = client.get("/api/graph/settings/autonomy.test.api")
    assert r.status_code == 200
    body = r.json()
    assert len(body["members"]) == 1
    assert body["members"][0]["id"] == sid
    assert body["members"][0]["payload"] == {"x": 1}


def test_get_setting_by_key(graph_db_env, example_schema, client):
    ops.add_setting("autonomy.test.api", 1, "alpha", {"x": 1}, org=ops.CALLER_ORG)
    r = client.get("/api/graph/settings/autonomy.test.api/alpha")
    assert r.status_code == 200
    assert r.json()["key"] == "alpha"


def test_get_setting_by_key_404(graph_db_env, example_schema, client):
    r = client.get("/api/graph/settings/autonomy.test.api/missing")
    assert r.status_code == 404


def test_get_setting_by_id(graph_db_env, example_schema, client):
    sid = ops.add_setting("autonomy.test.api", 1, "k", {"x": 1}, org=ops.CALLER_ORG)
    r = client.get(f"/api/graph/setting/{sid}")
    assert r.status_code == 200
    assert r.json()["id"] == sid


def test_get_setting_by_id_404(graph_db_env, example_schema, client):
    r = client.get("/api/graph/setting/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404


def test_get_set_ids(graph_db_env, example_schema, client):
    ops.add_setting("autonomy.test.api", 1, "k", {"x": 1}, org=ops.CALLER_ORG)
    r = client.get("/api/graph/sets")
    assert r.status_code == 200
    body = r.json()
    assert "autonomy.test.api" in body["set_ids"]
    assert "sets" not in body


def test_get_set_ids_summary(graph_db_env, example_schema, client):
    base = ops.add_setting("autonomy.test.api", 1, "k", {"x": 1}, org=ops.CALLER_ORG)
    ops.override_setting(base, {"label": "edited"}, org=ops.CALLER_ORG)
    r = client.get("/api/graph/sets?summary=1")
    assert r.status_code == 200
    body = r.json()
    assert "autonomy.test.api" in body["set_ids"]
    row = next(
        item for item in body["sets"]
        if item["set_id"] == "autonomy.test.api"
    )
    assert row["set_id"] == "autonomy.test.api"
    assert row["count"] == 1
    assert row["member_count"] == 1
    assert row["stored_row_count"] == 2
    assert row["stored_key_count"] == 1
    assert row["deprecated_row_count"] == 0
    assert row["payload_bytes"] > 0
    assert row["latest_updated_at"] is not None


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

    ops.add_setting("autonomy.test.api", 1, "k", {"x": 1}, org=ops.CALLER_ORG)
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
    ops.add_setting("autonomy.test.api", 1, "k1", {"x": 1}, org=ops.CALLER_ORG)
    ops.add_setting("autonomy.test.api", 2, "k2", {"x": 2}, org=ops.CALLER_ORG)
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
    got = ops.get_setting(sid, org=ops.CALLER_ORG)
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
    base = ops.add_setting("autonomy.test.api", 1, "k", {"a": 1, "b": 2}, org=ops.CALLER_ORG)
    r = client.post(f"/api/graph/setting/{base}/override", json={
        "payload": {"b": 99},
    })
    assert r.status_code == 201
    members = ops.read_set("autonomy.test.api", org=ops.CALLER_ORG).members
    assert members[0].payload == {"a": 1, "b": 99}


def test_post_override_missing_target_404(graph_db_env, example_schema, client):
    r = client.post("/api/graph/setting/00000000-0000-0000-0000-000000000000/override",
                    json={"payload": {"x": 1}})
    assert r.status_code == 404


def test_post_exclude(graph_db_env, example_schema, client):
    base = ops.add_setting("autonomy.test.api", 1, "k", {"x": 1},
                           state="canonical", org=ops.CALLER_ORG)
    r = client.post(f"/api/graph/setting/{base}/exclude", json={})
    assert r.status_code == 201
    assert ops.read_set("autonomy.test.api", org=ops.CALLER_ORG).members == []


def test_post_promote(graph_db_env, example_schema, client):
    sid = ops.add_setting("autonomy.test.api", 1, "k", {"x": 1}, org=ops.CALLER_ORG)
    r = client.post(f"/api/graph/setting/{sid}/promote", json={"to_state": "canonical"})
    assert r.status_code == 200
    assert ops.get_setting(sid, org=ops.CALLER_ORG).state == "canonical"


def test_post_promote_invalid_state_400(graph_db_env, example_schema, client):
    sid = ops.add_setting("autonomy.test.api", 1, "k", {"x": 1}, org=ops.CALLER_ORG)
    r = client.post(f"/api/graph/setting/{sid}/promote", json={"to_state": "garbage"})
    assert r.status_code == 400


def test_post_promote_missing_to_state_400(graph_db_env, example_schema, client):
    sid = ops.add_setting("autonomy.test.api", 1, "k", {"x": 1}, org=ops.CALLER_ORG)
    r = client.post(f"/api/graph/setting/{sid}/promote", json={})
    assert r.status_code == 400


def test_post_deprecate(graph_db_env, example_schema, client):
    sid = ops.add_setting("autonomy.test.api", 1, "k", {"x": 1}, org=ops.CALLER_ORG)
    r = client.post(f"/api/graph/setting/{sid}/deprecate", json={})
    assert r.status_code == 200
    assert ops.get_setting(sid, org=ops.CALLER_ORG).deprecated is True


def test_delete_setting(graph_db_env, example_schema, client):
    sid = ops.add_setting("autonomy.test.api", 1, "k", {"x": 1}, org=ops.CALLER_ORG)
    r = client.delete(f"/api/graph/setting/{sid}")
    assert r.status_code == 200
    assert ops.get_setting(sid, org=ops.CALLER_ORG) is None


def test_delete_canonical_blocked_400(graph_db_env, example_schema, client):
    sid = ops.add_setting("autonomy.test.api", 1, "k", {"x": 1},
                           state="canonical", org=ops.CALLER_ORG)
    r = client.delete(f"/api/graph/setting/{sid}")
    assert r.status_code == 400


# ── Resolve / chain endpoints (auto-xhimi) ─────────────────


def test_setting_resolve_unique_prefix(graph_db_env, example_schema, client):
    sid = ops.add_setting("autonomy.test.api", 1, "k", {"x": 1}, org=ops.CALLER_ORG)
    r = client.get(f"/api/graph/setting-resolve/{sid[:8]}")
    assert r.status_code == 200
    assert r.json()["id"] == sid


def test_setting_resolve_full_id(graph_db_env, example_schema, client):
    sid = ops.add_setting("autonomy.test.api", 1, "k", {"x": 1}, org=ops.CALLER_ORG)
    r = client.get(f"/api/graph/setting-resolve/{sid}")
    assert r.status_code == 200
    assert r.json()["id"] == sid


def test_setting_resolve_404(graph_db_env, example_schema, client):
    r = client.get("/api/graph/setting-resolve/no-such-prefix-xyz")
    assert r.status_code == 404


def test_setting_resolve_409_ambiguous(graph_db_env, example_schema, client, monkeypatch):
    import tools.graph.settings_ops as so
    forced = ["abcd5678-aaaa-aaaa-aaaa-000000000001",
              "abcd5678-bbbb-bbbb-bbbb-000000000002"]
    counter = iter(forced)
    monkeypatch.setattr(so, "uuid4", lambda: next(counter))
    ops.add_setting("autonomy.test.api", 1, "k1", {"x": 1}, org=ops.CALLER_ORG)
    ops.add_setting("autonomy.test.api", 1, "k2", {"x": 2}, org=ops.CALLER_ORG)
    r = client.get("/api/graph/setting-resolve/abcd5678")
    assert r.status_code == 409
    candidates = r.json()["candidates"]
    assert {c["id"] for c in candidates} == set(forced)


def test_settings_chain_returns_layers(graph_db_env, example_schema, client):
    base = ops.add_setting(
        "autonomy.test.api", 1, "k", {"name": "B", "v": 1}, state="canonical",
     org=ops.CALLER_ORG)
    ov = ops.override_setting(base, {"name": "O"}, org=ops.CALLER_ORG)
    r = client.get("/api/graph/settings/autonomy.test.api/k/chain")
    assert r.status_code == 200
    body = r.json()
    assert body["set_id"] == "autonomy.test.api"
    assert body["key"] == "k"
    assert len(body["layers"]) == 2
    assert body["layers"][0]["id"] == base
    assert body["layers"][1]["id"] == ov
    assert body["final"] == {"name": "O", "v": 1}


def test_settings_chain_404(graph_db_env, example_schema, client):
    r = client.get("/api/graph/settings/autonomy.test.api/missing/chain")
    assert r.status_code == 404


# ── Diag throughput endpoint ───────────────────────────────


def test_diag_settings_counts_direct_and_http_traffic(
    graph_db_env, example_schema, client,
):
    settings_ops.reset_settings_api_stats()
    sid = ops.add_setting("autonomy.test.api", 1, "k", {"x": 1}, org=ops.CALLER_ORG)
    assert ops.get_setting(sid, org=ops.CALLER_ORG) is not None

    r = client.get("/api/graph/settings/autonomy.test.api")
    assert r.status_code == 200

    diag = client.get("/api/diag/settings")
    assert diag.status_code == 200
    body = diag.json()

    assert body["totals"]["calls"] == 3
    assert body["totals"]["reads"] == 2
    assert body["totals"]["writes"] == 1
    assert body["totals"]["errors"] == 0
    assert body["totals"]["operations"] == {
        "add_setting": 1,
        "get_setting": 1,
        "read_set": 1,
    }
    assert body["last_60s"]["calls"] == 3
    assert body["last_60s"]["top_sets"] == [
        {
            "set_id": "autonomy.test.api",
            "calls": 3,
            "reads": 2,
            "writes": 1,
            "upserts": 0,
        },
    ]
    assert body["last_call"]["operation"] == "read_set"
    assert body["last_call"]["set_id"] == "autonomy.test.api"
    assert set(body["last_60s"]["latency_ms"]) == {"p50", "p95", "p99"}
    assert body["last_60s"]["latency_ms"]["p50"] is not None
    assert body["last_60s"]["latency_ms"]["p95"] is not None
    assert body["last_60s"]["latency_ms"]["p99"] is not None


def test_diag_settings_counts_errors(graph_db_env, example_schema, client):
    settings_ops.reset_settings_api_stats()

    with pytest.raises(LookupError):
        ops.promote_setting(
            "00000000-0000-0000-0000-000000000000", "canonical",
         org=ops.CALLER_ORG)

    diag = client.get("/api/diag/settings")
    assert diag.status_code == 200
    body = diag.json()

    assert body["totals"]["calls"] == 1
    assert body["totals"]["writes"] == 1
    assert body["totals"]["errors"] == 1
    assert body["totals"]["operations"] == {"promote_setting": 1}
    assert body["last_error"]["operation"] == "promote_setting"
    assert body["last_error"]["ok"] is False


def test_diag_settings_latency_percentiles(graph_db_env, client):
    settings_ops.reset_settings_api_stats()
    for duration_ms in (10, 20, 30, 40):
        settings_ops._SETTINGS_API_STATS.record(
            operation="synthetic_read",
            set_id="autonomy.test.synthetic",
            org=None,
            kind="read",
            ok=True,
            duration_ms=duration_ms,
            result_count=1,
        )

    diag = client.get("/api/diag/settings")
    assert diag.status_code == 200
    body = diag.json()

    assert body["totals"]["calls"] == 4
    assert body["totals"]["latency_ms"] == {
        "p50": 20,
        "p95": 40,
        "p99": 40,
    }


def test_diag_settings_sets_summary_and_detail(
    graph_db_env, example_schema, client,
):
    settings_ops.reset_settings_api_stats()
    base = ops.add_setting("autonomy.test.api", 1, "k", {"x": 1}, org=ops.CALLER_ORG)
    ops.override_setting(base, {"label": "edited"}, org=ops.CALLER_ORG)
    r = client.get("/api/graph/settings/autonomy.test.api")
    assert r.status_code == 200

    summary = client.get("/api/diag/settings/sets")
    assert summary.status_code == 200
    body = summary.json()
    assert body["windows"] == ["totals", "last_10s", "last_60s"]
    row = next(
        item for item in body["sets"]
        if item["set_id"] == "autonomy.test.api"
    )
    assert row["member_count"] == 1
    assert row["stored_row_count"] == 2
    assert row["stored_key_count"] == 1
    assert row["payload_bytes"] > 0
    assert row["activity"]["totals"] == {
        "calls": 3,
        "reads": 1,
        "writes": 2,
        "upserts": 0,
    }

    detail = client.get("/api/diag/settings/sets/autonomy.test.api")
    assert detail.status_code == 200
    detail_body = detail.json()
    assert detail_body["set"]["set_id"] == "autonomy.test.api"
    assert detail_body["set"]["member_count"] == 1
    assert len(detail_body["keys"]) == 1
    key_row = detail_body["keys"][0]
    assert key_row["key"] == "k"
    assert key_row["member_present"] is True
    assert key_row["stored_row_count"] == 2
    assert key_row["payload_bytes"] > 0
    assert key_row["latest_state"] == "raw"
