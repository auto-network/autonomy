"""Governed rewrite — chain membership rule (D4-11, D4-13).

DN4 (graph note ``175ff7fc-850``) §4: deciding which commits in a chain
get rewritten is NOT purely per-commit compliance. Every descendant of a
rewritten commit must be rewritten too, because its ``parent`` hash
changes — even a commit that is itself compliant under the target
policy. This module consumes the per-commit :class:`ComplianceReport`
list that :func:`tools.dashboard.commit_compliance.audit_compliance`
already produces (each entry independently re-audited against the same
target policy — nothing here relies on a stale/cached prior audit) and
decides membership + the audit-trail reason tag in one pass.

Deliberately independent of the commit-workflow DAO tables (D4-8
onward): this is pure decision logic over reports already in hand. The
D4-8+ workflow-creation step is the one that persists these decisions.
"""

from __future__ import annotations

from dataclasses import dataclass

from tools.dashboard.commit_compliance import ComplianceReport

REASON_VIOLATIONS = "rewritten_for_violations"
REASON_ANCESTOR_CHANGED = "rewritten_because_ancestor_changed"


@dataclass(frozen=True)
class RewriteMembership:
    commit_sha: str
    needs_rewrite: bool
    reason: str | None  # REASON_VIOLATIONS | REASON_ANCESTOR_CHANGED | None


def determine_rewrite_membership(reports: list[ComplianceReport]) -> list[RewriteMembership]:
    """Decide, in chain order, which commits need rewriting and why.

    Once any commit in the chain needs rewriting, every commit after it
    needs rewriting too — its parent hash is about to change regardless
    of its own compliance. A commit compliant under the target policy
    that only needs rewriting because an ancestor changed is tagged
    ``rewritten_because_ancestor_changed``, never
    ``rewritten_for_violations`` — an audit trail must not misreport a
    compliant commit as having had a violation.
    """
    result = []
    ancestor_rewritten = False
    for report in reports:
        if report.compliant and not ancestor_rewritten:
            result.append(RewriteMembership(commit_sha=report.commit_sha, needs_rewrite=False, reason=None))
            continue
        reason = REASON_VIOLATIONS if not report.compliant else REASON_ANCESTOR_CHANGED
        result.append(RewriteMembership(commit_sha=report.commit_sha, needs_rewrite=True, reason=reason))
        ancestor_rewritten = True
    return result
