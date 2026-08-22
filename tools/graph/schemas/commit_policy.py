"""Commit workflow policy Settings.

``autonomy.commit.policy#1`` selects the commit workflow a workspace/repo
should follow. ``autonomy.capability.operation_policy#1`` narrows specific
capability operations used by that workflow.
"""

from __future__ import annotations

from typing import Any

from .registry import SchemaValidationError, SettingSchema, field, keyed_per_entity, publication_band
from .registry import home


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


#: Not forced into any one store. This records that the question was
#: ASKED -- must this live in the operator's own database, or on
#: this machine alone? -- and answered no, which is different
#: from nobody having considered it.
#:
#: It is not a prohibition. The operator owns workspaces, so
#: their database is the organizational home of their own
#: things; reading this as "anywhere but personal" refuses
#: writes that are correct.
@publication_band(min="raw", max="curated")
@home("organization")
@keyed_per_entity(key_strategy="policy_scope_key")
class CommitPolicyV1(SettingSchema):
    set_id = COMMIT_POLICY_SET_ID
    schema_revision = COMMIT_POLICY_REVISION

    workspace_id: str = field(required=False, description="Workspace id this row targets")
    repo_slug: str = field(required=False, description="Canonical repo slug this row targets")
    repo_aliases: list = field(
        required=False, default_factory=list,
        description="Other slugs that resolve to this same repository",
    )
    applies_to: str = field(
        required=False, enum=["org", "workspace", "repo"],
        description="Scope this row governs — an org, one workspace, or one repository",
    )
    profile: str = field(required=False, description="Named built-in profile")
    override_mode: str = field(
        required=False, default="none", enum=list(OVERRIDE_MODES),
        description="Whether and how a narrower row may override this one",
    )
    commit_destination: str = field(
        required=False, enum=list(COMMIT_DESTINATIONS),
        description="How far a commit travels: local only, an integration branch, shared across the workspace, or the origin remote",
    )
    branch_mode: str = field(
        required=False, enum=list(BRANCH_MODES),
        description="Which branch work lands on — the current one, a new topic branch, or the managed session branch",
    )
    visibility: str = field(
        required=False, enum=list(VISIBILITIES),
        description="Who can see the resulting commits: local only, workspace-shared, or published to origin",
    )
    push_requirement: str = field(
        required=False, enum=list(PUSH_REQUIREMENTS),
        description="Whether pushing is forbidden, optional and manual, or required",
    )
    review_integration: str = field(
        required=False, enum=list(REVIEW_INTEGRATIONS),
        description="Whether a pull request is skipped, optional, or required and watched",
    )
    signature_requirement: str = field(
        required=False, enum=list(SIGNATURE_REQUIREMENTS),
        description="What must sign a commit — nothing, a sign-off trailer, or a GPG signature",
    )
    signing_boundary: str = field(
        required=False, enum=list(SIGNING_BOUNDARIES),
        description="Where signing happens: nowhere, mediated by the broker, or requiring the human to hold the key locally",
    )
    provider_credential_boundary: str = field(
        required=False, enum=list(CREDENTIAL_BOUNDARIES),
        description="How provider credentials may be reached — not at all, via a proxy, or only through the broker",
    )
    ref_update_permissions: str = field(
        required=False, enum=list(REF_UPDATE_PERMISSIONS),
        description="What ref updates are allowed: direct to the target, fast-forward only, or protected branches refused",
    )
    reviewer_audience: list = field(
        required=False, default_factory=list,
        description="Who reviews work produced under this policy",
    )
    author_policy: dict = field(
        required=False, default_factory=dict,
        description="Rules for how the commit author is set",
    )
    watch_policy: dict = field(
        required=False, default_factory=dict,
        description="Rules for watching a pull request through to completion",
    )
    issue_linkage: dict = field(
        required=False, default_factory=dict,
        description="Rules tying commits to issue-tracker records",
    )
    operation_overrides: dict = field(
        required=False, default_factory=dict,
        description="Per-operation exceptions to the dimensions above",
    )
    coherence_version: str = field(
        required=False,
        description="Version stamp of the dimension vocabulary this row was written against",
    )
    notes: str = field(
        required=False,
        description="Free text explaining why this policy is as it is",
    )

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


#: Not forced into any one store. This records that the question was
#: ASKED -- must this live in the operator's own database, or on
#: this machine alone? -- and answered no, which is different
#: from nobody having considered it.
#:
#: It is not a prohibition. The operator owns workspaces, so
#: their database is the organizational home of their own
#: things; reading this as "anywhere but personal" refuses
#: writes that are correct.
@publication_band(min="raw", max="canonical")
@home("organization")
@keyed_per_entity(key_strategy="contract_operation")
class OperationPolicyV1(SettingSchema):
    set_id = OPERATION_POLICY_SET_ID
    schema_revision = OPERATION_POLICY_REVISION

    contract: str = field(
        required=True,
        description='Capability contract the operation belongs to, e.g. "link"',
    )
    operation: str = field(
        required=True,
        description='Operation within that contract; contract and operation join into the class string policy and prompts name, e.g. "link.publish"',
    )
    allowed_execution_classes: list = field(
        required=False, default_factory=list,
        description="Execution classes permitted to perform this operation",
    )
    approval_required: bool = field(
        required=False, default=False,
        description="Whether a human must approve each invocation",
    )
    credential_boundary: str = field(
        required=False, enum=list(CREDENTIAL_BOUNDARIES),
        description="How provider credentials may be reached for this operation — not at all, via a proxy, or only through the broker",
    )
    audit_level: str = field(
        required=False, default="standard", enum=list(AUDIT_LEVELS),
        description="How much of each invocation is recorded: minimal, standard, or full",
    )
    input_redaction_rules: list = field(
        required=False, default_factory=list,
        description="What to strip from the recorded input before it is stored",
    )
    output_redaction_rules: list = field(
        required=False, default_factory=list,
        description="What to strip from the recorded output before it is stored",
    )
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
    notes: str = field(
        required=False,
        description="Free text explaining why this operation policy is as it is",
    )

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
