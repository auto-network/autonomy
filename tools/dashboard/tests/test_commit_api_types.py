from __future__ import annotations

import importlib
import pytest

t = importlib.import_module("tools.dashboard.commit_api.types")


def _round_trip(instance):
    cls = type(instance)
    assert cls.from_dict(instance.to_dict()) == instance


def test_types_roundtrip():
    scope = t.RepoScope(
        workspace_id="ws-1",
        repo_slug="repo-slug",
        repo_name_alias="repo",
        session_name="sess-1",
        worktree_path="/tmp/worktree",
        branch="session/sess-1",
        target_branch="main",
    )
    git = t.GitIdentity("Ada", "ada@example.com", "2026-07-06T19:00:00Z", "Z")
    message = t.CommitMessage("subject", "body", {"Signed-off-by": "Ada <ada@example.com>"})
    plan = t.ResolvedPlanRef(
        policy_key="commit.policy#1",
        policy_version="v1",
        profile="enterprise.signed-pr",
        resolved_plan={"steps": ["commit", "sign"]},
        security_dimensions_hash="hash-1",
    )
    drift = t.DriftToken("head", "tree", "index", "status-hash", "2026-07-06T19:00:00Z")
    content = t.ContentIdentity("head", "tree", "patch-id", "content-fp")
    actor = t.Actor("agent_session", "actor-1", "sess-1", None, {"provider": "github"}, "session_token")
    snapshot = t.ActorSnapshot("agent_session", "actor-1", "sess-1", None)
    approval = t.ApprovalSummary("approval-1", "commit", "pending", True, None)
    signing = t.SigningSummary("sign-1", "pending", "gpg", "canon-hash", "store://ref", None)
    event = t.WorkflowEventRecord(
        event_id="evt-1",
        workflow_id="wf-1",
        event_type="proposed",
        status_after="proposed",
        occurred_at="2026-07-06T19:00:00Z",
        actor_snapshot=snapshot,
        determinism_class="deterministic",
        result={"ok": True},
    )
    next_action = t.NextAction("approve", "operator", "approval required")
    workflow = t.WorkflowRef(
        workflow_id="wf-1",
        repo_slug="repo-slug",
        status="proposed",
        terminal=False,
        latest_event_id="evt-1",
        created_by_session_name="sess-1",
        created_by_actor_id="actor-1",
        mutation_owner_session_name="sess-1",
        handoff_state="owner_only",
    )

    instances = [
        actor,
        snapshot,
        scope,
        git,
        message,
        workflow,
        plan,
        drift,
        content,
        approval,
        signing,
        event,
        next_action,
        t.CommitWorkflowResponse(
            workflow=workflow,
            plan=plan,
            approvals=[approval],
            signing=signing,
            next_action=next_action,
            events=[event],
            warnings=[{"kind": "note"}],
        ),
        t.ResolvePolicyIntent("workspace_shared", ["operator"], {"review": True}, {"push": True}),
        t.ResolvePolicyRequest(scope=scope, intent=t.ResolvePolicyIntent("workspace_shared", ["operator"], {"review": True}, {"push": True})),
        t.ResolvePolicyResponse(
            canonical_scope=scope,
            plan=plan,
            required_steps=[next_action],
            disallowed_steps=[{"action": "publish"}],
            ui_hints={"title": "Commit policy"},
        ),
        t.DescribePolicyRequest("ws-1", "repo-slug", "repo", "sess-1", "main", "both"),
        t.DescribePolicyResponse(
            canonical_scope=scope,
            plan=plan,
            primer_text="primer",
            allowed_actions=["commit"],
            required_actions=["sign"],
            forbidden_actions=[{"action": "publish"}],
            source_settings_keys=["autonomy.commit.policy#1"],
        ),
        t.CommitProposeTarget("main", "current_branch", "workspace_shared", {"review_target": True}),
        t.CommitProposeRequest(
            idempotency_key="idempo-1",
            scope=scope,
            target=t.CommitProposeTarget("main", "current_branch", "workspace_shared", {"review_target": True}),
            content=content,
            drift_token=drift,
            message=message,
            author=git,
            committer=git,
            signoff_present=True,
            issue_refs=["#1"],
            push_pr_intent=None,
            client_observed_policy_version="v1",
        ),
        t.CommitProposeResponse(
            workflow=workflow,
            plan=plan,
            duplicate_of_workflow_id=None,
            approvals=[approval],
            drift_validation_token="token-1",
            next_action=next_action,
            events=["evt-1"],
        ),
        t.CommitReviseMessageRequest("idempo-2", "wf-1", message, "note"),
        t.CommitReviseMessageResponse(workflow, "msg-fp", ["approval-1"], ["sign-1"], next_action, ["evt-2"]),
        t.CommitValidateRequest("wf-1", drift, "policy"),
        t.CommitValidateResponse(
            workflow=workflow,
            drift_status="clean",
            policy_status="ok",
            signoff_status={"ok": True},
            authorship_status={"ok": True},
            required_next_steps=[next_action],
            errors=[],
        ),
        t.ApprovalConstraints("diff-fp", "msg-fp", "sha-1", "2026-07-07T00:00:00Z"),
        t.CommitApproveRequest(
            idempotency_key="idempo-3",
            workflow_id="wf-1",
            approval_id="approval-1",
            approval_type="force_with_lease",
            decision="approved",
            operator_edits={"note": "ok"},
            constraints=t.ApprovalConstraints("diff-fp", "msg-fp", "sha-1", "2026-07-07T00:00:00Z"),
            provider_entitlement_proof={"proof": True},
        ),
        t.CommitApproveResponse(workflow=workflow, approval=approval, next_action=next_action, events=["evt-3"]),
        t.CommitCreateRequest("idempo-4", "wf-1", drift, "unsigned_commit"),
        t.CommitCreateResponse(
            workflow=workflow,
            content=content,
            unsigned_commit_sha="commit-sha",
            trusted_object_store_ref="store://ref",
            canonical_payload_hash="canon-hash",
            signing=signing,
            next_action=next_action,
            events=["evt-4"],
        ),
        t.CommitRequestSignatureRequest("idempo-5", "wf-1", "gpg", "policy-v1", "op-1"),
        t.CommitRequestSignatureResponse(
            workflow=workflow,
            signing=signing,
            canonical_payload_preview={"sha": "commit-sha"},
            local_signer_handoff={"device": "laptop"},
            next_action=next_action,
            events=["evt-5"],
        ),
        t.LocalSignerAttestation("dev-1", "nonce-1", "canon-hash", "display-hash", "2026-07-06T19:00:00Z"),
        t.CommitAttachSignatureRequest(
            "idempo-6",
            "sign-1",
            "sig-ref",
            "armored-signature",
            "signed-object",
            t.LocalSignerAttestation("dev-1", "nonce-1", "canon-hash", "display-hash", "2026-07-06T19:00:00Z"),
        ),
        t.CommitAttachSignatureResponse(
            workflow=workflow,
            signing=signing,
            signed_commit_sha="commit-sha",
            verification={"ok": True},
            next_action=next_action,
            events=["evt-6"],
        ),
        t.CommitPublishRefUpdateIntent("github", "repo-slug", "refs/heads/main", "fast_forward_existing_ref", "old-sha", "new-sha"),
        t.CommitPublishRequest(
            "idempo-7",
            "wf-1",
            t.CommitPublishRefUpdateIntent("github", "repo-slug", "refs/heads/main", "fast_forward_existing_ref", "old-sha", "new-sha"),
            "origin_push",
        ),
        t.CommitPublishResponse(
            workflow=workflow,
            ref_update_result={"updated": True},
            provider_url="https://github.com/org/repo",
            pushed_ref="refs/heads/main",
            next_action=next_action,
            events=["evt-7"],
        ),
        t.CommitLinkReviewRequest("idempo-8", "wf-1", "github", "review-1", True, "base", "head"),
        t.CommitLinkReviewResponse(
            workflow=workflow,
            review={"review_id": "review-1"},
            review_binding_key="binding-1",
            watch_armed=True,
            next_action=next_action,
            events=["evt-8"],
        ),
        t.CommitMarkLandedEvidence("refs/heads/main", "target-sha", "2026-07-06T19:00:00Z", ["sha-1", "sha-2"]),
        t.CommitMarkLandedRequest(
            "idempo-9",
            "wf-1",
            "provider_merge",
            t.CommitMarkLandedEvidence("refs/heads/main", "target-sha", "2026-07-06T19:00:00Z", ["sha-1", "sha-2"]),
        ),
        t.CommitMarkLandedResponse(workflow=workflow, next_action=next_action, events=["evt-9"]),
        t.CommitSupersedeReplacement("wf-2", ["sha-2"], "superseded by newer patch"),
        t.CommitSupersedeRequest(
            "idempo-10",
            "wf-1",
            t.CommitSupersedeReplacement("wf-2", ["sha-2"], "superseded by newer patch"),
            True,
        ),
        t.CommitSupersedeResponse(workflow=workflow, lineage={"replacement": "wf-2"}, events=["evt-10"]),
        t.CommitAbandonRequest("idempo-11", "wf-1", "no longer needed", True),
        t.CommitAbandonResponse(workflow=workflow, teardown={"requested": True}, events=["evt-11"]),
        t.CommitMarkRevertedEvidence("refs/heads/main", ["sha-1"], "revert-sha", "2026-07-06T19:00:00Z"),
        t.CommitMarkRevertedRequest(
            "idempo-12",
            "wf-1",
            t.CommitMarkRevertedEvidence("refs/heads/main", ["sha-1"], "revert-sha", "2026-07-06T19:00:00Z"),
        ),
        t.CommitMarkRevertedResponse(workflow=workflow, events=["evt-12"]),
        t.CommitReconcileRequest("idempo-13", scope, ["wf-1"], ["event_projection", "agentic_advisory"], False),
        t.CommitReconcileResponse(observations=[{"kind": "observation"}], events=["evt-13"], dry_run=False),
    ]

    for instance in instances:
        _round_trip(instance)


def test_types_reject_bad_enum():
    with pytest.raises(ValueError):
        t.NextAction(action="frobnicate", required_actor="operator", reason="bad")
