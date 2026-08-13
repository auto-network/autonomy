from __future__ import annotations

from argparse import Namespace
from types import SimpleNamespace

import pytest

from tools.graph import ops
from tools.graph.commit_policy import (
    AUTONOMY_PROFILE,
    ENTERPRISE_PROFILE,
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
from agents.primer_renderer import _commit_policy_block, render_workspace_primer
from agents.workspace_settings import RepoMount, WorkspaceV1


@pytest.fixture
def graph_db_env(tmp_path, monkeypatch):
    db_path = tmp_path / "graph.db"
    monkeypatch.setenv("GRAPH_DB", str(db_path))
    monkeypatch.delenv("GRAPH_API", raising=False)
    yield db_path


@pytest.fixture
def multi_org_env(tmp_path, monkeypatch):
    """Real, separate per-org DBs (not the single-file ``graph_db_env``) —
    needed to prove org-scoped lookups actually route to different
    physical databases, per graph://bcce359d-a1d's per-org-DB model."""
    from tools.graph.db import GraphDB

    root = tmp_path / "orgs"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(root))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)
    GraphDB.close_all_pooled()  # no stale pooled DBs from a prior env
    GraphDB.create_org_db("autonomy").close()
    GraphDB.create_org_db("anchore").close()
    yield root
    GraphDB.close_all_pooled()


class _FakeSetMembers:
    def __init__(self, members):
        self.members = members

    def to_dict(self):
        return {member.key: member for member in self.members}


class _FakeClient:
    def __init__(self, members=None):
        if isinstance(members, dict):
            self.members_by_set_id = {
                key: list(value) for key, value in members.items()
            }
        else:
            self.members_by_set_id = {
                COMMIT_POLICY_SET_ID: list(members or []),
            }
        self.read_calls = []
        self.add_calls = []

    def read_set(self, set_id, *, org, target_revision=None, min_revision=None):
        self.read_calls.append((set_id, org, target_revision, min_revision))
        return _FakeSetMembers(self.members_by_set_id.get(set_id, []))

    def add_setting(
        self,
        set_id,
        schema_revision,
        key,
        payload,
        *,
        org,
        state="raw",
    ):
        self.add_calls.append(
            (set_id, schema_revision, key, payload, org, state)
        )
        return "fake-setting-id"


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
    # signing_boundary=human_local_crypto_required means signing happens on the
    # operator's device, so a missing local GPG/SSH signer is not an error.
    assert not any("GPG signature required" in err for err in errors)

    ok_ctx = WorkspaceCapabilityContext(
        issue_tracker_enabled=True,
        gpg_signer_available=True,
    )
    assert validate_commit_policy(payload, context=ok_ctx, raise_on_error=False) == []


def test_human_local_crypto_boundary_skips_signer_available_check():
    payload = expand_commit_policy_payload({
        "profile": "enterprise.signed-pr",
        "override_mode": "none",
    })
    ctx = WorkspaceCapabilityContext(issue_tracker_enabled=True)
    errors = validate_commit_policy(payload, context=ctx, raise_on_error=False)
    assert not any("GPG signature required" in e for e in errors)

    ssh_payload = dict(payload, signature_requirement="signoff_and_ssh")
    errors = validate_commit_policy(ssh_payload, context=ctx, raise_on_error=False)
    assert not any("SSH signature required" in e for e in errors)

    none_boundary_payload = dict(payload, signing_boundary="none")
    errors = validate_commit_policy(none_boundary_payload, context=ctx, raise_on_error=False)
    assert any("signing_boundary=none" in e for e in errors)


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


def test_describe_resolves_workspace_row_from_the_org_it_actually_lives_in(multi_org_env):
    """A workspace:<id> policy row is only visible when read from the org DB
    it was seeded in — resolving from a different org must not find it and
    must fall through to safe.default, proving the org scope is causal
    rather than incidental."""
    seed_workspace_policy(
        workspace_id="enterprise-ng",
        org="anchore",
        profile="enterprise.signed-pr",
    )
    resolved = resolve_commit_policy(workspace_id="enterprise-ng", org="anchore")
    assert resolved.key == "workspace:enterprise-ng"
    assert resolved.profile == "enterprise.signed-pr"

    wrong_org = resolve_commit_policy(workspace_id="enterprise-ng", org="autonomy")
    assert wrong_org.key == "built-in:safe.default"


def test_commit_policy_describe_uses_client_settings(monkeypatch, capsys):
    fake_client = _FakeClient([
        SimpleNamespace(
            id="setting-1",
            key="workspace:autonomy",
            payload={
                "profile": AUTONOMY_PROFILE,
                "override_mode": "none",
            },
        ),
    ])
    monkeypatch.setattr(
        "tools.graph.commit_policy_cmd.get_client",
        lambda: fake_client,
    )
    cmd_commit_policy_describe(Namespace(
        workspace="autonomy",
        repo=None,
        org=None,
        json=False,
    ))
    out = capsys.readouterr().out
    assert "Commit policy: autonomy.direct-master." in out
    assert "whole-branch Worktrees merge path" in out
    assert fake_client.read_calls == [
        (COMMIT_POLICY_SET_ID, ops.CALLER_ORG, COMMIT_POLICY_REVISION, None)
    ]


def test_commit_policy_describe_unknown_workspace_errors(monkeypatch, capsys):
    fake_client = _FakeClient([
        SimpleNamespace(
            id="setting-1",
            key="org:personal",
            payload={
                "profile": AUTONOMY_PROFILE,
                "override_mode": "none",
            },
        ),
    ])
    monkeypatch.setattr(
        "tools.graph.commit_policy_cmd.get_client",
        lambda: fake_client,
    )
    with pytest.raises(SystemExit) as excinfo:
        cmd_commit_policy_describe(Namespace(
            workspace="missing-workspace",
            repo=None,
            org=None,
            json=False,
        ))
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "no workspace 'missing-workspace' found in organization 'personal'" in err
    assert fake_client.read_calls == [
        (COMMIT_POLICY_SET_ID, ops.CALLER_ORG, COMMIT_POLICY_REVISION, None)
    ]


def test_commit_policy_seed_uses_client_settings(monkeypatch, capsys):
    fake_client = _FakeClient([])
    monkeypatch.setattr(
        "tools.graph.commit_policy_cmd.get_client",
        lambda: fake_client,
    )
    from tools.graph.commit_policy_cmd import cmd_commit_policy_seed
    cmd_commit_policy_seed(Namespace(
        workspace="autonomy",
        org=None,
        profile=AUTONOMY_PROFILE,
    ))
    out = capsys.readouterr().out
    assert "inserted: autonomy.commit.policy#1 workspace:autonomy" in out
    assert fake_client.read_calls == [
        (COMMIT_POLICY_SET_ID, ops.CALLER_ORG, COMMIT_POLICY_REVISION, None)
    ]
    assert fake_client.add_calls == [
        (
            COMMIT_POLICY_SET_ID,
            COMMIT_POLICY_REVISION,
            "workspace:autonomy",
            {
                "workspace_id": "autonomy",
                "applies_to": "workspace",
                "profile": AUTONOMY_PROFILE,
                "override_mode": "none",
            },
            ops.CALLER_ORG,
            "canonical",
        )
    ]


def test_describe_org_follows_the_workspaces_real_org_not_the_caller(graph_db_env, monkeypatch):
    """``cmd_commit_policy_describe`` must not silently default a
    workspace-scoped lookup to the caller's own org — it must follow the
    workspace's actual registered org when no ``--org`` is supplied."""
    from tools.graph import commit_policy_cmd

    class _FakeWorkspace:
        graph_project = "anchore"

    monkeypatch.setattr(
        "agents.workspace_settings.get_workspace",
        lambda workspace_id: _FakeWorkspace(),
    )
    assert commit_policy_cmd._describe_org("enterprise-ng", None) == "anchore"
    # An explicit --org always wins over the workspace's registered org.
    assert commit_policy_cmd._describe_org("enterprise-ng", "autonomy") == "autonomy"
    # No workspace id at all: falls back to the caller org.
    assert commit_policy_cmd._describe_org(None, None) == ops.CALLER_ORG


def test_describe_org_falls_back_to_caller_org_for_unknown_workspace(multi_org_env):
    """A workspace id that isn't registered yet (e.g. brand new) must not
    raise — describe still falls back to the caller org and correctly
    resolves to safe.default rather than erroring."""
    from tools.graph import commit_policy_cmd
    assert commit_policy_cmd._describe_org("does-not-exist", None) == ops.CALLER_ORG


def test_missing_workspace_still_resolves_safe_default(multi_org_env):
    """Regression guard: a workspace with no Setting row anywhere still
    correctly resolves to safe.default, not an error."""
    resolved = resolve_commit_policy(workspace_id="never-seeded", org="anchore")
    assert resolved.key == "built-in:safe.default"


def test_workspace_primer_renders_resolved_commit_policy(multi_org_env):
    # The workspace's policy lives in the workspace's org DB — a
    # caller-scope seed would land in personal.db where the renderer's
    # org-scoped resolve never looks.
    seed_workspace_policy(
        workspace_id="autonomy",
        org="autonomy",
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


def test_commit_policy_block_issue_linkage_uses_real_capability_context(graph_db_env):
    from agents.workspace_settings import MaterializedCapability

    # issue_linkage.required == False: no issue-linkage error regardless of
    # whether an issue tracker is configured for the workspace.
    seed_workspace_policy(
        workspace_id="no-linkage-ws",
        org=ops.CALLER_ORG,
        profile=AUTONOMY_PROFILE,
    )
    no_linkage_workspace = WorkspaceV1(
        id="no-linkage-ws",
        name="No Linkage",
        description="",
        image="autonomy-agent:dashboard",
        graph_project=ops.CALLER_ORG,
        repos=(RepoMount(url="u", mount="/workspace/repo", writable=True),),
    )
    block = _commit_policy_block(no_linkage_workspace)
    assert "issue_linkage is required" not in " ".join(block["errors"])

    # issue_linkage.required == True and the workspace really has no issue
    # tracker enabled: the error must still fire (regression guard).
    seed_workspace_policy(
        workspace_id="enterprise-no-tracker-ws",
        org=ops.CALLER_ORG,
        profile=ENTERPRISE_PROFILE,
    )
    no_tracker_workspace = WorkspaceV1(
        id="enterprise-no-tracker-ws",
        name="Enterprise No Tracker",
        description="",
        image="autonomy-agent:dashboard",
        graph_project=ops.CALLER_ORG,
        repos=(RepoMount(url="u", mount="/workspace/repo", writable=True),),
    )
    block = _commit_policy_block(no_tracker_workspace)
    assert "issue_linkage is required" in " ".join(block["errors"])

    # issue_linkage.required == True and the workspace DOES have an issue
    # tracker capability enabled: no issue-linkage error.
    tracker_workspace = WorkspaceV1(
        id="enterprise-no-tracker-ws",
        name="Enterprise With Tracker",
        description="",
        image="autonomy-agent:dashboard",
        graph_project=ops.CALLER_ORG,
        repos=(RepoMount(url="u", mount="/workspace/repo", writable=True),),
        capabilities=(
            MaterializedCapability(
                contract="issue_tracker",
                contract_version=1,
                implementation="jira",
                implementation_version=1,
                delivery_mode="mounted_tools",
                package_root="agents/capabilities/jira",
                mount_target="/opt/autonomy/capabilities/jira",
            ),
        ),
    )
    block = _commit_policy_block(tracker_workspace)
    assert "issue_linkage is required" not in " ".join(block["errors"])


def test_deploy_seed_only_autonomy_workspaces(multi_org_env):
    class Workspace:
        def __init__(self, graph_project: str):
            self.graph_project = graph_project

    results = seed_default_workspace_policies({
        "autonomy": Workspace("autonomy"),
        "enterprise": Workspace("anchore"),
    })
    assert results == {"autonomy": "inserted"}
    # Each workspace's policy resolves in the workspace's OWN org — the
    # old caller-scope reads only worked while a GRAPH_DB pin collapsed
    # every org into one file (the tautology this sweep retires).
    resolved = resolve_commit_policy(workspace_id="autonomy", org="autonomy")
    assert resolved.profile == AUTONOMY_PROFILE
    assert resolve_commit_policy(workspace_id="enterprise", org="anchore").profile == "safe.default"


def test_enterprise_no_issue_profile_matches_enterprise_signed_pr_except_linkage():
    """The dedicated no-issue profile is enterprise.signed-pr in every
    dimension except issue linkage — a ticket reference is a branch-naming
    convention (operator decision, 2026), not a policy requirement."""
    with_issue = expand_commit_policy_payload({
        "profile": "enterprise.signed-pr",
        "override_mode": "none",
    })
    without_issue = expand_commit_policy_payload({
        "profile": "enterprise.signed-pr-no-issue",
        "override_mode": "none",
    })
    diff_keys = {
        k for k in with_issue
        if with_issue[k] != without_issue.get(k)
    }
    assert diff_keys == {"profile", "issue_linkage"}
    assert without_issue["issue_linkage"]["required"] is False

    errors = validate_commit_policy(
        without_issue,
        context=WorkspaceCapabilityContext(),
        raise_on_error=False,
    )
    assert not any("issue_linkage" in e for e in errors)


def test_anchore_workspace_policies_seeded_and_resolve_without_issue_linkage(multi_org_env):
    """P0-1: the two Anchore workspace commit-policy Settings rows exist in
    the anchore org DB and resolve to the enterprise workflow shape without
    requiring issue linkage."""
    from tools.graph.commit_policy import seed_workspace_policy

    for workspace_id in ("enterprise-ng", "enterprise-v5"):
        inserted = seed_workspace_policy(
            workspace_id=workspace_id,
            org="anchore",
            profile="enterprise.signed-pr-no-issue",
        )
        assert inserted is True

        resolved = resolve_commit_policy(workspace_id=workspace_id, org="anchore")
        assert resolved.key == f"workspace:{workspace_id}"
        assert resolved.profile == "enterprise.signed-pr-no-issue"
        assert resolved.payload["issue_linkage"]["required"] is False
        assert resolved.payload["branch_mode"] == "pr_branch"
        assert resolved.payload["signature_requirement"] == "signoff_and_gpg"
        assert resolved.errors == ()

        # Not visible from the autonomy org — proves the row genuinely lives
        # in anchore, not merely resolvable from anywhere.
        wrong_org = resolve_commit_policy(workspace_id=workspace_id, org="autonomy")
        assert wrong_org.key == "built-in:safe.default"
