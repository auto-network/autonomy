"""Commit workflow policy Settings.

``autonomy.commit.policy#1`` selects the commit workflow a workspace/repo
should follow. ``autonomy.capability.operation_policy#1`` narrows specific
capability operations used by that workflow.
"""

from __future__ import annotations

from typing import Any

from .registry import SchemaValidationError, SettingSchema, field


COMMIT_POLICY_SET_ID = "autonomy.commit.policy"
COMMIT_POLICY_REVISION = 1
OPERATION_POLICY_SET_ID = "autonomy.capability.operation_policy"
OPERATION_POLICY_REVISION = 1


# Registered under BOTH set_ids in this module (the synopsis flush keys the
# module-level SYNOPSIS by each schema's set_id#rev).
SYNOPSIS = {
    "summary": (
        "Commit workflow policy per org/workspace/repo — destination, "
        "branch mode, visibility, push/review/signature requirements "
        "(autonomy.commit.policy) — plus per-operation capability narrowing: "
        "execution classes, approval, credential boundary, audit and "
        "redaction rules (autonomy.capability.operation_policy)."
    ),
    "nouns": [
        "commit policy", "commit workflow", "commit destination",
        "branch mode", "push requirement", "review integration",
        "signature requirement", "signing", "operation policy",
        "capability operation", "approval required", "credential boundary",
        "audit level", "redaction",
    ],
    "related_set_ids": [
        "autonomy.capability.contract#1",
        "autonomy.capability.impl#1",
        "autonomy.commit.signing-key#1",
    ],
}


COMMIT_DESTINATIONS = (
    "local_only",
    "local_integration_branch",
    "workspace_shared",
    "origin_branch",
    "provider_review",
    "direct_target_branch",
)
BRANCH_MODES = (
    "current_branch",
    "new_topic_branch",
    "managed_session_branch",
    "target_branch",
    "pr_branch",
)
VISIBILITIES = (
    "local_only",
    "workspace_shared",
    "origin_published",
    "provider_review_published",
)
PUSH_REQUIREMENTS = (
    "forbidden",
    "optional_manual",
    "required",
    "required_for_cross_user_handoff",
)
REVIEW_INTEGRATIONS = (
    "none",
    "optional_pr",
    "required_pr_watch",
)
SIGNATURE_REQUIREMENTS = (
    "none",
    "signoff",
    "gpg",
    "ssh",
    "signoff_and_gpg",
    "signoff_and_ssh",
)
SIGNING_BOUNDARIES = (
    "none",
    "broker_mediated",
    "human_local_crypto_required",
)
CREDENTIAL_BOUNDARIES = (
    "none",
    "proxy_allowed",
    "broker_required",
)
REF_UPDATE_PERMISSIONS = (
    "direct_target_allowed",
    "fast_forward_only",
    "protected_branch_disallowed",
    "force_with_lease_approval",
)
OVERRIDE_MODES = ("none", "narrow")
AUDIT_LEVELS = ("minimal", "standard", "full")

# auto.network link operation classes (spec graph://a17c8657-939 §6.6, §8).
# Rows use contract="link", operation=<one of LINK_OPERATIONS>; the joined
# class string ("link.publish") is what policy keys and prompts name. Each
# class resolves to prompt|delegated per org/workspace — a prompt is a
# missing delegation hop (§8), so the absence of a row means "prompt".
LINK_CONTRACT = "link"
LINK_OPERATIONS = ("publish", "revoke", "delegate")
LINK_OPERATION_CLASSES = tuple(f"{LINK_CONTRACT}.{op}" for op in LINK_OPERATIONS)
LINK_OPERATION_MODES = ("prompt", "delegated")


_COMMIT_DIMENSIONS = {
    "commit_destination": COMMIT_DESTINATIONS,
    "branch_mode": BRANCH_MODES,
    "visibility": VISIBILITIES,
    "push_requirement": PUSH_REQUIREMENTS,
    "review_integration": REVIEW_INTEGRATIONS,
    "signature_requirement": SIGNATURE_REQUIREMENTS,
    "signing_boundary": SIGNING_BOUNDARIES,
    "provider_credential_boundary": CREDENTIAL_BOUNDARIES,
    "ref_update_permissions": REF_UPDATE_PERMISSIONS,
}


def _require_string(payload: dict, key: str, cls_name: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise SchemaValidationError(
            f"{cls_name}: {key!r} must be a non-empty string"
        )
    return value


def _validate_enum(payload: dict, key: str, allowed: tuple[str, ...], cls_name: str) -> None:
    if key not in payload:
        return
    value = payload[key]
    if value not in allowed:
        raise SchemaValidationError(
            f"{cls_name}: {key!r} must be one of {allowed}, got {value!r}"
        )


def _validate_list_of_strings(payload: dict, key: str, cls_name: str) -> None:
    if key not in payload:
        return
    value = payload[key]
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise SchemaValidationError(
            f"{cls_name}: {key!r} must be a list of strings"
        )


def _validate_object(payload: dict, key: str, cls_name: str) -> None:
    if key not in payload:
        return
    if not isinstance(payload[key], dict):
        raise SchemaValidationError(f"{cls_name}: {key!r} must be an object")


class CommitPolicyV1(SettingSchema):
    set_id = COMMIT_POLICY_SET_ID
    schema_revision = COMMIT_POLICY_REVISION

    workspace_id: str = field(required=False, description="Workspace id this row targets")
    repo_slug: str = field(required=False, description="Canonical repo slug this row targets")
    repo_aliases: list = field(required=False, default_factory=list)
    applies_to: str = field(required=False, enum=["org", "workspace", "repo"])
    profile: str = field(required=False, description="Named built-in profile")
    override_mode: str = field(required=False, default="none", enum=list(OVERRIDE_MODES))
    commit_destination: str = field(required=False, enum=list(COMMIT_DESTINATIONS))
    branch_mode: str = field(required=False, enum=list(BRANCH_MODES))
    visibility: str = field(required=False, enum=list(VISIBILITIES))
    push_requirement: str = field(required=False, enum=list(PUSH_REQUIREMENTS))
    review_integration: str = field(required=False, enum=list(REVIEW_INTEGRATIONS))
    signature_requirement: str = field(required=False, enum=list(SIGNATURE_REQUIREMENTS))
    signing_boundary: str = field(required=False, enum=list(SIGNING_BOUNDARIES))
    provider_credential_boundary: str = field(required=False, enum=list(CREDENTIAL_BOUNDARIES))
    ref_update_permissions: str = field(required=False, enum=list(REF_UPDATE_PERMISSIONS))
    reviewer_audience: list = field(required=False, default_factory=list)
    author_policy: dict = field(required=False, default_factory=dict)
    watch_policy: dict = field(required=False, default_factory=dict)
    issue_linkage: dict = field(required=False, default_factory=dict)
    operation_overrides: dict = field(required=False, default_factory=dict)
    coherence_version: str = field(required=False)
    notes: str = field(required=False)

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        for key, allowed in _COMMIT_DIMENSIONS.items():
            _validate_enum(payload, key, allowed, cls.__name__)
        _validate_enum(payload, "override_mode", OVERRIDE_MODES, cls.__name__)
        _validate_enum(payload, "applies_to", ("org", "workspace", "repo"), cls.__name__)
        _validate_list_of_strings(payload, "repo_aliases", cls.__name__)
        _validate_list_of_strings(payload, "reviewer_audience", cls.__name__)
        for key in (
            "author_policy",
            "watch_policy",
            "issue_linkage",
            "operation_overrides",
        ):
            _validate_object(payload, key, cls.__name__)
        profile = payload.get("profile")
        if profile is not None and (not isinstance(profile, str) or not profile):
            raise SchemaValidationError(
                f"{cls.__name__}: 'profile' must be a non-empty string"
            )
        if profile is None:
            missing = [key for key in _COMMIT_DIMENSIONS if key not in payload]
            if missing:
                raise SchemaValidationError(
                    f"{cls.__name__}: custom policy without 'profile' "
                    f"must include all dimensions; missing {missing}"
                )


class OperationPolicyV1(SettingSchema):
    set_id = OPERATION_POLICY_SET_ID
    schema_revision = OPERATION_POLICY_REVISION

    contract: str = field(required=True)
    operation: str = field(required=True)
    allowed_execution_classes: list = field(required=False, default_factory=list)
    approval_required: bool = field(required=False, default=False)
    credential_boundary: str = field(required=False, enum=list(CREDENTIAL_BOUNDARIES))
    audit_level: str = field(required=False, default="standard", enum=list(AUDIT_LEVELS))
    input_redaction_rules: list = field(required=False, default_factory=list)
    output_redaction_rules: list = field(required=False, default_factory=list)
    mode: str = field(
        required=False,
        enum=list(LINK_OPERATION_MODES),
        description=(
            "prompt-vs-preauthorized resolution for auto.network link "
            "operation classes (link.publish / link.revoke / link.delegate): "
            "'prompt' asks the operator per operation, 'delegated' lets a "
            "policy-minted agent key act promptlessly. Only defined for "
            "contract='link'; absent rows resolve to 'prompt' (a prompt is "
            "a missing delegation hop, spec §8)."
        ),
    )
    notes: str = field(required=False)

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        contract = _require_string(payload, "contract", cls.__name__)
        operation = _require_string(payload, "operation", cls.__name__)
        _validate_enum(payload, "credential_boundary", CREDENTIAL_BOUNDARIES, cls.__name__)
        _validate_enum(payload, "audit_level", AUDIT_LEVELS, cls.__name__)
        _validate_enum(payload, "mode", LINK_OPERATION_MODES, cls.__name__)
        _validate_list_of_strings(payload, "allowed_execution_classes", cls.__name__)
        if contract == LINK_CONTRACT and operation not in LINK_OPERATIONS:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown link operation {operation!r}; "
                f"the link contract defines {LINK_OPERATIONS}"
            )
        if "mode" in payload and contract != LINK_CONTRACT:
            raise SchemaValidationError(
                f"{cls.__name__}: 'mode' is only defined for the "
                f"{LINK_CONTRACT!r} contract's operation classes "
                f"{LINK_OPERATION_CLASSES}, not contract {contract!r}"
            )
        if "approval_required" in payload and not isinstance(payload["approval_required"], bool):
            raise SchemaValidationError(
                f"{cls.__name__}: 'approval_required' must be a boolean"
            )
        for key in ("input_redaction_rules", "output_redaction_rules"):
            if key in payload and not isinstance(payload[key], list):
                raise SchemaValidationError(f"{cls.__name__}: {key!r} must be a list")
