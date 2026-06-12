from __future__ import annotations

from argparse import Namespace

import pytest

from tools.graph import ops
from tools.graph.commit_policy import (
    AUTONOMY_PROFILE,
    BranchProtectionEvidence,
    CommitPolicyError,
    RefUpdateIntent,
    WorkspaceCapabilityContext,
    describe_commit_policy,
    expand_commit_policy_payload,
    resolve_commit_policy,
    seed_default_workspace_policies,
    seed_workspace_policy,
    validate_commit_policy,
)
from tools.graph.commit_policy_cmd import cmd_commit_policy_describe
from tools.graph.schemas.commit_policy import (
    COMMIT_POLICY_REVISION,
    COMMIT_POLICY_SET_ID,
    OPERATION_POLICY_REVISION,
    OPERATION_POLICY_SET_ID,
)
from tools.graph.schemas.registry import get_schema
from agents.primer_renderer import render_workspace_primer
from agents.workspace_settings import RepoMount, WorkspaceV1


@pytest.fixture
def graph_db_env(tmp_path, monkeypatch):
    db_path = tmp_path / "graph.db"
    monkeypatch.setenv("GRAPH_DB", str(db_path))
    monkeypatch.delenv("GRAPH_API", raising=False)
    yield db_path


def _cross_user_payload() -> dict:
    return expand_commit_policy_payload({
        "profile": "cross-user-shared",
        "override_mode": "none",
    })


def test_commit_policy_schemas_are_registered():
    assert get_schema(COMMIT_POLICY_SET_ID, COMMIT_POLICY_REVISION) is not None
    assert get_schema(OPERATION_POLICY_SET_ID, OPERATION_POLICY_REVISION) is not None


def test_fresh_db_resolves_safe_default(graph_db_env):
    resolved = resolve_commit_policy(workspace_id="missing", org=ops.CALLER_ORG)
    assert resolved.profile == "safe.default"
    assert resolved.payload["commit_destination"] == "local_only"
    assert resolved.payload["push_requirement"] == "forbidden"


def test_seeded_autonomy_workspace_resolves_profile(graph_db_env):
    inserted = seed_workspace_policy(
        workspace_id="autonomy",
        org=ops.CALLER_ORG,
        profile=AUTONOMY_PROFILE,
    )
    assert inserted is True
    assert seed_workspace_policy(
        workspace_id="autonomy",
        org=ops.CALLER_ORG,
        profile=AUTONOMY_PROFILE,
    ) is False

    resolved = resolve_commit_policy(workspace_id="autonomy", org=ops.CALLER_ORG)
    assert resolved.profile == AUTONOMY_PROFILE
    assert resolved.payload["branch_mode"] == "managed_session_branch"
    assert resolved.payload["commit_destination"] == "local_integration_branch"


def test_profile_row_cannot_silently_weaken_builtin_profile(graph_db_env):
    ops.upsert_by_key(
        COMMIT_POLICY_SET_ID,
        COMMIT_POLICY_REVISION,
        "workspace:enterprise",
        {
            "profile": "enterprise.signed-pr",
            "override_mode": "none",
            "signature_requirement": "none",
        },
        org=ops.CALLER_ORG,
    )
    with pytest.raises(CommitPolicyError, match="override_mode=none"):
        resolve_commit_policy(workspace_id="enterprise", org=ops.CALLER_ORG)


def test_narrow_mode_rejects_issue_linkage_weakening():
    with pytest.raises(CommitPolicyError, match="issue_linkage"):
        expand_commit_policy_payload({
            "profile": "enterprise.signed-pr",
            "override_mode": "narrow",
            "issue_linkage": {"required": False},
        })


def test_narrow_mode_rejects_author_policy_weakening():
    with pytest.raises(CommitPolicyError, match="author_policy"):
        expand_commit_policy_payload({
            "profile": "enterprise.signed-pr",
            "override_mode": "narrow",
            "author_policy": {
                "require_operator_confirmation": False,
                "require_signoff": False,
            },
        })


def test_cross_user_create_new_ref_uses_capability_api_without_evidence():
    payload = _cross_user_payload()
    ctx = WorkspaceCapabilityContext(
        ref_update_intent=RefUpdateIntent(
            provider="github",
            repo_slug="autonomy/autonomy",
            ref="refs/heads/session/abc",
            operation="create_new_ref",
        ),
    )
    assert validate_commit_policy(payload, context=ctx, raise_on_error=False) == []


def test_raw_proxy_create_new_ref_is_rejected_without_api_prefight():
    payload = _cross_user_payload()
    payload["operation_overrides"] = {}
    ctx = WorkspaceCapabilityContext(
        ref_update_intent=RefUpdateIntent(
            provider="github",
            repo_slug="autonomy/autonomy",
            ref="refs/heads/session/abc",
            operation="create_new_ref",
        ),
    )
    errors = validate_commit_policy(payload, context=ctx, raise_on_error=False)
    assert any("create_new_ref cannot use raw proxy" in err for err in errors)


def test_raw_proxy_fast_forward_existing_ref_requires_force_push_blocking_evidence():
    payload = _cross_user_payload()
    payload["operation_overrides"] = {}
    ctx = WorkspaceCapabilityContext(
        ref_update_intent=RefUpdateIntent(
            provider="github",
            repo_slug="autonomy/autonomy",
            ref="refs/heads/shared",
            operation="fast_forward_existing_ref",
        ),
    )
    errors = validate_commit_policy(payload, context=ctx, raise_on_error=False)
    assert any("requires fresh branch-protection evidence" in err for err in errors)


def test_raw_proxy_fast_forward_existing_ref_accepts_fresh_force_push_blocking_evidence():
    payload = _cross_user_payload()
    payload["operation_overrides"] = {}
    intent = RefUpdateIntent(
        provider="github",
        repo_slug="autonomy/autonomy",
        ref="refs/heads/shared",
        operation="fast_forward_existing_ref",
    )
    ctx = WorkspaceCapabilityContext(
        now=1000.0,
        ref_update_intent=intent,
        branch_protection={
            ("github", "autonomy/autonomy", "refs/heads/shared"):
                BranchProtectionEvidence(
                    provider="github",
                    repo_slug="autonomy/autonomy",
                    ref="refs/heads/shared",
                    probed_at=950.0,
                    force_push_blocked=True,
                    deletion_blocked=False,
                    stale_after_seconds=3600,
                ),
        },
    )
    assert validate_commit_policy(payload, context=ctx, raise_on_error=False) == []


def test_stale_branch_protection_evidence_fails_closed():
    payload = _cross_user_payload()
    payload["operation_overrides"] = {}
    intent = RefUpdateIntent(
        provider="github",
        repo_slug="autonomy/autonomy",
        ref="refs/heads/shared",
        operation="fast_forward_existing_ref",
    )
    ctx = WorkspaceCapabilityContext(
        now=1000.0,
        policy_max_branch_protection_stale_seconds=60,
        ref_update_intent=intent,
        branch_protection={
            ("github", "autonomy/autonomy", "refs/heads/shared"):
                BranchProtectionEvidence(
                    provider="github",
                    repo_slug="autonomy/autonomy",
                    ref="refs/heads/shared",
                    probed_at=900.0,
                    force_push_blocked=True,
                    deletion_blocked=True,
                    stale_after_seconds=3600,
                ),
        },
    )
    errors = validate_commit_policy(payload, context=ctx, raise_on_error=False)
    assert any("requires fresh branch-protection evidence" in err for err in errors)


def test_raw_proxy_delete_existing_ref_requires_delete_blocking_evidence():
    payload = _cross_user_payload()
    payload["operation_overrides"] = {}
    intent = RefUpdateIntent(
        provider="github",
        repo_slug="autonomy/autonomy",
        ref="refs/heads/shared",
        operation="delete_existing_ref",
    )
    ctx = WorkspaceCapabilityContext(
        now=1000.0,
        ref_update_intent=intent,
        branch_protection={
            ("github", "autonomy/autonomy", "refs/heads/shared"):
                BranchProtectionEvidence(
                    provider="github",
                    repo_slug="autonomy/autonomy",
                    ref="refs/heads/shared",
                    probed_at=999.0,
                    force_push_blocked=True,
                    deletion_blocked=False,
                ),
        },
    )
    errors = validate_commit_policy(payload, context=ctx, raise_on_error=False)
    assert any("requires deletion_blocked=true" in err for err in errors)


def test_raw_proxy_delete_existing_ref_also_requires_force_push_blocking_evidence():
    payload = _cross_user_payload()
    payload["operation_overrides"] = {}
    intent = RefUpdateIntent(
        provider="github",
        repo_slug="autonomy/autonomy",
        ref="refs/heads/shared",
        operation="delete_existing_ref",
    )
    ctx = WorkspaceCapabilityContext(
        now=1000.0,
        ref_update_intent=intent,
        branch_protection={
            ("github", "autonomy/autonomy", "refs/heads/shared"):
                BranchProtectionEvidence(
                    provider="github",
                    repo_slug="autonomy/autonomy",
                    ref="refs/heads/shared",
                    probed_at=999.0,
                    force_push_blocked=False,
                    deletion_blocked=True,
                ),
        },
    )
    errors = validate_commit_policy(payload, context=ctx, raise_on_error=False)
    assert any("requires force_push_blocked=true" in err for err in errors)


def test_enterprise_policy_requires_issue_tracker_and_gpg_signer():
    payload = expand_commit_policy_payload({
        "profile": "enterprise.signed-pr",
        "override_mode": "none",
    })
    errors = validate_commit_policy(payload, raise_on_error=False)
    assert any("issue_tracker" in err for err in errors)
    assert any("GPG signature required" in err for err in errors)

    ok_ctx = WorkspaceCapabilityContext(
        issue_tracker_enabled=True,
        gpg_signer_available=True,
    )
    assert validate_commit_policy(payload, context=ok_ctx, raise_on_error=False) == []


def test_operation_policy_cannot_remove_required_execution_class():
    payload = _cross_user_payload()
    ctx = WorkspaceCapabilityContext(
        operation_policies={
            "source_control.push": {
                "allowed_execution_classes": ["broker"],
            },
        }
    )
    errors = validate_commit_policy(payload, context=ctx, raise_on_error=False)
    assert any("does not allow execution class" in err for err in errors)


def test_operation_policy_checks_effective_execution_class_without_override():
    payload = _cross_user_payload()
    payload["operation_overrides"] = {}
    ctx = WorkspaceCapabilityContext(
        operation_policies={
            "source_control.push": {
                "allowed_execution_classes": ["broker"],
            },
        }
    )
    errors = validate_commit_policy(payload, context=ctx, raise_on_error=False)
    assert any("does not allow execution class 'raw_proxy'" in err for err in errors)


def test_describe_autonomy_policy_is_plain_workflow_text(graph_db_env):
    seed_workspace_policy(
        workspace_id="autonomy",
        org=ops.CALLER_ORG,
        profile=AUTONOMY_PROFILE,
    )
    resolved = resolve_commit_policy(workspace_id="autonomy", org=ops.CALLER_ORG)
    text = describe_commit_policy(resolved)
    assert "managed session branch" in text
    assert "Never merge master into your session branch" in text
    assert "rebase onto master" in text
    assert "whole-branch Worktrees merge path" in text
    assert "Do not push to origin" in text
    assert "Do not create a PR" in text


def test_commit_policy_describe_command_uses_same_projection(graph_db_env, capsys):
    seed_workspace_policy(
        workspace_id="autonomy",
        org=ops.CALLER_ORG,
        profile=AUTONOMY_PROFILE,
    )
    cmd_commit_policy_describe(Namespace(
        workspace="autonomy",
        repo=None,
        org=ops.CALLER_ORG,
        json=False,
    ))
    out = capsys.readouterr().out
    assert "Commit policy: autonomy.direct-master." in out
    assert "whole-branch Worktrees merge path" in out


def test_workspace_primer_renders_resolved_commit_policy(graph_db_env):
    seed_workspace_policy(
        workspace_id="autonomy",
        org=ops.CALLER_ORG,
        profile=AUTONOMY_PROFILE,
    )
    workspace = WorkspaceV1(
        id="autonomy",
        name="Autonomy",
        description="Autonomy platform",
        image="autonomy-agent:dashboard",
        graph_project="autonomy",
        repos=(RepoMount(url="u", mount="/workspace/repo", writable=True),),
    )
    out = render_workspace_primer(workspace)
    assert "## Commit Policy" in out
    assert "Commit policy: autonomy.direct-master." in out
    assert "Never merge master into your session branch" in out


def test_deploy_seed_only_autonomy_workspaces(graph_db_env):
    class Workspace:
        def __init__(self, graph_project: str):
            self.graph_project = graph_project

    results = seed_default_workspace_policies({
        "autonomy": Workspace("autonomy"),
        "enterprise": Workspace("anchore"),
    })
    assert results == {"autonomy": "inserted"}
    resolved = resolve_commit_policy(workspace_id="autonomy", org=ops.CALLER_ORG)
    assert resolved.profile == AUTONOMY_PROFILE
    assert resolve_commit_policy(workspace_id="enterprise", org=ops.CALLER_ORG).profile == "safe.default"
