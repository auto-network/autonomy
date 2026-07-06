"""Commit workflow policy resolution and description.

This module is intentionally provider-neutral. It turns Settings rows into a
plain workflow plan, validates whether that plan is coherent for the current
workspace capabilities, and renders the agent-facing instructions used by both
startup primers and ``graph commit policy describe``.
"""

from __future__ import annotations

import copy
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping

from . import settings_ops
from .schemas.commit_policy import (
    COMMIT_POLICY_REVISION,
    COMMIT_POLICY_SET_ID,
    OPERATION_POLICY_REVISION,
    OPERATION_POLICY_SET_ID,
)


class CommitPolicyError(ValueError):
    """Raised when a commit policy is malformed or incoherent."""


RefUpdateOperation = Literal[
    "create_new_ref",
    "fast_forward_existing_ref",
    "force_update_existing_ref",
    "delete_existing_ref",
    "unknown",
]


@dataclass(frozen=True)
class BranchProtectionEvidence:
    provider: str
    repo_slug: str
    ref: str
    probed_at: float
    force_push_blocked: bool
    deletion_blocked: bool
    required_reviews: bool = False
    required_status_checks: bool = False
    stale_after_seconds: int = 3600


@dataclass(frozen=True)
class RefUpdateIntent:
    provider: str
    repo_slug: str
    ref: str
    operation: RefUpdateOperation


@dataclass(frozen=True)
class WorkspaceCapabilityContext:
    issue_tracker_enabled: bool = False
    gpg_signer_available: bool = False
    ssh_signer_available: bool = False
    operation_policies: Mapping[str, dict] = field(default_factory=dict)
    branch_protection: Mapping[tuple[str, str, str], BranchProtectionEvidence] = field(default_factory=dict)
    now: float = field(default_factory=time.time)
    policy_max_branch_protection_stale_seconds: int = 3600
    ref_update_intent: RefUpdateIntent | None = None


@dataclass(frozen=True)
class ResolvedCommitPolicy:
    key: str
    source: str
    profile: str
    payload: dict
    errors: tuple[str, ...] = ()


SAFE_DEFAULT_PROFILE = "safe.default"
AUTONOMY_PROFILE = "autonomy.direct-master"
ENTERPRISE_PROFILE = "enterprise.signed-pr"


BUILTIN_PROFILES: dict[str, dict] = {
    SAFE_DEFAULT_PROFILE: {
        "profile": SAFE_DEFAULT_PROFILE,
        "commit_destination": "local_only",
        "branch_mode": "current_branch",
        "visibility": "local_only",
        "push_requirement": "forbidden",
        "review_integration": "none",
        "signature_requirement": "none",
        "signing_boundary": "none",
        "provider_credential_boundary": "none",
        "ref_update_permissions": "fast_forward_only",
        "reviewer_audience": [],
        "author_policy": {"require_operator_confirmation": False, "require_signoff": False},
        "watch_policy": {"mode": "none"},
        "issue_linkage": {"required": False},
        "operation_overrides": {},
        "override_mode": "none",
    },
    AUTONOMY_PROFILE: {
        "profile": AUTONOMY_PROFILE,
        "commit_destination": "local_integration_branch",
        "branch_mode": "managed_session_branch",
        "visibility": "workspace_shared",
        "push_requirement": "forbidden",
        "review_integration": "none",
        "signature_requirement": "none",
        "signing_boundary": "none",
        "provider_credential_boundary": "none",
        "ref_update_permissions": "fast_forward_only",
        "reviewer_audience": ["same_workspace_agent"],
        "author_policy": {"require_operator_confirmation": False, "require_signoff": False},
        "watch_policy": {"mode": "none"},
        "issue_linkage": {"required": False},
        "operation_overrides": {},
        "override_mode": "none",
    },
    ENTERPRISE_PROFILE: {
        "profile": ENTERPRISE_PROFILE,
        "commit_destination": "provider_review",
        "branch_mode": "pr_branch",
        "visibility": "provider_review_published",
        "push_requirement": "required",
        "review_integration": "required_pr_watch",
        "signature_requirement": "signoff_and_gpg",
        "signing_boundary": "human_local_crypto_required",
        "provider_credential_boundary": "broker_required",
        "ref_update_permissions": "fast_forward_only",
        "reviewer_audience": ["operator", "provider_reviewers"],
        "author_policy": {"require_operator_confirmation": True, "require_signoff": True},
        "watch_policy": {"mode": "nag_when_terminal"},
        "issue_linkage": {"required": True, "capability_contract": "issue_tracker"},
        "operation_overrides": {},
        "override_mode": "none",
    },
    "local-review-branch": {
        "profile": "local-review-branch",
        "commit_destination": "workspace_shared",
        "branch_mode": "new_topic_branch",
        "visibility": "workspace_shared",
        "push_requirement": "forbidden",
        "review_integration": "none",
        "signature_requirement": "none",
        "signing_boundary": "none",
        "provider_credential_boundary": "none",
        "ref_update_permissions": "fast_forward_only",
        "reviewer_audience": ["same_workspace_agent"],
        "author_policy": {"require_operator_confirmation": False, "require_signoff": False},
        "watch_policy": {"mode": "none"},
        "issue_linkage": {"required": False},
        "operation_overrides": {},
        "override_mode": "none",
    },
    "cross-user-shared": {
        "profile": "cross-user-shared",
        "commit_destination": "origin_branch",
        "branch_mode": "managed_session_branch",
        "visibility": "origin_published",
        "push_requirement": "required_for_cross_user_handoff",
        "review_integration": "optional_pr",
        "signature_requirement": "none",
        "signing_boundary": "none",
        "provider_credential_boundary": "proxy_allowed",
        "ref_update_permissions": "fast_forward_only",
        "reviewer_audience": ["cross_user_session"],
        "author_policy": {"require_operator_confirmation": False, "require_signoff": False},
        "watch_policy": {"mode": "none"},
        "issue_linkage": {"required": False},
        "operation_overrides": {
            "source_control.push": {
                "execution_class": "capability_api",
                "notes": (
                    "Capability service pre-flights that the origin branch "
                    "does not exist, then performs the initial push."
                ),
            },
        },
        "override_mode": "none",
    },
}


_DIMENSION_KEYS = {
    "commit_destination",
    "branch_mode",
    "visibility",
    "push_requirement",
    "review_integration",
    "signature_requirement",
    "signing_boundary",
    "provider_credential_boundary",
    "ref_update_permissions",
}

_METADATA_KEYS = {
    "workspace_id",
    "repo_slug",
    "repo_aliases",
    "applies_to",
    "coherence_version",
    "notes",
}

_STRUCTURED_PROTECTED_KEYS = {
    "issue_linkage",
    "author_policy",
    "watch_policy",
    "reviewer_audience",
    "operation_overrides",
}

_ORDERED_DIMENSIONS = {
    "provider_credential_boundary": ["none", "proxy_allowed", "broker_required"],
    "signing_boundary": ["none", "broker_mediated", "human_local_crypto_required"],
}

_SIGNATURE_STRENGTH = {
    "none": set(),
    "signoff": {"signoff"},
    "gpg": {"gpg"},
    "ssh": {"ssh"},
    "signoff_and_gpg": {"signoff", "gpg"},
    "signoff_and_ssh": {"signoff", "ssh"},
}

_AUDIT_ORDER = ["minimal", "standard", "full"]
_EXECUTION_CLASS_ORDER = ["raw_proxy", "local", "capability_api", "broker"]


def _copy_profile(name: str) -> dict:
    try:
        return copy.deepcopy(BUILTIN_PROFILES[name])
    except KeyError as exc:
        raise CommitPolicyError(f"unknown commit policy profile: {name}") from exc


def _rank(value: str, order: list[str]) -> int:
    try:
        return order.index(value)
    except ValueError as exc:
        raise CommitPolicyError(f"unknown ordered value: {value}") from exc


def _is_narrower_or_equal(field_name: str, base: Any, candidate: Any) -> bool:
    if candidate == base:
        return True
    if field_name in _ORDERED_DIMENSIONS:
        order = _ORDERED_DIMENSIONS[field_name]
        return _rank(str(candidate), order) >= _rank(str(base), order)
    if field_name == "signature_requirement":
        return _SIGNATURE_STRENGTH[str(candidate)] >= _SIGNATURE_STRENGTH[str(base)]
    # These dimensions are intentionally not globally ordered; the only safe
    # automatic override is "no change". More nuanced transitions need a named
    # profile or explicit code review.
    return False


def expand_commit_policy_payload(payload: dict | None) -> dict:
    """Expand a raw Settings payload into a complete policy payload."""
    if not payload:
        return _copy_profile(SAFE_DEFAULT_PROFILE)
    raw = copy.deepcopy(payload)
    profile = raw.get("profile")
    if not profile:
        return raw
    base = _copy_profile(profile)
    override_mode = raw.get("override_mode", base.get("override_mode", "none"))
    if override_mode not in ("none", "narrow"):
        raise CommitPolicyError(f"invalid override_mode: {override_mode!r}")
    for key, value in raw.items():
        if key in {"profile", "override_mode"}:
            continue
        if key in _METADATA_KEYS:
            base[key] = value
            continue
        if override_mode == "none":
            if key in base and value != base[key]:
                raise CommitPolicyError(
                    f"profile {profile!r} uses override_mode=none but "
                    f"field {key!r} changes the built-in value"
                )
            if key not in base:
                raise CommitPolicyError(
                    f"profile {profile!r} uses override_mode=none but "
                    f"adds unknown field {key!r}"
                )
            continue
        if key in _STRUCTURED_PROTECTED_KEYS and value != base.get(key):
            raise CommitPolicyError(
                f"structured field {key!r} cannot be changed under "
                f"override_mode=narrow without a field-specific narrowing rule"
            )
        if key in _DIMENSION_KEYS and not _is_narrower_or_equal(key, base.get(key), value):
            raise CommitPolicyError(
                f"field {key!r} weakens or ambiguously changes profile {profile!r}"
            )
        base[key] = value
    base["profile"] = profile
    base["override_mode"] = override_mode
    return base


def _effective_org_slug(org: str | None | settings_ops._CallerOrgSentinel) -> str | None:
    if isinstance(org, settings_ops._CallerOrgSentinel):
        return os.environ.get("GRAPH_ORG")
    return org


def _candidate_keys(
    *,
    workspace_id: str | None,
    repo_slug: str | None,
    org_slug: str | None,
) -> list[str]:
    keys: list[str] = []
    if repo_slug:
        keys.append(f"repo:{repo_slug}")
    if workspace_id:
        keys.append(f"workspace:{workspace_id}")
    if org_slug:
        keys.append(f"org:{org_slug}")
    return keys


def resolve_commit_policy(
    *,
    workspace_id: str | None = None,
    repo_slug: str | None = None,
    org: str | None | settings_ops._CallerOrgSentinel = settings_ops.CALLER_ORG,
    context: WorkspaceCapabilityContext | None = None,
) -> ResolvedCommitPolicy:
    """Resolve the effective commit policy for a workspace/repo."""
    org_slug = _effective_org_slug(org)
    members = settings_ops.read_set(
        COMMIT_POLICY_SET_ID,
        org=org,
        peers=[],
        target_revision=COMMIT_POLICY_REVISION,
    ).to_dict()
    for key in _candidate_keys(workspace_id=workspace_id, repo_slug=repo_slug, org_slug=org_slug):
        member = members.get(key)
        if member is None:
            continue
        payload = expand_commit_policy_payload(member.payload)
        errors = tuple(validate_commit_policy(payload, context=context, raise_on_error=False))
        return ResolvedCommitPolicy(
            key=key,
            source=member.id,
            profile=str(payload.get("profile", "custom")),
            payload=payload,
            errors=errors,
        )
    payload = _copy_profile(SAFE_DEFAULT_PROFILE)
    errors = tuple(validate_commit_policy(payload, context=context, raise_on_error=False))
    return ResolvedCommitPolicy(
        key="built-in:safe.default",
        source="built-in",
        profile=SAFE_DEFAULT_PROFILE,
        payload=payload,
        errors=errors,
    )


def _valid_destination_visibility(payload: dict) -> bool:
    pair = (payload.get("commit_destination"), payload.get("visibility"))
    return pair in {
        ("local_only", "local_only"),
        ("local_integration_branch", "workspace_shared"),
        ("workspace_shared", "workspace_shared"),
        ("origin_branch", "origin_published"),
        ("provider_review", "provider_review_published"),
    }


def _requires_gpg(signature_requirement: str) -> bool:
    return "gpg" in _SIGNATURE_STRENGTH.get(signature_requirement, set())


def _requires_ssh(signature_requirement: str) -> bool:
    return "ssh" in _SIGNATURE_STRENGTH.get(signature_requirement, set())


def _push_execution_class(payload: dict) -> str:
    override = (payload.get("operation_overrides") or {}).get("source_control.push")
    if isinstance(override, dict):
        execution_class = override.get("execution_class")
        if isinstance(execution_class, str) and execution_class:
            return execution_class
    boundary = payload.get("provider_credential_boundary")
    if boundary == "broker_required":
        return "broker"
    if boundary == "proxy_allowed":
        return "raw_proxy"
    return "local"


def _fresh_evidence(
    context: WorkspaceCapabilityContext,
    intent: RefUpdateIntent,
) -> BranchProtectionEvidence | None:
    evidence = context.branch_protection.get((intent.provider, intent.repo_slug, intent.ref))
    if evidence is None:
        return None
    max_stale = min(
        int(context.policy_max_branch_protection_stale_seconds),
        int(evidence.stale_after_seconds),
    )
    if context.now - evidence.probed_at > max_stale:
        return None
    return evidence


def _validate_ref_update(
    payload: dict,
    context: WorkspaceCapabilityContext,
    errors: list[str],
) -> None:
    # Static policy resolution often has no concrete ref-update intent yet.
    # That is not a full proxy-write approval. Runtime commit@1 push handlers
    # must call this validator again with the actual RefUpdateIntent before
    # touching provider state.
    if payload.get("provider_credential_boundary") != "proxy_allowed":
        return
    intent = context.ref_update_intent
    if intent is None:
        return
    execution_class = _push_execution_class(payload)
    if intent.operation == "create_new_ref":
        if execution_class == "raw_proxy":
            errors.append(
                "create_new_ref cannot use raw proxy; route through capability API or broker"
            )
        return
    if intent.operation == "unknown":
        errors.append("unknown ref-update intent cannot use raw proxy")
        return
    if execution_class != "raw_proxy":
        return
    evidence = _fresh_evidence(context, intent)
    if evidence is None:
        errors.append(
            f"{intent.operation} on raw proxy requires fresh branch-protection evidence"
        )
        return
    if intent.operation in (
        "fast_forward_existing_ref",
        "force_update_existing_ref",
        "delete_existing_ref",
    ):
        if not evidence.force_push_blocked:
            errors.append(
                f"{intent.operation} on raw proxy requires force_push_blocked=true"
            )
    if intent.operation == "delete_existing_ref" and not evidence.deletion_blocked:
        errors.append(
            "delete_existing_ref on raw proxy requires deletion_blocked=true"
        )


def _validate_operation_policy_intersections(
    payload: dict,
    context: WorkspaceCapabilityContext,
    errors: list[str],
) -> None:
    # Sprint 1 only consumes execution-class allow-lists. Approval/audit/
    # redaction stricter-wins semantics belong at the runtime operation
    # boundary when operation_policy is fully consumed.
    overrides = payload.get("operation_overrides") or {}
    for op_key, policy in context.operation_policies.items():
        if not isinstance(policy, dict):
            continue
        allowed = policy.get("allowed_execution_classes") or []
        if not allowed:
            continue
        local_override = overrides.get(op_key) if isinstance(overrides, dict) else None
        requested = None
        if isinstance(local_override, dict):
            requested = local_override.get("execution_class")
        if requested is None and op_key == "source_control.push":
            requested = _push_execution_class(payload)
        if requested and requested not in allowed:
            errors.append(
                f"operation policy for {op_key} does not allow execution class {requested!r}"
            )


def validate_commit_policy(
    payload: dict,
    *,
    context: WorkspaceCapabilityContext | None = None,
    raise_on_error: bool = True,
) -> list[str]:
    """Return validation errors for a complete policy payload."""
    ctx = context or WorkspaceCapabilityContext()
    errors: list[str] = []
    if not _valid_destination_visibility(payload):
        errors.append(
            "commit_destination and visibility are not an approved pair"
        )
    if payload.get("visibility") == "local_only" and payload.get("push_requirement") != "forbidden":
        errors.append("local_only visibility cannot require or allow push")
    if payload.get("review_integration") == "required_pr_watch" \
            and payload.get("visibility") != "provider_review_published":
        errors.append("required PR watch needs provider_review_published visibility")
    issue_linkage = payload.get("issue_linkage") or {}
    if issue_linkage.get("required") and not ctx.issue_tracker_enabled:
        errors.append("issue_linkage is required but issue_tracker is not enabled")
    signature = str(payload.get("signature_requirement", "none"))
    local_signer_required = payload.get("signing_boundary") != "human_local_crypto_required"
    if _requires_gpg(signature) and not ctx.gpg_signer_available and local_signer_required:
        errors.append("GPG signature required but no GPG signer is available")
    if _requires_ssh(signature) and not ctx.ssh_signer_available and local_signer_required:
        errors.append("SSH signature required but no SSH signer is available")
    if (_requires_gpg(signature) or _requires_ssh(signature)) \
            and payload.get("signing_boundary") == "none":
        errors.append("cryptographic signature required but signing_boundary=none")
    _validate_ref_update(payload, ctx, errors)
    _validate_operation_policy_intersections(payload, ctx, errors)
    if errors and raise_on_error:
        raise CommitPolicyError("; ".join(errors))
    return errors


def chosen_execution_class(classes: list[str]) -> str | None:
    """Return the most restrictive known execution class in *classes*."""
    known = [c for c in classes if c in _EXECUTION_CLASS_ORDER]
    if not known:
        return None
    return max(known, key=lambda c: _rank(c, _EXECUTION_CLASS_ORDER))


def describe_commit_policy(resolved: ResolvedCommitPolicy) -> str:
    payload = resolved.payload
    profile = resolved.profile
    lines = [f"Commit policy: {profile}."]
    if resolved.key != "built-in:safe.default":
        lines.append(f"Resolved from Settings key: {resolved.key}.")
    if profile == AUTONOMY_PROFILE:
        lines.extend([
            "",
            "Make commits on your managed session branch.",
            "Keep the branch linear. Never merge master into your session branch.",
            "If master advances, rebase onto master.",
            "Do not push to origin. Do not create a PR.",
            "Do not GPG-sign or add sign-off trailers unless the user asks separately.",
            "",
            "When the change is ready and Worktrees shows the branch is "
            "fast-forward eligible, use the whole-branch Worktrees merge path "
            "to land it on master.",
        ])
    elif profile == ENTERPRISE_PROFILE:
        lines.extend([
            "",
            "Use a ticket-named PR branch.",
            "Include the required issue reference, sign-off trailer, and GPG signature.",
            "Do not expose provider tokens to the agent.",
            "Submit the workflow for operator approval/signing, publish through "
            "the configured credential boundary, create or link the PR, and "
            "watch PR checks/comments until terminal.",
        ])
    elif profile == "cross-user-shared":
        lines.extend([
            "",
            "Commit on your managed session branch.",
            "Publish a new origin branch through the capability API so the "
            "service can verify the branch does not already exist before pushing.",
            "Do not use raw proxy git push for this workflow.",
        ])
    else:
        lines.extend([
            "",
            f"Destination: {payload.get('commit_destination')}.",
            f"Branch mode: {payload.get('branch_mode')}.",
            f"Visibility after completion: {payload.get('visibility')}.",
            f"Push requirement: {payload.get('push_requirement')}.",
            f"Review integration: {payload.get('review_integration')}.",
            f"Signature requirement: {payload.get('signature_requirement')}.",
        ])
    if resolved.errors:
        lines.extend(["", "Policy is currently incoherent:"])
        lines.extend(f"- {err}" for err in resolved.errors)
    return "\n".join(lines).rstrip() + "\n"


def describe_commit_policy_json(resolved: ResolvedCommitPolicy) -> str:
    return json.dumps({
        "key": resolved.key,
        "source": resolved.source,
        "profile": resolved.profile,
        "payload": resolved.payload,
        "errors": list(resolved.errors),
        "text": describe_commit_policy(resolved),
    }, indent=2, sort_keys=True)


def seed_workspace_policy(
    *,
    workspace_id: str,
    org: str | None | settings_ops._CallerOrgSentinel,
    profile: str = AUTONOMY_PROFILE,
) -> bool:
    """Ensure a workspace policy row exists; return True when inserted."""
    key = f"workspace:{workspace_id}"
    members = settings_ops.read_set(COMMIT_POLICY_SET_ID, org=org, peers=[]).to_dict()
    if key in members:
        return False
    settings_ops.upsert_by_key(
        COMMIT_POLICY_SET_ID,
        COMMIT_POLICY_REVISION,
        key,
        {
            "workspace_id": workspace_id,
            "applies_to": "workspace",
            "profile": profile,
            "override_mode": "none",
        },
        org=org,
        state="canonical",
    )
    return True


def seed_default_workspace_policies(workspaces: Mapping[str, Any]) -> dict[str, str]:
    """Deploy/startup seed for built-in workspace defaults.

    Sprint 1 only auto-seeds Autonomy-owned workspaces. Structured enterprise
    workflows must opt in with an explicit workspace/repo Setting row.
    """
    results: dict[str, str] = {}
    for workspace_id, workspace in sorted(workspaces.items()):
        org = getattr(workspace, "graph_project", None)
        if org != "autonomy":
            continue
        try:
            inserted = seed_workspace_policy(
                workspace_id=workspace_id,
                org=org,
                profile=AUTONOMY_PROFILE,
            )
        except Exception as exc:  # pragma: no cover - startup logs this path
            results[workspace_id] = f"error:{exc}"
            continue
        results[workspace_id] = "inserted" if inserted else "exists"
    return results
