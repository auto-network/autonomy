"""PUT /api/orgs/{slug}/charter (auto-bkoe6): the Charter screen's write.

The route validates through ``autonomy.org#2`` and upserts the org's own
identity row at canonical state; ``GET /api/orgs/{slug}``'s resolver
(``org_ops.show_org``) must then return the edited payload, including a
revision-1 seed row reading back cleanly beside the new revision-2 row.
"""
from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard import org_membership_routes
from tools.graph import org_ops
from tools.graph.db import GraphDB


@pytest.fixture
def client(tmp_path, monkeypatch):
    GraphDB.close_all_pooled()
    orgs = tmp_path / "orgs"
    orgs.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.setattr(
        org_membership_routes.api_auth,
        "require_global_api_authority",
        lambda request: None,
    )
    # A founded-with-identity org: the revision-1 seed the charter must outrank.
    org_ops.create_org(
        "charterorg",
        identity_payload={"name": "Charter Org", "byline": "seed byline"},
    )
    app = Starlette(routes=org_membership_routes.ROUTES)
    with TestClient(app) as c:
        yield c
    GraphDB.close_all_pooled()


def _identity(slug: str) -> dict:
    detail = org_ops.show_org(slug)
    assert detail is not None and detail["identity"] is not None
    return detail["identity"]


def test_put_round_trips_through_show_org(client):
    body = {
        "name": "Autonomy Network",
        "byline": "AGI platform",
        "description": "What the org is, in its own words.",
        "color": "#6C63FF",
        "favicon": "/static/icon-192.png",
    }
    r = client.put("/api/orgs/charterorg/charter", json=body)
    assert r.status_code == 200, r.text
    identity = _identity("charterorg")
    assert identity["payload"] == body
    assert identity["schema_revision"] == 2
    assert identity["publication_state"] == "canonical"


def test_second_put_updates_the_same_row(client):
    first = client.put(
        "/api/orgs/charterorg/charter", json={"name": "One"}
    ).json()["setting_id"]
    second = client.put(
        "/api/orgs/charterorg/charter", json={"name": "Two"}
    ).json()["setting_id"]
    assert first == second, "upsert must evolve one base row, not append"
    assert _identity("charterorg")["payload"]["name"] == "Two"


def test_overlong_byline_is_refused_with_the_schema_error(client):
    r = client.put(
        "/api/orgs/charterorg/charter",
        json={"name": "X", "byline": "y" * 61},
    )
    assert r.status_code == 400
    assert "over the 60" in r.json()["error"]
    # The seed row is untouched by the refused write.
    assert _identity("charterorg")["payload"]["byline"] == "seed byline"


def test_missing_name_is_refused(client):
    r = client.put("/api/orgs/charterorg/charter", json={"byline": "b"})
    assert r.status_code == 400
    assert "name" in r.json()["error"]


def test_unknown_org_is_404(client):
    r = client.put("/api/orgs/nosuch/charter", json={"name": "X"})
    assert r.status_code == 404


def test_revision_1_seed_reads_back_before_any_put(client):
    identity = _identity("charterorg")
    assert identity["schema_revision"] == 1
    assert identity["payload"]["name"] == "Charter Org"
