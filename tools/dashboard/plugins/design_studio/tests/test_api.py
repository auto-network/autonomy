"""Unit tests for the Design Studio plugin catalog API."""
from __future__ import annotations

import asyncio
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
    # Thumbnails render headlessly on the dashboard; the librarian only
    # writes the summary, and only through bearer-authenticated calls.
    assert payload["writes"] == ["design.description"]
    prompt = payload["prompt_template"]
    assert "graph ui-design --pull" in prompt
    assert "/metadata" in prompt
    assert "CROSSTALK_TOKEN" in prompt
    assert "screenshot" not in prompt.lower()
    assert "session-auth" in prompt  # named only to forbid it
    assert "tools.dashboard.plugins.design_studio.librarian" not in prompt
    assert "Do not read or write data/experiments.db directly" in prompt


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
    assert "'Authorization': 'Bearer ' + os.environ['CROSSTALK_TOKEN']" in rendered
    assert 'graph ui-design --pull "$REV" /tmp/design-$REV' in rendered
    assert "/api/design-studio/designs/series-a" in rendered
    assert "{asset[" not in rendered and "{design[" not in rendered


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


def test_get_design_series_returns_revision_timeline_and_share_state():
    from tools.dashboard import design_shares

    grants = [{"target_uuid": "rev-a1", "target_type": "design", "token": "tok",
               "url": "https://relay.auto.network/l/tok", "issued_at": "2026-09-01T00:00:00Z"}]
    with patch.object(design_api, "_design_rows", return_value=_rows()), \
         patch.object(design_shares, "active_design_grants", return_value=grants):
        resp = _client().get("/api/design-studio/designs/series-a")

    assert resp.status_code == 200
    data = resp.json()
    assert data["design_id"] == "series-a"
    assert [row["id"] for row in data["revisions"]] == ["rev-a1", "rev-a2"]
    # A grant on ANY revision id (or the design id) makes the design shared.
    assert data["share"]["shared"] is True
    assert data["share"]["grants"][0]["token"] == "tok"


def test_badge_counts_live_designs_not_the_backlog():
    from tools.dashboard import design_lifecycle

    with patch.object(design_lifecycle, "live_design_count", return_value=2):
        assert design_api.badge_counter() == 2
    with patch.object(design_lifecycle, "live_design_count", side_effect=RuntimeError("db")):
        assert design_api.badge_counter() == 0


def test_revision_thumbnail_serves_screenshot_file(tmp_path):
    from tools.dashboard import design_thumbnails

    screenshot = tmp_path / "screenshot.png"
    screenshot.write_bytes(b"png")
    with patch.object(design_thumbnails, "thumbnail_path", return_value=screenshot):
        resp = _client().get("/api/design-studio/revisions/rev-a2/thumbnail")

    assert resp.status_code == 200
    assert resp.content == b"png"
    assert resp.headers["content-type"] == "image/png"


def test_revision_thumbnail_prefers_the_composed_jpeg(tmp_path):
    from tools.dashboard import design_thumbnails

    composed = tmp_path / "thumbnail.jpg"
    composed.write_bytes(b"jpg")
    with patch.object(design_thumbnails, "thumbnail_path", return_value=composed):
        resp = _client().get("/api/design-studio/revisions/rev-a2/thumbnail")

    assert resp.status_code == 200
    assert resp.content == b"jpg"
    assert resp.headers["content-type"] == "image/jpeg"


def test_revision_thumbnail_rejects_missing_file(tmp_path):
    from tools.dashboard import design_thumbnails

    missing = tmp_path / "missing.png"
    with patch.object(design_thumbnails, "thumbnail_path", return_value=missing):
        resp = _client().get("/api/design-studio/revisions/rev-a2/thumbnail")

    assert resp.status_code == 404


def test_thumbnail_inventory_counts_composites_and_browser_captures(tmp_path):
    from tools import data_paths

    (tmp_path / "experiments" / "rev-jpg").mkdir(parents=True)
    (tmp_path / "experiments" / "rev-jpg" / "thumbnail.jpg").write_bytes(b"jpg")
    (tmp_path / "experiments" / "rev-png").mkdir(parents=True)
    (tmp_path / "experiments" / "rev-png" / "screenshot.png").write_bytes(b"png")
    (tmp_path / "experiments" / "rev-none").mkdir(parents=True)
    design_api._clear_thumbnail_cache()
    with patch.object(data_paths, "DATA_ROOT", tmp_path):
        ids = design_api._screenshot_revision_ids()
    design_api._clear_thumbnail_cache()
    assert ids == {"rev-jpg", "rev-png"}


def test_catalog_row_carries_the_thumbnail_revision_and_form_factor():
    rows = [
        {"id": "rev-a1", "design_id": "design-a", "title": "Alpha", "status": "pending",
         "revision_seq": 1, "created_at": "2026-01-01 00:00:00", "thumbnail_url": "/thumb/a1"},
        {"id": "rev-a2", "design_id": "design-a", "title": "Alpha", "status": "pending",
         "revision_seq": 2, "created_at": "2026-01-02 00:00:00"},
    ]
    with patch.object(design_api, "_form_factor", side_effect=lambda rev: {"rev-a1": "mobile"}.get(rev, "")):
        series = design_api._series_from_rows(rows)[0]
    assert series["thumbnail_url"] == "/thumb/a1"
    assert series["thumbnail_revision_id"] == "rev-a1"
    assert series["form_factor"] == "mobile"


def test_render_route_reports_an_unavailable_renderer():
    from tools.dashboard import design_thumbnails

    with patch.object(design_api, "_design_org", return_value=None), \
         patch.object(design_thumbnails.queue, "enqueue", return_value=False), \
         patch.object(design_thumbnails.queue, "status", return_value={"available": False, "running": False}):
        resp = _client().post("/api/design-studio/revisions/rev-a2/render")
    assert resp.status_code == 503
    assert "agent-browser" in resp.json()["error"]


def test_render_route_queues_when_the_worker_is_running():
    from tools.dashboard import design_thumbnails

    status = {"available": True, "running": True, "pending": 1}
    with patch.object(design_api, "_design_org", return_value=None), \
         patch.object(design_thumbnails.queue, "enqueue", return_value=True) as enqueue, \
         patch.object(design_thumbnails.queue, "status", return_value=status):
        resp = _client().post("/api/design-studio/revisions/rev-a2/render")
    assert resp.status_code == 202
    assert resp.json()["queued"] is True
    enqueue.assert_called_once_with("rev-a2")


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


# ── Org-scoping: an org caller sees only its own org's designs ────────────


def _org_rows() -> list[dict]:
    """Two single-revision designs owned by different orgs."""
    base = {
        "description": "", "status": "pending", "revision_seq": 1,
        "creator_session_id": "", "creator_session_label": "",
        "variant_count": 1, "has_fixture": False,
    }
    return [
        {**base, "id": "rev-auto", "design_id": "series-auto",
         "title": "Autonomy design", "created_at": "2026-05-01 10:00:00",
         "org": "autonomy"},
        {**base, "id": "rev-anc", "design_id": "series-anc",
         "title": "Anchore design", "created_at": "2026-05-02 10:00:00",
         "org": "anchore"},
    ]


def _req(principal, *, path_params=None, query_string=b"") -> Request:
    return Request({
        "type": "http", "method": "GET", "path": "/x", "headers": [],
        "query_string": query_string,
        "path_params": path_params or {},
        "state": {"api_principal": principal},
    })


def _org_session(org: str):
    return api_auth.ApiPrincipal(
        api_auth.ApiPrincipalKind.ORG_SESSION, subject="agent", org=org)


def _operator():
    return api_auth.ApiPrincipal(
        api_auth.ApiPrincipalKind.OPERATOR_COOKIE, subject="op")


def test_list_designs_scopes_the_catalog_to_the_caller_org():
    with patch.object(design_api, "_design_rows", return_value=_org_rows()), \
         patch.object(design_api, "_thumbnail_url", lambda rev_id: ""):
        # An anchore agent sees ONLY anchore designs — and the summary counts
        # are computed over that visible set, so no cross-org total leaks.
        resp = asyncio.run(design_api.list_designs(_req(_org_session("anchore"))))
        data = json.loads(resp.body)
        assert {d["design_id"] for d in data["designs"]} == {"series-anc"}
        assert data["summary"]["series"] == 1

        # The operator (global authority) sees both orgs' designs.
        resp2 = asyncio.run(design_api.list_designs(_req(_operator())))
        data2 = json.loads(resp2.body)
        assert {d["design_id"] for d in data2["designs"]} == {
            "series-auto", "series-anc"}
        assert data2["summary"]["series"] == 2


def test_get_design_series_hides_a_cross_org_design_as_404():
    with patch.object(design_api, "_design_rows", return_value=_org_rows()), \
         patch.object(design_api, "_thumbnail_url", lambda rev_id: ""):
        # An anchore agent asking for the autonomy design gets the same 404 as a
        # nonexistent one — byte-indistinguishable, no cross-org existence leak.
        cross = asyncio.run(design_api.get_design_series(
            _req(_org_session("anchore"), path_params={"design_id": "series-auto"})))
        assert cross.status_code == 404

        # Its own org's design resolves.
        own = asyncio.run(design_api.get_design_series(
            _req(_org_session("anchore"), path_params={"design_id": "series-anc"})))
        assert own.status_code == 200
        assert json.loads(own.body)["design_id"] == "series-anc"

        # The operator sees the autonomy design.
        op = asyncio.run(design_api.get_design_series(
            _req(_operator(), path_params={"design_id": "series-auto"})))
        assert op.status_code == 200


def test_catalog_row_is_shared_when_a_grant_reaches_any_revision():
    rows = _rows()
    with patch.object(design_api, "_shared_ids_for_org", side_effect=lambda org: {"rev-a1"} if org == "autonomy" else set()):
        series = {s["design_id"]: s for s in design_api._series_from_rows(rows)}
    assert series["series-a"]["shared"] is True
    assert all(s["shared"] is False for key, s in series.items() if key != "series-a")


def test_shared_route_lists_grants_that_target_designs_not_on_this_machine():
    from tools.dashboard import design_shares

    grants = [
        {"target_uuid": "rev-a1", "target_type": "design", "token": "local", "url": "https://l/local"},
        {"target_uuid": "remote-design", "target_type": "present", "token": "remote", "url": "https://l/remote",
         "label": "Teammate's deck", "issued_at": "2026-09-06T00:00:00Z", "expires_at": None},
    ]
    with patch.object(design_api, "_design_rows", return_value=_rows()), \
         patch.object(design_api.api_auth, "organization_scope_from_request", return_value=None), \
         patch.object(design_shares, "active_design_grants", side_effect=lambda org, now=None: grants if org == "autonomy" else []):
        resp = _client().get("/api/design-studio/shared")
    assert resp.status_code == 200
    shares = resp.json()["shares"]
    assert [s["token"] for s in shares] == ["remote"]
    assert shares[0]["org"] == "autonomy"
    assert shares[0]["label"] == "Teammate's deck"


def test_thumbnail_artifact_upload_stores_and_broadcasts(tmp_path):
    import base64
    import io

    from PIL import Image

    from tools import data_paths

    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (1, 2, 3)).save(buf, "JPEG")
    payload = {
        "files": {"thumbnail.jpg": base64.b64encode(buf.getvalue()).decode("ascii")},
        "meta": {"form_factor": "both", "design_id": "series-a", "style": "overlay"},
    }
    (tmp_path / "experiments").mkdir()
    with patch.object(design_api, "_design_rows", return_value=_rows()), \
         patch.object(design_api.api_auth, "require_authenticated_api_caller", return_value=None), \
         patch.object(design_api.api_auth, "caller_org_scope_hides", return_value=False), \
         patch.object(data_paths, "DATA_ROOT", tmp_path):
        resp = _client().put("/api/design-studio/revisions/rev-a2/thumbnail-artifacts", json=payload)
        assert resp.status_code == 200, resp.text
        assert resp.json()["meta"]["form_factor"] == "both"
        assert (tmp_path / "experiments" / "rev-a2" / "thumbnail.jpg").is_file()

        bad = _client().put("/api/design-studio/revisions/rev-a2/thumbnail-artifacts",
                            json={"files": {"thumbnail.jpg": "!!!"}, "meta": {"form_factor": "both"}})
        assert bad.status_code == 400
        missing = _client().put("/api/design-studio/revisions/nope/thumbnail-artifacts", json=payload)
        assert missing.status_code == 404


def test_thumbnail_artifact_upload_requires_an_authenticated_caller():
    from starlette.responses import JSONResponse

    with patch.object(design_api.api_auth, "require_authenticated_api_caller",
                      return_value=JSONResponse({"error": "authentication required"}, status_code=401)):
        resp = _client().put("/api/design-studio/revisions/rev-a2/thumbnail-artifacts", json={})
    assert resp.status_code == 401
