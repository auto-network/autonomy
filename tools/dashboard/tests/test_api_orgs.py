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
    from tools.graph.schemas.registry import unregister_schema
    unregister_schema("autonomy.org", 1)

    class OrgV1(schemas.SettingSchema):
        set_id = "autonomy.org"
        schema_revision = 1

    return OrgV1


#: Creating an org IS the founding ceremony (auto-nixfv): the route
#: requires the owner's personal-identity password, which authorizes
#: founding and seals the org key. These tests enroll a throwaway
#: identity and pass its password, exercising the real ceremony.
PERSONAL_PASSWORD = "api-orgs-test-password"


def create_org_body(slug: str, **extra) -> dict:
    return {"slug": slug, "personal_password": PERSONAL_PASSWORD, **extra}


@pytest.fixture
def orgs_root(tmp_path, monkeypatch):
    # Same directory test_app exports as the app's hermetic orgs dir: the
    # identity seeded below must land where the running app actually looks,
    # or the create/delete routes read an empty personal store and refuse.
    root = tmp_path / "orgs"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(root))
    # The container's ambient GRAPH_DB pin would contradict the explicit-org
    # settings writes inside org creation/deletion — the fail-loud resolver
    # refuses that instead of silently misrouting the org key into the pinned
    # store. Org creation never coincides with a GRAPH_DB pin in production
    # (pin callers pass org=None), so unpinning is honest, not a workaround.
    monkeypatch.delenv("GRAPH_DB", raising=False)
    root.mkdir(parents=True, exist_ok=True)

    # personal.db must exist BEFORE the identity write: settings_ops with
    # org=None resolves to <orgs>/personal.db only when the file is there,
    # and the app's startup bootstrap creates it later — so without this
    # the write and the route's read land in different databases.
    from tools.graph.db import GraphDB

    GraphDB.create_org_db("personal", type_="personal", root=root).close()

    from tools.graph import settings_ops
    from tools.graph.schemas.personal_identity import PERSONAL_IDENTITY_SET_ID
    from tools.network.idkit import KeyPair
    from tools.network.idkit.armor import encrypt_root_key

    owner = KeyPair.generate()
    with settings_ops.identity_write_context():
        settings_ops.upsert_by_key(
            PERSONAL_IDENTITY_SET_ID, 1, "default",
            {
                "armored_private_key": encrypt_root_key(
                    owner, PERSONAL_PASSWORD, iterations=10_000
                ),
                "root_pub": owner.public_hex,
                "display_name": "API Orgs Test",
                "created_at": "2026-07-26T00:00:00Z",
            },
            org=None,
        )
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
    r = client.post("/api/orgs", json=create_org_body("anchore"))
    assert r.status_code == 201
    body = r.json()
    assert body["org"]["slug"] == "anchore"
    assert body["org"]["type"] == "shared"
    # The ceremony founded the ledger in the same act.
    assert len(body["identity"]["event_ids"]) == 4
    assert (orgs_root / "anchore.db").exists()


def test_orgs_create_missing_slug(orgs_root, client):
    r = client.post("/api/orgs", json={})
    assert r.status_code == 400
    assert "slug" in r.json()["error"].lower()


def test_orgs_create_existing_returns_409(orgs_root, client):
    client.post("/api/orgs", json=create_org_body("anchore"))
    r = client.post("/api/orgs", json=create_org_body("anchore"))
    assert r.status_code == 409


def test_orgs_create_invalid_slug_400(orgs_root, client):
    r = client.post("/api/orgs", json=create_org_body("bad/slug"))
    assert r.status_code == 400


def test_orgs_create_with_identity(orgs_root, stub_org_schema, client):
    r = client.post("/api/orgs", json=create_org_body(
        "anchore",
        type="shared",
        identity={"name": "Anchore", "color": "#2D7DD2", "type": "shared"},
    ))
    assert r.status_code == 201
    detail = client.get("/api/orgs/anchore").json()
    assert detail["identity"]["payload"]["name"] == "Anchore"


# ── DELETE /api/orgs/<slug> ───────────────────────────────


def test_orgs_delete(orgs_root, stub_org_schema, client):
    client.post("/api/orgs", json=create_org_body("anchore"))
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
    client.post("/api/orgs", json=create_org_body("anchore"))
    client.post("/api/orgs", json=create_org_body("personal", type="personal"))
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
    client.post("/api/orgs", json=create_org_body("anchore"))
    client.post("/api/orgs", json=create_org_body("personal", type="personal"))
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
