"""Tests for D4-11/D4-13 — chain rewrite membership + reason tagging
(DN4 §4, graph note ``175ff7fc-850``).

Pure decision logic over already-computed ComplianceReports — no git
repo, no DAO. Reports are built directly rather than through
audit_compliance since only the ``compliant`` flag matters here.
"""

from __future__ import annotations

from tools.dashboard.commit_compliance import (
    AuthorshipStatus,
    ComplianceReport,
    SignatureStatus,
    SignOffStatus,
)
from tools.dashboard.governed_rewrite import (
    REASON_ANCESTOR_CHANGED,
    REASON_VIOLATIONS,
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
