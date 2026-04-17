"""Tests for GET /api/projects — workspace registry endpoint."""

from __future__ import annotations

from starlette.testclient import TestClient


def test_api_projects_lists_all_workspaces(test_app):
    """Endpoint returns the full project registry loaded from projects.yaml."""
    with TestClient(test_app) as client:
        r = client.get("/api/projects")
        assert r.status_code == 200
        body = r.json()

    assert "projects" in body
    by_id = {p["id"]: p for p in body["projects"]}
    assert set(by_id) == {"autonomy", "enterprise", "enterprise-ng"}

    for entry in body["projects"]:
        assert set(entry) >= {"id", "name", "description", "graph_project", "dind"}

    assert by_id["autonomy"]["dind"] is False
    assert by_id["autonomy"]["graph_project"] == "autonomy"
    assert by_id["enterprise"]["dind"] is True
    assert by_id["enterprise"]["graph_project"] == "anchore"
    assert by_id["enterprise-ng"]["dind"] is True
    assert by_id["enterprise-ng"]["graph_project"] == "anchore"
