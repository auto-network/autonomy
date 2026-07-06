from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard.dao import commit_workflow_db as cdb
from tools.dashboard.plugins.commit_api.entrypoints import api as commit_api
from tools.graph import ops


@pytest.fixture
def graph_db_env(tmp_path, monkeypatch):
    db_path = tmp_path / "graph.db"
    monkeypatch.setenv("GRAPH_DB", str(db_path))
    monkeypatch.delenv("GRAPH_API", raising=False)
    yield db_path


@pytest.fixture
def workflow_db_env(tmp_path, monkeypatch):
    db_path = tmp_path / "commit_workflow.db"
    monkeypatch.setenv("COMMIT_WORKFLOW_DB", str(db_path))
    cdb.init_db(db_path)
    yield db_path


@pytest.fixture
def client():
    app = Starlette(routes=commit_api.routes)
    return TestClient(app)


def _enable_plugin(monkeypatch):
    monkeypatch.setattr(commit_api, "_plugin_enabled", lambda: True)


def _seed_policy(org: str, workspace_id: str, profile: str):
    ops.upsert_by_key(
        "autonomy.commit.policy",
        1,
        f"workspace:{workspace_id}",
        {
            "workspace_id": workspace_id,
            "applies_to": "workspace",
            "profile": profile,
            "override_mode": "none",
        },
        org=org,
        state="canonical",
    )


def test_resolve_policy_and_describe_policy_round_trip(graph_db_env, client, monkeypatch):
    _enable_plugin(monkeypatch)
    _seed_policy(ops.CALLER_ORG, "autonomy", "autonomy.direct-master")
    fake_ws = SimpleNamespace(id="autonomy", graph_project="autonomy")
    monkeypatch.setattr(commit_api, "get_workspace", lambda wid: fake_ws)
    monkeypatch.setattr(commit_api, "resolve_capabilities", lambda workspace_id, org=None: [])

    resp = client.post(
        "/api/capabilities/commit/v1/resolve-policy",
        json={
            "scope": {
                "workspace_id": "autonomy",
                "repo_slug": "autonomy/autonomy",
                "repo_name_alias": None,
                "session_name": "sess-1",
                "worktree_path": None,
                "branch": None,
                "target_branch": "main",
            },
            "intent": {
                "desired_visibility": "workspace_shared",
                "audience": ["operator"],
                "review_target": None,
                "push_intent": None,
            },
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["canonical_scope"]["workspace_id"] == "autonomy"
    assert body["plan"]["profile"] == "autonomy.direct-master"
    assert any(step["action"] == "create" for step in body["required_steps"])

    resp = client.get(
        "/api/capabilities/commit/v1/policy/describe",
        params={
            "workspace_id": "autonomy",
            "repo_slug": "autonomy/autonomy",
            "repo_name": "autonomy",
            "session_name": "sess-1",
            "target_branch": "main",
            "format": "text",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["plan"]["profile"] == "autonomy.direct-master"
    assert "Commit policy: autonomy.direct-master." in body["primer_text"]
    assert body["source_settings_keys"] == ["workspace:autonomy"]


def test_describe_policy_missing_named_workspace_fails_loudly(graph_db_env, client, monkeypatch):
    _enable_plugin(monkeypatch)
    monkeypatch.setattr(commit_api, "get_workspace", lambda wid: (_ for _ in ()).throw(KeyError(wid)))

    resp = client.get(
        "/api/capabilities/commit/v1/policy/describe",
        params={"workspace_id": "missing", "format": "text"},
    )
    assert resp.status_code == 404
    assert "no workspace 'missing' found in organization" in resp.json()["message"]


def test_propose_creates_workflow_and_replays_idempotently(graph_db_env, workflow_db_env, client, monkeypatch):
    _enable_plugin(monkeypatch)
    _seed_policy(ops.CALLER_ORG, "enterprise-ng", "enterprise.signed-pr-no-issue")
    fake_ws = SimpleNamespace(id="enterprise-ng", graph_project="autonomy")
    monkeypatch.setattr(commit_api, "get_workspace", lambda wid: fake_ws)
    monkeypatch.setattr(commit_api, "resolve_capabilities", lambda workspace_id, org=None: [])

    payload = {
        "idempotency_key": "idem-1",
        "scope": {
            "workspace_id": "enterprise-ng",
            "repo_slug": "autonomy/autonomy",
            "repo_name_alias": None,
            "session_name": "sess-1",
            "worktree_path": "/workspace/repo",
            "branch": "session/sess-1",
            "target_branch": "main",
        },
        "target": {
            "target_branch": "main",
            "ref_strategy": "current_branch",
            "intended_visibility": "workspace_shared",
            "review_target": None,
        },
        "content": {
            "head_sha": "head-1",
            "tree_sha": "tree-1",
            "patch_id": "patch-1",
            "content_fingerprint": "fingerprint-1",
        },
        "drift_token": {
            "head_sha": "head-1",
            "tree_sha": "tree-1",
            "index_sha": "index-1",
            "worktree_status_hash": "status-1",
            "generated_at": "2026-07-06T19:00:00Z",
        },
        "message": {
            "subject": "subject",
            "body": "body",
            "trailers": {},
        },
        "author": {
            "name": "Ada",
            "email": "ada@example.com",
            "timestamp": None,
            "timezone": None,
        },
        "committer": {
            "name": "Ada",
            "email": "ada@example.com",
            "timestamp": None,
            "timezone": None,
        },
        "signoff_present": True,
        "issue_refs": ["#1"],
        "push_pr_intent": None,
        "client_observed_policy_version": None,
    }

    first = client.post("/api/capabilities/commit/v1/proposals", json=payload)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["workflow"]["repo_slug"] == "autonomy/autonomy"
    assert body["workflow"]["status"] == "awaiting_approval"
    assert body["duplicate_of_workflow_id"] is None

    replay = client.post("/api/capabilities/commit/v1/proposals", json=payload)
    assert replay.status_code == 200, replay.text
    assert replay.json()["workflow"]["workflow_id"] == body["workflow"]["workflow_id"]

    second = dict(payload)
    second["idempotency_key"] = "idem-2"
    dup = client.post("/api/capabilities/commit/v1/proposals", json=second)
    assert dup.status_code == 200, dup.text
    dup_body = dup.json()
    assert dup_body["workflow"]["status"] == "duplicate_active"
    assert dup_body["duplicate_of_workflow_id"] == body["workflow"]["workflow_id"]

    conn = cdb._get_conn()
    try:
        state = conn.execute(
            "SELECT status, repo_slug, content_fingerprint FROM commit_workflow_states WHERE workflow_id = ?",
            (body["workflow"]["workflow_id"],),
        ).fetchone()
        assert state["status"] == "awaiting_approval"
        assert state["repo_slug"] == "autonomy/autonomy"
        assert state["content_fingerprint"] == "fingerprint-1"
    finally:
        conn.close()
