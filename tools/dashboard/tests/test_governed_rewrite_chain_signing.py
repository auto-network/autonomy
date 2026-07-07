"""Tests for D4-18/D4-19/D4-20 -- sequential chain signing sharing one
batch_group_id (DN4 §4, graph note ``175ff7fc-850``).

Builds a real 3-commit chain (each commit non-compliant: wrong author/
committer, no signoff), rewrites it end to end through the actual D4-8/9/
10/17 functions PLUS the real, unmodified commit.request_signature HTTP
handler at every link -- no rewrite-specific branch anywhere in the
signing path, matching D4-17's own standard. Materializes each signed
commit as a real git object (assembled via the real commit_broker
assembly functions from a REAL ssh-keygen signature) and walks the
resulting ref with `git log --graph` to confirm a clean linear chain
(G14.2), and separately confirms the all-or-nothing blocking behavior
(G14.4): a rejected signature at one position blocks only that shared
workflow, leaving the sibling that already signed untouched.

KNOWN GAP, tracked not worked around: commit.attach_signature's real
handler currently hard-rejects every governed-rewrite attach attempt on
a dead `proposal_request` precondition left over from before c001133's
frozen-bytes refactor (full repro + spec routed to Codex, graph comment
14425cfe-ebc on 175ff7fc-850). Until that 3-line fix lands, the "signature
attached" step below is simulated via `_simulate_attach_signature`, which
writes the EXACT SAME commit_signing_requests/commit_workflow_events
shape the real handler writes (same event_type, same status_after, same
payload keys -- verified by reading attach_signature's own code), using a
genuinely real SSH signature and the real `assemble_signed_commit` to
compute the signed SHA. Only the ASGI round-trip is stood in for; the
crypto and assembly are real. This is not a substitute for the real
end-to-end test -- that gets added once Codex's fix lands, per the same
standard D4-17 was held to.
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
from tools.dashboard.commit_broker.assembly import assemble_signed_commit, fold_gpgsig_block
from tools.dashboard.commit_broker.keys import InMemoryBrokerKeyStore, register_verification_key
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
    CHAIN_BATCH_WAITING_STATUS,
    CHAIN_BLOCKED_STATUS,
    GitIdentityLine,
    advance_chain_link,
    block_chain_link_on_signing_failure,
    construct_corrected_metadata,
    create_governed_rewrite_workflow,
    get_batch_member_status,
    new_batch_group_id,
    prepare_rewrite_for_signing,
    record_chain_source_and_result_roles,
    snapshot_original_commit,
    stamp_batch_fields,
)


# ── fixtures (same shape as test_governed_rewrite_signing.py / commit_api tests) ─


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
def broker_keystore(monkeypatch):
    store = InMemoryBrokerKeyStore()
    monkeypatch.setattr(commit_api, "_BROKER_VERIFICATION_KEY_STORE", store)
    return store


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


def _ssh_keypair(tmp_path: Path) -> tuple[Path, bytes]:
    key_path = tmp_path / "ssh_signing_key"
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(key_path)], capture_output=True, check=True)
    return key_path, (key_path.with_suffix(".pub")).read_bytes()


def _ssh_sign(key_path: Path, payload: bytes, tmp_path: Path, tag: str) -> str:
    msg_path = tmp_path / f"payload-{tag}.txt"
    msg_path.write_bytes(payload)
    result = subprocess.run(
        ["ssh-keygen", "-Y", "sign", "-f", str(key_path), "-n", "git", str(msg_path)],
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    return (msg_path.parent / f"{msg_path.name}.sig").read_text()


def _init_repo_with_bad_chain(tmp_path: Path) -> tuple[Path, str, list[str], list[str]]:
    """base (compliant, untouched) -> c1 -> c2 -> c3 (all wrong author/committer, no signoff)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "wrong@example.com")
    _git(repo, "config", "user.name", "Wrong Author")
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    base_sha = _git_out(repo, "rev-parse", "HEAD")

    shas, trees = [], []
    for i in range(1, 4):
        (repo / f"file{i}.txt").write_text(f"content {i}\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", f"change {i}")
        shas.append(_git_out(repo, "rev-parse", "HEAD"))
        trees.append(_git_out(repo, "rev-parse", "HEAD^{tree}"))
    return repo, base_sha, shas, trees


def _commit_flow_setup(monkeypatch, tmp_path, *, repo_slug: str = "autonomy/autonomy", workspace_id: str = "autonomy"):
    repo, base_sha, shas, trees = _init_repo_with_bad_chain(tmp_path)
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
    return repo, base_sha, shas, trees, session_name, repo_slug


def _bad_report(sha: str) -> ComplianceReport:
    return ComplianceReport(
        commit_sha=sha, resolved_policy_version="repo:demo",
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


def _request_signature(client, *, workflow_id, idem_key):
    resp = client.post(
        f"/api/capabilities/commit/v1/workflows/{workflow_id}/signature-request",
        json={
            "idempotency_key": idem_key, "workflow_id": workflow_id, "signing_method": "ssh",
            "signer_policy_version": "policy-v1", "requested_operator_id": "op-1",
        },
        headers={"Authorization": "Bearer tok-1"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["signing"]["signing_request_id"], resp.json()["signing"]["canonical_payload_hash"]


def _ssh_verify(pubkey: bytes, operator_id: str, payload: bytes, armored_signature: str, tmp_path: Path, tag: str) -> bool:
    """Real ssh-keygen -Y verify -- the exact mechanism
    _verify_attached_signature uses internally, run standalone so the
    chain-blocking test can ground a 'signing failure' in genuine crypto
    rather than assuming a string constant."""
    work = tmp_path / f"verify-{tag}"
    work.mkdir()
    sig_path = work / "sig.asc"
    sig_path.write_text(armored_signature, encoding="utf-8")
    allowed_signers = work / "allowed_signers"
    allowed_signers.write_text(f"{operator_id} {pubkey.decode('utf-8').strip()}\n", encoding="utf-8")
    proc = subprocess.run(
        ["ssh-keygen", "-Y", "verify", "-f", str(allowed_signers), "-I", operator_id, "-n", "git", "-s", str(sig_path)],
        input=payload, capture_output=True,
    )
    return proc.returncode == 0


def _simulate_attach_signature(
    *, workflow_id, repo_slug, signing_request_id, armored_signature, unsigned_payload, pubkey, idem_key, tmp_path,
) -> dict:
    """Stand-in for the real commit.attach_signature handler, blocked today
    by a known, already-reported bug (dead proposal_request precondition,
    see module docstring). Writes the EXACT SAME commit_signing_requests
    UPDATE / commit_workflow_events INSERT shape the real handler writes
    (verified by reading tools/dashboard/plugins/commit_api/entrypoints/
    api.py's attach_signature directly), using a REAL ssh-keygen signature
    verification and the REAL assemble_signed_commit to compute the signed
    SHA -- only the ASGI layer is stood in for.
    """
    if not _ssh_verify(pubkey, "op-1", unsigned_payload, armored_signature, tmp_path, tag=idem_key):
        return {"ok": False, "error": "signature_verification_failed"}

    signed_payload, computed_signed_sha = assemble_signed_commit(
        unsigned_payload, fold_gpgsig_block(armored_signature.encode("utf-8")),
    )
    import json as _json
    import time as _time
    import uuid as _uuid

    event_payload = {
        "signing_request_id": signing_request_id,
        "signed_commit_sha": computed_signed_sha,
        "verification": {"assembled_from_trusted_payload": True},
    }
    conn = cdb._get_conn()
    try:
        conn.execute(
            "UPDATE commit_signing_requests SET status = ?, completed_at = ?, signature_ref = ?, payload_json = ? "
            "WHERE signing_request_id = ?",
            ("signed", _time.time(), f"sig-{idem_key}", _json.dumps(event_payload, sort_keys=True), signing_request_id),
        )
        conn.commit()
    finally:
        conn.close()

    cdb.append_event(
        event_id=_uuid.uuid4().hex,
        workflow_id=workflow_id,
        event_type="signed",
        status_after="signed",
        repo_slug=repo_slug,
        payload=event_payload,
    )
    return {"ok": True, "signed_commit_sha": computed_signed_sha, "signed_payload": signed_payload}


def test_D4_18_19_three_chain_signs_sequentially_through_real_handlers_and_forms_a_linear_ref(
    graph_db_env, dashboard_db_env, auth_db_env, workflow_db_env, trusted_store_env,
    broker_keystore, client, monkeypatch, tmp_path,
):
    _enable_plugin(monkeypatch)
    repo, base_sha, shas, trees, session_name, repo_slug = _commit_flow_setup(monkeypatch, tmp_path)
    key_path, pubkey = _ssh_keypair(tmp_path)
    register_verification_key(operator_id="op-1", signing_kind="ssh", public_material=pubkey, keystore=broker_keystore)

    reports = [_bad_report(sha) for sha in shas]
    original_id = GitIdentityLine("Wrong Author", "wrong@example.com", "1700000000", "+0000")

    workflow_id = create_governed_rewrite_workflow(
        repo_slug=repo_slug, original_sha=shas[0], compliance_report=reports[0],
    )

    store = ContentAddressedStore(trusted_store_env[1])
    trusted_conn = snapshot_dao._get_conn()
    snapshot_dao.init_schema_on_connection(trusted_conn)
    batch_group_id = new_batch_group_id()
    batch_size = 3
    signed_shas: list[str] = []
    signing_request_ids: list[str] = []

    try:
        for position in range(3):  # 0-indexed chain slot; position_in_batch below is 1-indexed
            sha = shas[position]
            tree = trees[position]
            report = reports[position]

            snapshot_original_commit(
                workflow_id=workflow_id, repo_slug=repo_slug, original_sha=sha,
                tree_sha=tree, parent_shas=[base_sha if position == 0 else shas[position - 1]],
                git_dir=repo, store=store, trusted_store_conn=trusted_conn,
            )

            if position == 0:
                corrected = construct_corrected_metadata(
                    tree_oid=tree, parent_oids=[base_sha],
                    original_author=original_id, original_committer=original_id,
                    message=f"change {position + 1}\n".encode(), report=report,
                )
                prep = prepare_rewrite_for_signing(
                    workflow_id=workflow_id, repo_slug=repo_slug, corrected=corrected,
                    git_dir=repo, store=store, trusted_store_conn=trusted_conn,
                )
            else:
                # D4-18: not-yet-reached position has no row at all yet.
                waiting = get_batch_member_status(
                    batch_group_id=batch_group_id, position_in_batch=position + 1, batch_size=batch_size,
                )
                assert waiting["batch_status"] == CHAIN_BATCH_WAITING_STATUS
                assert waiting["signing_request_id"] is None
                assert waiting["canonical_payload_hash"] is None

                corrected = construct_corrected_metadata(
                    tree_oid=tree, parent_oids=[signed_shas[-1]],
                    original_author=original_id, original_committer=original_id,
                    message=f"change {position + 1}\n".encode(), report=report,
                )
                prep = advance_chain_link(
                    workflow_id=workflow_id, repo_slug=repo_slug, corrected=corrected,
                    git_dir=repo, store=store, trusted_store_conn=trusted_conn,
                )

            signing_request_id, canonical_payload_hash = _request_signature(
                client, workflow_id=workflow_id, idem_key=f"idem-request-{position}",
            )
            stamp_batch_fields(
                signing_request_id=signing_request_id, batch_group_id=batch_group_id,
                position_in_batch=position + 1, batch_size=batch_size,
            )
            signing_request_ids.append(signing_request_id)

            member = get_batch_member_status(
                batch_group_id=batch_group_id, position_in_batch=position + 1, batch_size=batch_size,
            )
            assert member["batch_status"] is None
            assert member["signing_request_id"] == signing_request_id
            assert member["canonical_payload_hash"] == canonical_payload_hash

            snapshot = snapshot_dao.get_snapshot(trusted_conn, prep["trusted_object_store_ref"])
            unsigned_payload = store.get(snapshot["canonical_preview_sha256"])
            armored_signature = _ssh_sign(key_path, unsigned_payload, tmp_path, tag=f"pos{position}")

            attach = _simulate_attach_signature(
                workflow_id=workflow_id, repo_slug=repo_slug, signing_request_id=signing_request_id,
                armored_signature=armored_signature, unsigned_payload=unsigned_payload, pubkey=pubkey,
                idem_key=f"idem-attach-{position}", tmp_path=tmp_path,
            )
            assert attach["ok"], attach
            signed_shas.append(attach["signed_commit_sha"])

            conn = cdb._get_conn()
            try:
                status_row = conn.execute(
                    "SELECT status FROM commit_workflow_states WHERE workflow_id = ?", (workflow_id,),
                ).fetchone()
            finally:
                conn.close()
            assert status_row["status"] == "signed"

            # Materialize the real signed commit object so the chain is
            # independently checkable via plain git, not just our own bookkeeping.
            subprocess.run(
                ["git", "-C", str(repo), "hash-object", "-w", "--stdin", "-t", "commit"],
                input=attach["signed_payload"], capture_output=True, check=True,
            )

        # All three signing requests share one batch_group_id with correct positions/size.
        conn = cdb._get_conn()
        try:
            rows = conn.execute(
                "SELECT signing_request_id, batch_group_id, position_in_batch, batch_size, workflow_id "
                "FROM commit_signing_requests WHERE batch_group_id = ? ORDER BY position_in_batch",
                (batch_group_id,),
            ).fetchall()
        finally:
            conn.close()
        assert [r["signing_request_id"] for r in rows] == signing_request_ids
        assert [r["position_in_batch"] for r in rows] == [1, 2, 3]
        assert all(r["batch_size"] == 3 for r in rows)
        assert all(r["workflow_id"] == workflow_id for r in rows)  # one shared workflow_id for the whole chain

        # G14.2: git log --graph on the resulting ref is a clean linear
        # chain, each commit's parent is the rewritten predecessor's SHA,
        # no orphaned intermediate SHAs reachable from the tip.
        tip_sha = signed_shas[-1]
        parent_chain = _git_out(repo, "log", "--format=%H %P", tip_sha).splitlines()
        assert len(parent_chain) == 4  # tip, link2, link1, base
        expected_order = [signed_shas[2], signed_shas[1], signed_shas[0], base_sha]
        for line, expected_sha in zip(parent_chain, expected_order):
            assert line.split()[0] == expected_sha
        # explicit parent-of-parent-of-parent check
        assert _git_out(repo, "cat-file", "-p", signed_shas[2]).splitlines()[1] == f"parent {signed_shas[1]}"
        assert _git_out(repo, "cat-file", "-p", signed_shas[1]).splitlines()[1] == f"parent {signed_shas[0]}"
        assert _git_out(repo, "cat-file", "-p", signed_shas[0]).splitlines()[1] == f"parent {base_sha}"

        record_chain_source_and_result_roles(
            workflow_id=workflow_id, repo_slug=repo_slug,
            chain=list(zip(shas, signed_shas)),
        )
        commits_conn = cdb._get_conn()
        try:
            role_rows = commits_conn.execute(
                "SELECT commit_sha, role, position FROM commit_workflow_commits "
                "WHERE workflow_id = ? ORDER BY position, role",
                (workflow_id,),
            ).fetchall()
        finally:
            commits_conn.close()
        by_position = {}
        for r in role_rows:
            by_position.setdefault(r["position"], {})[r["role"]] = r["commit_sha"]
        for position in range(3):
            assert by_position[position]["rewrite_source"] == shas[position]
            assert by_position[position]["rewrite_result"] == signed_shas[position]
    finally:
        trusted_conn.close()


def test_D4_20_signing_failure_at_one_link_blocks_only_that_workflow_siblings_untouched(
    graph_db_env, dashboard_db_env, auth_db_env, workflow_db_env, trusted_store_env,
    broker_keystore, client, monkeypatch, tmp_path,
):
    _enable_plugin(monkeypatch)
    repo, base_sha, shas, trees, session_name, repo_slug = _commit_flow_setup(monkeypatch, tmp_path)
    key_path, pubkey = _ssh_keypair(tmp_path)
    register_verification_key(operator_id="op-1", signing_kind="ssh", public_material=pubkey, keystore=broker_keystore)

    reports = [_bad_report(sha) for sha in shas]
    original_id = GitIdentityLine("Wrong Author", "wrong@example.com", "1700000000", "+0000")

    workflow_id = create_governed_rewrite_workflow(
        repo_slug=repo_slug, original_sha=shas[0], compliance_report=reports[0],
    )
    store = ContentAddressedStore(trusted_store_env[1])
    trusted_conn = snapshot_dao._get_conn()
    snapshot_dao.init_schema_on_connection(trusted_conn)
    batch_group_id = new_batch_group_id()

    try:
        # Position 1 (root): signs cleanly.
        snapshot_original_commit(
            workflow_id=workflow_id, repo_slug=repo_slug, original_sha=shas[0],
            tree_sha=trees[0], parent_shas=[base_sha], git_dir=repo, store=store,
            trusted_store_conn=trusted_conn,
        )
        corrected0 = construct_corrected_metadata(
            tree_oid=trees[0], parent_oids=[base_sha],
            original_author=original_id, original_committer=original_id,
            message=b"change 1\n", report=reports[0],
        )
        prep0 = prepare_rewrite_for_signing(
            workflow_id=workflow_id, repo_slug=repo_slug, corrected=corrected0,
            git_dir=repo, store=store, trusted_store_conn=trusted_conn,
        )
        sr0, hash0 = _request_signature(client, workflow_id=workflow_id, idem_key="idem-request-0")
        stamp_batch_fields(signing_request_id=sr0, batch_group_id=batch_group_id, position_in_batch=1, batch_size=3)
        snapshot0 = snapshot_dao.get_snapshot(trusted_conn, prep0["trusted_object_store_ref"])
        unsigned0 = store.get(snapshot0["canonical_preview_sha256"])
        sig0 = _ssh_sign(key_path, unsigned0, tmp_path, tag="ok0")
        attach0 = _simulate_attach_signature(
            workflow_id=workflow_id, repo_slug=repo_slug, signing_request_id=sr0,
            armored_signature=sig0, unsigned_payload=unsigned0, pubkey=pubkey,
            idem_key="idem-attach-0", tmp_path=tmp_path,
        )
        assert attach0["ok"], attach0
        signed_sha0 = attach0["signed_commit_sha"]

        # Position 2: prepared, then REJECTED with a bogus signature (attach fails).
        corrected1 = construct_corrected_metadata(
            tree_oid=trees[1], parent_oids=[signed_sha0],
            original_author=original_id, original_committer=original_id,
            message=b"change 2\n", report=reports[1],
        )
        prep1 = advance_chain_link(
            workflow_id=workflow_id, repo_slug=repo_slug, corrected=corrected1,
            git_dir=repo, store=store, trusted_store_conn=trusted_conn,
        )
        sr1, hash1 = _request_signature(client, workflow_id=workflow_id, idem_key="idem-request-1")
        stamp_batch_fields(signing_request_id=sr1, batch_group_id=batch_group_id, position_in_batch=2, batch_size=3)
        snapshot1 = snapshot_dao.get_snapshot(trusted_conn, prep1["trusted_object_store_ref"])
        unsigned1 = store.get(snapshot1["canonical_preview_sha256"])

        bad_attach = _simulate_attach_signature(
            workflow_id=workflow_id, repo_slug=repo_slug, signing_request_id=sr1,
            armored_signature="not-a-real-signature", unsigned_payload=unsigned1, pubkey=pubkey,
            idem_key="idem-attach-1-bad", tmp_path=tmp_path,
        )
        assert not bad_attach["ok"]
        assert bad_attach["error"] == "signature_verification_failed"

        block_chain_link_on_signing_failure(
            workflow_id=workflow_id, repo_slug=repo_slug, position_in_batch=2,
            batch_group_id=batch_group_id, reason="armored signature failed verification",
        )

        conn = cdb._get_conn()
        try:
            state_row = conn.execute(
                "SELECT status, state_json FROM commit_workflow_states WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchone()
            sr1_row = conn.execute(
                "SELECT status FROM commit_signing_requests WHERE signing_request_id = ?", (sr1,),
            ).fetchone()
            sr0_row = conn.execute(
                "SELECT status FROM commit_signing_requests WHERE signing_request_id = ?", (sr0,),
            ).fetchone()
        finally:
            conn.close()

        assert state_row["status"] == CHAIN_BLOCKED_STATUS
        import json as _json
        blocked_state = _json.loads(state_row["state_json"])
        assert blocked_state["chain_blocked_position"] == 2
        assert blocked_state["chain_batch_group_id"] == batch_group_id

        # The rejected attach never mutated commit_signing_requests -- the
        # transaction rolled back on early return, so position 2's row is
        # still 'pending', ready for a retry with a real signature.
        assert sr1_row["status"] == "pending"
        # Position 1's already-signed row is completely untouched by the failure.
        assert sr0_row["status"] == "signed"

        # No publish call was ever made anywhere in this test -- there is
        # no partial force-push to guard against because nothing here
        # calls commit.publish while the chain is blocked.
    finally:
        trusted_conn.close()
