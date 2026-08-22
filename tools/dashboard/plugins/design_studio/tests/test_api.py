"""Unit tests for the Design Studio plugin catalog API."""
from __future__ import annotations

from unittest.mock import patch
import json
import yaml

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.testclient import TestClient

from tools.dashboard import api_auth
from tools.dashboard.plugin_api.manifest import PluginManifest
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
    prompt = payload["prompt_template"]
    assert "/api/design/" in prompt
    assert "/metadata" in prompt
    assert "screenshot #design-iframe" in prompt
    assert "--data-binary" in prompt
    assert "manualCaptureScreenshot" not in prompt
    assert "tools.dashboard.plugins.design_studio.librarian" not in prompt
    assert "Do not read or write data/experiments.db directly" in prompt
    assert "Do not upload a full-page screenshot" in prompt


def test_librarian_agent_action_prompt_renders_through_dispatch_template_engine():
    from tools.dashboard.server import _render_agent_action_prompt

    plugin_dir = design_api.Path(__file__).resolve().parents[1]
    manifest = PluginManifest.model_validate(
        yaml.safe_load((plugin_dir / "plugin.yaml").read_text())
    )
    decl = {decl.key: decl for decl in manifest.settings}["design.refresh-preview"]
    payload = json.loads((plugin_dir / (decl.payload_file or "")).read_text())

    rendered = _render_agent_action_prompt(
        payload["prompt_template"],
        page_context={
            "asset": {
                "id": "rev-a2",
                "title": "Session card refined",
                "short_description": "",
                "url": "https://localhost:8080/design/rev-a2",
            },
            "design": {
                "design_id": "series-a",
                "status": "pending",
                "revision_count": 2,
                "variant_count": 4,
                "creator_session_id": "auto-designer",
            },
            "source": {},
            "bead": {},
            "tags": {"values": [], "list": ""},
        },
        dispatched_by_session="auto-test",
        member_key="design.refresh-preview",
    )

    assert 'export DASHBOARD="${DASHBOARD:-https://localhost:8080}"' in rendered
    assert "payload = {'description': 'REPLACE_WITH_CONCISE_RENDERED_DESIGN_SUMMARY'}" in rendered
    assert "f'{dash}/api/design-studio/revisions/{rev}/metadata'" in rendered
    assert 'headers={\'Content-Type\': \'application/json\'}' in rendered
    assert "screenshot #design-iframe $SHOT" in rendered
    assert '--data-binary "@$SHOT"' in rendered
    assert "{asset[" not in rendered


def test_update_revision_metadata_helper_preserves_revision_provenance(tmp_path):
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
        result = design_api._update_revision_metadata(
            revision_id,
            description="Shows active work and recent previews.",
        )
        refreshed = design_db.get_design(revision_id)

        assert result is not None
        assert result["description"] == "Shows active work and recent previews."
        assert refreshed["description"] == "Shows active work and recent previews."
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


def test_catalog_preserves_latest_nonempty_creator_link():
    rows = _rows() + [{
        "id": "rev-a3",
        "design_id": "series-a",
        "title": "Session card final",
        "description": "Latest pass without repeated creator metadata",
        "status": "pending",
        "revision_seq": 3,
        "created_at": "2026-03-01 12:00:00",
        "creator_session_id": "",
        "creator_session_label": "",
        "variant_count": 1,
        "has_fixture": False,
    }]

    with patch.object(design_api, "_design_rows", return_value=rows):
        resp = _client().get("/api/design-studio/designs?q=auto-designer")

    assert resp.status_code == 200
    design = resp.json()["designs"][0]
    assert design["latest_revision_id"] == "rev-a3"
    assert design["creator_session_id"] == "auto-designer"
    assert design["creator_session_label"] == "Designer"


def test_session_contribution_links_latest_design_for_exact_creator():
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/api/session-contributions",
        "headers": [],
        "state": {
            "api_principal": api_auth.ApiPrincipal(
                api_auth.ApiPrincipalKind.OPERATOR_COOKIE,
                subject="operator",
            ),
        },
    })
    with patch.object(design_api, "_design_rows", return_value=_rows()):
        rows = design_api.session_contributions(
            ["auto-designer", "auto-unlinked"],
            request,
        )

    [linked] = rows["auto-designer"]
    assert linked["kind"] == "action"
    assert linked["label"] == "Design Studio"
    assert linked["href"] == "/design/rev-a2?from_session=auto-designer"
    assert "Session card refined" in linked["title"]
    assert rows["auto-unlinked"] == []


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


def test_update_revision_metadata_updates_description_and_clears_cache():
    updated = dict(_rows()[1], description="Updated summary")
    with patch.object(design_api, "_update_revision_metadata", return_value=updated) as update, \
         patch.object(design_api, "_clear_catalog_cache") as clear_cache:
        resp = _client().patch(
            "/api/design-studio/revisions/rev-a2/metadata",
            json={"description": "Updated summary"},
        )

    assert resp.status_code == 200
    assert resp.json()["revision"]["description"] == "Updated summary"
    update.assert_called_once_with("rev-a2", title=None, description="Updated summary")
    assert clear_cache.called


def test_update_revision_metadata_accepts_put_title_and_description():
    updated = dict(_rows()[1], title="New title", description="Updated summary")
    with patch.object(design_api, "_update_revision_metadata", return_value=updated) as update:
        resp = _client().put(
            "/api/design-studio/revisions/rev-a2/metadata",
            json={"title": " New title ", "description": " Updated summary "},
        )

    assert resp.status_code == 200
    update.assert_called_once_with(
        "rev-a2",
        title="New title",
        description="Updated summary",
    )


def test_update_revision_metadata_rejects_unknown_fields():
    resp = _client().patch(
        "/api/design-studio/revisions/rev-a2/metadata",
        json={"status": "completed"},
    )

    assert resp.status_code == 400
    assert resp.json()["error"] == "unknown field(s)"


def test_update_revision_metadata_rejects_empty_body():
    resp = _client().patch("/api/design-studio/revisions/rev-a2/metadata", json={})

    assert resp.status_code == 400
    assert resp.json()["error"] == "title or description is required"


def test_update_revision_metadata_returns_404_for_missing_revision():
    with patch.object(design_api, "_update_revision_metadata", return_value=None):
        resp = _client().patch(
            "/api/design-studio/revisions/missing/metadata",
            json={"description": "Updated summary"},
        )

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
