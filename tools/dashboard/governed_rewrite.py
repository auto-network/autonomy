"""Governed rewrite — chain membership rule (D4-11/D4-13), workflow
instance creation (D4-8), and corrected-metadata construction (D4-10).

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

``construct_corrected_metadata`` (§3 step 3) takes tree_oid, parent_oids,
original author/committer identity lines, and message bytes as plain
parameters — the exact shape ``snapshot_original_commit`` (§3 step 2,
D4-9) below now supplies from a real DN2 snapshot.

``snapshot_original_commit`` (§3 step 2) calls DN2's ``capture_snapshot``
(Fable, tools.dashboard.services.trusted_git_object_store) to freeze the
original commit's tree into the trusted object store, then appends a
second event to the SAME workflow recording ``snapshot_ref`` in
``state_json`` — merged with the state D4-8 already wrote, since a new
event's payload replaces state_json wholesale on projection rebuild, it
is never merged automatically. This is what makes the snapshot
"referenced by the workflow": a later step reads the workflow's own
state_json for the ref rather than needing to know the trusted-store
DB's shape.

``record_chain_source_and_result_roles`` (§4) generalizes D4-8's
single-commit role/position write to a full chain: one
``rewrite_source`` row per original SHA and one ``rewrite_result`` row
per new SHA, sharing ``position`` at each chain slot. It takes the
chain's ``(original_sha, new_sha)`` pairs as a plain parameter — how
each new SHA gets determined (D4-17/18/19's sequential signing) is a
separate concern.

``check_publish_approval_gate`` (D4-14, §3 step 4) plus
``record_supersede_approval``/``record_force_with_lease_approval``/
``get_binding_lease`` (D4-15) are pure logic + bookkeeping over the
already-landed ``commit_workflow_approvals`` table — no dependency on
Codex's not-yet-built ``commit.publish``/``commit.approve`` handlers.
The T0/T2 split matters: the supersede approval (T0) can only ever
record a PROVISIONAL ref-tip, since the ref may move again before the
force_with_lease approval (T2) — only T2's observed ref-tip is ever the
binding lease a publish may act on.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from tools.dashboard.dao import commit_workflow_db
from tools.dashboard.services.trusted_git_object_store import capture_snapshot
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


# ── D4-10: construct corrected metadata without changing file contents ─


@dataclass(frozen=True)
class GitIdentityLine:
    """The identity+time bytes that follow ``author ``/``committer `` in a
    commit object (``Name <email> <unix_ts> <tzoffset>``)."""

    name: str
    email: str
    timestamp: str
    tzoffset: str

    def to_bytes(self) -> bytes:
        return f"{self.name} <{self.email}> {self.timestamp} {self.tzoffset}".encode()


@dataclass(frozen=True)
class CorrectedCommitMetadata:
    tree_oid: str
    parent_oids: tuple[str, ...]
    author: GitIdentityLine
    committer: GitIdentityLine
    message: bytes


def _fix_signoff_trailer(message: bytes, identity: dict) -> bytes:
    """Replace an existing (wrong) Signed-off-by line in place, or append a
    correct one as a new trailing paragraph if none exists. Every other
    byte of the message is untouched."""
    expected_line = f"Signed-off-by: {identity['name']} <{identity['email']}>".encode()
    lines = message.split(b"\n")
    for i, line in enumerate(lines):
        if line.startswith(b"Signed-off-by:"):
            lines[i] = expected_line
            return b"\n".join(lines)
    trimmed = message.rstrip(b"\n")
    return trimmed + b"\n\n" + expected_line + b"\n"


def construct_corrected_metadata(
    *,
    tree_oid: str,
    parent_oids: Sequence[str],
    original_author: GitIdentityLine,
    original_committer: GitIdentityLine,
    message: bytes,
    report: ComplianceReport,
    edited_message: bytes | None = None,
) -> CorrectedCommitMetadata:
    """Build corrected commit metadata per the audit's findings (§3 step 3).

    ``tree_oid``/``parent_oids`` pass through byte-identical — a
    compliance fix never changes file contents, and for a standalone
    single-commit rewrite the parent is unchanged. ``author``/
    ``committer`` are corrected only on the axis (author or committer)
    that the report says mismatched, using the report's own resolved
    ``required_identity`` — the same identity D4-4 already compared
    against — and preserving the ORIGINAL timestamp/tzoffset (a
    compliance fix corrects who, never when). The Signed-off-by trailer
    is fixed independently of the message body: ``message`` is passed
    through byte-identical unless ``edited_message`` is supplied (the
    operator's explicit message-revision review step), and the trailer
    correction is layered on top of whichever message body is in play.
    """
    required_identity = report.authorship.required_identity
    if required_identity is not None and required_identity.get("unresolved"):
        raise ValueError(
            "cannot construct corrected metadata: the report's required identity never resolved"
        )

    author = original_author
    if not report.authorship.author_matches and required_identity:
        author = GitIdentityLine(
            name=required_identity["name"], email=required_identity["email"],
            timestamp=original_author.timestamp, tzoffset=original_author.tzoffset,
        )

    committer = original_committer
    if not report.authorship.committer_matches and required_identity:
        committer = GitIdentityLine(
            name=required_identity["name"], email=required_identity["email"],
            timestamp=original_committer.timestamp, tzoffset=original_committer.tzoffset,
        )

    final_message = edited_message if edited_message is not None else message
    if not report.sign_off.matches_policy_identity and required_identity:
        final_message = _fix_signoff_trailer(final_message, required_identity)

    return CorrectedCommitMetadata(
        tree_oid=tree_oid,
        parent_oids=tuple(parent_oids),
        author=author,
        committer=committer,
        message=final_message,
    )


# ── D4-9: snapshot the original commit tree into the trusted object store ─


def _current_state_json(workflow_id: str, db_path=None) -> dict:
    conn = commit_workflow_db._get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT state_json FROM commit_workflow_states WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ValueError(f"no such workflow: {workflow_id!r}")
    return json.loads(row["state_json"])


def snapshot_original_commit(
    *,
    workflow_id: str,
    repo_slug: str,
    original_sha: str,
    tree_sha: str,
    parent_shas: Sequence[str],
    git_dir,
    store,
    trusted_store_conn,
    workflow_db_path=None,
) -> str:
    """Freeze the original commit's tree into the trusted object store and
    bind the resulting snapshot_ref to the SAME workflow (§3 step 2).

    This is the boundary that makes ``construct_corrected_metadata`` safe
    to call later against a tree_sha that can no longer be altered by any
    subsequent worktree/index/HEAD change: the caller should read
    ``snapshot_ref`` back out of the workflow's own ``state_json`` (never
    re-derive it from a live git read) and resolve the tree via
    ``dao.trusted_git_object_store.get_snapshot(ref)['tree_sha']``.
    """
    snapshot_ref = capture_snapshot(
        workflow_id=workflow_id,
        repo_slug=repo_slug,
        commit_sha=original_sha,
        tree_sha=tree_sha,
        parent_shas=parent_shas,
        git_dir=git_dir,
        store=store,
        dao_conn=trusted_store_conn,
        snapshot_type=ROLE_REWRITE_SOURCE,
    )

    current_state = _current_state_json(workflow_id, db_path=workflow_db_path)
    updated_state = {**current_state, "snapshot_ref": snapshot_ref}
    commit_workflow_db.append_event(
        event_id=uuid.uuid4().hex,
        workflow_id=workflow_id,
        event_type="governed_rewrite_snapshot_captured",
        status_after="draft",
        repo_slug=repo_slug,
        payload=updated_state,
        db_path=workflow_db_path,
    )
    return snapshot_ref


# ── D4-12: record chain source/result roles, preserving chain position ─


def record_chain_source_and_result_roles(
    *,
    workflow_id: str,
    repo_slug: str,
    chain: Sequence[tuple[str, str]],
    db_path=None,
) -> None:
    """Record one ``rewrite_source`` row per original SHA and one
    ``rewrite_result`` row per new SHA, preserving each commit's original
    root-to-tip chain order in ``position`` (§4).

    ``chain`` is an ordered list of ``(original_sha, new_sha)`` pairs, one
    per chain slot, root first. Source and result at the same slot share
    ``position`` — safe because original and new SHAs are always distinct
    strings, so the dict-by-sha ``commit_roles`` shape holds both without
    needing a list-of-entries form.
    """
    commit_shas: list[str] = []
    commit_roles: dict[str, tuple[str, int]] = {}
    for position, (original_sha, new_sha) in enumerate(chain):
        commit_shas.append(original_sha)
        commit_shas.append(new_sha)
        commit_roles[original_sha] = (ROLE_REWRITE_SOURCE, position)
        commit_roles[new_sha] = (ROLE_REWRITE_RESULT, position)

    current_state = _current_state_json(workflow_id, db_path=db_path)
    commit_workflow_db.append_event(
        event_id=uuid.uuid4().hex,
        workflow_id=workflow_id,
        event_type="governed_rewrite_chain_recorded",
        status_after="draft",
        repo_slug=repo_slug,
        commit_shas=commit_shas,
        commit_roles=commit_roles,
        payload=current_state,
        db_path=db_path,
    )


# ── D4-14: require both supersede and force_with_lease before publish ─

REQUIRED_PUBLISH_APPROVAL_TYPES = ("supersede", "force_with_lease")


@dataclass(frozen=True)
class PublishApprovalGateResult:
    allowed: bool
    missing_approval_types: tuple[str, ...]
    reason: str | None


def check_publish_approval_gate(*, workflow_id: str, db_path=None) -> PublishApprovalGateResult:
    """Decide whether ``commit.publish`` may proceed for a rewrite (§3 step
    4, §6). Both a ``supersede`` and a ``force_with_lease`` approval must
    reach ``status="approved"`` — a rewrite with only one approved is
    blocked, never partially executed. This is pure gate-check logic over
    the already-landed ``commit_workflow_approvals`` table; Codex's
    eventual ``commit.publish`` handler calls this before doing anything,
    same decouple pattern as D4-8/D4-9/D4-10.
    """
    conn = commit_workflow_db._get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT approval_type, status FROM commit_workflow_approvals WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchall()
    finally:
        conn.close()

    approved_types = {row["approval_type"] for row in rows if row["status"] == "approved"}
    missing = tuple(t for t in REQUIRED_PUBLISH_APPROVAL_TYPES if t not in approved_types)

    if not missing:
        return PublishApprovalGateResult(allowed=True, missing_approval_types=(), reason=None)

    reason = f"publish blocked: missing approved {' and '.join(missing)} approval"
    return PublishApprovalGateResult(allowed=False, missing_approval_types=missing, reason=reason)


# ── D4-15: capture the binding lease at T2, provisional at T0 ──────────


def record_supersede_approval(
    *,
    workflow_id: str,
    repo_slug: str,
    approval_id: str,
    observed_ref_tip: str,
    operator_id: str | None = None,
    db_path=None,
) -> None:
    """T0: record the supersede approval with a PROVISIONAL ref-tip only.

    Never the binding lease — a rewrite must not be published against a
    ref-tip observed this early, since the ref may advance again before
    T2 (force_with_lease). See ``record_force_with_lease_approval``.
    """
    now = time.time()
    conn = commit_workflow_db._get_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO commit_workflow_approvals "
            "(approval_id, workflow_id, repo_slug, approval_type, status, "
            "operator_id, requested_at, decided_at, payload_json) "
            "VALUES (?, ?, ?, 'supersede', 'approved', ?, ?, ?, ?)",
            (
                approval_id, workflow_id, repo_slug, operator_id, now, now,
                json.dumps({"ref_tip": observed_ref_tip, "binding": False}),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def record_force_with_lease_approval(
    *,
    workflow_id: str,
    repo_slug: str,
    approval_id: str,
    observed_ref_tip: str,
    operator_id: str | None = None,
    db_path=None,
) -> None:
    """T2 (strictly after T0): record the force_with_lease approval with
    the BINDING lease — the ref-tip observed AT THIS CALL, never reused
    from the T0 supersede record even if the ref never moved."""
    now = time.time()
    conn = commit_workflow_db._get_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO commit_workflow_approvals "
            "(approval_id, workflow_id, repo_slug, approval_type, status, "
            "operator_id, requested_at, decided_at, payload_json) "
            "VALUES (?, ?, ?, 'force_with_lease', 'approved', ?, ?, ?, ?)",
            (
                approval_id, workflow_id, repo_slug, operator_id, now, now,
                json.dumps({"ref_tip": observed_ref_tip, "binding": True}),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_binding_lease(*, workflow_id: str, db_path=None) -> str | None:
    """Return the binding lease (the force_with_lease approval's observed
    ref-tip), or ``None`` if not yet captured. Never reads the T0
    supersede record's provisional value."""
    conn = commit_workflow_db._get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT payload_json FROM commit_workflow_approvals "
            "WHERE workflow_id = ? AND approval_type = 'force_with_lease' AND status = 'approved'",
            (workflow_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    payload = json.loads(row["payload_json"])
    return payload["ref_tip"] if payload.get("binding") else None
