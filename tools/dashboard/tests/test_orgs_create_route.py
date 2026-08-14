"""POST /api/orgs — the founding-ceremony route contract (auto-nixfv #8)."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from tools.graph import org_ops, settings_ops
from tools.graph.schemas.personal_identity import PERSONAL_IDENTITY_SET_ID
from tools.network.idkit import KeyPair
from tools.network.idkit.armor import encrypt_root_key

PASSWORD = "week-glacier-thirty-nine"


@pytest.fixture
def client(tmp_path, monkeypatch):
    from tools.graph.db import GraphDB
    from tools.dashboard import server

    GraphDB.close_all_pooled()
    # Orgs-tree hermeticity, no GRAPH_DB pin: the ceremony writes to the
    # created org's OWN db, which a pin would contradict and the
    # fail-loud resolver refuses. delenv guards ambient leaks.
    orgs = tmp_path / "orgs"
    orgs.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    GraphDB.close_all_pooled()
    GraphDB.create_org_db(
        "personal", type_="personal", path=orgs / "personal.db"
    ).close()
    root = KeyPair.generate()
    with settings_ops.identity_write_context():
        settings_ops.upsert_by_key(
            PERSONAL_IDENTITY_SET_ID, 1, "default",
            {
                "armored_private_key": encrypt_root_key(root, PASSWORD, iterations=10_000),
                "root_pub": root.public_hex,
                "display_name": "Test Owner",
                "created_at": "2026-07-26T00:00:00Z",
            },
            org=None,
        )
    app = Starlette(routes=[Route("/api/orgs", server.api_orgs_create, methods=["POST"])])
    with TestClient(app) as c:
        yield c
    GraphDB.close_all_pooled()


def test_missing_password_is_400_naming_the_field(client):
    r = client.post("/api/orgs", json={"slug": "acme"})
    assert r.status_code == 400
    assert "personal_password" in r.json()["error"]
    assert all(o.slug != "acme" for o in org_ops.list_orgs())


def test_wrong_password_is_403_and_creates_nothing(client):
    r = client.post(
        "/api/orgs", json={"slug": "acme", "personal_password": "wrong"}
    )
    assert r.status_code == 403
    assert all(o.slug != "acme" for o in org_ops.list_orgs())


def test_success_returns_the_ceremony_result(client):
    r = client.post(
        "/api/orgs", json={"slug": "acme", "personal_password": PASSWORD}
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["org"]["slug"] == "acme"
    assert set(body["identity"]) == {
        "root_pub", "genesis_id", "founder_persona_pub", "event_ids"
    }
    assert len(body["identity"]["event_ids"]) == 4
    # Slug conflict on a second create: 409, unchanged.
    again = client.post(
        "/api/orgs", json={"slug": "acme", "personal_password": PASSWORD}
    )
    assert again.status_code == 409
