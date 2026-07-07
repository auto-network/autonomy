"""Tests for D4-23 -- publish with force_with_lease using the T2 lease,
failing closed on any ref divergence since T2 (DN4 §3 step 7, graph note
``175ff7fc-850``).

Builds a real single-commit governed rewrite (wrong author/committer, no
signoff), signs it through the real request_signature/attach_signature
handlers, records a real T0 (supersede) then T2 (force_with_lease)
approval via this module's own bookkeeping, then exercises the REAL,
unmodified commit.publish handler: a positive control where the T2 lease
still matches (publish succeeds using the T2-captured value verbatim),
and a negative case where the caller observes the ref has moved since T2
(publish fails closed, no ref_update_result is ever recorded, and this
module's own block_workflow_on_publish_lease_failure makes the failure
visible on the workflow as failed_retryable).

This is also the regression test for a real shape-mismatch bug found
while building this: record_force_with_lease_approval's own payload only
carried ref_tip/binding (this module's own reader, get_binding_lease) --
commit.publish's real _approval_expected_old_sha reads a DIFFERENT key
(payload["constraints"]["expected_ref_sha"]), so every governed-rewrite
publish attempt was silently rejected with "publish requires an approved
force_with_lease constraint" no matter how correctly T0/T2 were recorded.
Found by reading the real landed handler before building against it, not
by assuming this module's existing shape was sufficient -- fixed by
writing both shapes additively.
"""

from __future__ import annotations

import json

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard.dao import commit_workflow_db as cdb
from tools.dashboard.dao import trusted_git_object_store as snapshot_dao
from tools.dashboard.commit_broker.keys import InMemoryBrokerKeyStore, register_verification_key
from tools.dashboard.plugins.commit_api.entrypoints import api as commit_api
from tools.dashboard.services.trusted_git_object_store import ContentAddressedStore

from tools.dashboard.governed_rewrite import (
    PUBLISH_LEASE_STALE_STATUS,
    GitIdentityLine,
    block_workflow_on_publish_lease_failure,
    clear_publish_lease_failure_for_retry,
    construct_corrected_metadata,
    create_governed_rewrite_workflow,
    get_binding_lease,
    prepare_rewrite_for_signing,
    record_force_with_lease_approval,
    record_supersede_approval,
    snapshot_original_commit,
)

from tools.dashboard.tests.test_governed_rewrite_chain_signing import (
    _attach_signature,
    _bad_report,
    _commit_flow_setup,
    _enable_plugin,
    _request_signature,
    _ssh_keypair,
    _ssh_sign,
)


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
    from tools.dashboard.dao import dashboard_db
    db_path = tmp_path / "dashboard.db"
    dashboard_db.init_db(db_path)
    yield db_path


@pytest.fixture
def auth_db_env(tmp_path):
    from tools.dashboard.dao import auth_db
    db_path = tmp_path / "auth.db"
    auth_db.init_db(db_path)
    yield db_path


@pytest.fixture
def client():
    app = Starlette(routes=commit_api.routes)
    return TestClient(app)


def _sign_single_commit_rewrite(client, monkeypatch, tmp_path, key_path, pubkey, broker_keystore, store_root):
    """Build+sign a single-commit governed rewrite through the real
    handlers, reusing the chain-signing test's fixtures. Returns
    (workflow_id, repo_slug, base_sha, signed_sha)."""
    register_verification_key(operator_id="op-1", signing_kind="ssh", public_material=pubkey, keystore=broker_keystore)
    repo, base_sha, shas, trees, session_name, repo_slug = _commit_flow_setup(monkeypatch, tmp_path)
    report = _bad_report(shas[0])
    original_id = GitIdentityLine("Wrong Author", "wrong@example.com", "1700000000", "+0000")

    workflow_id = create_governed_rewrite_workflow(repo_slug=repo_slug, original_sha=shas[0], compliance_report=report)
    store = ContentAddressedStore(store_root)
    trusted_conn = snapshot_dao._get_conn()
    snapshot_dao.init_schema_on_connection(trusted_conn)
    try:
        snapshot_original_commit(
            workflow_id=workflow_id, repo_slug=repo_slug, original_sha=shas[0],
            tree_sha=trees[0], parent_shas=[base_sha], git_dir=repo, store=store,
            trusted_store_conn=trusted_conn,
        )
        corrected = construct_corrected_metadata(
            tree_oid=trees[0], parent_oids=[base_sha],
            original_author=original_id, original_committer=original_id,
            message=b"change 1\n", report=report,
        )
        prep = prepare_rewrite_for_signing(
            workflow_id=workflow_id, repo_slug=repo_slug, corrected=corrected,
            git_dir=repo, store=store, trusted_store_conn=trusted_conn,
        )
        sr, hash_ = _request_signature(client, workflow_id=workflow_id, idem_key="idem-request-0")
        snapshot = snapshot_dao.get_snapshot(trusted_conn, prep["trusted_object_store_ref"])
        unsigned_payload = store.get(snapshot["canonical_preview_sha256"])
        armored_signature = _ssh_sign(key_path, unsigned_payload, tmp_path, tag="single")
        attach = _attach_signature(
            client, signing_request_id=sr, armored_signature=armored_signature,
            canonical_payload_hash=hash_, idem_key="idem-attach-0",
        )
        assert attach.status_code == 200, attach.text
        signed_sha = attach.json()["signed_commit_sha"]
    finally:
        trusted_conn.close()
    return workflow_id, repo_slug, base_sha, signed_sha


def test_D4_23_publish_uses_the_t2_lease_and_succeeds_when_it_still_matches(
    graph_db_env, dashboard_db_env, auth_db_env, workflow_db_env, trusted_store_env,
    broker_keystore, client, monkeypatch, tmp_path,
):
    _enable_plugin(monkeypatch)
    key_path, pubkey = _ssh_keypair(tmp_path)
    workflow_id, repo_slug, base_sha, signed_sha = _sign_single_commit_rewrite(
        client, monkeypatch, tmp_path, key_path, pubkey, broker_keystore, trusted_store_env[1],
    )

    record_supersede_approval(
        workflow_id=workflow_id, repo_slug=repo_slug, approval_id="approval-t0",
        observed_ref_tip=base_sha,
    )
    record_force_with_lease_approval(
        workflow_id=workflow_id, repo_slug=repo_slug, approval_id="approval-t2",
        observed_ref_tip=base_sha,
    )
    assert get_binding_lease(workflow_id=workflow_id) == base_sha

    publish = client.post(
        f"/api/capabilities/commit/v1/workflows/{workflow_id}/publish",
        json={
            "idempotency_key": "idem-publish", "workflow_id": workflow_id,
            "ref_update_intent": {
                "provider": "github", "repo_slug": repo_slug, "ref": "refs/heads/main",
                "operation": "fast_forward_existing_ref", "expected_old_sha": None,
                "new_sha": signed_sha,
            },
            "publish_mode": "workspace_shared",
        },
        headers={"Authorization": "Bearer tok-1"},
    )
    assert publish.status_code == 200, publish.text
    body = publish.json()
    assert body["workflow"]["status"] == "published"
    # Publish used the T2-captured lease verbatim, not a fresh re-read.
    assert body["ref_update_result"]["expected_old_sha"] == base_sha


def test_D4_23_ref_advanced_since_t2_fails_closed_and_workflow_reacts_failed_retryable(
    graph_db_env, dashboard_db_env, auth_db_env, workflow_db_env, trusted_store_env,
    broker_keystore, client, monkeypatch, tmp_path,
):
    _enable_plugin(monkeypatch)
    key_path, pubkey = _ssh_keypair(tmp_path)
    workflow_id, repo_slug, base_sha, signed_sha = _sign_single_commit_rewrite(
        client, monkeypatch, tmp_path, key_path, pubkey, broker_keystore, trusted_store_env[1],
    )

    record_supersede_approval(
        workflow_id=workflow_id, repo_slug=repo_slug, approval_id="approval-t0",
        observed_ref_tip=base_sha,
    )
    record_force_with_lease_approval(
        workflow_id=workflow_id, repo_slug=repo_slug, approval_id="approval-t2",
        observed_ref_tip=base_sha,
    )

    # A concurrent push advanced the ref after T2 -- the caller (dashboard)
    # freshly observes a DIFFERENT current sha right before submitting publish.
    concurrently_advanced_sha = "f" * 40
    publish = client.post(
        f"/api/capabilities/commit/v1/workflows/{workflow_id}/publish",
        json={
            "idempotency_key": "idem-publish-stale", "workflow_id": workflow_id,
            "ref_update_intent": {
                "provider": "github", "repo_slug": repo_slug, "ref": "refs/heads/main",
                "operation": "fast_forward_existing_ref",
                "expected_old_sha": concurrently_advanced_sha,
                "new_sha": signed_sha,
            },
            "publish_mode": "workspace_shared",
        },
        headers={"Authorization": "Bearer tok-1"},
    )
    assert publish.status_code != 200
    assert publish.json()["code"] == "approval_stale_requires_reapproval"

    conn = cdb._get_conn()
    try:
        pre_reaction_status = conn.execute(
            "SELECT status FROM commit_workflow_states WHERE workflow_id = ?", (workflow_id,),
        ).fetchone()["status"]
        published_events = conn.execute(
            "SELECT COUNT(*) AS n FROM commit_workflow_events WHERE workflow_id = ? AND event_type = 'published'",
            (workflow_id,),
        ).fetchone()["n"]
    finally:
        conn.close()
    # The rejected attempt never mutated anything -- ref is unchanged (no
    # published event exists at all) and the workflow's signed status
    # survived the rejected publish attempt untouched.
    assert published_events == 0
    assert pre_reaction_status == "signed"

    block_workflow_on_publish_lease_failure(
        workflow_id=workflow_id, repo_slug=repo_slug,
        reason="ref advanced since T2 approval",
    )

    conn = cdb._get_conn()
    try:
        row = conn.execute(
            "SELECT status, state_json FROM commit_workflow_states WHERE workflow_id = ?", (workflow_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] == PUBLISH_LEASE_STALE_STATUS
    assert json.loads(row["state_json"])["publish_lease_failure_reason"] == "ref advanced since T2 approval"

    # A fresh T2 approval at the NEW ref-tip (T0 is still valid, no need to
    # redo it) lets publish succeed on retry -- the signature is still there.
    record_force_with_lease_approval(
        workflow_id=workflow_id, repo_slug=repo_slug, approval_id="approval-t2-retry",
        observed_ref_tip=concurrently_advanced_sha, delta_disclosed=True,
    )
    clear_publish_lease_failure_for_retry(workflow_id=workflow_id, repo_slug=repo_slug)
    retry = client.post(
        f"/api/capabilities/commit/v1/workflows/{workflow_id}/publish",
        json={
            "idempotency_key": "idem-publish-retry", "workflow_id": workflow_id,
            "ref_update_intent": {
                "provider": "github", "repo_slug": repo_slug, "ref": "refs/heads/main",
                "operation": "fast_forward_existing_ref", "expected_old_sha": None,
                "new_sha": signed_sha,
            },
            "publish_mode": "workspace_shared",
        },
        headers={"Authorization": "Bearer tok-1"},
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["workflow"]["status"] == "published"
    assert retry.json()["ref_update_result"]["expected_old_sha"] == concurrently_advanced_sha
