"""Tests for GET /api/projects — workspace registry endpoint."""

from __future__ import annotations

from starlette.testclient import TestClient

from agents import workspace_settings
from tools.dashboard import server
from tools.graph import ops


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
        assert set(entry) >= {
            "id",
            "name",
            "description",
            "graph_project",
            "dind",
            "needs_nested_docker",
            "session_runtime",
            "org",
        }

    assert by_id["autonomy"]["dind"] is False
    assert by_id["autonomy"]["session_runtime"] == "standard"
    assert by_id["autonomy"]["graph_project"] == "autonomy"
    assert by_id["enterprise-v5"]["dind"] is True
    assert by_id["enterprise-v5"]["session_runtime"] == "privileged"
    assert by_id["enterprise-v5"]["graph_project"] == "anchore"
    assert by_id["enterprise-ng"]["dind"] is True
    assert by_id["enterprise-ng"]["session_runtime"] == "privileged"
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


def test_local_workspace_api_attaches_repo_to_existing_personal_workspace(
    shipped_settings_orgs, test_app, tmp_path, monkeypatch,
):
    repo = tmp_path / "workspace-repos" / "personal" / "idea-board"
    ops.add_setting(
        workspace_settings.WORKSPACE_SET_ID,
        workspace_settings.WORKSPACE_REVISION,
        "idea-board",
        {
            "name": "Idea Board",
            "description": "Personal ideas",
            "image": "autonomy-session-platform",
            "harness": "codex",
            "working_dir": "/workspace/repo",
            "repos": [{
                "local_path": str(repo),
                "mount": "/workspace/repo",
                "writable": True,
            }],
        },
        state="raw",
        org="personal",
    )
    monkeypatch.setattr(
        server,
        "local_workspace_repo_path",
        lambda org, workspace_id: repo,
    )
    monkeypatch.setattr(
        server,
        "ensure_local_workspace_repository",
        lambda org, workspace_id, *, name=None: (repo, True),
    )

    with TestClient(test_app) as client:
        response = client.post(
            "/api/workspaces/local",
            headers={"X-Graph-Org": "personal"},
            json={"id": "idea-board", "name": "Idea Board"},
        )

    assert response.status_code == 201
    assert response.json() == {
        "id": "idea-board",
        "org": "personal",
        "setting_id": response.json()["setting_id"],
        "repo": str(repo),
        "mount": "/workspace/idea-board",
        "repo_created": True,
        "workspace_created": False,
        "workspace_overridden": True,
    }
    member = next(
        m for m in ops.read_set(
            workspace_settings.WORKSPACE_SET_ID, org="personal", peers=[],
        ).members
        if m.key == "idea-board"
    )
    assert member.payload["working_dir"] == "/workspace/idea-board"
    assert member.payload["repos"] == [{
        "local_path": str(repo),
        "mount": "/workspace/idea-board",
        "writable": True,
    }]


def test_local_workspace_api_requires_explicit_org(test_app):
    with TestClient(test_app) as client:
        response = client.post(
            "/api/workspaces/local",
            json={"id": "idea-board"},
        )
    assert response.status_code == 400
    assert response.json()["error"] == "X-Graph-Org header is required"


def test_local_workspace_api_creates_new_workspace_in_one_call(
    shipped_settings_orgs, test_app, tmp_path, monkeypatch,
):
    repo = tmp_path / "workspace-repos" / "personal" / "field-notes"
    monkeypatch.setattr(
        server, "local_workspace_repo_path", lambda org, workspace_id: repo,
    )
    monkeypatch.setattr(
        server,
        "ensure_local_workspace_repository",
        lambda org, workspace_id, *, name=None: (repo, True),
    )

    with TestClient(test_app) as client:
        response = client.post(
            "/api/workspaces/local",
            headers={"X-Graph-Org": "personal"},
            json={
                "id": "field-notes",
                "name": "Field Notes",
                "description": "A durable personal notebook",
                "harness": "codex",
                "model": "gpt-5.6-sol",
            },
        )

    assert response.status_code == 201
    assert response.json()["workspace_created"] is True
    member = next(
        m for m in ops.read_set(
            workspace_settings.WORKSPACE_SET_ID, org="personal", peers=[],
        ).members
        if m.key == "field-notes"
    )
    assert member.payload["name"] == "Field Notes"
    assert member.payload["model"] == "gpt-5.6-sol"
    assert member.payload["working_dir"] == "/workspace/field-notes"
    assert member.payload["repos"][0]["local_path"] == str(repo)
