"""Session groups — the Session Board's columns (bead auto-q9y6e.2).

A group is a shared record: agents write it from `graph group`, the operator
writes it by dragging a card, and both land in the Settings set
dashboard.session.group in the operator's personal store (session_board_settings).
Two layers are pinned here:

* the record helpers against an isolated personal store (schema, one group
  per session, dissolve releases members, the layout member), and
* the HTTP API through the booted test app (create/update/dissolve, the
  per-session membership write, and the registry projection that the
  session store and the board read).
"""

import time

import pytest

# Imported at collection time so the two schemas are in the registry before
# the per-test registry snapshot is taken (conftest restores that snapshot
# after every test, which would drop a registration made inside a fixture).
from tools.dashboard import session_board_settings  # noqa: E402


# ── The Settings record ─────────────────────────────────────────────────

@pytest.fixture
def board_settings(tmp_path, monkeypatch):
    """session_board_settings against an isolated personal store (beside a temp orgs dir)."""
    orgs = tmp_path / "orgs"
    orgs.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))
    return session_board_settings


class TestGroupRecord:
    def test_schemas_are_registered(self):
        from tools.graph.schemas.registry import SCHEMAS
        assert "dashboard.session.group#1" in SCHEMAS and "dashboard.session.board.layout#1" in SCHEMAS

    def test_upsert_get_list(self, board_settings):
        s = board_settings
        g = s.upsert_group("deploy", {"name": "Registry deploy lane", "color": "#f59e0b", "refs": ["bead:auto-x"]}, created_by="auto-a")
        assert g["slug"] == "deploy" and g["name"] == "Registry deploy lane" and g["refs"] == ["bead:auto-x"]
        assert g["created_by"] == "auto-a" and g["members"] == []
        g2 = s.upsert_group("deploy", {"why": "one effort"})
        assert g2["name"] == "Registry deploy lane" and g2["why"] == "one effort"
        assert [x["slug"] for x in s.list_groups()] == ["deploy"]
        assert s.group_summaries()["deploy"]["color"] == "#f59e0b"
        assert s.get_group("nope") is None

    def test_membership_is_one_group_per_session(self, board_settings):
        s = board_settings
        s.upsert_group("a", {"name": "A"}); s.upsert_group("b", {"name": "B"})
        s.set_session_group("auto-a", "a", "crypto", "self")
        idx = s.session_group_index()
        assert idx["auto-a"]["group_id"] == "a" and idx["auto-a"]["group_tab"] == "crypto" and idx["auto-a"]["group"]["name"] == "A"
        s.set_session_group("auto-a", "b", "t", "operator")
        assert s.group_members("a", live_only=False) == [] and s.group_members("b", live_only=False) == ["auto-a"]
        s.set_session_group("auto-a", None)
        assert "auto-a" not in s.session_group_index()

    def test_dissolve_releases_members(self, board_settings):
        s = board_settings
        s.upsert_group("lane", {"name": "Lane"})
        s.set_session_group("auto-a", "lane", "", "self"); s.set_session_group("auto-b", "lane", "", "self")
        assert s.group_members("lane", live_only=False) == ["auto-a", "auto-b"]
        assert s.delete_group("lane") == 2
        assert s.get_group("lane") is None and s.session_group_index() == {}

    def test_layout_round_trip(self, board_settings):
        s = board_settings
        assert s.read_layout() == {"presentation": "transcript", "column_order": [], "widths": {}, "heights": {}, "updated_at": 0}
        s.write_layout({"column_order": ["deploy", "solo"], "widths": {"deploy": 700}})
        s.write_layout({"presentation": "stats", "heights": {"auto-a": 540}})
        got = s.read_layout()
        assert got["column_order"] == ["deploy", "solo"] and got["widths"] == {"deploy": 700}
        assert got["presentation"] == "stats" and got["heights"] == {"auto-a": 540} and got["updated_at"] > 0


# ── HTTP API ─────────────────────────────────────────────────────────────

@pytest.fixture
def seeded_session(test_client):
    """A live session's tmux name (the registry keys session_id by tmux name)."""
    rows = test_client.get("/api/dao/active_sessions").json()
    assert rows, "test database has no live sessions"
    return rows[0]["session_id"]


def _row(test_client, name):
    return [s for s in test_client.get("/api/dao/active_sessions").json() if s["session_id"] == name][0]


class TestGroupsApi:
    def test_create_lists_and_gets(self, test_client):
        resp = test_client.post("/api/groups", json={"name": "Registry deploy lane", "slug": "deploy",
                                                     "color": "#f59e0b", "why": "one effort", "refs": ["bead:auto-x"]})
        assert resp.status_code == 201, resp.text
        g = resp.json()["group"]
        assert g["slug"] == "deploy" and g["name"] == "Registry deploy lane"
        assert g["refs"] == ["bead:auto-x"] and g["members"] == []
        assert any(x["slug"] == "deploy" for x in test_client.get("/api/groups").json()["groups"])
        assert test_client.get("/api/groups/deploy").json()["group"]["why"] == "one effort"

    def test_slug_derives_from_name_and_is_validated(self, test_client):
        g = test_client.post("/api/groups", json={"name": "Post-crash Recovery!"}).json()["group"]
        assert g["slug"] == "post-crash-recovery"
        assert test_client.post("/api/groups", json={"name": "x", "slug": "Bad Slug"}).status_code == 400
        assert test_client.post("/api/groups", json={"name": "x", "refs": "nope"}).status_code == 400

    def test_membership_round_trip_through_the_registry(self, test_client, seeded_session):
        test_client.post("/api/groups", json={"name": "Lane", "slug": "lane", "color": "#123456"})
        resp = test_client.put(f"/api/session/{seeded_session}/group", json={"group": "lane", "tab": "crypto", "joined_by": "self"})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "session": seeded_session, "group": "lane", "tab": "crypto"}
        me = _row(test_client, seeded_session)
        assert me["group_id"] == "lane" and me["group_tab"] == "crypto"
        assert me["group"] == {"slug": "lane", "name": "Lane", "short": "", "color": "#123456", "why": ""}
        assert test_client.get("/api/groups/lane").json()["group"]["members"] == [seeded_session]

    def test_one_group_per_session_and_leave(self, test_client, seeded_session):
        test_client.post("/api/groups", json={"name": "A", "slug": "a"})
        test_client.post("/api/groups", json={"name": "B", "slug": "b"})
        test_client.put(f"/api/session/{seeded_session}/group", json={"group": "a"})
        test_client.put(f"/api/session/{seeded_session}/group", json={"group": "b", "tab": "t"})
        assert test_client.get("/api/groups/a").json()["group"]["members"] == []
        assert test_client.get("/api/groups/b").json()["group"]["members"] == [seeded_session]
        resp = test_client.put(f"/api/session/{seeded_session}/group", json={"group": None})
        assert resp.status_code == 200 and resp.json()["group"] is None
        me = _row(test_client, seeded_session)
        assert me["group_id"] is None and me["group_tab"] == "" and me["group"] is None

    def test_update_and_dissolve(self, test_client, seeded_session):
        test_client.post("/api/groups", json={"name": "Lane", "slug": "lane", "members": [seeded_session]})
        assert _row(test_client, seeded_session)["group_id"] == "lane"
        resp = test_client.put("/api/groups/lane", json={"name": "Renamed lane", "why": "because"})
        assert resp.status_code == 200 and resp.json()["group"]["name"] == "Renamed lane"
        assert _row(test_client, seeded_session)["group"]["name"] == "Renamed lane"
        assert test_client.put("/api/groups/lane", json={"name": ""}).status_code == 400
        assert test_client.put("/api/groups/missing", json={"name": "x"}).status_code == 404
        resp = test_client.delete("/api/groups/lane")
        assert resp.status_code == 200 and resp.json()["released"] == 1
        assert _row(test_client, seeded_session)["group_id"] is None
        assert test_client.get("/api/groups/lane").status_code == 404

    def test_unknown_group_is_rejected(self, test_client, seeded_session):
        assert test_client.put(f"/api/session/{seeded_session}/group", json={"group": "nope"}).status_code == 404
        assert test_client.put(f"/api/session/{seeded_session}/group", json={"group": 5}).status_code == 400
        assert _row(test_client, seeded_session)["group_id"] is None


class TestLayoutApi:
    def test_layout_get_put_round_trip(self, test_client):
        assert test_client.get("/api/session-board/layout").json()["layout"]["column_order"] == []
        resp = test_client.put("/api/session-board/layout", json={"presentation": "stats", "column_order": ["deploy", "solo"], "widths": {"deploy": 700.4}, "heights": {"auto-test-alpha": 540}})
        assert resp.status_code == 200, resp.text
        got = test_client.get("/api/session-board/layout").json()["layout"]
        assert got["presentation"] == "stats" and got["column_order"] == ["deploy", "solo"]
        assert got["widths"] == {"deploy": 700} and got["heights"] == {"auto-test-alpha": 540}
        # Partial writes keep the rest.
        test_client.put("/api/session-board/layout", json={"presentation": "transcript"})
        got = test_client.get("/api/session-board/layout").json()["layout"]
        assert got["presentation"] == "transcript" and got["column_order"] == ["deploy", "solo"]

    def test_layout_validation(self, test_client):
        assert test_client.put("/api/session-board/layout", json={"presentation": "huge"}).status_code == 400
        assert test_client.put("/api/session-board/layout", json={"column_order": "deploy"}).status_code == 400
        assert test_client.put("/api/session-board/layout", json={"widths": {"deploy": "wide"}}).status_code == 400
        assert test_client.put("/api/session-board/layout", json={}).status_code == 400


class TestGroupCrossTalkLog:
    def test_group_channel_filter(self, tmp_path):
        from tools.dashboard.dao import auth_db
        auth_db.init_db(tmp_path / "auth.db")   # never the real auth.db
        auth_db.insert_message("s1", "S1", "group:lane", None, None, "hello lane", time.time())
        auth_db.insert_message("s1", "S1", "s2", None, None, "direct", time.time())
        msgs = auth_db.get_messages(limit=10, session="group:lane")
        assert [m["message"] for m in msgs] == ["hello lane"]
