"""Typed request/response models for the commit@1 API.

These are the shared contract objects for the Sprint 3 commit workflow
tranche. They intentionally stay free of transport concerns so handlers
can use them for validation, serialization, and tests.
"""

from __future__ import annotations

import types
from dataclasses import dataclass, fields
from typing import Any, ClassVar, Literal, Mapping, get_args, get_origin, get_type_hints
import uuid


JsonDict = dict[str, Any]


def _new_correlation_id() -> str:
    return uuid.uuid4().hex


def _is_dataclass_type(tp: Any) -> bool:
    return isinstance(tp, type) and hasattr(tp, "from_dict") and hasattr(tp, "to_dict")


def _encode(value: Any) -> Any:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return value.to_dict()
    if isinstance(value, list):
        return [_encode(item) for item in value]
    if isinstance(value, tuple):
        return [_encode(item) for item in value]
    if isinstance(value, dict):
        return {key: _encode(item) for key, item in value.items()}
    return value


def _decode(type_hint: Any, value: Any) -> Any:
    if value is None:
        return None
    origin = get_origin(type_hint)
    if _is_dataclass_type(type_hint) and isinstance(value, dict):
        return type_hint.from_dict(value)
    if origin is list:
        (item_type,) = get_args(type_hint) or (Any,)
        return [_decode(item_type, item) for item in value]
    if origin is dict:
        key_type, val_type = get_args(type_hint) or (Any, Any)
        if val_type in (Any, object):
            return dict(value)
        return {key: _decode(val_type, item) for key, item in value.items()}
    if origin is tuple:
        item_types = get_args(type_hint)
        if len(item_types) == 2 and item_types[1] is Ellipsis:
            return tuple(_decode(item_types[0], item) for item in value)
        return tuple(_decode(item_type, item) for item_type, item in zip(item_types, value))
    if origin is Literal:
        allowed = get_args(type_hint)
        if value not in allowed:
            raise ValueError(f"invalid literal {value!r}; expected one of {allowed!r}")
        return value
    if origin is types.UnionType:
        union_args = get_args(type_hint)
        non_none = [arg for arg in union_args if arg is not type(None)]
        if len(non_none) == 1:
            return _decode(non_none[0], value)
        return value
    union_args = get_args(type_hint)
    if union_args and type(None) in union_args:
        non_none = [arg for arg in union_args if arg is not type(None)]
        if len(non_none) == 1:
            return _decode(non_none[0], value)
    return value


class CommitApiModel:
    """Mixin for typed commit API dataclasses."""

    _ENUM_FIELDS: ClassVar[Mapping[str, frozenset[str]]] = {}
    _LIST_ENUM_FIELDS: ClassVar[Mapping[str, frozenset[str]]] = {}

    def __post_init__(self) -> None:  # pragma: no cover - exercised via subclasses
        for field_name, allowed in self._ENUM_FIELDS.items():
            value = getattr(self, field_name)
            if value not in allowed:
                raise ValueError(f"{type(self).__name__}.{field_name} must be one of {sorted(allowed)!r}")
        for field_name, allowed in self._LIST_ENUM_FIELDS.items():
            values = getattr(self, field_name)
            for value in values:
                if value not in allowed:
                    raise ValueError(
                        f"{type(self).__name__}.{field_name} entry {value!r} must be one of {sorted(allowed)!r}"
                    )

    def to_dict(self) -> JsonDict:
        return {field.name: _encode(getattr(self, field.name)) for field in fields(self)}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> CommitApiModel:
        hints = get_type_hints(cls)
        kwargs = {}
        for field in fields(cls):
            if field.name not in payload:
                continue
            kwargs[field.name] = _decode(hints.get(field.name, Any), payload[field.name])
        return cls(**kwargs)  # type: ignore[misc]


ACTOR_TYPES = frozenset({"agent_session", "operator", "dashboard", "reconciler", "local_signer"})
AUTH_STRENGTHS = frozenset({"session_token", "operator_passkey", "device_pairing", "internal"})
HANDOFF_STATES = frozenset({"owner_only", "explicit_handoff"})
NEXT_ACTIONS = frozenset({
    "none",
    "revise_message",
    "approve",
    "create",
    "request_signature",
    "attach_signature",
    "publish",
    "link_review",
    "mark_landed",
    "resolve_blocker",
})
NEXT_REQUIRED_ACTORS = frozenset({"agent_session", "operator", "dashboard", "local_signer", "reconciler"})
APPROVAL_TYPES = frozenset({"commit", "push", "force_with_lease", "sign", "abandon", "supersede"})
APPROVAL_STATUSES = frozenset({"pending", "approved", "rejected", "expired", "revoked"})
SIGNING_STATUSES = frozenset({"pending", "signed", "rejected", "expired", "failed"})
SIGNING_METHODS = frozenset({"gpg", "ssh"})
DETERMINISM_CLASSES = frozenset({
    "deterministic",
    "provider_authoritative",
    "operator_judgment",
    "agentic_advisory",
})
RESOLVE_FORMATS = frozenset({"text", "json", "both"})
VALIDATION_LEVELS = frozenset({"policy", "content", "pre_sensitive_operation"})
APPROVE_DECISIONS = frozenset({"approved", "rejected"})
CREATE_MODES = frozenset({"unsigned_commit", "snapshot_for_signature"})
DRIFT_STATUSES = frozenset({"clean", "worktree_changed", "head_changed", "store_changed", "unknown"})
POLICY_STATUSES = frozenset({
    "ok",
    "needs_reproposal",
    "blocked_by_stricter_current_policy",
    "incoherent",
})
PUBLISH_MODES = frozenset({
    "local_only_noop",
    "workspace_shared",
    "origin_push",
    "direct_target_update",
    "pr_branch_push",
})
LANDING_SOURCES = frozenset({"autonomy_host_merge", "provider_merge", "reconciler"})
RECONCILE_TIERS = frozenset({
    "event_projection",
    "sha_ancestry",
    "patch_id",
    "provider_pr",
    "hard_negative_absence",
    "agentic_advisory",
})
REF_UPDATE_OPERATIONS = frozenset({
    "create_new_ref",
    "fast_forward_existing_ref",
    "force_update_existing_ref",
    "delete_existing_ref",
})


@dataclass(frozen=True)
class Actor(CommitApiModel):
    actor_type: str
    actor_id: str
    session_name: str | None
    operator_id: str | None
    provider_identity: dict[str, Any] | None
    auth_strength: str

    _ENUM_FIELDS = {
        "actor_type": ACTOR_TYPES,
        "auth_strength": AUTH_STRENGTHS,
    }


@dataclass(frozen=True)
class ActorSnapshot(CommitApiModel):
    actor_type: str
    actor_id: str | None
    session_name: str | None
    operator_id: str | None

    _ENUM_FIELDS = {
        "actor_type": ACTOR_TYPES,
    }


@dataclass(frozen=True)
class RepoScope(CommitApiModel):
    workspace_id: str
    repo_slug: str
    repo_name_alias: str | None
    session_name: str | None
    worktree_path: str | None
    branch: str | None
    target_branch: str | None


@dataclass(frozen=True)
class GitIdentity(CommitApiModel):
    name: str
    email: str
    timestamp: str | None
    timezone: str | None


@dataclass(frozen=True)
class CommitMessage(CommitApiModel):
    subject: str
    body: str
    trailers: dict[str, Any]


@dataclass(frozen=True)
class WorkflowRef(CommitApiModel):
    workflow_id: str
    repo_slug: str
    status: str
    terminal: bool
    latest_event_id: str
    created_by_session_name: str | None
    created_by_actor_id: str | None
    mutation_owner_session_name: str | None
    handoff_state: str

    _ENUM_FIELDS = {
        "handoff_state": HANDOFF_STATES,
    }


@dataclass(frozen=True)
class ResolvedPlanRef(CommitApiModel):
    policy_key: str
    policy_version: str
    profile: str
    resolved_plan: dict[str, Any]
    security_dimensions_hash: str


@dataclass(frozen=True)
class DriftToken(CommitApiModel):
    head_sha: str
    tree_sha: str | None
    index_sha: str | None
    worktree_status_hash: str
    generated_at: str


@dataclass(frozen=True)
class ContentIdentity(CommitApiModel):
    head_sha: str
    tree_sha: str
    patch_id: str
    content_fingerprint: str


@dataclass(frozen=True)
class ApprovalSummary(CommitApiModel):
    approval_id: str
    approval_type: str
    status: str
    provider_permission_checked: bool
    expected_ref_sha: str | None

    _ENUM_FIELDS = {
        "approval_type": APPROVAL_TYPES,
        "status": APPROVAL_STATUSES,
    }


@dataclass(frozen=True)
class SigningSummary(CommitApiModel):
    signing_request_id: str
    status: str
    signing_method: str
    canonical_payload_hash: str
    trusted_object_store_ref: str
    expires_at: str | None

    _ENUM_FIELDS = {
        "status": SIGNING_STATUSES,
        "signing_method": SIGNING_METHODS,
    }


@dataclass(frozen=True)
class WorkflowEventRecord(CommitApiModel):
    event_id: str
    workflow_id: str
    event_type: str
    status_after: str | None
    occurred_at: str
    actor_snapshot: ActorSnapshot
    determinism_class: str
    result: dict[str, Any]

    _ENUM_FIELDS = {
        "determinism_class": DETERMINISM_CLASSES,
    }


@dataclass(frozen=True)
class NextAction(CommitApiModel):
    action: str
    required_actor: str
    reason: str

    _ENUM_FIELDS = {
        "action": NEXT_ACTIONS,
        "required_actor": NEXT_REQUIRED_ACTORS,
    }


@dataclass(frozen=True)
class CommitWorkflowResponse(CommitApiModel):
    workflow: WorkflowRef
    plan: ResolvedPlanRef
    approvals: list[ApprovalSummary]
    signing: SigningSummary | None
    next_action: NextAction
    events: list[WorkflowEventRecord]
    warnings: list[dict[str, Any]]


@dataclass(frozen=True)
class ResolvePolicyIntent(CommitApiModel):
    desired_visibility: str | None
    audience: list[str]
    review_target: dict[str, Any] | None
    push_intent: dict[str, Any] | None


@dataclass(frozen=True)
class ResolvePolicyRequest(CommitApiModel):
    scope: RepoScope
    intent: ResolvePolicyIntent


@dataclass(frozen=True)
class ResolvePolicyResponse(CommitApiModel):
    canonical_scope: RepoScope
    plan: ResolvedPlanRef
    required_steps: list[NextAction]
    disallowed_steps: list[dict[str, Any]]
    ui_hints: dict[str, Any]


@dataclass(frozen=True)
class DescribePolicyRequest(CommitApiModel):
    workspace_id: str | None
    repo_slug: str | None
    repo_name: str | None
    session_name: str | None
    target_branch: str | None
    format: str

    _ENUM_FIELDS = {
        "format": RESOLVE_FORMATS,
    }


@dataclass(frozen=True)
class DescribePolicyResponse(CommitApiModel):
    canonical_scope: RepoScope
    plan: ResolvedPlanRef
    primer_text: str | None
    allowed_actions: list[str]
    required_actions: list[str]
    forbidden_actions: list[dict[str, Any]]
    source_settings_keys: list[str]


@dataclass(frozen=True)
class CommitProposeTarget(CommitApiModel):
    target_branch: str
    ref_strategy: str
    intended_visibility: str
    review_target: dict[str, Any] | None


@dataclass(frozen=True)
class CommitProposeRequest(CommitApiModel):
    idempotency_key: str
    scope: RepoScope
    target: CommitProposeTarget
    content: ContentIdentity
    drift_token: DriftToken
    message: CommitMessage
    author: GitIdentity
    committer: GitIdentity
    signoff_present: bool
    issue_refs: list[str]
    push_pr_intent: dict[str, Any] | None
    client_observed_policy_version: str | None


@dataclass(frozen=True)
class CommitProposeResponse(CommitApiModel):
    workflow: WorkflowRef
    plan: ResolvedPlanRef
    duplicate_of_workflow_id: str | None
    approvals: list[ApprovalSummary]
    drift_validation_token: str
    next_action: NextAction
    events: list[str]


@dataclass(frozen=True)
class CommitReviseMessageRequest(CommitApiModel):
    idempotency_key: str
    workflow_id: str
    message: CommitMessage
    actor_note: str | None


@dataclass(frozen=True)
class CommitReviseMessageResponse(CommitApiModel):
    workflow: WorkflowRef
    message_fingerprint: str
    invalidated_approval_ids: list[str]
    invalidated_signing_request_ids: list[str]
    next_action: NextAction
    events: list[str]


@dataclass(frozen=True)
class CommitValidateRequest(CommitApiModel):
    workflow_id: str
    drift_token: DriftToken | None
    validation_level: str

    _ENUM_FIELDS = {
        "validation_level": VALIDATION_LEVELS,
    }


@dataclass(frozen=True)
class CommitValidateResponse(CommitApiModel):
    workflow: WorkflowRef
    drift_status: str
    policy_status: str
    signoff_status: dict[str, Any]
    authorship_status: dict[str, Any]
    required_next_steps: list[NextAction]
    errors: list[dict[str, Any]]

    _ENUM_FIELDS = {
        "drift_status": DRIFT_STATUSES,
        "policy_status": POLICY_STATUSES,
    }


@dataclass(frozen=True)
class ApprovalConstraints(CommitApiModel):
    diff_fingerprint: str
    message_fingerprint: str
    expected_ref_sha: str | None
    expires_at: str | None


@dataclass(frozen=True)
class CommitApproveRequest(CommitApiModel):
    idempotency_key: str
    workflow_id: str
    approval_id: str
    approval_type: str
    decision: str
    operator_edits: dict[str, Any] | None
    constraints: ApprovalConstraints
    provider_entitlement_proof: dict[str, Any] | None

    _ENUM_FIELDS = {
        "approval_type": APPROVAL_TYPES,
        "decision": APPROVE_DECISIONS,
    }


@dataclass(frozen=True)
class CommitApproveResponse(CommitApiModel):
    workflow: WorkflowRef
    approval: ApprovalSummary
    next_action: NextAction
    events: list[str]


@dataclass(frozen=True)
class CommitCreateRequest(CommitApiModel):
    idempotency_key: str
    workflow_id: str
    drift_token: DriftToken
    create_mode: str

    _ENUM_FIELDS = {
        "create_mode": CREATE_MODES,
    }


@dataclass(frozen=True)
class CommitCreateResponse(CommitApiModel):
    workflow: WorkflowRef
    content: ContentIdentity
    unsigned_commit_sha: str | None
    trusted_object_store_ref: str | None
    canonical_payload_hash: str | None
    signing: SigningSummary | None
    next_action: NextAction
    events: list[str]


@dataclass(frozen=True)
class CommitRequestSignatureRequest(CommitApiModel):
    idempotency_key: str
    workflow_id: str
    signing_method: str
    signer_policy_version: str
    requested_operator_id: str | None

    _ENUM_FIELDS = {
        "signing_method": SIGNING_METHODS,
    }


@dataclass(frozen=True)
class CommitRequestSignatureResponse(CommitApiModel):
    workflow: WorkflowRef
    signing: SigningSummary
    canonical_payload_preview: dict[str, Any]
    local_signer_handoff: dict[str, Any]
    next_action: NextAction
    events: list[str]


@dataclass(frozen=True)
class LocalSignerAttestation(CommitApiModel):
    device_id: str
    request_nonce: str
    canonical_payload_hash: str
    displayed_payload_hash: str
    signed_at: str


@dataclass(frozen=True)
class CommitAttachSignatureRequest(CommitApiModel):
    idempotency_key: str
    signing_request_id: str
    signature_ref: str | None
    armored_signature: str | None
    signed_commit_object_ref: str | None
    local_signer_attestation: LocalSignerAttestation


@dataclass(frozen=True)
class CommitAttachSignatureResponse(CommitApiModel):
    workflow: WorkflowRef
    signing: SigningSummary
    signed_commit_sha: str
    verification: dict[str, Any]
    next_action: NextAction
    events: list[str]


@dataclass(frozen=True)
class CommitPublishRefUpdateIntent(CommitApiModel):
    provider: str | None
    repo_slug: str
    ref: str
    operation: str
    expected_old_sha: str | None
    new_sha: str

    _ENUM_FIELDS = {
        "operation": REF_UPDATE_OPERATIONS,
    }


@dataclass(frozen=True)
class CommitPublishRequest(CommitApiModel):
    idempotency_key: str
    workflow_id: str
    ref_update_intent: CommitPublishRefUpdateIntent
    publish_mode: str

    _ENUM_FIELDS = {
        "publish_mode": PUBLISH_MODES,
    }


@dataclass(frozen=True)
class CommitPublishResponse(CommitApiModel):
    workflow: WorkflowRef
    ref_update_result: dict[str, Any]
    provider_url: str | None
    pushed_ref: str | None
    next_action: NextAction
    events: list[str]


@dataclass(frozen=True)
class CommitLinkReviewRequest(CommitApiModel):
    idempotency_key: str
    workflow_id: str
    provider: str
    review_id: str | None
    create_if_missing: bool
    base_ref: str
    head_ref: str


@dataclass(frozen=True)
class CommitLinkReviewResponse(CommitApiModel):
    workflow: WorkflowRef
    review: dict[str, Any]
    review_binding_key: str
    watch_armed: bool
    next_action: NextAction
    events: list[str]


@dataclass(frozen=True)
class CommitMarkLandedEvidence(CommitApiModel):
    target_ref: str
    target_sha: str
    provider_merged_at: str | None
    commit_shas: list[str]


@dataclass(frozen=True)
class CommitMarkLandedRequest(CommitApiModel):
    idempotency_key: str
    workflow_id: str
    landing_source: str
    evidence: CommitMarkLandedEvidence

    _ENUM_FIELDS = {
        "landing_source": LANDING_SOURCES,
    }


@dataclass(frozen=True)
class CommitMarkLandedResponse(CommitApiModel):
    workflow: WorkflowRef
    next_action: NextAction
    events: list[str]


@dataclass(frozen=True)
class CommitSupersedeReplacement(CommitApiModel):
    replacement_workflow_id: str | None
    replacement_commit_shas: list[str]
    reason: str


@dataclass(frozen=True)
class CommitSupersedeRequest(CommitApiModel):
    idempotency_key: str
    workflow_id: str
    replacement: CommitSupersedeReplacement
    requires_operator_approval: bool


@dataclass(frozen=True)
class CommitSupersedeResponse(CommitApiModel):
    workflow: WorkflowRef
    lineage: dict[str, Any]
    events: list[str]


@dataclass(frozen=True)
class CommitAbandonRequest(CommitApiModel):
    idempotency_key: str
    workflow_id: str
    reason: str
    teardown_worktree: bool


@dataclass(frozen=True)
class CommitAbandonResponse(CommitApiModel):
    workflow: WorkflowRef
    teardown: dict[str, Any] | None
    events: list[str]


@dataclass(frozen=True)
class CommitMarkRevertedEvidence(CommitApiModel):
    target_ref: str
    missing_commit_shas: list[str]
    revert_commit_sha: str | None
    observed_at: str


@dataclass(frozen=True)
class CommitMarkRevertedRequest(CommitApiModel):
    idempotency_key: str
    workflow_id: str
    evidence: CommitMarkRevertedEvidence


@dataclass(frozen=True)
class CommitMarkRevertedResponse(CommitApiModel):
    workflow: WorkflowRef
    events: list[str]


@dataclass(frozen=True)
class CommitReconcileRequest(CommitApiModel):
    idempotency_key: str
    scope: RepoScope
    workflow_ids: list[str] | None
    tiers: list[str]
    dry_run: bool

    _LIST_ENUM_FIELDS = {
        "tiers": RECONCILE_TIERS,
    }


@dataclass(frozen=True)
class CommitReconcileResponse(CommitApiModel):
    observations: list[dict[str, Any]]
    events: list[str]
    dry_run: bool
