"""API tests for the Mission Control plugin."""
from __future__ import annotations

import yaml
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard.dao import mission_control_db as db
from tools.dashboard.plugin_api.manifest import PluginManifest
from tools.dashboard.plugins.mission_control.entrypoints import api as mc_api


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Route every DAO call in this test module at a fresh per-test DB.

    The API layer calls ``mission_control_db`` functions without a
    ``db_path`` override, so isolation happens by monkeypatching the
    module-level default.
    """
    path = tmp_path / "mission_control.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db(path)


def _client() -> TestClient:
    app = Starlette(routes=mc_api.routes)
    return TestClient(app)


def test_manifest_declares_skill_doc():
    plugin_dir = mc_api.__file__
    from pathlib import Path
    plugin_dir = Path(plugin_dir).resolve().parents[1]
    manifest = PluginManifest.model_validate(
        yaml.safe_load((plugin_dir / "plugin.yaml").read_text())
    )
    assert manifest.skill == "SKILL.md"
    assert (plugin_dir / "SKILL.md").is_file()


def test_create_mission():
    client = _client()
    resp = client.post("/api/missions", json={"name": "OSS Insights", "coordinator_session": "auto-x"})
    assert resp.status_code == 201
    body = resp.json()["mission"]
    assert body["name"] == "OSS Insights"
    assert body["coordinator_session"] == "auto-x"
    assert body["current_revision_id"] is None


def test_create_mission_requires_name():
    client = _client()
    resp = client.post("/api/missions", json={})
    assert resp.status_code == 400


def test_list_missions():
    client = _client()
    client.post("/api/missions", json={"name": "A"})
    client.post("/api/missions", json={"name": "B"})
    resp = client.get("/api/missions")
    assert resp.status_code == 200
    names = {m["name"] for m in resp.json()["missions"]}
    assert names == {"A", "B"}


def test_get_mission_not_found():
    client = _client()
    resp = client.get("/api/missions/nope")
    assert resp.status_code == 404


def test_get_mission_includes_current_revision_after_push():
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    client.post(f"/api/missions/{mission_id}/site", json={"html": "<html>v1</html>"})

    resp = client.get(f"/api/missions/{mission_id}")
    assert resp.status_code == 200
    body = resp.json()["mission"]
    assert body["current_revision"]["revision_seq"] == 1
    assert "html" not in body["current_revision"]


def test_delete_mission():
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    resp = client.delete(f"/api/missions/{mission_id}")
    assert resp.status_code == 200
    assert client.get(f"/api/missions/{mission_id}").status_code == 404


def test_delete_mission_not_found():
    client = _client()
    resp = client.delete("/api/missions/nope")
    assert resp.status_code == 404


def test_push_site_revision_stores_and_publishes_in_one_call():
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]

    resp = client.post(
        f"/api/missions/{mission_id}/site",
        json={"html": "<html>v1</html>", "note": "first push"},
    )
    assert resp.status_code == 201
    revision = resp.json()["revision"]
    assert revision["revision_seq"] == 1
    assert revision["note"] == "first push"
    # byte_size must be populated on push too, symmetric with the
    # revisions-list route — not left null until the next history read.
    assert revision["byte_size"] == len("<html>v1</html>")

    site = client.get(f"/api/missions/{mission_id}/site")
    assert site.status_code == 200
    assert site.json()["revision"]["html"] == "<html>v1</html>"


def test_push_site_revision_requires_html():
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    resp = client.post(f"/api/missions/{mission_id}/site", json={})
    assert resp.status_code == 400


def test_push_site_revision_missing_mission():
    client = _client()
    resp = client.post("/api/missions/nope/site", json={"html": "<html></html>"})
    assert resp.status_code == 404


def test_get_current_site_before_any_push_is_404():
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    resp = client.get(f"/api/missions/{mission_id}/site")
    assert resp.status_code == 404


def test_list_and_get_site_revisions():
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    client.post(f"/api/missions/{mission_id}/site", json={"html": "<html>v1</html>"})
    client.post(f"/api/missions/{mission_id}/site", json={"html": "<html>v2</html>"})

    listed = client.get(f"/api/missions/{mission_id}/site/revisions")
    assert listed.status_code == 200
    revisions = listed.json()["revisions"]
    assert [r["revision_seq"] for r in revisions] == [2, 1]
    assert "html" not in revisions[0]

    rev1_id = revisions[1]["revision_id"]
    single = client.get(f"/api/missions/{mission_id}/site/revisions/{rev1_id}")
    assert single.status_code == 200
    assert single.json()["revision"]["html"] == "<html>v1</html>"


def test_activate_revision_rolls_back_current_pointer():
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    rev1 = client.post(f"/api/missions/{mission_id}/site", json={"html": "<html>good</html>"}).json()["revision"]
    client.post(f"/api/missions/{mission_id}/site", json={"html": "<html>bad</html>"})

    resp = client.post(f"/api/missions/{mission_id}/site/revisions/{rev1['revision_id']}/activate")
    assert resp.status_code == 200
    assert resp.json()["revision"]["revision_id"] == rev1["revision_id"]

    current = client.get(f"/api/missions/{mission_id}/site").json()["revision"]
    assert current["html"] == "<html>good</html>"
    # No new revision was fabricated by rolling back.
    revisions = client.get(f"/api/missions/{mission_id}/site/revisions").json()["revisions"]
    assert len(revisions) == 2


def test_activate_revision_not_found():
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    resp = client.post(f"/api/missions/{mission_id}/site/revisions/nope/activate")
    assert resp.status_code == 404


# ── Chromeless public serving ────────────────────────────────────


def test_serve_mission_site_renders_current_revision_raw():
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    client.post(f"/api/missions/{mission_id}/site", json={"html": "<html><body>hello</body></html>"})

    resp = client.get(f"/missions/{mission_id}")
    assert resp.status_code == 200
    assert resp.text == "<html><body>hello</body></html>"
    assert resp.headers["content-type"].startswith("text/html")


def test_serve_mission_site_no_stale_serve_window():
    """A push must be visible on the very next request — response is
    marked uncacheable so no browser or proxy can serve a stale copy."""
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    client.post(f"/api/missions/{mission_id}/site", json={"html": "<html>v1</html>"})

    first = client.get(f"/missions/{mission_id}")
    assert first.text == "<html>v1</html>"
    assert "no-store" in first.headers["cache-control"]

    client.post(f"/api/missions/{mission_id}/site", json={"html": "<html>v2</html>"})
    second = client.get(f"/missions/{mission_id}")
    assert second.text == "<html>v2</html>"


def test_serve_mission_site_missing_mission_is_404():
    client = _client()
    resp = client.get("/missions/nope")
    assert resp.status_code == 404


def test_serve_mission_site_before_any_push_is_404():
    client = _client()
    mission_id = client.post("/api/missions", json={"name": "A"}).json()["mission"]["mission_id"]
    resp = client.get(f"/missions/{mission_id}")
    assert resp.status_code == 404
