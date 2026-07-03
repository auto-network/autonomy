"""Unit tests for the Design Studio plugin catalog API."""
from __future__ import annotations

from unittest.mock import patch
import json
import yaml

from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard.plugin_api.manifest import PluginManifest
from tools.dashboard.plugins.design_studio import librarian
from tools.dashboard.plugins.design_studio.entrypoints import api as design_api


def _client() -> TestClient:
    app = Starlette(routes=design_api.routes)
    return TestClient(app)


def test_manifest_declares_librarian_agent_action():
    plugin_dir = design_api.Path(__file__).resolve().parents[1]
    manifest = PluginManifest.model_validate(
        yaml.safe_load((plugin_dir / "plugin.yaml").read_text())
    )
    declarations = {decl.key: decl for decl in manifest.settings}

    decl = declarations["design.refresh-preview"]
    assert decl.set_id == "dashboard.agent-actions"
    assert decl.schema_revision == 2
    assert decl.uninstall == "deprecate_if_unchanged"

    payload = json.loads((plugin_dir / (decl.payload_file or "")).read_text())
    assert payload["asset_type"] == "design"
    assert payload["writes"] == ["design.thumbnail", "design.description"]
    assert "tools.dashboard.plugins.design_studio.librarian" in payload["prompt_template"]


def test_librarian_updates_stale_description_without_touching_provenance(tmp_path, monkeypatch):
    from agents import design_db

    old_db_path = design_db.DB_PATH
    old_initialized = design_db._initialized
    design_db.DB_PATH = tmp_path / "experiments.db"
    design_db._initialized = False
    try:
        revision_id = design_db.create_design(
            title="Catalog card",
            description="",
            fixture=json.dumps({"states": {"Default": {"count": 1}}}),
            variants=[
                {
                    "id": "variant-a",
                    "html": "<main><h1>Fast catalog</h1><p>Shows active work and recent previews.</p></main>",
                }
            ],
            creator_session_id="auto-designer",
            creator_session_label="Designer",
        )
        screenshot = tmp_path / "screenshot.png"

        def fake_capture(html_path, screenshot_path, *, viewport):
            assert "Fast catalog" in html_path.read_text()
            screenshot_path.parent.mkdir(parents=True, exist_ok=True)
            screenshot_path.write_bytes(screenshot.read_bytes())

        screenshot.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        monkeypatch.setattr(librarian, "_capture_with_agent_browser", fake_capture)
        monkeypatch.setattr(librarian, "_repo_root", lambda: tmp_path)

        result = librarian.run_librarian(revision_id)
        refreshed = design_db.get_design(revision_id)

        assert result.screenshot_written is True
        assert (tmp_path / "data" / "experiments" / revision_id / "screenshot.png").is_file()
        assert result.description_updated is True
        assert "Fast catalog" in result.description_after
        assert refreshed["description"] == result.description_after
        assert refreshed["creator_session_id"] == "auto-designer"
        assert refreshed["creator_session_label"] == "Designer"
        assert refreshed["design_id"] == revision_id
        assert refreshed["revision_seq"] == 1
        assert refreshed["status"] == "pending"
    finally:
        design_db.DB_PATH = old_db_path
        design_db._initialized = old_initialized


def _rows() -> list[dict]:
    return [
        {
            "id": "rev-a1",
            "design_id": "series-a",
            "title": "Session card",
            "description": "First pass",
            "status": "dismissed",
            "revision_seq": 1,
            "created_at": "2026-03-01 10:00:00",
            "creator_session_id": "",
            "creator_session_label": "",
            "variant_count": 1,
            "has_fixture": False,
        },
        {
            "id": "rev-a2",
            "design_id": "series-a",
            "title": "Session card refined",
            "description": "Second pass",
            "status": "pending",
            "revision_seq": 2,
            "created_at": "2026-03-01 11:00:00",
            "creator_session_id": "auto-designer",
            "creator_session_label": "Designer",
            "variant_count": 4,
            "has_fixture": True,
        },
        {
            "id": "rev-b1",
            "design_id": "series-b",
            "title": "Settings page",
            "description": "",
            "status": "completed",
            "revision_seq": 1,
            "created_at": "2026-04-01 09:00:00",
            "creator_session_id": "",
            "creator_session_label": "",
            "variant_count": 2,
            "has_fixture": False,
        },
    ]


def test_list_designs_groups_revisions_and_summarizes_all_statuses():
    with patch.object(design_api, "_design_rows", return_value=_rows()), \
         patch.object(design_api, "_thumbnail_url", lambda rev_id: "/thumb/" + rev_id):
        resp = _client().get("/api/design-studio/designs")

    assert resp.status_code == 200
    data = resp.json()
    assert data["summary"] == {
        "series": 2,
        "revisions": 3,
        "variants": 7,
        "pending_series": 1,
        "dismissed_series": 0,
        "completed_series": 1,
    }
    by_id = {row["design_id"]: row for row in data["designs"]}
    assert by_id["series-a"]["latest_revision_id"] == "rev-a2"
    assert by_id["series-a"]["revision_count"] == 2
    assert by_id["series-a"]["variant_count"] == 5
    assert by_id["series-a"]["has_fixture"] is True
    assert by_id["series-a"]["thumbnail_url"] == "/thumb/rev-a2"


def test_list_designs_filters_by_status_and_query():
    with patch.object(design_api, "_design_rows", return_value=_rows()):
        resp = _client().get("/api/design-studio/designs?status=pending&q=refined")

    assert resp.status_code == 200
    data = resp.json()
    assert data["filtered_count"] == 1
    assert data["designs"][0]["design_id"] == "series-a"


def test_list_designs_uses_older_revision_thumbnail_when_latest_has_none():
    def thumbnail_url(rev_id: str) -> str:
        return "/thumb/rev-a1" if rev_id == "rev-a1" else ""

    with patch.object(design_api, "_design_rows", return_value=_rows()), \
         patch.object(design_api, "_thumbnail_url", thumbnail_url):
        resp = _client().get("/api/design-studio/designs?status=pending")

    assert resp.status_code == 200
    assert resp.json()["designs"][0]["thumbnail_url"] == "/thumb/rev-a1"


def test_get_design_series_returns_revision_timeline():
    with patch.object(design_api, "_design_rows", return_value=_rows()):
        resp = _client().get("/api/design-studio/designs/series-a")

    assert resp.status_code == 200
    data = resp.json()
    assert data["design_id"] == "series-a"
    assert [row["id"] for row in data["revisions"]] == ["rev-a1", "rev-a2"]


def test_revision_thumbnail_serves_screenshot_file(tmp_path):
    screenshot = tmp_path / "screenshot.png"
    screenshot.write_bytes(b"png")
    with patch.object(design_api, "_screenshot_path", return_value=screenshot):
        resp = _client().get("/api/design-studio/revisions/rev-a2/thumbnail")

    assert resp.status_code == 200
    assert resp.content == b"png"


def test_revision_thumbnail_rejects_missing_file(tmp_path):
    missing = tmp_path / "missing.png"
    with patch.object(design_api, "_screenshot_path", return_value=missing):
        resp = _client().get("/api/design-studio/revisions/rev-a2/thumbnail")

    assert resp.status_code == 404


def test_update_design_status_updates_series_and_clears_cache():
    with patch.object(design_api, "_set_design_series_status") as set_status, \
         patch.object(design_api, "_clear_catalog_cache") as clear_cache:
        set_status.return_value = dict(
            _rows()[1],
            design_id="series-a",
            latest_revision_id="rev-a2",
            revision_count=2,
            status="completed",
        )
        resp = _client().post(
            "/api/design-studio/designs/series-a/status",
            json={"status": "completed"},
        )

    assert resp.status_code == 200
    assert resp.json()["design"]["status"] == "completed"
    set_status.assert_called_once_with("series-a", "completed")
    assert clear_cache.called


def test_update_design_status_rejects_invalid_status():
    resp = _client().post(
        "/api/design-studio/designs/series-a/status",
        json={"status": "archived"},
    )

    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid status"
