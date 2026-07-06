"""Commit API capability routes.

This plugin exposes the first runtime tranche of the commit@1 API:
policy resolution, policy description, and workflow proposal.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from agents.workspace_settings import get_workspace, resolve_capabilities
from tools.graph import ops
from tools.graph.commit_policy import (
    ResolvedCommitPolicy,
    WorkspaceCapabilityContext,
    describe_commit_policy,
    describe_commit_policy_json,
    resolve_commit_policy_from_members,
)
from tools.graph.schemas.commit_policy import (
    COMMIT_POLICY_REVISION,
    COMMIT_POLICY_SET_ID,
)
from tools.dashboard.commit_api.errors import commit_api_error
from tools.dashboard.commit_api.types import (
    CommitProposeRequest,
    CommitProposeResponse,
    DescribePolicyRequest,
    DescribePolicyResponse,
    NextAction,
    ResolvePolicyRequest,
    ResolvePolicyResponse,
    ResolvedPlanRef,
    RepoScope,
    WorkflowRef,
)
from tools.dashboard.plugin_api.schema import PLUGIN_SET_ID
from tools.dashboard.dao import commit_workflow_db as cdb


logger = logging.getLogger(__name__)

PLUGIN_ID = "commit-api"
PLUGIN_ORG = "autonomy"
PLUGIN_DIR = Path(__file__).resolve().parents[1]


def _json_error(code: str, message: str, *, details: dict[str, Any] | None = None):
    err = commit_api_error(code, message, details=details)
    return JSONResponse(err.to_response(), status_code=err.http_status)


def _plugin_enabled() -> bool:
    try:
        settings = ops.read_set(PLUGIN_SET_ID, org=PLUGIN_ORG, peers=[]).to_dict()
    except Exception:
        return False
    payload = settings.get(PLUGIN_ID)
    if payload is None:
        return False
    return bool(getattr(payload, "payload", payload).get("enabled", True))


def _caller_org(request: Request) -> str | None:
    return request.headers.get("X-Graph-Org") or ops.CALLER_ORG


def _workspace_graph_org(workspace_id: str | None) -> str | None:
    if not workspace_id:
        return None
    try:
        return get_workspace(workspace_id).graph_project
    except Exception:
        return None


def _capability_context_for_workspace(
    workspace_id: str | None,
    org: str | None,
) -> WorkspaceCapabilityContext | None:
    if not workspace_id:
        return None
    try:
        caps = resolve_capabilities(workspace_id, org=org)
    except Exception:
        return None
    return WorkspaceCapabilityContext(
        issue_tracker_enabled=any(cap.contract == "issue_tracker" for cap in caps),
    )


def _policy_members(org: str | None) -> dict[str, Any]:
    if org is None:
        return {}
    try:
        return ops.read_set(
            COMMIT_POLICY_SET_ID,
            org=org,
            peers=[],
            target_revision=COMMIT_POLICY_REVISION,
        ).to_dict()
    except Exception:
        return {}


def _policy_resolution(
    *,
    workspace_id: str | None,
    repo_slug: str | None,
    org: str | None,
) -> ResolvedCommitPolicy:
    effective_org = _workspace_graph_org(workspace_id) or org
    members = _policy_members(effective_org)
    ctx = _capability_context_for_workspace(workspace_id, effective_org)
    return resolve_commit_policy_from_members(
        members=members,
        workspace_id=workspace_id,
        repo_slug=repo_slug,
        org=effective_org,
        context=ctx,
    )


def _security_dimensions_hash(payload: dict[str, Any]) -> str:
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return digest


def _resolved_plan_ref(resolved: ResolvedCommitPolicy) -> ResolvedPlanRef:
    return ResolvedPlanRef(
        policy_key=resolved.key,
        policy_version=str(COMMIT_POLICY_REVISION),
        profile=resolved.profile,
        resolved_plan=resolved.payload,
        security_dimensions_hash=_security_dimensions_hash(resolved.payload),
    )


def _scope_from_resolve_request(request: ResolvePolicyRequest, org: str | None) -> tuple[RepoScope, str | None, ResolvedCommitPolicy]:
    resolved = _policy_resolution(
        workspace_id=request.scope.workspace_id,
        repo_slug=request.scope.repo_slug,
        org=org,
    )
    return request.scope, org, resolved


def _describe_org(workspace_id: str | None, explicit_org: str | None, request_org: str | None) -> str | None:
    if explicit_org:
        return explicit_org
    if workspace_id:
        ws_org = _workspace_graph_org(workspace_id)
        if ws_org:
            return ws_org
        return request_org
    return request_org


def _describe_actions(payload: dict[str, Any]) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    allowed = ["resolve_policy", "describe_policy"]
    required: list[str] = []
    forbidden: list[dict[str, Any]] = []

    if payload.get("author_policy", {}).get("require_operator_confirmation"):
        required.append("approve")
        allowed.append("approve")
    else:
        required.append("create")
        allowed.append("create")

    if payload.get("signature_requirement") != "none":
        required.append("request_signature")
        allowed.extend(["request_signature", "attach_signature"])

    if payload.get("review_integration") == "required_pr_watch":
        required.append("link_review")
        allowed.append("link_review")

    if payload.get("push_requirement") != "forbidden":
        allowed.append("publish")
    else:
        forbidden.append({
            "action": "publish",
            "reason": "push is forbidden by the active policy",
        })

    seen: set[str] = set()
    ordered_allowed: list[str] = []
    for action in allowed:
        if action in seen:
            continue
        seen.add(action)
        ordered_allowed.append(action)
    return ordered_allowed, required, forbidden


def _required_steps(resolved: ResolvedCommitPolicy) -> list[NextAction]:
    payload = resolved.payload
    if resolved.errors:
        return [NextAction(
            action="resolve_blocker",
            required_actor="operator",
            reason="; ".join(resolved.errors),
        )]
    steps: list[NextAction] = []
    if payload.get("author_policy", {}).get("require_operator_confirmation"):
        steps.append(NextAction(
            action="approve",
            required_actor="operator",
            reason="policy requires operator approval",
        ))
    if payload.get("signature_requirement") != "none":
        steps.append(NextAction(
            action="request_signature",
            required_actor="local_signer",
            reason="policy requires a local signature handoff",
        ))
    else:
        steps.append(NextAction(
            action="create",
            required_actor="agent_session",
            reason="policy allows commit creation",
        ))
    if payload.get("review_integration") == "required_pr_watch":
        steps.append(NextAction(
            action="link_review",
            required_actor="dashboard",
            reason="policy requires provider review linkage",
        ))
    return steps


def _workflow_ref(
    *,
    workflow_id: str,
    repo_slug: str,
    status: str,
    latest_event_id: str,
    created_by_session_name: str | None,
    created_by_actor_id: str | None,
    mutation_owner_session_name: str | None,
) -> WorkflowRef:
    return WorkflowRef(
        workflow_id=workflow_id,
        repo_slug=repo_slug,
        status=status,
        terminal=status in cdb.TERMINAL_STATUSES,
        latest_event_id=latest_event_id,
        created_by_session_name=created_by_session_name,
        created_by_actor_id=created_by_actor_id,
        mutation_owner_session_name=mutation_owner_session_name,
        handoff_state="owner_only",
    )


def _status_next_action(status: str) -> NextAction:
    if status == "duplicate_active":
        return NextAction(
            action="resolve_blocker",
            required_actor="operator",
            reason="a workflow with the same content fingerprint is already active",
        )
    if status == "awaiting_approval":
        return NextAction(
            action="approve",
            required_actor="operator",
            reason="workflow awaits operator approval",
        )
    return NextAction(
        action="create",
        required_actor="agent_session",
        reason="policy permits creation",
    )


def _proposal_review_state(resolved: ResolvedCommitPolicy) -> str:
    if resolved.errors:
        return "blocked"
    if resolved.payload.get("author_policy", {}).get("require_operator_confirmation"):
        return "awaiting_approval"
    return "proposed"


def _serialize_propose_response(
    *,
    workflow: WorkflowRef,
    plan: ResolvedPlanRef,
    duplicate_of_workflow_id: str | None,
    next_action: NextAction,
    event_id: str,
) -> CommitProposeResponse:
    return CommitProposeResponse(
        workflow=workflow,
        plan=plan,
        duplicate_of_workflow_id=duplicate_of_workflow_id,
        approvals=[],
        drift_validation_token=hashlib.sha256(
            f"{workflow.workflow_id}:{workflow.latest_event_id}".encode("utf-8")
        ).hexdigest(),
        next_action=next_action,
        events=[event_id],
    )


def _parse_describe_request(request: Request) -> DescribePolicyRequest:
    query = request.query_params
    return DescribePolicyRequest(
        workspace_id=query.get("workspace_id") or None,
        repo_slug=query.get("repo_slug") or None,
        repo_name=query.get("repo_name") or None,
        session_name=query.get("session_name") or None,
        target_branch=query.get("target_branch") or None,
        format=query.get("format") or "text",
    )


async def resolve_policy(request: Request) -> JSONResponse:
    if not _plugin_enabled():
        return _json_error("route_not_trusted", "commit API plugin is disabled")
    try:
        body = await request.json()
        parsed = ResolvePolicyRequest.from_dict(body)
    except Exception as exc:
        return _json_error("invalid_request", f"invalid resolve-policy request: {exc}")

    org = _caller_org(request)
    scope, _org, resolved = _scope_from_resolve_request(parsed, org)
    plan = _resolved_plan_ref(resolved)
    response = ResolvePolicyResponse(
        canonical_scope=scope,
        plan=plan,
        required_steps=_required_steps(resolved),
        disallowed_steps=[] if not resolved.errors else [
            {"action": "resolve_blocker", "reason": err} for err in resolved.errors
        ],
        ui_hints={
            "policy_text": describe_commit_policy(resolved).rstrip(),
            "policy_json": json.loads(describe_commit_policy_json(resolved)),
        },
    )
    return JSONResponse(response.to_dict())


async def describe_policy(request: Request) -> JSONResponse:
    if not _plugin_enabled():
        return _json_error("route_not_trusted", "commit API plugin is disabled")
    try:
        parsed = _parse_describe_request(request)
    except Exception as exc:
        return _json_error("invalid_request", f"invalid describe-policy query: {exc}")

    request_org = _caller_org(request)
    resolved_org = _describe_org(parsed.workspace_id, None, request_org)
    if parsed.workspace_id and _workspace_graph_org(parsed.workspace_id) is None:
        display_org = request.headers.get("X-Graph-Org") or os.environ.get("GRAPH_ORG") or "personal"
        return _json_error(
            "workflow_not_found",
            f"no workspace {parsed.workspace_id!r} found in organization {display_org!r}",
            details={"workspace_id": parsed.workspace_id, "org": display_org},
        )

    resolved = _policy_resolution(
        workspace_id=parsed.workspace_id,
        repo_slug=parsed.repo_slug,
        org=resolved_org,
    )
    plan = _resolved_plan_ref(resolved)
    allowed_actions, required_actions, forbidden_actions = _describe_actions(resolved.payload)
    primer_text = (
        describe_commit_policy_json(resolved)
        if parsed.format == "json"
        else describe_commit_policy(resolved)
    )
    response = DescribePolicyResponse(
        canonical_scope=RepoScope(
            workspace_id=parsed.workspace_id or "",
            repo_slug=parsed.repo_slug or "",
            repo_name_alias=parsed.repo_name,
            session_name=parsed.session_name,
            worktree_path=None,
            branch=None,
            target_branch=parsed.target_branch,
        ),
        plan=plan,
        primer_text=primer_text,
        allowed_actions=allowed_actions,
        required_actions=required_actions,
        forbidden_actions=forbidden_actions,
        source_settings_keys=[] if resolved.key == "built-in:safe.default" else [resolved.key],
    )
    return JSONResponse(response.to_dict())


def _proposal_scope(request: CommitProposeRequest, request_org: str | None) -> str | None:
    ws_org = _workspace_graph_org(request.scope.workspace_id)
    if ws_org:
        return ws_org
    return request_org


def _persist_proposal(
    *,
    request_model: CommitProposeRequest,
    resolved: ResolvedCommitPolicy,
    org: str | None,
) -> CommitProposeResponse:
    conn = cdb._get_conn()
    try:
        cdb.init_schema_on_connection(conn)
        conn.execute("BEGIN IMMEDIATE")

        actor_session = request_model.scope.session_name or request_model.author.name
        actor_type = "agent_session"
        actor_id = actor_session or request_model.author.email
        scope_key = f"{request_model.scope.repo_slug}:{request_model.content.content_fingerprint}"
        request_fields = request_model.to_dict()
        idem_state = cdb.lookup_idempotency(
            conn,
            actor_type=actor_type,
            actor_id=actor_id,
            scope_key=scope_key,
            operation="propose",
            raw_idempotency_key=request_model.idempotency_key,
            request_fields=request_fields,
        )
        if idem_state["kind"] == "conflict":
            raise commit_api_error(
                "idempotency_conflict",
                "same idempotency key used for a different propose request",
            )
        if idem_state["kind"] == "in_flight":
            raise commit_api_error(
                "idempotency_in_flight",
                "a proposal with this idempotency key is already in flight",
            )
        if idem_state["kind"] == "completed_replay":
            payload = idem_state["response_json"]
            return CommitProposeResponse.from_dict(payload)
        if idem_state["kind"] == "failed_retryable":
            raise commit_api_error(
                "idempotency_in_flight",
                "a previous proposal with this idempotency key is retryable but still unresolved",
            )
        if idem_state["kind"] == "failed_terminal":
            raise commit_api_error(
                "invalid_transition",
                "a previous proposal with this idempotency key failed terminally",
            )

        now = time.time()
        idem_record = cdb.reserve_idempotency(
            conn,
            actor_type=actor_type,
            actor_id=actor_id,
            scope_key=scope_key,
            operation="propose",
            raw_idempotency_key=request_model.idempotency_key,
            request_fields=request_fields,
            workflow_id=uuid.uuid4().hex,
        )
        workflow_id = str(idem_record["workflow_id"])
        duplicate_of_workflow_id = None
        active = conn.execute(
            """
            SELECT workflow_id
            FROM commit_workflow_states
            WHERE repo_slug = ?
              AND content_fingerprint = ?
              AND terminal = 0
              AND status <> 'duplicate_active'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (request_model.scope.repo_slug, request_model.content.content_fingerprint),
        ).fetchone()
        if active is not None:
            duplicate_of_workflow_id = str(active["workflow_id"])

        status_after = _proposal_review_state(resolved)
        event_id = uuid.uuid4().hex
        payload = {
            "request": request_fields,
            "policy_key": resolved.key,
            "policy_version": str(COMMIT_POLICY_REVISION),
            "resolved_plan": resolved.payload,
            "duplicate_of_workflow_id": duplicate_of_workflow_id,
        }
        conn.execute(
            """
            INSERT INTO commit_workflow_events (
                event_id, workflow_id, event_type, status_after, occurred_at,
                actor_type, actor_id, session_name, repo_slug, branch,
                commit_shas_json, commit_roles_json, content_fingerprint, provider,
                provider_review_id, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                workflow_id,
                "duplicate_active" if duplicate_of_workflow_id else status_after,
                "duplicate_active" if duplicate_of_workflow_id else status_after,
                now,
                actor_type,
                actor_id,
                request_model.scope.session_name,
                request_model.scope.repo_slug,
                request_model.scope.branch,
                json.dumps([request_model.content.head_sha]),
                json.dumps({}),
                request_model.content.content_fingerprint,
                None,
                None,
                json.dumps(payload, sort_keys=True),
            ),
        )
        cdb._rebuild_projection_for_workflow(conn, workflow_id)
        state = conn.execute(
            "SELECT * FROM commit_workflow_states WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
        if state is None:
            raise RuntimeError("proposal projection missing after append")
        workflow = _workflow_ref(
            workflow_id=workflow_id,
            repo_slug=request_model.scope.repo_slug,
            status=state["status"],
            latest_event_id=state["last_event_id"],
            created_by_session_name=state["session_name"],
            created_by_actor_id=actor_id,
            mutation_owner_session_name=state["session_name"],
        )
        response = _serialize_propose_response(
            workflow=workflow,
            plan=_resolved_plan_ref(resolved),
            duplicate_of_workflow_id=duplicate_of_workflow_id,
            next_action=_status_next_action(state["status"]),
            event_id=event_id,
        )
        cdb.finalize_idempotency(
            conn,
            idempotency_id=str(idem_record["idempotency_id"]),
            status="completed",
            response_json=response.to_dict(),
            event_ids=[event_id],
            side_effect_ref=workflow_id,
            updated_at=now,
            expires_at=float(idem_record["expires_at"]),
        )
        conn.commit()
        return response
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise commit_api_error(
            "duplicate_active_workflow",
            f"proposal violated workflow uniqueness: {exc}",
        )
    finally:
        conn.close()


async def propose(request: Request) -> JSONResponse:
    if not _plugin_enabled():
        return _json_error("route_not_trusted", "commit API plugin is disabled")
    try:
        body = await request.json()
        parsed = CommitProposeRequest.from_dict(body)
    except Exception as exc:
        return _json_error("invalid_request", f"invalid proposal request: {exc}")

    request_org = _caller_org(request)
    org = _proposal_scope(parsed, request_org)
    resolved = _policy_resolution(
        workspace_id=parsed.scope.workspace_id,
        repo_slug=parsed.scope.repo_slug,
        org=org,
    )
    try:
        response = _persist_proposal(
            request_model=parsed,
            resolved=resolved,
            org=org,
        )
    except Exception as exc:
        if isinstance(exc, sqlite3.IntegrityError):
            return _json_error("duplicate_active_workflow", str(exc))
        if isinstance(exc, json.JSONDecodeError):
            return _json_error("invalid_request", str(exc))
        if hasattr(exc, "code") and hasattr(exc, "http_status"):
            return JSONResponse(exc.to_response(), status_code=exc.http_status)  # type: ignore[union-attr]
        logger.exception("commit propose failed")
        return _json_error("provider_error", f"proposal failed: {exc}")
    return JSONResponse(response.to_dict())


routes: list[Route] = [
    Route("/api/capabilities/commit/v1/resolve-policy", resolve_policy, methods=["POST"]),
    Route("/api/capabilities/commit/v1/policy/describe", describe_policy, methods=["GET"]),
    Route("/api/capabilities/commit/v1/proposals", propose, methods=["POST"]),
]
