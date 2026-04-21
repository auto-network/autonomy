"""Tests for GET /api/projects — workspace registry endpoint."""

from __future__ import annotations

from starlette.testclient import TestClient


def test_api_projects_lists_all_workspaces(shipped_settings_orgs, test_app):
    """Endpoint returns the full workspace registry loaded from Settings."""
    with TestClient(test_app) as client:
        r = client.get("/api/projects")
        assert r.status_code == 200
        body = r.json()

    assert "projects" in body
    by_id = {p["id"]: p for p in body["projects"]}
    assert set(by_id) == {"autonomy", "enterprise-v5", "enterprise-ng"}

    for entry in body["projects"]:
        assert set(entry) >= {"id", "name", "description", "graph_project", "dind", "org"}

    assert by_id["autonomy"]["dind"] is False
    assert by_id["autonomy"]["graph_project"] == "autonomy"
    assert by_id["enterprise-v5"]["dind"] is True
    assert by_id["enterprise-v5"]["graph_project"] == "anchore"
    assert by_id["enterprise-ng"]["dind"] is True
    assert by_id["enterprise-ng"]["graph_project"] == "anchore"


def test_api_projects_includes_resolved_org_identity(shipped_settings_orgs, test_app):
    """Each project entry carries a resolved ``org`` identity dict so the
    frontend can render the workspace picker header without a second
    round-trip. Shape: ``{slug, name, color, favicon, initial, resolved}``."""
    with TestClient(test_app) as client:
        r = client.get("/api/projects")
        assert r.status_code == 200
        body = r.json()

    by_id = {p["id"]: p for p in body["projects"]}
    anchore_org = by_id["enterprise-ng"]["org"]
    assert anchore_org["slug"] == "anchore"
    # Fields required for the picker + session-card glyphs.
    assert set(anchore_org) >= {"slug", "name", "color", "favicon", "initial", "resolved"}
    assert anchore_org["resolved"] is True

    autonomy_org = by_id["autonomy"]["org"]
    assert autonomy_org["slug"] == "autonomy"
    assert autonomy_org["resolved"] is True
