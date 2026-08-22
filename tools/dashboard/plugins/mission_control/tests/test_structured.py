"""Structured style: the mission style toggle, the item CRUD routes, and
the settings-rendered screen.

Item writes go through the real Settings substrate against the hermetic
per-test org databases the shared conftest provisions, so what these
tests prove is the actual storage path, not a mock of it.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.testclient import TestClient

from tools.dashboard import api_auth
from tools.dashboard.dao import mission_control_db as db
from tools.dashboard.plugins.mission_control.entrypoints import api as mc_api
# Importing the schemas module registers MissionItemV1 with the settings
# schema registry — the same side effect the plugin loader relies on.
from tools.dashboard.plugins.mission_control.entrypoints import schemas  # noqa: F401


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    path = tmp_path / "mission_control.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db(path)


@pytest.fixture(autouse=True)
def _no_real_presence_writes():
    with patch("tools.graph.surface.Presence", MagicMock()):
        yield


class _OperatorPrincipalMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        scope.setdefault("state", {})["api_principal"] = api_auth.ApiPrincipal(
            api_auth.ApiPrincipalKind.OPERATOR_COOKIE,
            subject="test-operator",
        )
        await self.app(scope, receive, send)


def _client() -> TestClient:
    app = Starlette(
        routes=mc_api.routes,
        middleware=[Middleware(_OperatorPrincipalMiddleware)],
    )
    return TestClient(app)


def _structured_mission(client) -> dict:
    mission = client.post("/api/missions", json={
        "name": "Structured Test", "style": "structured",
    }).json()["mission"]
    # Pin to the hermetic org the conftest provisioned, so item writes and
    # reads resolve against per-test settings storage.
    db.set_mission_org(mission["mission_id"], "autonomy")
    return db.get_mission(mission["mission_id"])


# ── style ─────────────────────────────────────────────────────────


def test_mission_style_defaults_to_freeform():
    client = _client()
    mission = client.post("/api/missions", json={"name": "M"}).json()["mission"]
    assert mission["style"] == "freeform"


def test_mission_style_set_at_creation_and_toggled():
    client = _client()
    mission = client.post("/api/missions", json={
        "name": "M", "style": "structured"}).json()["mission"]
    assert mission["style"] == "structured"
    out = client.post(f"/api/missions/{mission['mission_id']}/style",
                      json={"style": "freeform"})
    assert out.status_code == 200
    assert out.json()["mission"]["style"] == "freeform"


def test_mission_style_rejects_unknown_value():
    client = _client()
    mission = client.post("/api/missions", json={"name": "M"}).json()["mission"]
    out = client.post(f"/api/missions/{mission['mission_id']}/style",
                      json={"style": "artisanal"})
    assert out.status_code == 400
    assert db.get_mission(mission["mission_id"])["style"] == "freeform"


# ── items ─────────────────────────────────────────────────────────


def test_item_put_and_read_back_on_pillar():
    client = _client()
    mission = _structured_mission(client)
    pillar = client.post(
        f"/api/missions/{mission['mission_id']}/pillars",
        json={"name": "Relay"}).json()["pillar"]
    put = client.put(
        f"/api/pillars/{pillar['pillar_id']}/items/checkpoint-a",
        json={"kind": "checkpoint", "title": "Checkpoint A", "state": "proven",
              "order": 10, "refs": ["commit:a0cc1541"]})
    assert put.status_code == 200, put.text
    assert put.json()["key"] == f"{pillar['pillar_id']}:checkpoint-a"

    mine = client.get(f"/api/pillars/{pillar['pillar_id']}/items").json()["items"]
    assert [i["item_id"] for i in mine] == ["checkpoint-a"]
    assert mine[0]["state"] == "proven"
    assert mine[0]["refs"] == ["commit:a0cc1541"]

    # The mission route aggregates the whole mission, pillars included.
    whole = client.get(
        f"/api/missions/{mission['mission_id']}/items").json()["items"]
    assert {i["item_id"] for i in whole} == {"checkpoint-a"}


def test_item_surface_id_cannot_be_spoofed():
    client = _client()
    mission = _structured_mission(client)
    put = client.put(
        f"/api/missions/{mission['mission_id']}/items/x",
        json={"kind": "work", "title": "T", "surface_id": "somewhere-else",
              "item_id": "not-x"})
    assert put.status_code == 200
    item = put.json()["item"]
    assert item["surface_id"] == mission["mission_id"]
    assert item["item_id"] == "x"


def test_item_rejects_unknown_kind_and_state():
    client = _client()
    mission = _structured_mission(client)
    bad_kind = client.put(
        f"/api/missions/{mission['mission_id']}/items/x",
        json={"kind": "vibe", "title": "T"})
    assert bad_kind.status_code == 400
    bad_state = client.put(
        f"/api/missions/{mission['mission_id']}/items/x",
        json={"kind": "work", "title": "T", "state": "sideways"})
    assert bad_state.status_code == 400


def test_item_state_transition_stamps_happened_at_and_appends_note():
    client = _client()
    mission = _structured_mission(client)
    client.put(f"/api/missions/{mission['mission_id']}/items/w",
               json={"kind": "work", "title": "W", "state": "active",
                     "body": "Building."})
    out = client.post(
        f"/api/missions/{mission['mission_id']}/items/w/state",
        json={"state": "proven", "note": "Watched it run live."})
    assert out.status_code == 200, out.text
    item = out.json()["item"]
    assert item["state"] == "proven"
    assert item["happened_at"].endswith("Z")
    assert item["body"] == "Building.\n\nWatched it run live."


def test_item_state_transition_on_missing_item_is_404():
    client = _client()
    mission = _structured_mission(client)
    out = client.post(
        f"/api/missions/{mission['mission_id']}/items/ghost/state",
        json={"state": "done"})
    assert out.status_code == 404


# ── serving ───────────────────────────────────────────────────────


def test_structured_mission_serves_viewer_without_any_revision():
    client = _client()
    mission = _structured_mission(client)
    client.put(f"/api/missions/{mission['mission_id']}/items/hello",
               json={"kind": "work", "title": "First item"})
    page = client.get(f"/missions/{mission['mission_id']}")
    assert page.status_code == 200
    assert "mc-data" in page.text            # baked JSON block
    assert "First item" in page.text
    assert "__MC_STRUCTURED_DATA__" not in page.text


def test_freeform_mission_without_revision_still_404s():
    client = _client()
    mission = client.post("/api/missions", json={"name": "F"}).json()["mission"]
    page = client.get(f"/missions/{mission['mission_id']}")
    assert page.status_code == 404


def test_structured_pillar_url_serves_the_same_app_focused():
    client = _client()
    mission = _structured_mission(client)
    pillar = client.post(
        f"/api/missions/{mission['mission_id']}/pillars",
        json={"name": "Relay"}).json()["pillar"]
    page = client.get(
        f"/missions/{mission['mission_id']}/pillars/{pillar['pillar_id']}")
    assert page.status_code == 200
    assert f'"focus": "{pillar["pillar_id"]}"' in page.text.replace(
        '":"', '": "')


# ── org boundary ──────────────────────────────────────────────────


def _org_client(org: str) -> TestClient:
    """A client the boundary classified as an org-bound session bearer."""
    class _OrgPrincipalMiddleware:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            scope.setdefault("state", {})["api_principal"] = api_auth.ApiPrincipal(
                api_auth.ApiPrincipalKind.ORG_SESSION,
                subject="test-session", org=org,
            )
            await self.app(scope, receive, send)

    return TestClient(Starlette(
        routes=mc_api.routes,
        middleware=[Middleware(_OrgPrincipalMiddleware)],
    ))


def test_cross_org_caller_cannot_see_or_write_items():
    operator = _client()
    mission = _structured_mission(operator)          # org: autonomy
    operator.put(f"/api/missions/{mission['mission_id']}/items/x",
                 json={"kind": "work", "title": "T"})

    stranger = _org_client("someother")
    mid = mission["mission_id"]
    # Reads and writes both render as not-found — indistinguishable from
    # a mission that does not exist.
    assert stranger.get(f"/api/missions/{mid}/items").status_code == 404
    assert stranger.put(f"/api/missions/{mid}/items/y",
                        json={"kind": "work", "title": "Y"}).status_code == 404
    assert stranger.post(f"/api/missions/{mid}/items/x/state",
                         json={"state": "done"}).status_code == 404
    assert stranger.post(f"/api/missions/{mid}/style",
                         json={"style": "freeform"}).status_code == 404
    # And the list simply omits it.
    listed = stranger.get("/api/missions").json()["missions"]
    assert all(m["mission_id"] != mid for m in listed)

    # The mission's own org sees and writes normally.
    member = _org_client("autonomy")
    assert member.get(f"/api/missions/{mid}/items").status_code == 200
    assert member.put(f"/api/missions/{mid}/items/z",
                      json={"kind": "work", "title": "Z"}).status_code == 200
    assert any(m["mission_id"] == mid
               for m in member.get("/api/missions").json()["missions"])


def test_create_mission_defaults_to_caller_org():
    session = _org_client("autonomy")
    mission = session.post("/api/missions", json={"name": "Mine"}).json()["mission"]
    assert mission["org"] == "autonomy"
    explicit = session.post("/api/missions", json={
        "name": "Named", "org": "elsewhere"}).json()["mission"]
    assert explicit["org"] == "elsewhere"


def test_update_attribution_follows_the_routes_pillar():
    """One session coordinating the mission and several pillars must be
    attributed to the pillar in the REQUEST PATH, not to whichever
    coordinated pillar a lookup happens to return first (observed live:
    updates posted on one pillar's route labeled as a sibling pillar)."""
    operator = _client()
    mission = operator.post("/api/missions", json={"name": "Attrib"}).json()["mission"]
    db.set_mission_org(mission["mission_id"], "autonomy")
    db.set_mission_coordinator(mission["mission_id"], "multi-sess")
    p1 = operator.post(f"/api/missions/{mission['mission_id']}/pillars",
                       json={"name": "First", "coordinator_session": "multi-sess"}
                       ).json()["pillar"]
    operator.post(f"/api/missions/{mission['mission_id']}/pillars",
                  json={"name": "Second", "coordinator_session": "multi-sess"})

    entry = operator.post(f"/api/pillars/{p1['pillar_id']}/questions",
                          json={"question": "attribution probe"}).json()["question"]

    session = _org_client("autonomy")  # ORG_SESSION principal, subject test-session
    # Rebind the subject to the coordinating session name.
    class _Mw:
        def __init__(self, app): self.app = app
        async def __call__(self, scope, receive, send):
            scope.setdefault("state", {})["api_principal"] = api_auth.ApiPrincipal(
                api_auth.ApiPrincipalKind.ORG_SESSION,
                subject="multi-sess", org="autonomy")
            await self.app(scope, receive, send)
    session = TestClient(Starlette(routes=mc_api.routes,
                                   middleware=[Middleware(_Mw)]))

    out = session.post(
        f"/api/pillars/{p1['pillar_id']}/questions/{entry['entry_id']}/update",
        json={"text": "working on it"})
    assert out.status_code == 201, out.text
    rows = operator.get(f"/api/pillars/{p1['pillar_id']}/questions").json()["questions"]
    ups = [u for q in rows if q["entry_id"] == entry["entry_id"]
           for u in q.get("updates", []) if u.get("kind") != "echo"]
    assert ups, "update row missing"
    assert ups[-1]["author_participant_id"] == f"pillar:{p1['pillar_id']}", ups[-1]
    assert ups[-1]["author_label"] == "First"
