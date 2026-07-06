"""Governed rewrite — chain membership rule (D4-11/D4-13) and workflow
instance creation (D4-8).

DN4 (graph note ``175ff7fc-850``) §4: deciding which commits in a chain
get rewritten is NOT purely per-commit compliance. Every descendant of a
rewritten commit must be rewritten too, because its ``parent`` hash
changes — even a commit that is itself compliant under the target
policy. ``determine_rewrite_membership`` consumes the per-commit
:class:`ComplianceReport` list that
:func:`tools.dashboard.commit_compliance.audit_compliance` already
produces (each entry independently re-audited against the same target
policy — nothing here relies on a stale/cached prior audit) and decides
membership + the audit-trail reason tag in one pass.

``create_governed_rewrite_workflow`` (§3 step 1) persists a fresh
rewrite workflow instance via ``commit_workflow_db.append_event`` — the
one place this module touches the shared commit-workflow DAO. It relies
on the ``commit_roles`` override (Codex, commit_workflow_db.py) so the
original SHA's ``commit_workflow_commits`` row survives a projection
rebuild with ``role="rewrite_source"``, ``position=0``, rather than the
default ``workflow_commit``/1-indexed behavior every other caller gets.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from tools.dashboard.dao import commit_workflow_db
from tools.dashboard.commit_compliance import ComplianceReport

REASON_VIOLATIONS = "rewritten_for_violations"
REASON_ANCESTOR_CHANGED = "rewritten_because_ancestor_changed"

ORIGIN_KIND_GOVERNED_REWRITE = "governed_rewrite"
ROLE_REWRITE_SOURCE = "rewrite_source"
ROLE_REWRITE_RESULT = "rewrite_result"


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


# ── D4-8: create the governed-rewrite workflow instance ────────────────


def create_governed_rewrite_workflow(
    *,
    repo_slug: str,
    original_sha: str,
    compliance_report: ComplianceReport,
    db_path=None,
) -> str:
    """Create a brand-new commit_workflow_states row for a rewrite (§3 step 1).

    Always mints a fresh workflow_id — a rewrite is never attached to a
    pre-existing workflow, even if this exact original_sha was audited
    or rewritten before. Returns the new workflow_id.
    """
    workflow_id = uuid.uuid4().hex
    commit_workflow_db.append_event(
        event_id=uuid.uuid4().hex,
        workflow_id=workflow_id,
        event_type="governed_rewrite_created",
        status_after="draft",
        repo_slug=repo_slug,
        commit_shas=[original_sha],
        commit_roles={original_sha: (ROLE_REWRITE_SOURCE, 0)},
        payload={
            "origin_kind": ORIGIN_KIND_GOVERNED_REWRITE,
            "source_commit_shas": [original_sha],
            "compliance_report": compliance_report.to_dict(),
        },
        db_path=db_path,
    )
    return workflow_id
