"""Unit tests for the Design Studio plugin catalog API."""
from __future__ import annotations

from unittest.mock import patch

from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard.plugins.design_studio.entrypoints import api as design_api


def _client() -> TestClient:
    app = Starlette(routes=design_api.routes)
    return TestClient(app)


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
    with patch.object(design_api, "_design_rows", return_value=_rows()):
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


def test_list_designs_filters_by_status_and_query():
    with patch.object(design_api, "_design_rows", return_value=_rows()):
        resp = _client().get("/api/design-studio/designs?status=pending&q=refined")

    assert resp.status_code == 200
    data = resp.json()
    assert data["filtered_count"] == 1
    assert data["designs"][0]["design_id"] == "series-a"


def test_get_design_series_returns_revision_timeline():
    with patch.object(design_api, "_design_rows", return_value=_rows()):
        resp = _client().get("/api/design-studio/designs/series-a")

    assert resp.status_code == 200
    data = resp.json()
    assert data["design_id"] == "series-a"
    assert [row["id"] for row in data["revisions"]] == ["rev-a1", "rev-a2"]

