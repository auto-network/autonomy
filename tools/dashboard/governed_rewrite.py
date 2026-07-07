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

D4-16's disclosure gate is folded directly into
``record_force_with_lease_approval``: it reads T0's own provisional
ref-tip back out of the ``commit_workflow_approvals`` table itself
(never trusts a caller-supplied claim of what T0 was) and refuses to
record the T2 approval if the ref advanced and the caller hasn't passed
``delta_disclosed=True``. No ref movement means no disclosure
requirement — a single combined confirmation is allowed.

D4-18/D4-19 (§4) sequential chain signing: a whole chain is ONE
workflow_id (``record_chain_source_and_result_roles`` already assumes
this — one workflow, N ``rewrite_source``/``rewrite_result`` pairs
distinguished by ``position``). ``advance_chain_link`` re-runs D4-17's
``prepare_rewrite_for_signing`` on that SAME workflow_id once per link,
after reading the previous link's ``signed_commit_sha`` back out of the
workflow's own state — never re-deriving or trusting a caller-supplied
parent. ``stamp_batch_fields``/``get_batch_member_status`` are the
chain-specific bookkeeping layered on top of ``commit.request_signature``
(never touched or branched): each link's row is created by the real
handler exactly like an ordinary proposal, then batch_group_id/
position_in_batch/batch_size are stamped on as an additive follow-up
write, and a not-yet-reached position (no row yet) reports
``waiting_on_predecessor`` — matching the already-landed local-signer
read side (D3-19) exactly.

D4-20 (§4): ``block_chain_link_on_signing_failure`` transitions the
chain's shared workflow to ``blocked`` naming the failing position. No
force-push code path exists to guard against here — this module never
calls ``commit.publish``, so a blocked link intrinsically prevents the
whole chain from reaching publish.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from tools.dashboard.dao import commit_workflow_db
from tools.dashboard.dao import trusted_git_object_store as snapshot_dao
from tools.dashboard.services.trusted_git_object_store import capture_snapshot
from tools.dashboard.commit_broker.assembly import commit_object_sha, serialize_unsigned_commit_payload
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


def _current_status(workflow_id: str, db_path=None) -> str:
    conn = commit_workflow_db._get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT status FROM commit_workflow_states WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ValueError(f"no such workflow: {workflow_id!r}")
    return str(row["status"])


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

    Passes the workflow's OWN CURRENT status through as ``status_after``
    rather than hardcoding one: for a chain, every ``new_sha`` is only
    known once its own link has actually signed (D4-18/19), so this is
    necessarily called AFTER the whole chain is signed. Hardcoding
    ``status_after="draft"`` here regressed a freshly-``signed`` workflow
    back to ``draft`` and left it permanently unpublishable — found by
    running a real chain through request_signature and attach_signature
    end to end, not by inspection. Passing ``status_after=None`` instead
    is ALSO wrong: ``_rebuild_projection_for_workflow`` returns before
    projecting ``commit_workflow_commits`` at all when the latest event's
    status_after is None, which would silently drop this call's entire
    role/position write. Reading the current status and passing it back
    unchanged keeps both the status AND the commit-role projection
    correct regardless of when in the workflow's lifecycle this runs.
    """
    commit_shas: list[str] = []
    commit_roles: dict[str, tuple[str, int]] = {}
    for position, (original_sha, new_sha) in enumerate(chain):
        commit_shas.append(original_sha)
        commit_shas.append(new_sha)
        commit_roles[original_sha] = (ROLE_REWRITE_SOURCE, position)
        commit_roles[new_sha] = (ROLE_REWRITE_RESULT, position)

    current_state = _current_state_json(workflow_id, db_path=db_path)
    current_status = _current_status(workflow_id, db_path=db_path)
    commit_workflow_db.append_event(
        event_id=uuid.uuid4().hex,
        workflow_id=workflow_id,
        event_type="governed_rewrite_chain_recorded",
        status_after=current_status,
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


def _get_provisional_ref_tip(workflow_id: str, db_path=None) -> str | None:
    conn = commit_workflow_db._get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT payload_json FROM commit_workflow_approvals "
            "WHERE workflow_id = ? AND approval_type = 'supersede' AND status = 'approved'",
            (workflow_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return json.loads(row["payload_json"])["ref_tip"]


def record_force_with_lease_approval(
    *,
    workflow_id: str,
    repo_slug: str,
    approval_id: str,
    observed_ref_tip: str,
    delta_disclosed: bool = False,
    operator_id: str | None = None,
    db_path=None,
) -> None:
    """T2 (strictly after T0): record the force_with_lease approval with
    the BINDING lease — the ref-tip observed AT THIS CALL, never reused
    from the T0 supersede record even if the ref never moved.

    Requires a prior T0 supersede approval to already exist — T2 can
    never be recorded first. Without this, an out-of-order caller could
    record force_with_lease before any supersede approval, satisfying
    D4-14's dual-approval publish gate without the T0→T2 disclosure
    check ever running (there would be nothing to disclose a delta
    against). Codex's review finding.

    D4-16 disclosure gate: if the ref advanced between T0 and T2 (the T0
    supersede record's provisional ref-tip differs from ``observed_ref_tip``
    here), the T0→T2 delta must have been shown to the operator before
    this approval can be granted — raises unless the caller passes
    ``delta_disclosed=True``. When the ref never moved, disclosure isn't
    required and a single combined confirmation is allowed.

    Payload carries ``ref_tip``/``binding`` (this module's own reader,
    ``get_binding_lease``) AND a ``constraints.expected_ref_sha`` mirror of
    the same value — that second shape is what the REAL, landed
    ``commit.publish`` handler's own ``_approval_expected_old_sha`` reads
    (tools/dashboard/plugins/commit_api/entrypoints/api.py). Without it,
    publish's lease check silently finds no usable constraint and rejects
    every governed-rewrite publish attempt no matter how correctly T0/T2
    were recorded — found by reading the real handler before building
    D4-23, not by assuming this module's own shape was sufficient.
    """
    t0_ref_tip = _get_provisional_ref_tip(workflow_id, db_path=db_path)
    if t0_ref_tip is None:
        raise ValueError(
            "force_with_lease (T2) requires a prior supersede (T0) approval to already exist"
        )
    ref_advanced = t0_ref_tip != observed_ref_tip
    if ref_advanced and not delta_disclosed:
        raise ValueError(
            "force_with_lease approval blocked: the ref advanced between T0 and T2 "
            "and the intervening delta was not disclosed to the operator"
        )

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
                json.dumps({
                    "ref_tip": observed_ref_tip, "binding": True,
                    "ref_advanced": ref_advanced, "delta_disclosed": delta_disclosed,
                    "constraints": {"expected_ref_sha": observed_ref_tip},
                }),
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


# ── D4-17: prepare a rewrite for commit.request_signature ──────────────

ROLE_REWRITE_RESULT_SNAPSHOT_TYPE = "rewrite_result"


def prepare_rewrite_for_signing(
    *,
    workflow_id: str,
    repo_slug: str,
    corrected: CorrectedCommitMetadata,
    git_dir,
    store,
    trusted_store_conn,
    workflow_db_path=None,
) -> dict:
    """Prepare a governed rewrite's workflow so the UNMODIFIED
    ``commit.request_signature`` handler can fire a signing request for
    it exactly as for any ordinary proposal (§3 step 5) — no
    rewrite-specific branch in the signing path.

    Captures a SECOND, ``rewrite_result`` snapshot rather than mutating
    the D4-9 ``rewrite_source`` snapshot — N3's guarantee is that a
    snapshot's manifest/preview bindings freeze at capture and never
    change afterward, so a rewrite is two snapshots (source untouched,
    result correct-by-construction), never one snapshot edited in
    place. This is exactly what the ``rewrite_source``/``rewrite_result``
    snapshot_type enum and D4-12's source/result commit_roles chain
    already anticipate.

    Then merges ``unsigned_commit_sha``/``trusted_object_store_ref``/
    ``canonical_payload_hash`` into the workflow's state_json via a new
    event, transitioning status to ``awaiting_signature`` — the exact
    shape ``commit.request_signature``'s own precondition check reads
    (``state_payload.get("unsigned_commit_sha"/"trusted_object_store_ref"
    /"canonical_payload_hash")``), so the unmodified handler works
    against it with zero new fields it needs to know about.
    """
    unsigned_payload = serialize_unsigned_commit_payload(
        tree_oid=corrected.tree_oid,
        parent_oids=corrected.parent_oids,
        author_line=corrected.author.to_bytes(),
        committer_line=corrected.committer.to_bytes(),
        message=corrected.message,
    )
    unsigned_commit_sha = commit_object_sha(unsigned_payload)

    result_snapshot_ref = capture_snapshot(
        workflow_id=workflow_id,
        repo_slug=repo_slug,
        commit_sha=unsigned_commit_sha,
        tree_sha=corrected.tree_oid,
        parent_shas=list(corrected.parent_oids),
        git_dir=git_dir,
        store=store,
        dao_conn=trusted_store_conn,
        snapshot_type=ROLE_REWRITE_RESULT_SNAPSHOT_TYPE,
        canonical_payload=unsigned_payload,
    )
    snapshot = snapshot_dao.get_snapshot(trusted_store_conn, result_snapshot_ref)
    canonical_payload_hash = snapshot["canonical_preview_sha256"]

    current_state = _current_state_json(workflow_id, db_path=workflow_db_path)
    updated_state = {
        **current_state,
        "unsigned_commit_sha": unsigned_commit_sha,
        "tree_sha": corrected.tree_oid,
        "parent_shas": list(corrected.parent_oids),
        "trusted_object_store_ref": result_snapshot_ref,
        "canonical_payload_hash": canonical_payload_hash,
        "canonical_payload_preview": {
            "unsigned_commit_sha": unsigned_commit_sha,
            "tree_sha": corrected.tree_oid,
            "parent_shas": list(corrected.parent_oids),
        },
    }
    commit_workflow_db.append_event(
        event_id=uuid.uuid4().hex,
        workflow_id=workflow_id,
        event_type="governed_rewrite_ready_for_signature",
        status_after="awaiting_signature",
        repo_slug=repo_slug,
        payload=updated_state,
        db_path=workflow_db_path,
    )
    return {
        "unsigned_commit_sha": unsigned_commit_sha,
        "trusted_object_store_ref": result_snapshot_ref,
        "canonical_payload_hash": canonical_payload_hash,
    }


# ── D4-18/D4-19: sequential chain signing sharing one batch_group_id ───
#
# Each chain link is its OWN governed-rewrite workflow (its own D4-8
# call, its own D4-9/17 snapshots) -- "chain" is purely a cross-workflow
# concept added here via a shared batch_group_id on each link's
# commit_signing_requests row. commit.request_signature itself is never
# touched or branched (D4-17's own acceptance criterion): this module
# calls it exactly like an ordinary proposal for whichever link is ready,
# then stamps the batch_group_id/position_in_batch/batch_size columns
# onto the row it just created as a strictly-additive follow-up write --
# those columns are chain-signing bookkeeping the ordinary single-commit
# path has no reason to know about.
#
# NOTE on indexing: position_in_batch/batch_size below are 1-indexed,
# matching the already-landed local-signer read side (D3-19,
# local_signer_routes.py) exactly. This is a different axis from
# record_chain_source_and_result_roles' 0-indexed commit_workflow_commits
# `position` (D4-12) -- one indexes commit ROLES within a workflow, the
# other indexes SIGNING REQUESTS within a batch; they are not the same
# number space and are kept named distinctly on purpose.

CHAIN_BATCH_WAITING_STATUS = "waiting_on_predecessor"


def new_batch_group_id() -> str:
    """Mint one shared id for every signing request in a chain (§4)."""
    return uuid.uuid4().hex


def stamp_batch_fields(
    *,
    signing_request_id: str,
    batch_group_id: str,
    position_in_batch: int,
    batch_size: int,
    db_path=None,
) -> None:
    """Stamp batch_group_id/position_in_batch/batch_size onto a
    commit_signing_requests row that commit.request_signature already
    created. Called strictly AFTER request_signature returns -- the
    handler's own INSERT has no batch columns and is never modified to
    add them; this is out-of-band chain bookkeeping this module owns.
    """
    conn = commit_workflow_db._get_conn(db_path)
    try:
        conn.execute(
            "UPDATE commit_signing_requests "
            "SET batch_group_id = ?, position_in_batch = ?, batch_size = ? "
            "WHERE signing_request_id = ?",
            (batch_group_id, position_in_batch, batch_size, signing_request_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_batch_member_status(
    *,
    batch_group_id: str,
    position_in_batch: int,
    batch_size: int,
    db_path=None,
) -> dict:
    """Query one position of a chain-signing batch (§4, D4-18).

    Rows are created one at a time in chain order -- a not-yet-reached
    position has no commit_signing_requests row at all yet. Rather than
    404/None, this synthesizes the same ``waiting_on_predecessor`` answer
    the already-landed local-signer GET path (D3-19) returns for a row
    that exists but lacks a real payload hash: a not-yet-reachable
    position is a real, ordered chain member, just not ready.
    """
    conn = commit_workflow_db._get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT signing_request_id, workflow_id, canonical_payload_hash, status "
            "FROM commit_signing_requests WHERE batch_group_id = ? AND position_in_batch = ?",
            (batch_group_id, position_in_batch),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return {
            "signing_request_id": None,
            "workflow_id": None,
            "batch_group_id": batch_group_id,
            "position_in_batch": position_in_batch,
            "batch_size": batch_size,
            "canonical_payload_hash": None,
            "status": None,
            "batch_status": CHAIN_BATCH_WAITING_STATUS,
        }
    return {
        "signing_request_id": row["signing_request_id"],
        "workflow_id": row["workflow_id"],
        "batch_group_id": batch_group_id,
        "position_in_batch": position_in_batch,
        "batch_size": batch_size,
        "canonical_payload_hash": row["canonical_payload_hash"],
        "status": row["status"],
        "batch_status": None,
    }


def advance_chain_link(
    *,
    workflow_id: str,
    repo_slug: str,
    corrected: CorrectedCommitMetadata,
    git_dir,
    store,
    trusted_store_conn,
    workflow_db_path=None,
) -> dict:
    """Root-to-tip walk (§4, D4-19): fix a descendant's parent to its
    predecessor's FINAL post-signing SHA, then run it through D4-17
    exactly as the root link.

    A whole chain is ONE workflow_id (D4-12's ``record_chain_source_and_
    result_roles`` already establishes this -- one workflow spanning every
    ``rewrite_source``/``rewrite_result`` pair in the chain, distinguished
    by ``position``, not by separate workflow_ids). ``prepare_rewrite_for_
    signing`` is called again and again on that SAME workflow_id, once per
    link: each call overwrites the workflow's ``awaiting_signature``
    fields with the NEXT link's payload and flips status back to
    ``awaiting_signature`` (even after a prior link left it ``signed``),
    which is exactly what lets ``commit.request_signature`` fire again,
    unmodified, for the next link.

    Once a link's signature is attached, the workflow's state_json
    carries ``signed_commit_sha`` -- the final post-signing SHA
    (inserting ``gpgsig`` changes the object id per spec §11), never
    ``unsigned_commit_sha``. This reads that value BEFORE overwriting
    state for the next link, fails closed if it isn't there yet (the
    current link's own signature isn't attached), and fails closed if
    ``corrected`` wasn't actually built with that exact SHA as its sole
    parent -- a caller mistake here would silently orphan the next link
    from the real signed chain, so this is checked rather than trusted.
    """
    current_state = _current_state_json(workflow_id, db_path=workflow_db_path)
    predecessor_signed_sha = current_state.get("signed_commit_sha")
    if not predecessor_signed_sha:
        raise ValueError(
            f"workflow {workflow_id!r} has no signed_commit_sha yet -- "
            "the current link's signature must be attached before the next link's parent can be fixed"
        )
    if list(corrected.parent_oids) != [predecessor_signed_sha]:
        raise ValueError(
            "corrected metadata's parent must be exactly the predecessor's signed_commit_sha "
            f"({predecessor_signed_sha!r}), got {list(corrected.parent_oids)!r}"
        )
    return prepare_rewrite_for_signing(
        workflow_id=workflow_id,
        repo_slug=repo_slug,
        corrected=corrected,
        git_dir=git_dir,
        store=store,
        trusted_store_conn=trusted_store_conn,
        workflow_db_path=workflow_db_path,
    )


# ── D4-20: all-or-nothing chain -- block on any signing failure ────────

CHAIN_BLOCKED_STATUS = "blocked"


def block_chain_link_on_signing_failure(
    *,
    workflow_id: str,
    repo_slug: str,
    position_in_batch: int,
    batch_group_id: str,
    reason: str,
    db_path=None,
) -> None:
    """A chain link's own signing attempt failed (hash mismatch, revoked
    device mid-batch, operator cancel) -- transition ONLY that position's
    workflow to ``blocked``, naming the position and cause (§4, G14.4).

    No force-push happens for any position while a link is blocked:
    commit.publish is a separate, later step this module never calls as
    part of chain orchestration, so there is no partial-force-push code
    path to guard against here -- nothing in this module ever
    force-pushes. Already-signed sibling positions are untouched by this
    call (their own workflows are simply never named here), so a retry
    can reuse their signatures rather than restarting the whole batch.
    """
    current_state = _current_state_json(workflow_id, db_path=db_path)
    updated_state = {
        **current_state,
        "chain_blocked_reason": reason,
        "chain_blocked_position": position_in_batch,
        "chain_batch_group_id": batch_group_id,
    }
    commit_workflow_db.append_event(
        event_id=uuid.uuid4().hex,
        workflow_id=workflow_id,
        event_type="governed_rewrite_chain_blocked",
        status_after=CHAIN_BLOCKED_STATUS,
        repo_slug=repo_slug,
        payload=updated_state,
        db_path=db_path,
    )


# ── D4-23: publish with the T2 lease, fail closed on any divergence ────

PUBLISH_LEASE_STALE_STATUS = "failed_retryable"


def block_workflow_on_publish_lease_failure(
    *,
    workflow_id: str,
    repo_slug: str,
    reason: str,
    db_path=None,
) -> None:
    """React to commit.publish rejecting a stale T2 lease (§3 step 7,
    D4-23): transition the workflow to ``failed_retryable`` naming the
    cause.

    commit.publish's own lease check (``_approval_expected_old_sha``)
    already fails closed on a stale lease with NO mutation at all — an
    early return before any INSERT/UPDATE, so the ref is left unchanged
    and the workflow's prior status/signatures are already untouched by
    construction; nothing here needs to preserve them, they were never
    at risk. This call is the governed-rewrite-specific reaction layered
    on top: it makes the failure VISIBLE on the workflow itself (an
    operator watching status sees ``failed_retryable``, not a workflow
    that silently looks like nothing happened), distinct from D4-20's
    ``blocked`` — a stale lease only needs a fresh T2 re-approval at the
    new ref-tip (T0 is still valid, D4-15's dual-approval gate doesn't
    need re-running from scratch), not the harder intervention a signing
    failure requires.
    """
    current_state = _current_state_json(workflow_id, db_path=db_path)
    updated_state = {**current_state, "publish_lease_failure_reason": reason}
    commit_workflow_db.append_event(
        event_id=uuid.uuid4().hex,
        workflow_id=workflow_id,
        event_type="governed_rewrite_publish_lease_stale",
        status_after=PUBLISH_LEASE_STALE_STATUS,
        repo_slug=repo_slug,
        payload=updated_state,
        db_path=db_path,
    )


def clear_publish_lease_failure_for_retry(
    *,
    workflow_id: str,
    repo_slug: str,
    db_path=None,
) -> None:
    """After a fresh T2 approval lands at the new ref-tip, restore the
    workflow to ``signed`` so ``commit.publish``'s own precondition
    (``status in {"signed", "awaiting_publish", "published"}``) accepts a
    retry (§3 step 7, D4-23).

    Deliberately a separate call from ``record_force_with_lease_approval``
    rather than a side effect of it: recording an approval is pure
    bookkeeping over ``commit_workflow_approvals`` with no opinion on
    workflow status, and not every ``force_with_lease`` approval follows a
    ``failed_retryable`` rejection (the very first T2 approval on a
    workflow that's never failed has nothing to clear). The caller decides
    when a retry is actually being attempted.
    """
    current_state = _current_state_json(workflow_id, db_path=db_path)
    updated_state = {k: v for k, v in current_state.items() if k != "publish_lease_failure_reason"}
    commit_workflow_db.append_event(
        event_id=uuid.uuid4().hex,
        workflow_id=workflow_id,
        event_type="governed_rewrite_publish_lease_retry_cleared",
        status_after="signed",
        repo_slug=repo_slug,
        payload=updated_state,
        db_path=db_path,
    )


# ── single-target-branch restriction ────────────────────────────────────


class MultiTargetRefError(ValueError):
    """A rewrite's commits would land on more than one target branch."""


def validate_single_target_ref(target_refs: Sequence[str]) -> str:
    """Reject a chain of commits that would span more than one target
    branch (§3, §6).

    There is no mechanism here for updating two branches as a single
    all-or-nothing operation, so a rewrite that landed a signed commit on
    one branch and then failed partway through a second branch would have
    no way to undo the first branch's change. Restricting every rewrite
    to exactly one target branch avoids that half-finished, unrecoverable
    state entirely rather than trying to build a rollback for it.

    ``target_refs`` is the target branch resolved for each commit in the
    chain (whatever the caller's own policy resolution decided per
    commit) — this function does no resolution of its own, it only
    checks that they all agree. Returns the single branch name if the
    chain is valid.

    NOT YET WIRED INTO ANY REAL CALL PATH: nothing in this codebase calls
    this function outside its own tests. A caller starting a real rewrite
    must call this BEFORE its own first ``create_governed_rewrite_
    workflow`` call for the guarantee to actually hold — today nothing
    enforces that ordering, because no such caller/orchestrator exists
    yet. This function is a validated building block, not live
    enforcement, until it is wired into whatever entry point starts a
    real rewrite.
    """
    distinct = sorted(set(target_refs))
    if not distinct:
        raise ValueError("no target refs supplied")
    if len(distinct) > 1:
        raise MultiTargetRefError(
            f"this rewrite's commits target more than one branch ({', '.join(distinct)}) -- "
            "split it into independent rewrites, one per target branch, before proceeding"
        )
    return distinct[0]


# ── chain publish: the ordered links a chain-aware push needs ──────────


def get_chain_publish_links(*, batch_group_id: str, db_path=None) -> list[tuple[str, str]]:
    """Return the ordered ``(snapshot_ref, signed_object_sha256)`` for every
    link in a chain-signing batch, root to tip (§4, D4-18/19) -- exactly the
    ``links`` shape ``commit_broker.pusher.build_chain_pusher`` needs to
    materialize a governed-rewrite chain's full ancestry before pushing the
    tip.

    Every link in a batch already carries what's needed once fully signed:
    its own ``trusted_object_store_ref`` (the rewrite_result snapshot
    ``prepare_rewrite_for_signing``/``advance_chain_link`` captured for that
    link) and its own ``signed_object_sha256`` (recorded on the signing
    request's ``payload_json`` once ``attach_signature`` assembles and
    stores the signed bytes). No new data needs capturing here -- this is
    purely a read, ordered by ``position_in_batch``, which is already
    exactly how ``stamp_batch_fields``/``get_batch_member_status`` order a
    batch elsewhere in this module.

    Raises if any link in the batch hasn't actually been signed yet
    (no ``signed_object_sha256`` recorded) -- a caller building the publish
    links must only do so once every position in the batch is signed;
    D4-20's own blocking guard is what should have stopped a caller from
    reaching this point on an incomplete chain in the first place.
    """
    conn = commit_workflow_db._get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT signing_request_id, position_in_batch, trusted_object_store_ref, payload_json "
            "FROM commit_signing_requests WHERE batch_group_id = ? ORDER BY position_in_batch",
            (batch_group_id,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        raise ValueError(f"no signing requests found for batch_group_id={batch_group_id!r}")

    links: list[tuple[str, str]] = []
    for row in rows:
        payload = json.loads(row["payload_json"] or "{}")
        signed_object_sha256 = payload.get("signed_object_sha256")
        if not signed_object_sha256:
            raise ValueError(
                f"signing request {row['signing_request_id']!r} (position {row['position_in_batch']!r} "
                f"in batch {batch_group_id!r}) has no signed_object_sha256 yet -- the chain isn't fully signed"
            )
        trusted_object_store_ref = row["trusted_object_store_ref"]
        if not trusted_object_store_ref:
            raise ValueError(
                f"signing request {row['signing_request_id']!r} (position {row['position_in_batch']!r} "
                f"in batch {batch_group_id!r}) has no trusted_object_store_ref"
            )
        links.append((trusted_object_store_ref, signed_object_sha256))
    return links
