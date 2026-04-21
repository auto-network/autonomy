"""API tests for the org registry endpoints.

Spec: graph://d970d946-f95.

Exercises GET /api/orgs (list), GET /api/orgs/<slug> (show),
POST /api/orgs (create), DELETE /api/orgs/<slug> (remove). The orgs root
is redirected to a tmp directory via ``AUTONOMY_ORGS_DIR`` so tests don't
touch the operator's real ``data/orgs/``.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from starlette.testclient import TestClient

from tools.graph import org_ops, schemas
from tools.graph.schemas.registry import SCHEMAS, UPCONVERTERS


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
def stub_org_schema():
    class OrgV1(schemas.SettingSchema):
        set_id = "autonomy.org"
        schema_revision = 1

    schemas.register_schema("autonomy.org", 1, OrgV1)
    return OrgV1


@pytest.fixture
def orgs_root(tmp_path, monkeypatch):
    root = tmp_path / "data" / "orgs"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(root))
    return root


@pytest.fixture
def client(test_app):
    with TestClient(test_app) as c:
        yield c


# ── GET /api/orgs ─────────────────────────────────────────


def test_orgs_list_includes_bootstrap(orgs_root, client):
    """Dashboard startup auto-bootstraps autonomy + personal orgs."""
    r = client.get("/api/orgs")
    assert r.status_code == 200
    slugs = {entry["org"]["slug"] for entry in r.json()["orgs"]}
    assert {"autonomy", "personal"} <= slugs


def test_orgs_list_after_create(orgs_root, stub_org_schema, client):
    org_ops.create_org("anchore", type_="shared", identity_payload={
        "name": "Anchore", "color": "#2D7DD2", "type": "shared",
    })
    r = client.get("/api/orgs")
    assert r.status_code == 200
    by_slug = {entry["org"]["slug"]: entry for entry in r.json()["orgs"]}
    assert "anchore" in by_slug
    assert by_slug["anchore"]["identity"]["payload"]["name"] == "Anchore"
    # Cascade-resolved identity is attached for renderer convenience.
    assert by_slug["anchore"]["identity_resolved"]["slug"] == "anchore"


# ── GET /api/orgs/<slug> ──────────────────────────────────


def test_orgs_show_not_found(orgs_root, client):
    r = client.get("/api/orgs/ghost")
    assert r.status_code == 404


def test_orgs_show_returns_bootstrap_and_identity(
    orgs_root, stub_org_schema, client,
):
    org_ops.create_org("anchore", identity_payload={
        "name": "Anchore", "color": "#2D7DD2", "type": "shared",
    })
    r = client.get("/api/orgs/anchore")
    assert r.status_code == 200
    body = r.json()
    assert body["org"]["slug"] == "anchore"
    assert body["org"]["type"] == "shared"
    assert body["identity"]["payload"]["color"] == "#2D7DD2"
    assert body["identity_resolved"]["resolved"] is True


# ── POST /api/orgs ────────────────────────────────────────


def test_orgs_create(orgs_root, client):
    r = client.post("/api/orgs", json={"slug": "anchore"})
    assert r.status_code == 201
    body = r.json()
    assert body["slug"] == "anchore"
    assert body["type"] == "shared"
    assert (orgs_root / "anchore.db").exists()


def test_orgs_create_missing_slug(orgs_root, client):
    r = client.post("/api/orgs", json={})
    assert r.status_code == 400
    assert "slug" in r.json()["error"].lower()


def test_orgs_create_existing_returns_409(orgs_root, client):
    client.post("/api/orgs", json={"slug": "anchore"})
    r = client.post("/api/orgs", json={"slug": "anchore"})
    assert r.status_code == 409


def test_orgs_create_invalid_slug_400(orgs_root, client):
    r = client.post("/api/orgs", json={"slug": "bad/slug"})
    assert r.status_code == 400


def test_orgs_create_with_identity(orgs_root, stub_org_schema, client):
    r = client.post("/api/orgs", json={
        "slug": "anchore",
        "type": "shared",
        "identity": {
            "name": "Anchore", "color": "#2D7DD2", "type": "shared",
        },
    })
    assert r.status_code == 201
    detail = client.get("/api/orgs/anchore").json()
    assert detail["identity"]["payload"]["name"] == "Anchore"


# ── DELETE /api/orgs/<slug> ───────────────────────────────


def test_orgs_delete(orgs_root, stub_org_schema, client):
    client.post("/api/orgs", json={"slug": "anchore"})
    r = client.delete("/api/orgs/anchore")
    assert r.status_code == 200
    assert r.json()["removed"] is True
    assert not (orgs_root / "anchore.db").exists()


def test_orgs_delete_missing(orgs_root, client):
    r = client.delete("/api/orgs/ghost")
    assert r.status_code == 404


def test_orgs_delete_refuses_with_references(
    orgs_root, stub_org_schema, client,
):
    client.post("/api/orgs", json={"slug": "anchore"})
    client.post("/api/orgs", json={"slug": "personal", "type": "personal"})
    # Insert a reference in personal.db keyed by 'anchore'.
    conn = sqlite3.connect(str(orgs_root / "personal.db"))
    conn.execute(
        "INSERT INTO settings(id, set_id, schema_revision, key, payload, "
        "publication_state, created_at, updated_at) "
        "VALUES('s1','autonomy.org.identity-override',1,'anchore',"
        "'{}','canonical','t','t')"
    )
    conn.commit()
    conn.close()

    r = client.delete("/api/orgs/anchore")
    assert r.status_code == 409
    body = r.json()
    assert "references" in body
    assert any(ref["key"] == "anchore" for ref in body["references"])
    assert (orgs_root / "anchore.db").exists()


def test_orgs_delete_force(orgs_root, stub_org_schema, client):
    client.post("/api/orgs", json={"slug": "anchore"})
    client.post("/api/orgs", json={"slug": "personal", "type": "personal"})
    conn = sqlite3.connect(str(orgs_root / "personal.db"))
    conn.execute(
        "INSERT INTO settings(id, set_id, schema_revision, key, payload, "
        "publication_state, created_at, updated_at) "
        "VALUES('s1','autonomy.org.identity-override',1,'anchore',"
        "'{}','canonical','t','t')"
    )
    conn.commit()
    conn.close()

    r = client.delete("/api/orgs/anchore?force=1")
    assert r.status_code == 200
    body = r.json()
    assert body["removed"] is True
    assert len(body["references"]) >= 1
    assert not (orgs_root / "anchore.db").exists()
