"""Producer-side tests for the ``setting.changed`` SSE event.

Each Settings write endpoint (POST /api/graph/setting, override, exclude,
promote, deprecate, DELETE, migrate) must publish a ``setting.changed``
event on the dashboard EventBus. These tests assert event presence,
operation tag, and required payload fields. Bead: auto-p5rbu.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from tools.dashboard import server as server_mod
from tools.dashboard.event_bus import EventBus
from tools.graph import ops, schemas
from tools.graph.schemas.registry import SCHEMAS, UPCONVERTERS


# ── Fixtures ────────────────────────────────────────────────


@pytest.fixture
def graph_db_env(tmp_path, monkeypatch):
    # Orgs-tree, no pin: the emit path resolves explicit 'autonomy',
    # which a pin contradicts under the fail-loud resolver (73bad14e).
    from tools.graph.db import GraphDB
    orgs = tmp_path / "orgs"
    orgs.mkdir(exist_ok=True)
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    GraphDB.close_all_pooled()
    GraphDB.create_org_db("autonomy").close()
    db_path = orgs / "autonomy.db"
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
        set_id = "autonomy.test.events"
        schema_revision = 1
    schemas.register_schema("autonomy.test.events", 1, V1)
    return V1


@pytest.fixture
def fresh_bus():
    """Swap a fresh EventBus into the server module for the test.

    Tests inspect ``bus._buffer`` directly rather than subscribing — broadcast
    records every event in the ring buffer regardless of subscriber count.
    """
    bus = EventBus()
    original = server_mod.event_bus
    server_mod.event_bus = bus
    try:
        yield bus
    finally:
        server_mod.event_bus = original


@pytest.fixture
def client(test_app, fresh_bus):
    with TestClient(test_app) as c:
        # Server lifespan startup writes plugin-declared settings (e.g.
        # design.refresh-preview), which emit setting.changed events of
        # their own. Drop the boot-time events so each test's buffer
        # inspection sees only the writes the test itself performs.
        fresh_bus._buffer.clear()
        fresh_bus._buffer_bytes = 0
        yield c


def _setting_changed_events(bus: EventBus) -> list[dict]:
    """Return all ``setting.changed`` payloads from the bus buffer in order."""
    out = []
    for entry in bus._buffer:
        if entry.topic == "setting.changed":
            import json
            out.append(json.loads(entry.serialised))
    return out


# ── Producer tests ──────────────────────────────────────────


def test_write_setting_emits_event(graph_db_env, example_schema, client, fresh_bus):
    r = client.post("/api/graph/setting", json={
        "set_id": "autonomy.test.events",
        "schema_revision": 1,
        "key": "alpha",
        "payload": {"x": 1},
    })
    assert r.status_code == 201

    events = _setting_changed_events(fresh_bus)
    assert len(events) == 1
    ev = events[0]
    assert ev["operation"] == "write"
    assert ev["set_id"] == "autonomy.test.events"
    assert ev["schema_revision"] == 1
    assert ev["key"] == "alpha"
    assert ev["publication_state"] == "raw"
    assert ev["deprecated"] is False
    assert "org" in ev


def test_write_setting_with_state_canonical_emits_state(
    graph_db_env, example_schema, client, fresh_bus,
):
    r = client.post("/api/graph/setting", json={
        "set_id": "autonomy.test.events",
        "schema_revision": 1,
        "key": "k",
        "payload": {"x": 1},
        "state": "canonical",
    })
    assert r.status_code == 201

    events = _setting_changed_events(fresh_bus)
    assert len(events) == 1
    assert events[0]["publication_state"] == "canonical"
    assert events[0]["operation"] == "write"


def test_override_setting_emits_event(graph_db_env, example_schema, client, fresh_bus):
    base = ops.add_setting("autonomy.test.events", 1, "k", {"a": 1, "b": 2}, org=ops.CALLER_ORG)
    fresh_bus._buffer.clear()
    fresh_bus._buffer_bytes = 0

    r = client.post(f"/api/graph/setting/{base}/override", json={"payload": {"b": 99}})
    assert r.status_code == 201

    events = _setting_changed_events(fresh_bus)
    assert len(events) == 1
    ev = events[0]
    assert ev["operation"] == "override"
    assert ev["set_id"] == "autonomy.test.events"
    assert ev["key"] == "k"


def test_exclude_setting_emits_event(graph_db_env, example_schema, client, fresh_bus):
    base = ops.add_setting("autonomy.test.events", 1, "k", {"x": 1}, state="canonical", org=ops.CALLER_ORG)
    fresh_bus._buffer.clear()
    fresh_bus._buffer_bytes = 0

    r = client.post(f"/api/graph/setting/{base}/exclude", json={})
    assert r.status_code == 201

    events = _setting_changed_events(fresh_bus)
    assert len(events) == 1
    assert events[0]["operation"] == "exclude"
    assert events[0]["set_id"] == "autonomy.test.events"


def test_promote_setting_emits_event(graph_db_env, example_schema, client, fresh_bus):
    sid = ops.add_setting("autonomy.test.events", 1, "k", {"x": 1}, org=ops.CALLER_ORG)
    fresh_bus._buffer.clear()
    fresh_bus._buffer_bytes = 0

    r = client.post(f"/api/graph/setting/{sid}/promote", json={"to_state": "canonical"})
    assert r.status_code == 200

    events = _setting_changed_events(fresh_bus)
    assert len(events) == 1
    ev = events[0]
    assert ev["operation"] == "promote"
    assert ev["publication_state"] == "canonical"
    assert ev["set_id"] == "autonomy.test.events"
    assert ev["key"] == "k"


def test_deprecate_setting_emits_event(graph_db_env, example_schema, client, fresh_bus):
    sid = ops.add_setting("autonomy.test.events", 1, "k", {"x": 1}, org=ops.CALLER_ORG)
    fresh_bus._buffer.clear()
    fresh_bus._buffer_bytes = 0

    r = client.post(f"/api/graph/setting/{sid}/deprecate", json={})
    assert r.status_code == 200

    events = _setting_changed_events(fresh_bus)
    assert len(events) == 1
    ev = events[0]
    assert ev["operation"] == "deprecate"
    assert ev["deprecated"] is True


def test_delete_setting_emits_event(graph_db_env, example_schema, client, fresh_bus):
    sid = ops.add_setting("autonomy.test.events", 1, "k", {"x": 1}, org=ops.CALLER_ORG)
    fresh_bus._buffer.clear()
    fresh_bus._buffer_bytes = 0

    r = client.delete(f"/api/graph/setting/{sid}")
    assert r.status_code == 200

    events = _setting_changed_events(fresh_bus)
    assert len(events) == 1
    ev = events[0]
    assert ev["operation"] == "delete"
    assert ev["set_id"] == "autonomy.test.events"
    assert ev["key"] == "k"
    # Row was raw at delete time (only raw is delete-eligible).
    assert ev["publication_state"] == "raw"


def test_migrate_setting_set_emits_event_per_member(
    graph_db_env, client, fresh_bus,
):
    class V1(schemas.SettingSchema):
        set_id = "autonomy.test.migrate"
        schema_revision = 1
    class V2(schemas.SettingSchema):
        set_id = "autonomy.test.migrate"
        schema_revision = 2
    schemas.register_schema("autonomy.test.migrate", 1, V1)
    schemas.register_schema(
        "autonomy.test.migrate", 2, V2,
        upconvert_from_prev=lambda p: {**p, "v2": True},
    )
    ops.add_setting("autonomy.test.migrate", 1, "a", {"x": 1}, org=ops.CALLER_ORG)
    ops.add_setting("autonomy.test.migrate", 1, "b", {"x": 2}, org=ops.CALLER_ORG)
    ops.add_setting("autonomy.test.migrate", 1, "c", {"x": 3}, org=ops.CALLER_ORG)
    fresh_bus._buffer.clear()
    fresh_bus._buffer_bytes = 0

    r = client.post(
        "/api/graph/settings/autonomy.test.migrate/migrate",
        json={"to_rev": 2},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["rewrote"] == 3

    events = _setting_changed_events(fresh_bus)
    assert len(events) == 3
    keys = sorted(ev["key"] for ev in events)
    assert keys == ["a", "b", "c"]
    for ev in events:
        assert ev["operation"] == "migrate"
        assert ev["schema_revision"] == 2
        assert ev["set_id"] == "autonomy.test.migrate"


def test_migrate_dry_run_does_not_emit(graph_db_env, client, fresh_bus):
    class V1(schemas.SettingSchema):
        set_id = "autonomy.test.migrate"
        schema_revision = 1
    class V2(schemas.SettingSchema):
        set_id = "autonomy.test.migrate"
        schema_revision = 2
    schemas.register_schema("autonomy.test.migrate", 1, V1)
    schemas.register_schema(
        "autonomy.test.migrate", 2, V2,
        upconvert_from_prev=lambda p: {**p, "v2": True},
    )
    ops.add_setting("autonomy.test.migrate", 1, "a", {"x": 1}, org=ops.CALLER_ORG)
    fresh_bus._buffer.clear()
    fresh_bus._buffer_bytes = 0

    r = client.post(
        "/api/graph/settings/autonomy.test.migrate/migrate",
        json={"to_rev": 2, "dry_run": True},
    )
    assert r.status_code == 200

    events = _setting_changed_events(fresh_bus)
    assert events == []


def test_unrelated_writes_do_not_emit_setting_changed(
    graph_db_env, example_schema, client, fresh_bus,
):
    r = client.post("/api/graph/note", json={
        "content": "an unrelated note",
        "tags": "test",
    })
    # The endpoint may fail in this minimal setup (depends on graph DB
    # state), but regardless: no setting.changed event should appear.
    assert _setting_changed_events(fresh_bus) == []


def test_setting_emit_hook_invalidates_targeted_caches(monkeypatch, fresh_bus):
    from tools.dashboard import feature_flags

    workspace_invalidations: list[str] = []
    feature_invalidations: list[str | None] = []
    monkeypatch.setattr(
        server_mod.workspace_settings,
        "invalidate_for_setting",
        workspace_invalidations.append,
    )
    monkeypatch.setattr(
        feature_flags,
        "invalidate_cache",
        lambda *, org=None, all_orgs=False: feature_invalidations.append(org),
    )

    server_mod._settings_emit_hook(
        operation="upsert",
        snapshot={
            "set_id": feature_flags.FEATURE_FLAGS_SET_ID,
            "schema_revision": 1,
            "key": "voice.audio_capture",
            "publication_state": "raw",
            "deprecated": False,
        },
        org="personal",
    )

    assert workspace_invalidations == [feature_flags.FEATURE_FLAGS_SET_ID]
    assert feature_invalidations == ["personal"]


def test_failed_write_does_not_emit(graph_db_env, example_schema, client, fresh_bus):
    """400 from validation must not produce a setting.changed event."""
    r = client.post("/api/graph/setting", json={
        "set_id": "autonomy.test.events",
        # missing key + payload
        "schema_revision": 1,
    })
    assert r.status_code == 400
    assert _setting_changed_events(fresh_bus) == []


def test_promote_404_does_not_emit(graph_db_env, example_schema, client, fresh_bus):
    r = client.post(
        "/api/graph/setting/00000000-0000-0000-0000-000000000000/promote",
        json={"to_state": "canonical"},
    )
    assert r.status_code == 404
    assert _setting_changed_events(fresh_bus) == []
