"""Tests for D4-11/D4-13 — chain rewrite membership + reason tagging
(DN4 §4, graph note ``175ff7fc-850``).

Pure decision logic over already-computed ComplianceReports — no git
repo, no DAO. Reports are built directly rather than through
audit_compliance since only the ``compliant`` flag matters here.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from tools.dashboard.commit_compliance import (
    AuthorshipStatus,
    ComplianceReport,
    SignatureStatus,
    SignOffStatus,
)
from tools.dashboard.dao import commit_workflow_db
from tools.dashboard.dao import trusted_git_object_store as object_store_dao
from tools.dashboard.services.trusted_git_object_store import ContentAddressedStore
from tools.dashboard.governed_rewrite import (
    REASON_ANCESTOR_CHANGED,
    REASON_VIOLATIONS,
    ROLE_REWRITE_RESULT,
    ROLE_REWRITE_SOURCE,
    GitIdentityLine,
    check_publish_approval_gate,
    construct_corrected_metadata,
    create_governed_rewrite_workflow,
    determine_rewrite_membership,
    get_binding_lease,
    record_chain_source_and_result_roles,
    record_force_with_lease_approval,
    record_supersede_approval,
    snapshot_original_commit,
)


def _report(sha: str, *, compliant: bool, violations: tuple[str, ...] = ()) -> ComplianceReport:
    return ComplianceReport(
        commit_sha=sha,
        resolved_policy_version="repo:demo",
        sign_off=SignOffStatus(required=True, present=True, trailer_value="x", matches_policy_identity=True),
        authorship=AuthorshipStatus(
            required_identity=None,
            actual_author={"name": "a", "email": "a@example.com"},
            actual_committer={"name": "a", "email": "a@example.com"},
            author_matches=True, committer_matches=True,
        ),
        signature=SignatureStatus(
            required="none", present=False, kind=None, valid=False,
            verified_key_fingerprint=None, verification_method="git verify-commit",
        ),
        compliant=compliant,
        violations=violations,
    )


def test_D4_11_only_first_commit_noncompliant_puts_all_three_in_rewrite_set():
    reports = [
        _report("sha1", compliant=False, violations=("signature_absent",)),
        _report("sha2", compliant=True),
        _report("sha3", compliant=True),
    ]

    membership = determine_rewrite_membership(reports)

    assert [m.needs_rewrite for m in membership] == [True, True, True]
    assert [m.commit_sha for m in membership] == ["sha1", "sha2", "sha3"]


def test_D4_11_fully_compliant_chain_needs_no_rewrite():
    reports = [_report("sha1", compliant=True), _report("sha2", compliant=True)]

    membership = determine_rewrite_membership(reports)

    assert [m.needs_rewrite for m in membership] == [False, False]
    assert [m.reason for m in membership] == [None, None]


def test_D4_11_middle_commit_noncompliant_only_tail_is_ancestor_changed():
    reports = [
        _report("sha1", compliant=True),
        _report("sha2", compliant=False, violations=("author_mismatch",)),
        _report("sha3", compliant=True),
    ]

    membership = determine_rewrite_membership(reports)

    assert membership[0].needs_rewrite is False
    assert membership[1].needs_rewrite is True
    assert membership[2].needs_rewrite is True


# ── D4-13: violations vs ancestor-changed reason tag ───────────────────


def test_D4_13_noncompliant_commit_tagged_for_violations():
    reports = [_report("sha1", compliant=False, violations=("signoff_missing",)), _report("sha2", compliant=True)]

    membership = determine_rewrite_membership(reports)

    assert membership[0].reason == REASON_VIOLATIONS


def test_D4_13_compliant_descendant_tagged_ancestor_changed_not_violations():
    reports = [
        _report("sha1", compliant=False, violations=("signoff_missing",)),
        _report("sha2", compliant=True),
        _report("sha3", compliant=True),
    ]

    membership = determine_rewrite_membership(reports)

    assert membership[1].reason == REASON_ANCESTOR_CHANGED
    assert membership[2].reason == REASON_ANCESTOR_CHANGED
    # A compliant-under-target commit must never be mislabeled as having had a violation.
    assert membership[1].reason != REASON_VIOLATIONS
    assert membership[2].reason != REASON_VIOLATIONS


def test_D4_13_not_needing_rewrite_has_no_reason():
    reports = [_report("sha1", compliant=True)]

    membership = determine_rewrite_membership(reports)

    assert membership[0].needs_rewrite is False
    assert membership[0].reason is None


# ── D4-8: create the governed-rewrite workflow instance ────────────────


def test_D4_8_mints_a_fresh_workflow_with_origin_kind_and_source_sha(tmp_path):
    path = tmp_path / "wf.db"
    commit_workflow_db.init_db(path)
    report = _report("orig-sha", compliant=False, violations=("signature_absent",))

    workflow_id = create_governed_rewrite_workflow(
        repo_slug="autonomy", original_sha="orig-sha", compliance_report=report, db_path=path,
    )

    conn = commit_workflow_db._get_conn(path)
    try:
        state_row = conn.execute(
            "SELECT state_json FROM commit_workflow_states WHERE workflow_id = ?", (workflow_id,),
        ).fetchone()
        state = json.loads(state_row["state_json"])
        assert state["origin_kind"] == "governed_rewrite"
        assert state["source_commit_shas"] == ["orig-sha"]
        assert state["compliance_report"]["commit_sha"] == "orig-sha"

        commit_row = conn.execute(
            "SELECT commit_sha, role, position FROM commit_workflow_commits WHERE workflow_id = ?", (workflow_id,),
        ).fetchone()
        assert commit_row["commit_sha"] == "orig-sha"
        assert commit_row["role"] == ROLE_REWRITE_SOURCE
        assert commit_row["position"] == 0
    finally:
        conn.close()


def test_D4_8_never_reuses_a_pre_existing_workflow_for_the_same_sha(tmp_path):
    path = tmp_path / "wf.db"
    commit_workflow_db.init_db(path)
    report = _report("orig-sha", compliant=False, violations=("signature_absent",))

    first_workflow_id = create_governed_rewrite_workflow(
        repo_slug="autonomy", original_sha="orig-sha", compliance_report=report, db_path=path,
    )
    second_workflow_id = create_governed_rewrite_workflow(
        repo_slug="autonomy", original_sha="orig-sha", compliance_report=report, db_path=path,
    )

    assert first_workflow_id != second_workflow_id

    conn = commit_workflow_db._get_conn(path)
    try:
        rows = conn.execute(
            "SELECT DISTINCT workflow_id FROM commit_workflow_states WHERE repo_slug = 'autonomy'"
        ).fetchall()
        assert {r["workflow_id"] for r in rows} == {first_workflow_id, second_workflow_id}
    finally:
        conn.close()


def test_D4_8_survives_projection_rebuild(tmp_path):
    path = tmp_path / "wf.db"
    commit_workflow_db.init_db(path)
    report = _report("orig-sha", compliant=False, violations=("signature_absent",))

    workflow_id = create_governed_rewrite_workflow(
        repo_slug="autonomy", original_sha="orig-sha", compliance_report=report, db_path=path,
    )
    commit_workflow_db.rebuild_projection(path)

    conn = commit_workflow_db._get_conn(path)
    try:
        commit_row = conn.execute(
            "SELECT role, position FROM commit_workflow_commits WHERE workflow_id = ?", (workflow_id,),
        ).fetchone()
        assert commit_row["role"] == ROLE_REWRITE_SOURCE
        assert commit_row["position"] == 0
    finally:
        conn.close()


# ── D4-10: construct corrected metadata without changing file contents ─

REQUIRED_IDENTITY = {"name": "Ada Operator", "email": "ada@example.com"}


def _correction_report(
    *, author_matches: bool, committer_matches: bool, signoff_matches: bool,
    required_identity: dict | None = REQUIRED_IDENTITY,
) -> ComplianceReport:
    return ComplianceReport(
        commit_sha="orig-sha",
        resolved_policy_version="repo:demo",
        sign_off=SignOffStatus(
            required=True, present=True, trailer_value="whatever",
            matches_policy_identity=signoff_matches,
        ),
        authorship=AuthorshipStatus(
            required_identity=required_identity,
            actual_author={"name": "Wrong Author", "email": "wrong@example.com"},
            actual_committer={"name": "Wrong Author", "email": "wrong@example.com"},
            author_matches=author_matches, committer_matches=committer_matches,
        ),
        signature=SignatureStatus(
            required="none", present=False, kind=None, valid=False,
            verified_key_fingerprint=None, verification_method="git verify-commit",
        ),
        compliant=author_matches and committer_matches and signoff_matches,
        violations=(),
    )


def test_D4_10_tree_and_parent_are_byte_identical_to_original():
    report = _correction_report(author_matches=False, committer_matches=False, signoff_matches=False)
    original_id = GitIdentityLine("Wrong Author", "wrong@example.com", "1700000000", "+0000")

    corrected = construct_corrected_metadata(
        tree_oid="tree-abc", parent_oids=["parent-abc"],
        original_author=original_id, original_committer=original_id,
        message=b"fix thing\n", report=report,
    )

    assert corrected.tree_oid == "tree-abc"
    assert corrected.parent_oids == ("parent-abc",)


def test_D4_10_wrong_author_and_missing_signoff_gets_corrected_identity_and_trailer():
    report = _correction_report(author_matches=False, committer_matches=False, signoff_matches=False)
    original_id = GitIdentityLine("Wrong Author", "wrong@example.com", "1700000000", "+0000")

    corrected = construct_corrected_metadata(
        tree_oid="tree-abc", parent_oids=["parent-abc"],
        original_author=original_id, original_committer=original_id,
        message=b"fix thing\n", report=report,
    )

    assert corrected.author.name == "Ada Operator"
    assert corrected.author.email == "ada@example.com"
    assert corrected.committer.name == "Ada Operator"
    assert corrected.committer.email == "ada@example.com"
    # Original timestamp/tzoffset preserved -- a compliance fix corrects who, never when.
    assert corrected.author.timestamp == "1700000000"
    assert corrected.author.tzoffset == "+0000"
    assert corrected.message == b"fix thing\n\nSigned-off-by: Ada Operator <ada@example.com>\n"


def test_D4_10_message_byte_identical_when_no_edit_and_signoff_already_correct():
    report = _correction_report(author_matches=False, committer_matches=False, signoff_matches=True)
    original_id = GitIdentityLine("Wrong Author", "wrong@example.com", "1700000000", "+0000")
    original_message = b"fix thing\n\nSigned-off-by: Ada Operator <ada@example.com>\n"

    corrected = construct_corrected_metadata(
        tree_oid="tree-abc", parent_oids=["parent-abc"],
        original_author=original_id, original_committer=original_id,
        message=original_message, report=report,
    )

    assert corrected.message == original_message


def test_D4_10_only_the_mismatched_identity_axis_is_corrected():
    report = _correction_report(author_matches=True, committer_matches=False, signoff_matches=True)
    original_author_id = GitIdentityLine("Ada Operator", "ada@example.com", "1700000000", "+0000")
    original_committer_id = GitIdentityLine("Agent Bot", "bot@example.com", "1700000000", "+0000")

    corrected = construct_corrected_metadata(
        tree_oid="tree-abc", parent_oids=["parent-abc"],
        original_author=original_author_id, original_committer=original_committer_id,
        message=b"msg\n", report=report,
    )

    assert corrected.author == original_author_id
    assert corrected.committer.name == "Ada Operator"
    assert corrected.committer.email == "ada@example.com"


def test_D4_10_existing_wrong_signoff_trailer_replaced_in_place():
    report = _correction_report(author_matches=True, committer_matches=True, signoff_matches=False)
    identity = GitIdentityLine("Ada Operator", "ada@example.com", "1700000000", "+0000")

    corrected = construct_corrected_metadata(
        tree_oid="tree-abc", parent_oids=["parent-abc"],
        original_author=identity, original_committer=identity,
        message=b"fix thing\n\nSigned-off-by: Eve <eve@example.com>\n", report=report,
    )

    assert corrected.message == b"fix thing\n\nSigned-off-by: Ada Operator <ada@example.com>\n"


def test_D4_10_explicit_edited_message_used_instead_of_original():
    report = _correction_report(author_matches=True, committer_matches=True, signoff_matches=True)
    identity = GitIdentityLine("Ada Operator", "ada@example.com", "1700000000", "+0000")

    corrected = construct_corrected_metadata(
        tree_oid="tree-abc", parent_oids=["parent-abc"],
        original_author=identity, original_committer=identity,
        message=b"original\n", report=report, edited_message=b"operator revised subject\n",
    )

    assert corrected.message == b"operator revised subject\n"


def test_D4_10_unresolved_identity_raises_rather_than_silently_correcting():
    report = _correction_report(
        author_matches=False, committer_matches=False, signoff_matches=False,
        required_identity={"unresolved": True},
    )
    identity = GitIdentityLine("Wrong Author", "wrong@example.com", "1700000000", "+0000")

    with pytest.raises(ValueError):
        construct_corrected_metadata(
            tree_oid="tree-abc", parent_oids=["parent-abc"],
            original_author=identity, original_committer=identity,
            message=b"msg\n", report=report,
        )


# ── D4-9: snapshot the original commit tree into the trusted object store ─



def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)


def _git_out(repo, *args) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, check=True
    ).stdout.decode().strip()


def _fixture_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "wrong@example.com")
    _git(repo, "config", "user.name", "Wrong Author")
    (repo / "file.txt").write_text("original\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fix thing")
    return repo, _git_out(repo, "rev-parse", "HEAD"), _git_out(repo, "rev-parse", "HEAD^{tree}")


def test_D4_9_snapshot_is_referenced_by_the_workflow(tmp_path):
    workflow_db_path = tmp_path / "wf.db"
    trusted_store_path = tmp_path / "trusted.db"
    commit_workflow_db.init_db(workflow_db_path)
    trusted_conn = object_store_dao._get_conn(trusted_store_path)
    object_store_dao.init_schema_on_connection(trusted_conn)
    store = ContentAddressedStore(tmp_path / "store")
    repo, orig_sha, orig_tree = _fixture_repo(tmp_path)

    report = _correction_report(author_matches=False, committer_matches=False, signoff_matches=False)
    workflow_id = create_governed_rewrite_workflow(
        repo_slug="autonomy", original_sha=orig_sha, compliance_report=report, db_path=workflow_db_path,
    )

    snapshot_ref = snapshot_original_commit(
        workflow_id=workflow_id, repo_slug="autonomy", original_sha=orig_sha,
        tree_sha=orig_tree, parent_shas=[], git_dir=repo, store=store,
        trusted_store_conn=trusted_conn, workflow_db_path=workflow_db_path,
    )
    trusted_conn.close()

    conn = commit_workflow_db._get_conn(workflow_db_path)
    try:
        row = conn.execute(
            "SELECT state_json FROM commit_workflow_states WHERE workflow_id = ?", (workflow_id,),
        ).fetchone()
        state = json.loads(row["state_json"])
        # The snapshot ref is now readable from the workflow itself...
        assert state["snapshot_ref"] == snapshot_ref
        # ...and D4-8's fields are still present -- the new event must not
        # have clobbered the state D4-8 already wrote.
        assert state["origin_kind"] == "governed_rewrite"
        assert state["source_commit_shas"] == [orig_sha]

        # D4-8's role/position row must survive this second event too.
        commit_row = conn.execute(
            "SELECT role, position FROM commit_workflow_commits WHERE workflow_id = ?", (workflow_id,),
        ).fetchone()
        assert commit_row["role"] == ROLE_REWRITE_SOURCE
        assert commit_row["position"] == 0
    finally:
        conn.close()


def test_D4_9_snapshot_tree_survives_worktree_and_branch_mutation(tmp_path):
    workflow_db_path = tmp_path / "wf.db"
    trusted_store_path = tmp_path / "trusted.db"
    commit_workflow_db.init_db(workflow_db_path)
    trusted_conn = object_store_dao._get_conn(trusted_store_path)
    object_store_dao.init_schema_on_connection(trusted_conn)
    store = ContentAddressedStore(tmp_path / "store")
    repo, orig_sha, orig_tree = _fixture_repo(tmp_path)

    report = _correction_report(author_matches=False, committer_matches=False, signoff_matches=False)
    workflow_id = create_governed_rewrite_workflow(
        repo_slug="autonomy", original_sha=orig_sha, compliance_report=report, db_path=workflow_db_path,
    )
    snapshot_ref = snapshot_original_commit(
        workflow_id=workflow_id, repo_slug="autonomy", original_sha=orig_sha,
        tree_sha=orig_tree, parent_shas=[], git_dir=repo, store=store,
        trusted_store_conn=trusted_conn, workflow_db_path=workflow_db_path,
    )

    # Mutate the working tree AND advance the branch to a whole new commit.
    (repo / "file.txt").write_text("mutated after snapshot\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "later work")
    new_head = _git_out(repo, "rev-parse", "HEAD")
    assert new_head != orig_sha

    snap = object_store_dao.get_snapshot(trusted_conn, snapshot_ref)
    assert snap["tree_sha"] == orig_tree
    trusted_conn.close()


# ── D4-12: record chain source/result roles, preserved position ────────


def test_D4_12_three_commit_chain_gets_matching_source_and_result_rows(tmp_path):
    path = tmp_path / "wf.db"
    commit_workflow_db.init_db(path)
    report = _report("sha1", compliant=False, violations=("signature_absent",))
    workflow_id = create_governed_rewrite_workflow(
        repo_slug="autonomy", original_sha="sha1", compliance_report=report, db_path=path,
    )

    chain = [("sha1", "new1"), ("sha2", "new2"), ("sha3", "new3")]
    record_chain_source_and_result_roles(
        workflow_id=workflow_id, repo_slug="autonomy", chain=chain, db_path=path,
    )

    conn = commit_workflow_db._get_conn(path)
    try:
        rows = conn.execute(
            "SELECT commit_sha, role, position FROM commit_workflow_commits "
            "WHERE workflow_id = ? ORDER BY position, role",
            (workflow_id,),
        ).fetchall()
        by_position = {}
        for r in rows:
            by_position.setdefault(r["position"], {})[r["role"]] = r["commit_sha"]

        assert set(by_position.keys()) == {0, 1, 2}
        assert by_position[0] == {ROLE_REWRITE_SOURCE: "sha1", ROLE_REWRITE_RESULT: "new1"}
        assert by_position[1] == {ROLE_REWRITE_SOURCE: "sha2", ROLE_REWRITE_RESULT: "new2"}
        assert by_position[2] == {ROLE_REWRITE_SOURCE: "sha3", ROLE_REWRITE_RESULT: "new3"}

        source_rows = [r for r in rows if r["role"] == ROLE_REWRITE_SOURCE]
        result_rows = [r for r in rows if r["role"] == ROLE_REWRITE_RESULT]
        assert len(source_rows) == 3
        assert len(result_rows) == 3
    finally:
        conn.close()


def test_D4_12_preserves_workflow_state_from_earlier_steps(tmp_path):
    path = tmp_path / "wf.db"
    commit_workflow_db.init_db(path)
    report = _report("sha1", compliant=False, violations=("signature_absent",))
    workflow_id = create_governed_rewrite_workflow(
        repo_slug="autonomy", original_sha="sha1", compliance_report=report, db_path=path,
    )

    record_chain_source_and_result_roles(
        workflow_id=workflow_id, repo_slug="autonomy",
        chain=[("sha1", "new1"), ("sha2", "new2")], db_path=path,
    )

    conn = commit_workflow_db._get_conn(path)
    try:
        row = conn.execute(
            "SELECT state_json FROM commit_workflow_states WHERE workflow_id = ?", (workflow_id,),
        ).fetchone()
        state = json.loads(row["state_json"])
        assert state["origin_kind"] == "governed_rewrite"
        assert state["source_commit_shas"] == ["sha1"]
    finally:
        conn.close()


def test_D4_12_survives_projection_rebuild(tmp_path):
    path = tmp_path / "wf.db"
    commit_workflow_db.init_db(path)
    report = _report("sha1", compliant=False, violations=("signature_absent",))
    workflow_id = create_governed_rewrite_workflow(
        repo_slug="autonomy", original_sha="sha1", compliance_report=report, db_path=path,
    )
    record_chain_source_and_result_roles(
        workflow_id=workflow_id, repo_slug="autonomy",
        chain=[("sha1", "new1"), ("sha2", "new2")], db_path=path,
    )
    commit_workflow_db.rebuild_projection(path)

    conn = commit_workflow_db._get_conn(path)
    try:
        rows = conn.execute(
            "SELECT commit_sha, role, position FROM commit_workflow_commits WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchall()
        by_role_position = {(r["role"], r["position"]): r["commit_sha"] for r in rows}
        assert by_role_position[(ROLE_REWRITE_SOURCE, 0)] == "sha1"
        assert by_role_position[(ROLE_REWRITE_RESULT, 0)] == "new1"
        assert by_role_position[(ROLE_REWRITE_SOURCE, 1)] == "sha2"
        assert by_role_position[(ROLE_REWRITE_RESULT, 1)] == "new2"
    finally:
        conn.close()


# ── D4-14: require both supersede and force_with_lease before publish ─


def _insert_approval(conn, *, approval_id, workflow_id, repo_slug, approval_type, status):
    conn.execute(
        "INSERT INTO commit_workflow_approvals "
        "(approval_id, workflow_id, repo_slug, approval_type, status, requested_at) "
        "VALUES (?, ?, ?, ?, ?, 1.0)",
        (approval_id, workflow_id, repo_slug, approval_type, status),
    )
    conn.commit()


def test_D4_14_only_supersede_approved_blocks_publish_naming_force_with_lease(tmp_path):
    path = tmp_path / "wf.db"
    commit_workflow_db.init_db(path)
    report = _report("sha1", compliant=False, violations=("signature_absent",))
    workflow_id = create_governed_rewrite_workflow(
        repo_slug="autonomy", original_sha="sha1", compliance_report=report, db_path=path,
    )

    conn = commit_workflow_db._get_conn(path)
    _insert_approval(conn, approval_id="ap-1", workflow_id=workflow_id, repo_slug="autonomy",
                      approval_type="supersede", status="approved")
    _insert_approval(conn, approval_id="ap-2", workflow_id=workflow_id, repo_slug="autonomy",
                      approval_type="force_with_lease", status="pending")

    result = check_publish_approval_gate(workflow_id=workflow_id, db_path=path)
    assert result.allowed is False
    assert result.missing_approval_types == ("force_with_lease",)
    assert "force_with_lease" in result.reason

    rows = conn.execute(
        "SELECT approval_id FROM commit_workflow_approvals WHERE workflow_id = ?", (workflow_id,),
    ).fetchall()
    assert len(rows) == 2
    conn.close()


def test_D4_14_both_approved_makes_publish_callable(tmp_path):
    path = tmp_path / "wf.db"
    commit_workflow_db.init_db(path)
    report = _report("sha1", compliant=False, violations=("signature_absent",))
    workflow_id = create_governed_rewrite_workflow(
        repo_slug="autonomy", original_sha="sha1", compliance_report=report, db_path=path,
    )

    conn = commit_workflow_db._get_conn(path)
    _insert_approval(conn, approval_id="ap-1", workflow_id=workflow_id, repo_slug="autonomy",
                      approval_type="supersede", status="approved")
    _insert_approval(conn, approval_id="ap-2", workflow_id=workflow_id, repo_slug="autonomy",
                      approval_type="force_with_lease", status="pending")
    assert check_publish_approval_gate(workflow_id=workflow_id, db_path=path).allowed is False

    conn.execute(
        "UPDATE commit_workflow_approvals SET status = 'approved' WHERE approval_id = 'ap-2'"
    )
    conn.commit()

    result = check_publish_approval_gate(workflow_id=workflow_id, db_path=path)
    assert result.allowed is True
    assert result.missing_approval_types == ()
    assert result.reason is None
    conn.close()


def test_D4_14_neither_approved_names_both_missing_types(tmp_path):
    path = tmp_path / "wf.db"
    commit_workflow_db.init_db(path)
    report = _report("sha1", compliant=False, violations=("signature_absent",))
    workflow_id = create_governed_rewrite_workflow(
        repo_slug="autonomy", original_sha="sha1", compliance_report=report, db_path=path,
    )

    result = check_publish_approval_gate(workflow_id=workflow_id, db_path=path)
    assert result.allowed is False
    assert set(result.missing_approval_types) == {"supersede", "force_with_lease"}


# ── D4-15: binding lease captured at T2, provisional at T0 ─────────────


def test_D4_15_binding_lease_is_the_t2_value_not_t0_when_ref_advanced(tmp_path):
    path = tmp_path / "wf.db"
    commit_workflow_db.init_db(path)
    report = _report("sha1", compliant=False, violations=("signature_absent",))
    workflow_id = create_governed_rewrite_workflow(
        repo_slug="autonomy", original_sha="sha1", compliance_report=report, db_path=path,
    )

    # T0: supersede approved while the ref is at A.
    record_supersede_approval(
        workflow_id=workflow_id, repo_slug="autonomy", approval_id="ap-supersede",
        observed_ref_tip="sha-A", db_path=path,
    )
    # ...the ref advances to B between T0 and T2 -- disclosed per D4-16...
    # T2: force_with_lease approved while the ref is now at B.
    record_force_with_lease_approval(
        workflow_id=workflow_id, repo_slug="autonomy", approval_id="ap-lease",
        observed_ref_tip="sha-B", delta_disclosed=True, db_path=path,
    )

    binding_lease = get_binding_lease(workflow_id=workflow_id, db_path=path)
    assert binding_lease == "sha-B"
    assert binding_lease != "sha-A"


def test_D4_15_t0_record_present_but_flagged_provisional_not_binding(tmp_path):
    path = tmp_path / "wf.db"
    commit_workflow_db.init_db(path)
    report = _report("sha1", compliant=False, violations=("signature_absent",))
    workflow_id = create_governed_rewrite_workflow(
        repo_slug="autonomy", original_sha="sha1", compliance_report=report, db_path=path,
    )

    record_supersede_approval(
        workflow_id=workflow_id, repo_slug="autonomy", approval_id="ap-supersede",
        observed_ref_tip="sha-A", db_path=path,
    )

    conn = commit_workflow_db._get_conn(path)
    try:
        row = conn.execute(
            "SELECT approval_type, status, payload_json FROM commit_workflow_approvals "
            "WHERE workflow_id = ? AND approval_type = 'supersede'",
            (workflow_id,),
        ).fetchone()
        assert row is not None
        assert row["status"] == "approved"
        payload = json.loads(row["payload_json"])
        assert payload["ref_tip"] == "sha-A"
        assert payload["binding"] is False
    finally:
        conn.close()

    # No force_with_lease yet -- no binding lease should be resolvable.
    assert get_binding_lease(workflow_id=workflow_id, db_path=path) is None


def test_D4_15_binding_lease_equals_t0_ref_tip_when_ref_never_moved(tmp_path):
    """Positive control: when the ref genuinely didn't move between T0 and
    T2, the binding lease legitimately equals the T0 value too -- but it
    must come from the T2 record, not be read from T0 as a shortcut."""
    path = tmp_path / "wf.db"
    commit_workflow_db.init_db(path)
    report = _report("sha1", compliant=False, violations=("signature_absent",))
    workflow_id = create_governed_rewrite_workflow(
        repo_slug="autonomy", original_sha="sha1", compliance_report=report, db_path=path,
    )
    record_supersede_approval(
        workflow_id=workflow_id, repo_slug="autonomy", approval_id="ap-supersede",
        observed_ref_tip="sha-A", db_path=path,
    )
    record_force_with_lease_approval(
        workflow_id=workflow_id, repo_slug="autonomy", approval_id="ap-lease",
        observed_ref_tip="sha-A", db_path=path,
    )
    assert get_binding_lease(workflow_id=workflow_id, db_path=path) == "sha-A"


# ── D4-16: T0->T2 delta must be disclosed before force_with_lease grants ─


def test_D4_16_ref_advanced_undisclosed_blocks_force_with_lease(tmp_path):
    path = tmp_path / "wf.db"
    commit_workflow_db.init_db(path)
    report = _report("sha1", compliant=False, violations=("signature_absent",))
    workflow_id = create_governed_rewrite_workflow(
        repo_slug="autonomy", original_sha="sha1", compliance_report=report, db_path=path,
    )
    record_supersede_approval(
        workflow_id=workflow_id, repo_slug="autonomy", approval_id="ap-supersede",
        observed_ref_tip="sha-A", db_path=path,
    )

    with pytest.raises(ValueError):
        record_force_with_lease_approval(
            workflow_id=workflow_id, repo_slug="autonomy", approval_id="ap-lease",
            observed_ref_tip="sha-B", db_path=path,
        )

    # The rejected attempt must not have written a row at all.
    assert get_binding_lease(workflow_id=workflow_id, db_path=path) is None


def test_D4_16_ref_advanced_disclosed_allows_force_with_lease(tmp_path):
    path = tmp_path / "wf.db"
    commit_workflow_db.init_db(path)
    report = _report("sha1", compliant=False, violations=("signature_absent",))
    workflow_id = create_governed_rewrite_workflow(
        repo_slug="autonomy", original_sha="sha1", compliance_report=report, db_path=path,
    )
    record_supersede_approval(
        workflow_id=workflow_id, repo_slug="autonomy", approval_id="ap-supersede",
        observed_ref_tip="sha-A", db_path=path,
    )

    record_force_with_lease_approval(
        workflow_id=workflow_id, repo_slug="autonomy", approval_id="ap-lease",
        observed_ref_tip="sha-B", delta_disclosed=True, db_path=path,
    )

    assert get_binding_lease(workflow_id=workflow_id, db_path=path) == "sha-B"


def test_D4_16_no_ref_movement_allows_combined_confirmation_without_disclosure(tmp_path):
    path = tmp_path / "wf.db"
    commit_workflow_db.init_db(path)
    report = _report("sha1", compliant=False, violations=("signature_absent",))
    workflow_id = create_governed_rewrite_workflow(
        repo_slug="autonomy", original_sha="sha1", compliance_report=report, db_path=path,
    )
    record_supersede_approval(
        workflow_id=workflow_id, repo_slug="autonomy", approval_id="ap-supersede",
        observed_ref_tip="sha-A", db_path=path,
    )

    # No ref movement -- a single combined confirmation (no delta_disclosed) is fine.
    record_force_with_lease_approval(
        workflow_id=workflow_id, repo_slug="autonomy", approval_id="ap-lease",
        observed_ref_tip="sha-A", db_path=path,
    )

    assert get_binding_lease(workflow_id=workflow_id, db_path=path) == "sha-A"


def test_D4_16_force_with_lease_without_a_prior_supersede_raises(tmp_path):
    """Codex's review finding: without this guard, T2 could be recorded
    before any T0 supersede approval exists, since _get_provisional_ref_tip
    returns None and the disclosure check was (wrongly) skipped entirely --
    silently satisfying D4-14's dual-approval gate out of order."""
    path = tmp_path / "wf.db"
    commit_workflow_db.init_db(path)
    report = _report("sha1", compliant=False, violations=("signature_absent",))
    workflow_id = create_governed_rewrite_workflow(
        repo_slug="autonomy", original_sha="sha1", compliance_report=report, db_path=path,
    )

    with pytest.raises(ValueError):
        record_force_with_lease_approval(
            workflow_id=workflow_id, repo_slug="autonomy", approval_id="ap-lease",
            observed_ref_tip="sha-A", db_path=path,
        )

    # No row should exist, and the publish gate must still be fully blocked.
    assert get_binding_lease(workflow_id=workflow_id, db_path=path) is None
    result = check_publish_approval_gate(workflow_id=workflow_id, db_path=path)
    assert result.allowed is False
    assert set(result.missing_approval_types) == {"supersede", "force_with_lease"}
