"""Tests for D4-11/D4-13 — chain rewrite membership + reason tagging
(DN4 §4, graph note ``175ff7fc-850``).

Pure decision logic over already-computed ComplianceReports — no git
repo, no DAO. Reports are built directly rather than through
audit_compliance since only the ``compliant`` flag matters here.
"""

from __future__ import annotations

import json

import pytest

from tools.dashboard.commit_compliance import (
    AuthorshipStatus,
    ComplianceReport,
    SignatureStatus,
    SignOffStatus,
)
from tools.dashboard.dao import commit_workflow_db
from tools.dashboard.governed_rewrite import (
    REASON_ANCESTOR_CHANGED,
    REASON_VIOLATIONS,
    ROLE_REWRITE_SOURCE,
    GitIdentityLine,
    construct_corrected_metadata,
    create_governed_rewrite_workflow,
    determine_rewrite_membership,
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
