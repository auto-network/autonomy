from __future__ import annotations

import hashlib
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard.dao import auth_db, dashboard_db
from tools.dashboard.dao import commit_workflow_db as cdb
from tools.dashboard.dao import trusted_git_object_store as snapshot_dao
from tools.dashboard.commit_broker.keys import InMemoryBrokerKeyStore, register_verification_key
from tools.dashboard.plugins.commit_api.entrypoints import api as commit_api
from tools.dashboard.services.trusted_git_object_store import ContentAddressedStore
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
    monkeypatch.setattr(cdb, "DB_PATH", db_path)
    cdb.init_db(db_path)
    yield db_path


@pytest.fixture
def trusted_store_env(tmp_path, monkeypatch):
    db_path = tmp_path / "trusted_git_object_store.db"
    root_path = tmp_path / "trusted_git_object_store"
    monkeypatch.setenv("TRUSTED_GIT_OBJECT_STORE_DB", str(db_path))
    monkeypatch.setenv("TRUSTED_GIT_OBJECT_STORE_ROOT", str(root_path))
    monkeypatch.setattr(snapshot_dao, "DB_PATH", db_path)
    yield db_path, root_path


@pytest.fixture
def broker_keystore(monkeypatch):
    store = InMemoryBrokerKeyStore()
    monkeypatch.setattr(commit_api, "_BROKER_VERIFICATION_KEY_STORE", store)
    return store


@pytest.fixture
def dashboard_db_env(tmp_path, monkeypatch):
    db_path = tmp_path / "dashboard.db"
    monkeypatch.setenv("DASHBOARD_DB", str(db_path))
    monkeypatch.setattr(dashboard_db, "_DB_PATH", db_path)
    monkeypatch.setattr(dashboard_db, "_conn", None)
    dashboard_db.init_db(db_path)
    yield db_path


@pytest.fixture
def auth_db_env(tmp_path, monkeypatch):
    db_path = tmp_path / "auth.db"
    monkeypatch.setattr(auth_db, "_DB_PATH", db_path)
    monkeypatch.setattr(auth_db, "_conn", None)
    auth_db.init_db(db_path)
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


def _seed_session(tmux_name: str, project: str):
    dashboard_db.upsert_session(
        tmux_name,
        "host",
        project,
        is_live=True,
        harness="claude",
    )


def _seed_token(tmux_name: str, raw_token: str):
    auth_db.insert_token(hashlib.sha256(raw_token.encode("utf-8")).hexdigest(), tmux_name)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)


def _git_out(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True).stdout.decode().strip()


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "README.md").write_text("hello\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial")
    return repo


def _ssh_keypair(tmp_path: Path) -> tuple[Path, bytes]:
    key_path = tmp_path / "ssh_signing_key"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(key_path)],
        capture_output=True,
        check=True,
    )
    return key_path, (key_path.with_suffix(".pub")).read_bytes()


def _ssh_sign(key_path: Path, payload: bytes, tmp_path: Path) -> str:
    msg_path = tmp_path / "payload.txt"
    msg_path.write_bytes(payload)
    subprocess.run(
        ["ssh-keygen", "-Y", "sign", "-f", str(key_path), "-n", "git", str(msg_path)],
        capture_output=True,
        check=True,
    )
    return (msg_path.parent / f"{msg_path.name}.sig").read_text()


def _proposal_payload(*, repo: Path, session_name: str, workspace_id: str, repo_slug: str) -> dict:
    head_sha = _git_out(repo, "rev-parse", "HEAD")
    tree_sha = _git_out(repo, "rev-parse", "HEAD^{tree}")
    return {
        "idempotency_key": "idem-propose",
        "scope": {
            "workspace_id": workspace_id,
            "repo_slug": repo_slug,
            "repo_name_alias": None,
            "session_name": session_name,
            "worktree_path": str(repo),
            "branch": f"session/{session_name}",
            "target_branch": "main",
        },
        "target": {
            "target_branch": "main",
            "ref_strategy": "current_branch",
            "intended_visibility": "workspace_shared",
            "review_target": None,
        },
        "content": {
            "head_sha": head_sha,
            "tree_sha": tree_sha,
            "patch_id": "patch-1",
            "content_fingerprint": "fingerprint-1",
        },
        "drift_token": {
            "head_sha": head_sha,
            "tree_sha": tree_sha,
            "index_sha": tree_sha,
            "worktree_status_hash": hashlib.sha256(b"").hexdigest(),
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


def _commit_flow_setup(monkeypatch, tmp_path, *, repo_slug: str = "autonomy/autonomy", workspace_id: str = "autonomy"):
    repo = _init_repo(tmp_path)
    session_name = "sess-1"
    _seed_policy(ops.CALLER_ORG, workspace_id, "autonomy.direct-master")
    _seed_session(session_name, workspace_id)
    _seed_token(session_name, "tok-1")
    fake_ws = SimpleNamespace(id=workspace_id, graph_project=workspace_id)
    monkeypatch.setattr(commit_api, "get_workspace", lambda wid: fake_ws)
    monkeypatch.setattr(commit_api, "resolve_capabilities", lambda workspace_id, org=None: [])
    worktree_row = SimpleNamespace(
        session_name=session_name,
        repo_name="autonomy",
        managed_clone=repo,
        branch=f"session/{session_name}",
        worktree_path=repo,
    )
    monkeypatch.setattr(commit_api.worktree_monitor, "get_all", lambda: [worktree_row])
    monkeypatch.setattr(commit_api, "derive_repo_slug", lambda _path: repo_slug)
    return repo, session_name, workspace_id, repo_slug


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


def test_propose_creates_workflow_and_replays_idempotently(
    graph_db_env,
    dashboard_db_env,
    auth_db_env,
    workflow_db_env,
    client,
    monkeypatch,
):
    _enable_plugin(monkeypatch)
    _seed_policy(ops.CALLER_ORG, "enterprise-ng", "enterprise.signed-pr-no-issue")
    fake_ws = SimpleNamespace(id="enterprise-ng", graph_project="autonomy")
    monkeypatch.setattr(commit_api, "get_workspace", lambda wid: fake_ws)
    monkeypatch.setattr(commit_api, "resolve_capabilities", lambda workspace_id, org=None: [])
    _seed_session("sess-1", "enterprise-ng")
    _seed_token("sess-1", "tok-1")
    fake_row = SimpleNamespace(
        session_name="sess-1",
        repo_name="autonomy",
        managed_clone=Path("/workspace/repo"),
        branch="session/sess-1",
        worktree_path=Path("/workspace/repo"),
    )
    monkeypatch.setattr(commit_api.worktree_monitor, "get_all", lambda: [fake_row])
    monkeypatch.setattr(commit_api, "derive_repo_slug", lambda _path: "autonomy/autonomy")

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

    first = client.post(
        "/api/capabilities/commit/v1/proposals",
        json=payload,
        headers={"Authorization": "Bearer tok-1"},
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["workflow"]["repo_slug"] == "autonomy/autonomy"
    assert body["workflow"]["status"] == "awaiting_approval"
    assert body["duplicate_of_workflow_id"] is None

    replay = client.post(
        "/api/capabilities/commit/v1/proposals",
        json=payload,
        headers={"Authorization": "Bearer tok-1"},
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["workflow"]["workflow_id"] == body["workflow"]["workflow_id"]

    second = dict(payload)
    second["idempotency_key"] = "idem-2"
    dup = client.post(
        "/api/capabilities/commit/v1/proposals",
        json=second,
        headers={"Authorization": "Bearer tok-1"},
    )
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


def test_propose_requires_bearer_session_token(
    graph_db_env,
    dashboard_db_env,
    auth_db_env,
    workflow_db_env,
    client,
    monkeypatch,
):
    _enable_plugin(monkeypatch)
    _seed_session("sess-1", "enterprise-ng")
    fake_ws = SimpleNamespace(id="enterprise-ng", graph_project="autonomy")
    monkeypatch.setattr(commit_api, "get_workspace", lambda wid: fake_ws)
    monkeypatch.setattr(commit_api.worktree_monitor, "get_all", lambda: [])

    resp = client.post("/api/capabilities/commit/v1/proposals", json={
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
    })
    assert resp.status_code == 401
    assert resp.json()["code"] == "unauthenticated"


def test_propose_rejects_foreign_workspace_scope(
    graph_db_env,
    dashboard_db_env,
    auth_db_env,
    workflow_db_env,
    client,
    monkeypatch,
):
    _enable_plugin(monkeypatch)
    _seed_session("sess-1", "enterprise-ng")
    _seed_token("sess-1", "tok-1")
    fake_ws = SimpleNamespace(id="enterprise-ng", graph_project="autonomy")
    monkeypatch.setattr(commit_api, "get_workspace", lambda wid: fake_ws)
    monkeypatch.setattr(commit_api, "resolve_capabilities", lambda workspace_id, org=None: [])
    fake_row = SimpleNamespace(
        session_name="sess-1",
        repo_name="autonomy",
        managed_clone=Path("/workspace/repo"),
        branch="session/sess-1",
        worktree_path=Path("/workspace/repo"),
    )
    monkeypatch.setattr(commit_api.worktree_monitor, "get_all", lambda: [fake_row])
    monkeypatch.setattr(commit_api, "derive_repo_slug", lambda _path: "autonomy/autonomy")

    payload = {
        "idempotency_key": "idem-1",
        "scope": {
            "workspace_id": "foreign-workspace",
            "repo_slug": "foreign/foreign",
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

    conn = cdb._get_conn()
    before = conn.execute("SELECT COUNT(*) AS n FROM commit_workflow_events").fetchone()["n"]
    resp = client.post(
        "/api/capabilities/commit/v1/proposals",
        json=payload,
        headers={"Authorization": "Bearer tok-1"},
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "scope_mismatch"
    try:
        rows = conn.execute("SELECT COUNT(*) AS n FROM commit_workflow_events").fetchone()
        assert rows["n"] == before
    finally:
        conn.close()


def test_commit_create_request_signature_attach_and_publish_round_trip(
    graph_db_env,
    dashboard_db_env,
    auth_db_env,
    workflow_db_env,
    trusted_store_env,
    broker_keystore,
    client,
    monkeypatch,
):
    _enable_plugin(monkeypatch)
    repo, session_name, workspace_id, repo_slug = _commit_flow_setup(monkeypatch, Path(graph_db_env).parent)
    key_path, pubkey = _ssh_keypair(Path(graph_db_env).parent)
    register_verification_key(
        operator_id="op-1",
        signing_kind="ssh",
        public_material=pubkey,
        keystore=broker_keystore,
    )

    proposal_payload = _proposal_payload(repo=repo, session_name=session_name, workspace_id=workspace_id, repo_slug=repo_slug)

    propose = client.post(
        "/api/capabilities/commit/v1/proposals",
        json=proposal_payload,
        headers={"Authorization": "Bearer tok-1"},
    )
    assert propose.status_code == 200, propose.text
    workflow_id = propose.json()["workflow"]["workflow_id"]

    create = client.post(
        f"/api/capabilities/commit/v1/workflows/{workflow_id}/commit",
        json={
            "idempotency_key": "idem-create",
            "workflow_id": workflow_id,
            "drift_token": _proposal_payload(
                repo=repo,
                session_name=session_name,
                workspace_id=workspace_id,
                repo_slug=repo_slug,
            )["drift_token"],
            "create_mode": "snapshot_for_signature",
        },
        headers={"Authorization": "Bearer tok-1"},
    )
    assert create.status_code == 200, create.text
    create_body = create.json()
    assert create_body["next_action"]["action"] == "request_signature"
    assert create_body["trusted_object_store_ref"]
    assert create_body["canonical_payload_hash"]
    assert create_body["signing"] is None

    snapshot_conn = snapshot_dao._get_conn()
    try:
        snapshot_dao.init_schema_on_connection(snapshot_conn)
        snapshot = snapshot_dao.get_snapshot(snapshot_conn, create_body["trusted_object_store_ref"])
        assert snapshot is not None
        unsigned_payload = ContentAddressedStore(trusted_store_env[1]).get(snapshot["canonical_preview_sha256"])
    finally:
        snapshot_conn.close()
    armored_signature = _ssh_sign(key_path, unsigned_payload, Path(graph_db_env).parent)

    request_signature = client.post(
        f"/api/capabilities/commit/v1/workflows/{workflow_id}/signature-request",
        json={
            "idempotency_key": "idem-request-signature",
            "workflow_id": workflow_id,
            "signing_method": "ssh",
            "signer_policy_version": "policy-v1",
            "requested_operator_id": "op-1",
        },
        headers={"Authorization": "Bearer tok-1"},
    )
    assert request_signature.status_code == 200, request_signature.text
    request_body = request_signature.json()
    signing_request_id = request_body["signing"]["signing_request_id"]
    canonical_payload_hash = request_body["signing"]["canonical_payload_hash"]

    attach = client.post(
        f"/api/capabilities/commit/v1/signing-requests/{signing_request_id}/attach",
        json={
            "idempotency_key": "idem-attach",
            "signing_request_id": signing_request_id,
            "signature_ref": "sig-1",
            "armored_signature": armored_signature,
            "signed_commit_object_ref": None,
            "local_signer_attestation": {
                "device_id": "device-1",
                "request_nonce": "nonce-1",
                "canonical_payload_hash": canonical_payload_hash,
                "displayed_payload_hash": canonical_payload_hash,
                "signed_at": "2026-07-06T19:00:00Z",
            },
        },
        headers={"Authorization": "Bearer tok-1"},
    )
    assert attach.status_code == 200, attach.text
    attach_body = attach.json()
    signed_commit_sha = attach_body["signed_commit_sha"]
    assert attach_body["workflow"]["status"] == "signed"
    assert attach_body["next_action"]["action"] == "publish"

    conn = cdb._get_conn()
    try:
        repo_head_sha = _git_out(repo, "rev-parse", "HEAD")
        conn.execute(
            """
            INSERT INTO commit_workflow_approvals (
                approval_id, workflow_id, repo_slug, approval_type, status,
                requested_by_session, operator_id, requested_at, decided_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "approval-1",
                workflow_id,
                repo_slug,
                "force_with_lease",
                "approved",
                session_name,
                "op-1",
                1.0,
                2.0,
                json.dumps({"constraints": {"expected_ref_sha": repo_head_sha}}),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    publish = client.post(
        f"/api/capabilities/commit/v1/workflows/{workflow_id}/publish",
        json={
            "idempotency_key": "idem-publish",
            "workflow_id": workflow_id,
            "ref_update_intent": {
                "provider": "github",
                "repo_slug": repo_slug,
                "ref": "refs/heads/main",
                "operation": "fast_forward_existing_ref",
                "expected_old_sha": None,
                "new_sha": signed_commit_sha,
            },
            "publish_mode": "workspace_shared",
        },
        headers={"Authorization": "Bearer tok-1"},
    )
    assert publish.status_code == 200, publish.text
    publish_body = publish.json()
    assert publish_body["workflow"]["status"] == "published"
    assert publish_body["ref_update_result"]["expected_old_sha"] == repo_head_sha
    assert publish_body["pushed_ref"] == "refs/heads/main"
    assert publish_body["provider_url"] == "https://github.com/autonomy/autonomy"


def test_attach_signature_rejects_bogus_signature(
    graph_db_env,
    dashboard_db_env,
    auth_db_env,
    workflow_db_env,
    trusted_store_env,
    broker_keystore,
    client,
    monkeypatch,
):
    _enable_plugin(monkeypatch)
    repo, session_name, workspace_id, repo_slug = _commit_flow_setup(monkeypatch, Path(graph_db_env).parent)
    key_path, pubkey = _ssh_keypair(Path(graph_db_env).parent)
    register_verification_key(
        operator_id="op-1",
        signing_kind="ssh",
        public_material=pubkey,
        keystore=broker_keystore,
    )
    proposal_payload = _proposal_payload(repo=repo, session_name=session_name, workspace_id=workspace_id, repo_slug=repo_slug)

    propose = client.post(
        "/api/capabilities/commit/v1/proposals",
        json=proposal_payload,
        headers={"Authorization": "Bearer tok-1"},
    )
    assert propose.status_code == 200, propose.text
    workflow_id = propose.json()["workflow"]["workflow_id"]

    create = client.post(
        f"/api/capabilities/commit/v1/workflows/{workflow_id}/commit",
        json={
            "idempotency_key": "idem-create",
            "workflow_id": workflow_id,
            "drift_token": proposal_payload["drift_token"],
            "create_mode": "snapshot_for_signature",
        },
        headers={"Authorization": "Bearer tok-1"},
    )
    assert create.status_code == 200, create.text

    request_signature = client.post(
        f"/api/capabilities/commit/v1/workflows/{workflow_id}/signature-request",
        json={
            "idempotency_key": "idem-request-signature",
            "workflow_id": workflow_id,
            "signing_method": "ssh",
            "signer_policy_version": "policy-v1",
            "requested_operator_id": "op-1",
        },
        headers={"Authorization": "Bearer tok-1"},
    )
    assert request_signature.status_code == 200, request_signature.text
    request_body = request_signature.json()

    attach = client.post(
        f"/api/capabilities/commit/v1/signing-requests/{request_body['signing']['signing_request_id']}/attach",
        json={
            "idempotency_key": "idem-attach",
            "signing_request_id": request_body["signing"]["signing_request_id"],
            "signature_ref": "sig-1",
            "armored_signature": "-----BEGIN SSH SIGNATURE-----\nZm9yZ2Vk\n-----END SSH SIGNATURE-----\n",
            "signed_commit_object_ref": None,
            "local_signer_attestation": {
                "device_id": "device-1",
                "request_nonce": "nonce-1",
                "canonical_payload_hash": request_body["signing"]["canonical_payload_hash"],
                "displayed_payload_hash": request_body["signing"]["canonical_payload_hash"],
                "signed_at": "2026-07-06T19:00:00Z",
            },
        },
        headers={"Authorization": "Bearer tok-1"},
    )
    assert attach.status_code == 422, attach.text
    assert attach.json()["code"] == "signature_verification_failed"


def test_publish_rejects_missing_force_with_lease_approval(
    graph_db_env,
    dashboard_db_env,
    auth_db_env,
    workflow_db_env,
    trusted_store_env,
    broker_keystore,
    client,
    monkeypatch,
):
    _enable_plugin(monkeypatch)
    repo, session_name, workspace_id, repo_slug = _commit_flow_setup(monkeypatch, Path(graph_db_env).parent)
    key_path, pubkey = _ssh_keypair(Path(graph_db_env).parent)
    register_verification_key(
        operator_id="op-1",
        signing_kind="ssh",
        public_material=pubkey,
        keystore=broker_keystore,
    )
    proposal_payload = _proposal_payload(repo=repo, session_name=session_name, workspace_id=workspace_id, repo_slug=repo_slug)

    propose = client.post(
        "/api/capabilities/commit/v1/proposals",
        json=proposal_payload,
        headers={"Authorization": "Bearer tok-1"},
    )
    assert propose.status_code == 200, propose.text
    workflow_id = propose.json()["workflow"]["workflow_id"]

    create = client.post(
        f"/api/capabilities/commit/v1/workflows/{workflow_id}/commit",
        json={
            "idempotency_key": "idem-create",
            "workflow_id": workflow_id,
            "drift_token": proposal_payload["drift_token"],
            "create_mode": "snapshot_for_signature",
        },
        headers={"Authorization": "Bearer tok-1"},
    )
    assert create.status_code == 200, create.text
    create_body = create.json()

    snapshot_conn = snapshot_dao._get_conn()
    try:
        snapshot_dao.init_schema_on_connection(snapshot_conn)
        snapshot = snapshot_dao.get_snapshot(snapshot_conn, create_body["trusted_object_store_ref"])
        assert snapshot is not None
        unsigned_payload = ContentAddressedStore(trusted_store_env[1]).get(snapshot["canonical_preview_sha256"])
    finally:
        snapshot_conn.close()
    armored_signature = _ssh_sign(key_path, unsigned_payload, Path(graph_db_env).parent)

    request_signature = client.post(
        f"/api/capabilities/commit/v1/workflows/{workflow_id}/signature-request",
        json={
            "idempotency_key": "idem-request-signature",
            "workflow_id": workflow_id,
            "signing_method": "ssh",
            "signer_policy_version": "policy-v1",
            "requested_operator_id": "op-1",
        },
        headers={"Authorization": "Bearer tok-1"},
    )
    assert request_signature.status_code == 200, request_signature.text
    request_body = request_signature.json()

    attach = client.post(
        f"/api/capabilities/commit/v1/signing-requests/{request_body['signing']['signing_request_id']}/attach",
        json={
            "idempotency_key": "idem-attach",
            "signing_request_id": request_body["signing"]["signing_request_id"],
            "signature_ref": "sig-1",
            "armored_signature": armored_signature,
            "signed_commit_object_ref": None,
            "local_signer_attestation": {
                "device_id": "device-1",
                "request_nonce": "nonce-1",
                "canonical_payload_hash": request_body["signing"]["canonical_payload_hash"],
                "displayed_payload_hash": request_body["signing"]["canonical_payload_hash"],
                "signed_at": "2026-07-06T19:00:00Z",
            },
        },
        headers={"Authorization": "Bearer tok-1"},
    )
    assert attach.status_code == 200, attach.text

    publish = client.post(
        f"/api/capabilities/commit/v1/workflows/{workflow_id}/publish",
        json={
            "idempotency_key": "idem-publish",
            "workflow_id": workflow_id,
            "ref_update_intent": {
                "provider": "github",
                "repo_slug": repo_slug,
                "ref": "refs/heads/main",
                "operation": "fast_forward_existing_ref",
                "expected_old_sha": None,
                "new_sha": attach.json()["signed_commit_sha"],
            },
            "publish_mode": "workspace_shared",
        },
        headers={"Authorization": "Bearer tok-1"},
    )
    assert publish.status_code == 409, publish.text
    assert publish.json()["code"] == "ref_update_rejected"


def test_commit_create_rejects_foreign_scope_before_writing_events(
    graph_db_env,
    dashboard_db_env,
    auth_db_env,
    workflow_db_env,
    trusted_store_env,
    client,
    monkeypatch,
):
    _enable_plugin(monkeypatch)
    repo, session_name, workspace_id, repo_slug = _commit_flow_setup(monkeypatch, Path(graph_db_env).parent)

    propose = client.post(
        "/api/capabilities/commit/v1/proposals",
        json=_proposal_payload(repo=repo, session_name=session_name, workspace_id=workspace_id, repo_slug=repo_slug),
        headers={"Authorization": "Bearer tok-1"},
    )
    assert propose.status_code == 200, propose.text
    workflow_id = propose.json()["workflow"]["workflow_id"]

    conn = cdb._get_conn()
    try:
        before = conn.execute("SELECT COUNT(*) AS n FROM commit_workflow_events").fetchone()["n"]
    finally:
        conn.close()

    monkeypatch.setattr(commit_api, "derive_repo_slug", lambda _path: "foreign/foreign")
    resp = client.post(
        f"/api/capabilities/commit/v1/workflows/{workflow_id}/commit",
        json={
            "idempotency_key": "idem-create",
            "workflow_id": workflow_id,
            "drift_token": _proposal_payload(
                repo=repo,
                session_name=session_name,
                workspace_id=workspace_id,
                repo_slug=repo_slug,
            )["drift_token"],
            "create_mode": "snapshot_for_signature",
        },
        headers={"Authorization": "Bearer tok-1"},
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "scope_mismatch"

    conn = cdb._get_conn()
    try:
        after = conn.execute("SELECT COUNT(*) AS n FROM commit_workflow_events").fetchone()["n"]
    finally:
        conn.close()
    assert after == before
