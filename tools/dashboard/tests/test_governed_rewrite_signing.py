"""Tests for D4-17 — fire a fresh signing request through the standard
signer path (DN4 §3 step 5, graph note ``175ff7fc-850``).

Builds a governed-rewrite workflow through the real D4-8/9/10/17
functions, then invokes the UNMODIFIED ``commit.request_signature`` HTTP
handler against it -- proving no rewrite-specific branch exists in the
signing path, and that N3 holds (the D4-9 source snapshot's own
canonical_preview_sha256 is never mutated; a rewrite is two snapshots,
never one edited in place).
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard.dao import auth_db, commit_workflow_db as cdb, dashboard_db
from tools.dashboard.dao import trusted_git_object_store as snapshot_dao
from tools.dashboard.plugins.commit_api.entrypoints import api as commit_api
from tools.dashboard.services.trusted_git_object_store import ContentAddressedStore
from tools.graph import ops

from tools.dashboard.commit_compliance import (
    AuthorshipStatus,
    ComplianceReport,
    SignatureStatus,
    SignOffStatus,
)
from tools.dashboard.governed_rewrite import (
    construct_corrected_metadata,
    create_governed_rewrite_workflow,
    prepare_rewrite_for_signing,
    snapshot_original_commit,
    GitIdentityLine,
)


# ── fixtures (same shape as tools/dashboard/plugins/commit_api/tests/test_api.py) ─


@pytest.fixture
def graph_db_env(tmp_path, monkeypatch):
    db_path = tmp_path / "graph.db"
    monkeypatch.setenv("GRAPH_DB", str(db_path))
    monkeypatch.delenv("GRAPH_API", raising=False)
    yield db_path


@pytest.fixture
def workflow_db_env(tmp_path, monkeypatch):
    db_path = tmp_path / "commit_workflow.db"
    monkeypatch.setattr(cdb, "DB_PATH", db_path)
    cdb.init_db(db_path)
    yield db_path


@pytest.fixture
def trusted_store_env(tmp_path, monkeypatch):
    db_path = tmp_path / "trusted_git_object_store.db"
    root_path = tmp_path / "trusted_git_object_store"
    monkeypatch.setattr(snapshot_dao, "DB_PATH", db_path)
    yield db_path, root_path


@pytest.fixture
def dashboard_db_env(tmp_path, monkeypatch):
    db_path = tmp_path / "dashboard.db"
    dashboard_db.init_db(db_path)
    yield db_path


@pytest.fixture
def auth_db_env(tmp_path):
    db_path = tmp_path / "auth.db"
    auth_db.init_db(db_path)
    yield db_path


@pytest.fixture
def client():
    app = Starlette(routes=commit_api.routes)
    return TestClient(app)


def _enable_plugin(monkeypatch):
    monkeypatch.setattr(commit_api, "_plugin_enabled", lambda: True)


def _seed_session(tmux_name: str, project: str):
    dashboard_db.upsert_session(tmux_name, "host", project, is_live=True, harness="claude")


def _seed_token(tmux_name: str, raw_token: str):
    auth_db.insert_token(hashlib.sha256(raw_token.encode("utf-8")).hexdigest(), tmux_name)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)


def _git_out(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True).stdout.decode().strip()


def _init_repo_with_bad_commit(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "wrong@example.com")
    _git(repo, "config", "user.name", "Wrong Author")
    (repo / "file.txt").write_text("original\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "fix thing")
    return repo, _git_out(repo, "rev-parse", "HEAD"), _git_out(repo, "rev-parse", "HEAD^{tree}")


def _commit_flow_setup(monkeypatch, tmp_path, *, repo_slug: str = "autonomy/autonomy", workspace_id: str = "autonomy"):
    repo, orig_sha, orig_tree = _init_repo_with_bad_commit(tmp_path)
    session_name = "sess-1"
    ops.upsert_by_key(
        "autonomy.commit.policy", 1, f"workspace:{workspace_id}",
        {"workspace_id": workspace_id, "applies_to": "workspace", "profile": "autonomy.direct-master", "override_mode": "none"},
        org=ops.CALLER_ORG, state="canonical",
    )
    _seed_session(session_name, workspace_id)
    _seed_token(session_name, "tok-1")
    fake_ws = SimpleNamespace(id=workspace_id, graph_project=workspace_id)
    monkeypatch.setattr(commit_api, "get_workspace", lambda wid: fake_ws)
    monkeypatch.setattr(commit_api, "resolve_capabilities", lambda workspace_id, org=None: [])
    worktree_row = SimpleNamespace(
        session_name=session_name, repo_name="autonomy", managed_clone=repo,
        branch=f"session/{session_name}", worktree_path=repo,
    )
    monkeypatch.setattr(commit_api.worktree_monitor, "get_all", lambda: [worktree_row])
    monkeypatch.setattr(commit_api, "derive_repo_slug", lambda _path: repo_slug)
    return repo, orig_sha, orig_tree, session_name, repo_slug


def test_D4_17_request_signature_handler_accepts_a_prepared_rewrite_with_no_code_changes(
    graph_db_env, dashboard_db_env, auth_db_env, workflow_db_env, trusted_store_env,
    client, monkeypatch, tmp_path,
):
    _enable_plugin(monkeypatch)
    repo, orig_sha, orig_tree, session_name, repo_slug = _commit_flow_setup(monkeypatch, tmp_path)

    report = ComplianceReport(
        commit_sha=orig_sha, resolved_policy_version="repo:demo",
        sign_off=SignOffStatus(required=True, present=False, trailer_value=None, matches_policy_identity=False),
        authorship=AuthorshipStatus(
            required_identity={"name": "Ada Operator", "email": "ada@example.com"},
            actual_author={"name": "Wrong Author", "email": "wrong@example.com"},
            actual_committer={"name": "Wrong Author", "email": "wrong@example.com"},
            author_matches=False, committer_matches=False,
        ),
        signature=SignatureStatus(required="none", present=False, kind=None, valid=False,
                                   verified_key_fingerprint=None, verification_method="git verify-commit"),
        compliant=False, violations=("author_mismatch", "committer_mismatch", "signoff_missing"),
    )

    workflow_id = create_governed_rewrite_workflow(
        repo_slug=repo_slug, original_sha=orig_sha, compliance_report=report,
    )

    store = ContentAddressedStore(trusted_store_env[1])
    trusted_conn = snapshot_dao._get_conn()
    snapshot_dao.init_schema_on_connection(trusted_conn)
    try:
        source_snapshot_ref = snapshot_original_commit(
            workflow_id=workflow_id, repo_slug=repo_slug, original_sha=orig_sha,
            tree_sha=orig_tree, parent_shas=[], git_dir=repo, store=store,
            trusted_store_conn=trusted_conn,
        )
        source_snapshot_before = snapshot_dao.get_snapshot(trusted_conn, source_snapshot_ref)

        original_id = GitIdentityLine("Wrong Author", "wrong@example.com", "1700000000", "+0000")
        corrected = construct_corrected_metadata(
            tree_oid=orig_tree, parent_oids=[],
            original_author=original_id, original_committer=original_id,
            message=b"fix thing\n", report=report,
        )

        prep = prepare_rewrite_for_signing(
            workflow_id=workflow_id, repo_slug=repo_slug, corrected=corrected,
            git_dir=repo, store=store, trusted_store_conn=trusted_conn,
        )

        # N3 check: the SOURCE snapshot must be completely untouched.
        source_snapshot_after = snapshot_dao.get_snapshot(trusted_conn, source_snapshot_ref)
        assert source_snapshot_after == source_snapshot_before, "the D4-9 source snapshot must never be mutated"

        # Two distinct snapshots must now exist for this workflow.
        assert prep["trusted_object_store_ref"] != source_snapshot_ref
        result_snapshot = snapshot_dao.get_snapshot(trusted_conn, prep["trusted_object_store_ref"])
        assert result_snapshot["snapshot_type"] == "rewrite_result"
        assert result_snapshot["canonical_preview_sha256"] == prep["canonical_payload_hash"]
    finally:
        trusted_conn.close()

    # Now invoke the REAL, unmodified request_signature handler.
    resp = client.post(
        f"/api/capabilities/commit/v1/workflows/{workflow_id}/signature-request",
        json={
            "idempotency_key": "idem-rewrite-signreq",
            "workflow_id": workflow_id,
            "signing_method": "ssh",
            "signer_policy_version": "policy-v1",
            "requested_operator_id": "op-1",
        },
        headers={"Authorization": "Bearer tok-1"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["signing"]["canonical_payload_hash"] == prep["canonical_payload_hash"]
    assert body["signing"]["trusted_object_store_ref"] == prep["trusted_object_store_ref"]
    assert body["signing"]["status"] == "pending"

    conn = cdb._get_conn()
    try:
        row = conn.execute(
            "SELECT signing_request_id, canonical_payload_hash, trusted_object_store_ref "
            "FROM commit_signing_requests WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["canonical_payload_hash"] == prep["canonical_payload_hash"]
    assert row["trusted_object_store_ref"] == prep["trusted_object_store_ref"]

    # Rewrite identity is discoverable only from state_json, never from
    # the signing request or its response shape.
    assert "origin_kind" not in body["signing"]
    assert "origin_kind" not in body
    state_conn = cdb._get_conn()
    try:
        state_row = state_conn.execute(
            "SELECT state_json FROM commit_workflow_states WHERE workflow_id = ?", (workflow_id,),
        ).fetchone()
        # request_signature's own event replaces state_json with a small
        # delta (signing_request_id/method/hashes only) rather than merging
        # -- the SAME pattern _proposal_request_for_workflow relies on for
        # the ordinary-proposal case: origin_kind is recoverable by walking
        # commit_workflow_events history, not by trusting the latest
        # projected state_json alone. Confirms rewrite identity is still
        # discoverable from state (the event history IS state), never from
        # the signing request/response shape itself.
        event_rows = state_conn.execute(
            "SELECT payload_json FROM commit_workflow_events WHERE workflow_id = ? ORDER BY occurred_at DESC, seq DESC",
            (workflow_id,),
        ).fetchall()
    finally:
        state_conn.close()

    import json as _json
    latest_state = _json.loads(state_row["state_json"])
    assert "origin_kind" not in latest_state, (
        "documents request_signature's existing delta-replace behavior -- "
        "origin_kind is not in the LATEST projection, only in event history"
    )

    origin_kind = None
    source_commit_shas = None
    for row in event_rows:
        payload = _json.loads(row["payload_json"] or "{}")
        if "origin_kind" in payload:
            origin_kind = payload["origin_kind"]
            source_commit_shas = payload.get("source_commit_shas")
            break
    assert origin_kind == "governed_rewrite"
    assert source_commit_shas == [orig_sha]
