"""Tests for get_chain_publish_links -- the ordered
(snapshot_ref, signed_object_sha256) list a chain-aware push needs to
materialize a governed-rewrite chain's full ancestry before pushing the
tip (DN4 §4, graph note ``175ff7fc-850``).

Builds and fully signs a real 3-commit chain through the actual
request_signature/attach_signature handlers (reusing the chain-signing
test's own fixtures), then confirms the query returns exactly the right
ordered links -- each link's own trusted_object_store_ref and its own
signed_object_sha256, root to tip -- straight from what request_signature
and attach_signature already wrote, with no new data capture needed. Also
confirms it fails closed on an unknown batch and on a batch where a link
isn't fully signed yet.
"""

from __future__ import annotations

import json

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard.dao import commit_workflow_db as cdb
from tools.dashboard.dao import trusted_git_object_store as snapshot_dao
from tools.dashboard.commit_broker.keys import InMemoryBrokerKeyStore, register_verification_key
from tools.dashboard.commit_broker.pusher import build_chain_pusher
from tools.dashboard.plugins.commit_api.entrypoints import api as commit_api
from tools.dashboard.services.trusted_git_object_store import ContentAddressedStore

from tools.dashboard.governed_rewrite import (
    GitIdentityLine,
    advance_chain_link,
    construct_corrected_metadata,
    create_governed_rewrite_workflow,
    get_chain_publish_links,
    new_batch_group_id,
    prepare_rewrite_for_signing,
    snapshot_original_commit,
    stamp_batch_fields,
)

from tools.dashboard.tests.test_governed_rewrite_chain_signing import (
    _attach_signature,
    _bad_report,
    _commit_flow_setup,
    _enable_plugin,
    _git_out,
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


def test_get_chain_publish_links_returns_the_real_ordered_links_for_a_fully_signed_chain(
    graph_db_env, dashboard_db_env, auth_db_env, workflow_db_env, trusted_store_env,
    broker_keystore, client, monkeypatch, tmp_path,
):
    _enable_plugin(monkeypatch)
    repo, base_sha, shas, trees, session_name, repo_slug = _commit_flow_setup(monkeypatch, tmp_path)
    key_path, pubkey = _ssh_keypair(tmp_path)
    register_verification_key(operator_id="op-1", signing_kind="ssh", public_material=pubkey, keystore=broker_keystore)

    reports = [_bad_report(sha) for sha in shas]
    original_id = GitIdentityLine("Wrong Author", "wrong@example.com", "1700000000", "+0000")
    workflow_id = create_governed_rewrite_workflow(repo_slug=repo_slug, original_sha=shas[0], compliance_report=reports[0])

    store = ContentAddressedStore(trusted_store_env[1])
    trusted_conn = snapshot_dao._get_conn()
    snapshot_dao.init_schema_on_connection(trusted_conn)
    batch_group_id = new_batch_group_id()
    signed_shas = []
    expected_links = []

    try:
        for position in range(3):
            sha, tree, report = shas[position], trees[position], reports[position]
            snapshot_original_commit(
                workflow_id=workflow_id, repo_slug=repo_slug, original_sha=sha, tree_sha=tree,
                parent_shas=[base_sha if position == 0 else shas[position - 1]],
                git_dir=repo, store=store, trusted_store_conn=trusted_conn,
            )
            corrected = construct_corrected_metadata(
                tree_oid=tree, parent_oids=[base_sha] if position == 0 else [signed_shas[-1]],
                original_author=original_id, original_committer=original_id,
                message=f"change {position + 1}\n".encode(), report=report,
            )
            if position == 0:
                prep = prepare_rewrite_for_signing(
                    workflow_id=workflow_id, repo_slug=repo_slug, corrected=corrected,
                    git_dir=repo, store=store, trusted_store_conn=trusted_conn,
                )
            else:
                prep = advance_chain_link(
                    workflow_id=workflow_id, repo_slug=repo_slug, corrected=corrected,
                    git_dir=repo, store=store, trusted_store_conn=trusted_conn,
                )
            sr, hash_ = _request_signature(client, workflow_id=workflow_id, idem_key=f"idem-request-{position}")
            stamp_batch_fields(
                signing_request_id=sr, batch_group_id=batch_group_id,
                position_in_batch=position + 1, batch_size=3,
            )
            snapshot = snapshot_dao.get_snapshot(trusted_conn, prep["trusted_object_store_ref"])
            unsigned_payload = store.get(snapshot["canonical_preview_sha256"])
            armored_signature = _ssh_sign(key_path, unsigned_payload, tmp_path, tag=f"pos{position}")
            attach = _attach_signature(
                client, signing_request_id=sr, armored_signature=armored_signature,
                canonical_payload_hash=hash_, idem_key=f"idem-attach-{position}",
            )
            assert attach.status_code == 200, attach.text
            signed_shas.append(attach.json()["signed_commit_sha"])
            expected_links.append((prep["trusted_object_store_ref"], attach.json()["verification"]["signed_object_sha256"]))

        links = get_chain_publish_links(batch_group_id=batch_group_id)
        assert links == expected_links
        assert len(links) == 3
        # Every link's snapshot ref is a real, distinct rewrite_result snapshot.
        for snapshot_ref, signed_object_sha256 in links:
            snap = snapshot_dao.get_snapshot(trusted_conn, snapshot_ref)
            assert snap is not None
            assert snap["snapshot_type"] == "rewrite_result"
            assert store.get(signed_object_sha256)  # the signed bytes are really there

        # This is the piece that was missing before: feed this query's real
        # output straight into the real chain-aware pusher and confirm the
        # tip actually lands with its full ancestry -- the composition the
        # xfailed handler-level test (test_governed_rewrite_chain_signing.py)
        # is still waiting on the handler wiring for. Proving it here, at the
        # connector level, closes the loop on the two pieces this session
        # built (the query, the pusher) independent of that wiring.
        push_objects = build_chain_pusher(
            store=store, snapshot_dao=snapshot_dao, dao_conn=trusted_conn, links=links,
            remote=str(repo), staging_dir=str(tmp_path / "chain-publish-staging"),
        )
        # _commit_flow_setup already forces "main" to base_sha (matching the
        # real repo's current state), so this is an existing-ref update.
        assert _git_out(repo, "rev-parse", "refs/heads/main") == base_sha
        result = push_objects(
            signed_commit_sha=signed_shas[-1], target_ref="refs/heads/main",
            expected_ref_sha=base_sha, is_new_ref=False, credential=None,
        )
        assert result.ok, result.reason
        assert _git_out(repo, "rev-parse", "refs/heads/main") == signed_shas[-1]
        # The intermediate links' own commit objects really landed too, not
        # just the tip -- git can walk the whole chain back to base_sha.
        parent_chain = _git_out(repo, "log", "--format=%H", signed_shas[-1]).splitlines()
        assert parent_chain[:3] == [signed_shas[2], signed_shas[1], signed_shas[0]]
        assert f"parent {base_sha}" in _git_out(repo, "cat-file", "commit", signed_shas[0]).splitlines()
    finally:
        trusted_conn.close()


def test_get_chain_publish_links_raises_for_an_unknown_batch(workflow_db_env):
    with pytest.raises(ValueError, match="no signing requests found"):
        get_chain_publish_links(batch_group_id="no-such-batch")


def test_get_chain_publish_links_fails_closed_when_a_link_is_not_yet_signed(workflow_db_env):
    conn = cdb._get_conn()
    try:
        conn.execute(
            "INSERT INTO commit_workflow_events (event_id, workflow_id, event_type, occurred_at, actor_type, repo_slug) "
            "VALUES ('evt-1', 'wf-1', 'proposed', 1.0, 'agent_session', 'repo')"
        )
        conn.execute(
            "INSERT INTO commit_workflow_states (workflow_id, repo_slug, status, last_event_id, created_at, updated_at, state_json) "
            "VALUES ('wf-1', 'repo', 'awaiting_signature', 'evt-1', 1.0, 1.0, '{}')"
        )
        conn.execute(
            "INSERT INTO commit_signing_requests (signing_request_id, workflow_id, repo_slug, status, "
            "signing_method, trusted_object_store_ref, canonical_payload_hash, batch_group_id, "
            "position_in_batch, batch_size, payload_json, requested_at) "
            "VALUES ('sr-1', 'wf-1', 'repo', 'pending', 'ssh', 'store://ref-1', 'hash-1', "
            "'batch-x', 1, 2, '{}', 1.0)"
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(ValueError, match="isn't fully signed"):
        get_chain_publish_links(batch_group_id="batch-x")
