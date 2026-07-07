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
import shutil
import subprocess
import time
import uuid
import tempfile
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from agents.workspace_settings import get_workspace, resolve_capabilities
from agents.capabilities.github.service import derive_repo_slug
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
from tools.dashboard.commit_broker.keys import (
    InMemoryBrokerKeyStore,
    VerificationKeyNotRegistered,
    resolve_verification_key,
)
from tools.dashboard.commit_api.types import (
    CommitAttachSignatureRequest,
    CommitAttachSignatureResponse,
    CommitCreateRequest,
    CommitCreateResponse,
    CommitPublishRequest,
    CommitPublishRefUpdateIntent,
    CommitPublishResponse,
    CommitRequestSignatureRequest,
    CommitRequestSignatureResponse,
    DriftToken,
    CommitProposeRequest,
    CommitProposeResponse,
    DescribePolicyRequest,
    DescribePolicyResponse,
    NextAction,
    SigningSummary,
    ResolvePolicyRequest,
    ResolvePolicyResponse,
    ResolvedPlanRef,
    RepoScope,
    WorkflowRef,
)
from tools.dashboard.dao import auth_db, dashboard_db
from tools.dashboard.plugin_api.schema import PLUGIN_SET_ID
from tools.dashboard.dao import commit_workflow_db as cdb
from tools.dashboard.dao import trusted_git_object_store as snapshot_dao
from tools.dashboard.commit_broker.assembly import (
    assemble_signed_commit,
    commit_object_sha,
    fold_gpgsig_block,
    serialize_unsigned_commit_payload,
)
from tools.dashboard.services.trusted_git_object_store import (
    ContentAddressedStore,
    ObjectIntegrityError,
    capture_snapshot,
    verify_snapshot,
)
from tools.dashboard.worktree_monitor import worktree_monitor


logger = logging.getLogger(__name__)

PLUGIN_ID = "commit-api"
PLUGIN_ORG = "autonomy"
PLUGIN_DIR = Path(__file__).resolve().parents[1]
_BROKER_VERIFICATION_KEY_STORE = InMemoryBrokerKeyStore()


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


def _row_value(row: Any, key: str) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    return getattr(row, key, None)


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


def _authorize_proposal_request(
    request: Request,
    request_model: CommitProposeRequest,
) -> tuple[CommitProposeRequest, str, str] | JSONResponse:
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        return _json_error(
            "unauthenticated",
            "missing or invalid Authorization header",
        )

    raw_token = auth_header[7:]
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    session_name = auth_db.resolve_token(token_hash)
    if session_name is None:
        return _json_error(
            "unauthenticated",
            "invalid or revoked session token",
        )

    session_row = dashboard_db.get_session(session_name)
    if session_row is None or not bool(session_row.get("is_live")):
        return _json_error(
            "unauthenticated",
            "session is not live",
        )

    project = str(session_row.get("project") or "").strip()
    if not project:
        return _json_error(
            "scope_mismatch",
            "authenticated session is not bound to a workspace",
        )

    try:
        workspace = get_workspace(project)
    except Exception:
        return _json_error(
            "scope_mismatch",
            f"authenticated session workspace {project!r} could not be resolved",
        )

    scoped_rows = [
        row for row in worktree_monitor.get_all()
        if _row_value(row, "session_name") == session_name
    ]
    requested_repo_slug = request_model.scope.repo_slug.strip()
    candidates: list[tuple[Any, str]] = []
    for row in scoped_rows:
        managed_clone = _row_value(row, "managed_clone")
        repo_slug = derive_repo_slug(managed_clone) if managed_clone is not None else None
        if not repo_slug:
            continue
        if requested_repo_slug and repo_slug != requested_repo_slug:
            continue
        candidates.append((row, repo_slug))

    if not candidates:
        return _json_error(
            "scope_mismatch",
            "proposal scope does not match the caller's live session worktree",
        )
    if not requested_repo_slug and len(candidates) != 1:
        return _json_error(
            "scope_mismatch",
            "proposal scope is ambiguous for the caller's live session",
        )

    chosen_row, chosen_repo_slug = candidates[0]

    request_workspace_id = request_model.scope.workspace_id.strip()
    if request_workspace_id and request_workspace_id != workspace.id:
        return _json_error(
            "scope_mismatch",
            "proposal scope does not match the caller's live session workspace",
        )

    request_session_name = request_model.scope.session_name or ""
    if request_session_name and request_session_name != session_name:
        return _json_error(
            "scope_mismatch",
            "proposal scope does not match the caller's live session identity",
        )

    request_repo_slug = request_model.scope.repo_slug.strip()
    if request_repo_slug and request_repo_slug != chosen_repo_slug:
        return _json_error(
            "scope_mismatch",
            "proposal scope does not match the caller's allowed worktree",
        )

    chosen_branch = _row_value(chosen_row, "branch")
    if request_model.scope.branch and chosen_branch and request_model.scope.branch != chosen_branch:
        return _json_error(
            "scope_mismatch",
            "proposal branch does not match the caller's allowed worktree",
        )
    if request_model.scope.branch and not chosen_branch:
        return _json_error(
            "scope_mismatch",
            "proposal branch does not match the caller's allowed worktree",
        )

    chosen_worktree_path = _row_value(chosen_row, "worktree_path")
    if request_model.scope.worktree_path and chosen_worktree_path:
        if str(request_model.scope.worktree_path) != str(chosen_worktree_path):
            return _json_error(
                "scope_mismatch",
                "proposal worktree does not match the caller's allowed worktree",
            )
    if request_model.scope.worktree_path and not chosen_worktree_path:
        return _json_error(
            "scope_mismatch",
            "proposal worktree does not match the caller's allowed worktree",
        )

    canonical_scope = RepoScope(
        workspace_id=workspace.id,
        repo_slug=chosen_repo_slug,
        repo_name_alias=request_model.scope.repo_name_alias,
        session_name=session_name,
        worktree_path=str(chosen_worktree_path) if chosen_worktree_path else None,
        branch=str(chosen_branch) if chosen_branch else None,
        target_branch=request_model.scope.target_branch,
    )
    canonical_payload = request_model.to_dict()
    canonical_payload["scope"] = canonical_scope.to_dict()
    return CommitProposeRequest.from_dict(canonical_payload), workspace.graph_project, session_name


def _require_authenticated_session(request: Request) -> tuple[str, dict[str, Any]] | JSONResponse:
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        return _json_error(
            "unauthenticated",
            "missing or invalid Authorization header",
        )

    raw_token = auth_header[7:]
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    session_name = auth_db.resolve_token(token_hash)
    if session_name is None:
        return _json_error(
            "unauthenticated",
            "invalid or revoked session token",
        )

    session_row = dashboard_db.get_session(session_name)
    if session_row is None or not bool(session_row.get("is_live")):
        return _json_error(
            "unauthenticated",
            "session is not live",
        )

    return session_name, session_row


def _row_repo_slug(row: Any) -> str | None:
    managed_clone = _row_value(row, "managed_clone")
    if managed_clone is not None:
        try:
            derived = derive_repo_slug(managed_clone)
        except Exception:
            derived = None
        if derived:
            return derived
    repo_name = _row_value(row, "repo_name")
    if repo_name:
        return str(repo_name)
    return None


def _select_worktree_for_session(
    session_name: str,
    *,
    repo_slug: str | None = None,
) -> tuple[Any, str] | JSONResponse:
    candidates: list[tuple[Any, str]] = []
    for row in worktree_monitor.get_all():
        if _row_value(row, "session_name") != session_name:
            continue
        row_repo_slug = _row_repo_slug(row)
        if not row_repo_slug:
            continue
        if repo_slug and row_repo_slug != repo_slug:
            continue
        candidates.append((row, row_repo_slug))
    if not candidates:
        if repo_slug:
            return _json_error(
                "scope_mismatch",
                "request scope does not match the caller's live session worktree",
            )
        return _json_error(
            "scope_mismatch",
            "request scope is ambiguous for the caller's live session",
        )
    return candidates[0]


def _workflow_state_row(conn: sqlite3.Connection, workflow_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM commit_workflow_states WHERE workflow_id = ?",
        (workflow_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def _workflow_state_payload(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {}
    try:
        return json.loads(row.get("state_json") or "{}")
    except Exception:
        return {}


def _signing_request_row(conn: sqlite3.Connection, signing_request_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM commit_signing_requests WHERE signing_request_id = ?",
        (signing_request_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def _verification_key_store() -> InMemoryBrokerKeyStore:
    return _BROKER_VERIFICATION_KEY_STORE


def _current_drift(worktree_path: Path | str) -> DriftToken:
    head_sha = subprocess.run(
        ["git", "-C", str(worktree_path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    tree_sha = subprocess.run(
        ["git", "-C", str(worktree_path), "write-tree"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "-C", str(worktree_path), "status", "--porcelain=v1", "--untracked-files=all"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    worktree_status_hash = hashlib.sha256(status.encode("utf-8")).hexdigest()
    return DriftToken(
        head_sha=head_sha,
        tree_sha=tree_sha,
        index_sha=tree_sha,
        worktree_status_hash=worktree_status_hash,
        generated_at=str(time.time()),
    )


def _trusted_store() -> ContentAddressedStore:
    root = os.environ.get(
        "TRUSTED_GIT_OBJECT_STORE_ROOT",
        str(Path(__file__).resolve().parents[5] / "data" / "trusted_git_object_store"),
    )
    return ContentAddressedStore(root)


def _git_head_commit(worktree_path: Path | str) -> str | None:
    try:
        return subprocess.run(
            ["git", "-C", str(worktree_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except subprocess.CalledProcessError:
        return None


def _git_index_tree(worktree_path: Path | str) -> str:
    return subprocess.run(
        ["git", "-C", str(worktree_path), "write-tree"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _git_parent_shas(worktree_path: Path | str) -> list[str]:
    head = _git_head_commit(worktree_path)
    if not head:
        return []
    out = subprocess.run(
        ["git", "-C", str(worktree_path), "show", "-s", "--format=%P", head],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return [sha for sha in out.split() if sha]


def _git_identity_line(identity: GitIdentity) -> bytes:
    timestamp = identity.timestamp or str(int(time.time()))
    timezone = identity.timezone or "+0000"
    return f"{identity.name} <{identity.email}> {timestamp} {timezone}".encode("utf-8")


def _commit_message_bytes(message: CommitMessage) -> bytes:
    subject = message.subject.rstrip("\n")
    body = message.body
    trailers = message.trailers or {}
    trailer_lines: list[str] = []
    for key, value in trailers.items():
        if isinstance(value, list):
            for item in value:
                trailer_lines.append(f"{key}: {item}")
        else:
            trailer_lines.append(f"{key}: {value}")
    parts = [subject]
    if body:
        parts.extend(["", body.rstrip("\n")])
    if trailer_lines:
        parts.extend(["", *trailer_lines])
    return ("\n".join(parts) + "\n").encode("utf-8")


def _commit_payload(
    *,
    worktree_path: Path | str,
    message: CommitMessage,
    author: GitIdentity,
    committer: GitIdentity,
) -> tuple[str, bytes, list[str]]:
    tree_oid = _git_index_tree(worktree_path)
    parent_oids = _git_parent_shas(worktree_path)
    payload = serialize_unsigned_commit_payload(
        tree_oid=tree_oid,
        parent_oids=parent_oids,
        author_line=_git_identity_line(author),
        committer_line=_git_identity_line(committer),
        message=_commit_message_bytes(message),
    )
    return tree_oid, payload, parent_oids


def _resolved_workflow_scope(
    request: Request,
    *,
    workflow_id: str,
    repo_slug: str | None = None,
) -> tuple[str, dict[str, Any], Any, str, dict[str, Any], dict[str, Any]] | JSONResponse:
    session_result = _require_authenticated_session(request)
    if isinstance(session_result, JSONResponse):
        return session_result
    session_name, session_row = session_result
    conn = cdb._get_conn()
    try:
        state_row = _workflow_state_row(conn, workflow_id)
        if state_row is None:
            return _json_error("workflow_not_found", f"workflow {workflow_id!r} not found")
        workflow_repo_slug = str(state_row["repo_slug"])
        workflow_session = state_row.get("session_name")
        if workflow_session and workflow_session != session_name:
            return _json_error(
                "scope_mismatch",
                "request scope does not match the caller's live session ownership",
            )
        selected = _select_worktree_for_session(session_name, repo_slug=repo_slug or workflow_repo_slug)
        if isinstance(selected, JSONResponse):
            return selected
        worktree_row, chosen_repo_slug = selected
        if chosen_repo_slug != workflow_repo_slug:
            return _json_error(
                "scope_mismatch",
                "request scope does not match the caller's live session worktree",
            )
        return session_name, session_row, worktree_row, chosen_repo_slug, state_row, _workflow_state_payload(state_row)
    finally:
        conn.close()


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


def _persist_proposal(
    *,
    request_model: CommitProposeRequest,
    resolved: ResolvedCommitPolicy,
    org: str | None,
) -> CommitProposeResponse | JSONResponse:
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
            return _json_error(
                "idempotency_conflict",
                "same idempotency key used for a different propose request",
            )
        if idem_state["kind"] == "in_flight":
            return _json_error(
                "idempotency_in_flight",
                "a proposal with this idempotency key is already in flight",
            )
        if idem_state["kind"] == "completed_replay":
            payload = idem_state["response_json"]
            return CommitProposeResponse.from_dict(payload)
        if idem_state["kind"] == "failed_retryable":
            return _json_error(
                "idempotency_in_flight",
                "a previous proposal with this idempotency key is retryable but still unresolved",
            )
        if idem_state["kind"] == "failed_terminal":
            return _json_error(
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
        return _json_error(
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

    auth_result = _authorize_proposal_request(request, parsed)
    if isinstance(auth_result, JSONResponse):
        return auth_result
    parsed, org, _session_name = auth_result

    resolved = _policy_resolution(
        workspace_id=parsed.scope.workspace_id,
        repo_slug=parsed.scope.repo_slug,
        org=org,
    )
    response = _persist_proposal(
        request_model=parsed,
        resolved=resolved,
        org=org,
    )
    if isinstance(response, JSONResponse):
        return response
    return JSONResponse(response.to_dict())


def _proposal_request_from_state_payload(state_payload: dict[str, Any]) -> CommitProposeRequest | None:
    request_payload = state_payload.get("request")
    if not isinstance(request_payload, dict):
        request_payload = state_payload.get("proposal_request")
    if not isinstance(request_payload, dict):
        return None
    try:
        return CommitProposeRequest.from_dict(request_payload)
    except Exception:
        return None


def _proposal_request_for_workflow(
    conn: sqlite3.Connection,
    workflow_id: str,
    state_payload: dict[str, Any],
) -> CommitProposeRequest | None:
    proposal_request = _proposal_request_from_state_payload(state_payload)
    if proposal_request is not None:
        return proposal_request
    rows = conn.execute(
        """
        SELECT payload_json
        FROM commit_workflow_events
        WHERE workflow_id = ?
        ORDER BY occurred_at DESC, seq DESC
        """,
        (workflow_id,),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except Exception:
            continue
        proposal_request = _proposal_request_from_state_payload(payload if isinstance(payload, dict) else {})
        if proposal_request is not None:
            return proposal_request
    return None


def _drift_matches(requested: DriftToken, live: DriftToken) -> bool:
    return (
        requested.head_sha == live.head_sha
        and requested.tree_sha == live.tree_sha
        and requested.index_sha == live.index_sha
        and requested.worktree_status_hash == live.worktree_status_hash
    )


def _response_idempotency_id(idem_state: dict[str, Any]) -> str | None:
    row = idem_state.get("row")
    if isinstance(row, dict):
        return str(row.get("idempotency_id") or "") or None
    return None


def _response_idempotency_expires_at(idem_state: dict[str, Any]) -> float | None:
    row = idem_state.get("row")
    if isinstance(row, dict):
        expires_at = row.get("expires_at")
        if expires_at is not None:
            return float(expires_at)
    return None


def _canonical_preview_payload(
    *,
    unsigned_commit_sha: str,
    tree_sha: str,
    parent_shas: list[str],
    message: CommitMessage,
    author: GitIdentity,
    committer: GitIdentity,
) -> dict[str, Any]:
    return {
        "unsigned_commit_sha": unsigned_commit_sha,
        "tree_sha": tree_sha,
        "parent_shas": parent_shas,
        "message": message.to_dict(),
        "author": author.to_dict(),
        "committer": committer.to_dict(),
    }


def _workflow_state_json_payload(
    *,
    request_model: CommitCreateRequest,
    proposal_request: CommitProposeRequest,
    unsigned_commit_sha: str,
    tree_sha: str,
    parent_shas: list[str],
    canonical_preview: dict[str, Any],
    trusted_object_store_ref: str | None,
    canonical_payload_hash: str | None,
) -> dict[str, Any]:
    return {
        "request": request_model.to_dict(),
        "proposal_request": proposal_request.to_dict(),
        "create_mode": request_model.create_mode,
        "unsigned_commit_sha": unsigned_commit_sha,
        "tree_sha": tree_sha,
        "parent_shas": parent_shas,
        "trusted_object_store_ref": trusted_object_store_ref,
        "canonical_payload_hash": canonical_payload_hash,
        "canonical_payload_preview": canonical_preview,
    }


def _next_action_for_create(create_mode: str) -> NextAction:
    if create_mode == "unsigned_commit":
        return NextAction(
            action="publish",
            required_actor="dashboard",
            reason="unsigned commit is ready for publish",
        )
    return NextAction(
        action="request_signature",
        required_actor="local_signer",
        reason="commit snapshot captured and awaiting signature request",
    )


def _workflow_ref_from_state(state_row: dict[str, Any], *, actor_id: str | None = None) -> WorkflowRef:
    return _workflow_ref(
        workflow_id=str(state_row["workflow_id"]),
        repo_slug=str(state_row["repo_slug"]),
        status=str(state_row["status"]),
        latest_event_id=str(state_row["last_event_id"]),
        created_by_session_name=state_row.get("session_name"),
        created_by_actor_id=actor_id or state_row.get("session_name"),
        mutation_owner_session_name=state_row.get("session_name"),
    )


async def create(request: Request) -> JSONResponse:
    if not _plugin_enabled():
        return _json_error("route_not_trusted", "commit API plugin is disabled")
    try:
        body = await request.json()
        parsed = CommitCreateRequest.from_dict(body)
    except Exception as exc:
        return _json_error("invalid_request", f"invalid commit-create request: {exc}")

    auth_scope = _resolved_workflow_scope(request, workflow_id=parsed.workflow_id)
    if isinstance(auth_scope, JSONResponse):
        return auth_scope
    session_name, _session_row, worktree_row, repo_slug, state_row, state_payload = auth_scope
    proposal_request = _proposal_request_from_state_payload(state_payload)
    if proposal_request is None:
        return _json_error("invalid_transition", "workflow has no proposal request to create from")

    conn = cdb._get_conn()
    snapshot_conn = None
    try:
        cdb.init_schema_on_connection(conn)
        conn.execute("BEGIN IMMEDIATE")

        current_state = _workflow_state_row(conn, parsed.workflow_id)
        if current_state is None:
            return _json_error("workflow_not_found", f"workflow {parsed.workflow_id!r} not found")
        if current_state["repo_slug"] != repo_slug:
            return _json_error("scope_mismatch", "request scope does not match the caller's live session worktree")

        request_fields = parsed.to_dict()
        scope_key = cdb.scope_key(repo_slug, parsed.workflow_id)
        actor_type = "agent_session"
        actor_id = session_name
        idem_state = cdb.lookup_idempotency(
            conn,
            actor_type=actor_type,
            actor_id=actor_id,
            scope_key=scope_key,
            operation="create",
            raw_idempotency_key=parsed.idempotency_key,
            request_fields=request_fields,
        )
        if idem_state["kind"] == "conflict":
            return _json_error("idempotency_conflict", "same idempotency key used for a different create request")
        if idem_state["kind"] == "in_flight":
            return _json_error("idempotency_in_flight", "a commit create with this idempotency key is already in flight")
        if idem_state["kind"] == "completed_replay":
            return JSONResponse(idem_state["response_json"])
        if idem_state["kind"] == "failed_retryable":
            return _json_error("idempotency_in_flight", "a previous create with this idempotency key is still retryable")
        if idem_state["kind"] == "failed_terminal":
            return _json_error("invalid_transition", "a previous create with this idempotency key failed terminally")

        idem_record = cdb.reserve_idempotency(
            conn,
            actor_type=actor_type,
            actor_id=actor_id,
            scope_key=scope_key,
            operation="create",
            raw_idempotency_key=parsed.idempotency_key,
            request_fields=request_fields,
            workflow_id=parsed.workflow_id,
        )

        live_drift = _current_drift(_row_value(worktree_row, "worktree_path"))
        if not _drift_matches(parsed.drift_token, live_drift):
            return _json_error(
                "drift_detected",
                "live worktree drift no longer matches the caller's drift token",
                details={"live": live_drift.to_dict(), "requested": parsed.drift_token.to_dict()},
            )

        tree_sha, unsigned_payload, parent_shas = _commit_payload(
            worktree_path=_row_value(worktree_row, "worktree_path"),
            message=proposal_request.message,
            author=proposal_request.author,
            committer=proposal_request.committer,
        )
        unsigned_commit_sha = commit_object_sha(unsigned_payload)

        trusted_object_store_ref: str | None = None
        canonical_payload_hash: str | None = None
        if parsed.create_mode == "snapshot_for_signature":
            snapshot_conn = snapshot_dao._get_conn()
            snapshot_dao.init_schema_on_connection(snapshot_conn)
            store = _trusted_store()
            trusted_object_store_ref = capture_snapshot(
                workflow_id=parsed.workflow_id,
                repo_slug=repo_slug,
                commit_sha=unsigned_commit_sha,
                tree_sha=tree_sha,
                parent_shas=parent_shas,
                git_dir=_row_value(worktree_row, "worktree_path"),
                store=store,
                dao_conn=snapshot_conn,
                snapshot_type="commit_create",
                retention_class="active",
                store_root=str(store.root),
                canonical_payload=unsigned_payload,
            )
            snapshot = snapshot_dao.get_snapshot(snapshot_conn, trusted_object_store_ref)
            if snapshot is None:
                return _json_error("invalid_transition", "snapshot capture did not persist metadata")
            canonical_payload_hash = snapshot["canonical_preview_sha256"]

        canonical_preview = _canonical_preview_payload(
            unsigned_commit_sha=unsigned_commit_sha,
            tree_sha=tree_sha,
            parent_shas=parent_shas,
            message=proposal_request.message,
            author=proposal_request.author,
            committer=proposal_request.committer,
        )
        event_id = uuid.uuid4().hex
        state_payload = _workflow_state_json_payload(
            request_model=parsed,
            proposal_request=proposal_request,
            unsigned_commit_sha=unsigned_commit_sha,
            tree_sha=tree_sha,
            parent_shas=parent_shas,
            canonical_preview=canonical_preview,
            trusted_object_store_ref=trusted_object_store_ref,
            canonical_payload_hash=canonical_payload_hash,
        )
        event_status = "committed" if parsed.create_mode == "unsigned_commit" else "awaiting_signature"
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
                parsed.workflow_id,
                event_status,
                event_status,
                time.time(),
                actor_type,
                actor_id,
                session_name,
                repo_slug,
                _row_value(worktree_row, "branch"),
                json.dumps([unsigned_commit_sha]),
                json.dumps({}),
                proposal_request.content.content_fingerprint,
                None,
                None,
                json.dumps(state_payload, sort_keys=True),
            ),
        )
        cdb._rebuild_projection_for_workflow(conn, parsed.workflow_id)
        current_state = _workflow_state_row(conn, parsed.workflow_id)
        if current_state is None:
            raise RuntimeError("create projection missing after append")
        workflow = _workflow_ref_from_state(current_state, actor_id=actor_id)
        response = CommitCreateResponse(
            workflow=workflow,
            content=proposal_request.content,
            unsigned_commit_sha=unsigned_commit_sha,
            trusted_object_store_ref=trusted_object_store_ref,
            canonical_payload_hash=canonical_payload_hash,
            signing=None,
            next_action=_next_action_for_create(parsed.create_mode),
            events=[event_id],
        )
        cdb.finalize_idempotency(
            conn,
            idempotency_id=str(idem_record["idempotency_id"]),
            status="completed",
            response_json=response.to_dict(),
            event_ids=[event_id],
            side_effect_ref=parsed.workflow_id,
            updated_at=time.time(),
            expires_at=_response_idempotency_expires_at(idem_state),
        )
        conn.commit()
        return JSONResponse(response.to_dict())
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        return _json_error("duplicate_active_workflow", f"create violated workflow uniqueness: {exc}")
    finally:
        conn.close()
        if snapshot_conn is not None:
            snapshot_conn.close()


def _request_signature_preview(state_payload: dict[str, Any]) -> dict[str, Any]:
    preview = state_payload.get("canonical_payload_preview")
    if isinstance(preview, dict) and preview:
        return preview
    return {
        "unsigned_commit_sha": state_payload.get("unsigned_commit_sha"),
        "tree_sha": state_payload.get("tree_sha"),
        "parent_shas": state_payload.get("parent_shas") or [],
    }


async def request_signature(request: Request) -> JSONResponse:
    if not _plugin_enabled():
        return _json_error("route_not_trusted", "commit API plugin is disabled")
    try:
        body = await request.json()
        parsed = CommitRequestSignatureRequest.from_dict(body)
    except Exception as exc:
        return _json_error("invalid_request", f"invalid signature-request payload: {exc}")

    auth_scope = _resolved_workflow_scope(request, workflow_id=parsed.workflow_id)
    if isinstance(auth_scope, JSONResponse):
        return auth_scope
    session_name, _session_row, worktree_row, repo_slug, state_row, state_payload = auth_scope

    conn = cdb._get_conn()
    try:
        cdb.init_schema_on_connection(conn)
        conn.execute("BEGIN IMMEDIATE")
        request_fields = parsed.to_dict()
        scope_key = cdb.scope_key(repo_slug, parsed.workflow_id)
        idem_state = cdb.lookup_idempotency(
            conn,
            actor_type="agent_session",
            actor_id=session_name,
            scope_key=scope_key,
            operation="request_signature",
            raw_idempotency_key=parsed.idempotency_key,
            request_fields=request_fields,
        )
        if idem_state["kind"] == "conflict":
            return _json_error("idempotency_conflict", "same idempotency key used for a different signature request")
        if idem_state["kind"] == "in_flight":
            return _json_error("idempotency_in_flight", "a signature request with this idempotency key is already in flight")
        if idem_state["kind"] == "completed_replay":
            return JSONResponse(idem_state["response_json"])
        if idem_state["kind"] == "failed_retryable":
            return _json_error("idempotency_in_flight", "a previous signature request with this key is still retryable")
        if idem_state["kind"] == "failed_terminal":
            return _json_error("invalid_transition", "a previous signature request with this key failed terminally")

        idem_record = cdb.reserve_idempotency(
            conn,
            actor_type="agent_session",
            actor_id=session_name,
            scope_key=scope_key,
            operation="request_signature",
            raw_idempotency_key=parsed.idempotency_key,
            request_fields=request_fields,
            workflow_id=parsed.workflow_id,
        )

        if state_row["status"] not in {"committed", "awaiting_signature"}:
            return _json_error("invalid_transition", f"workflow {parsed.workflow_id!r} is not ready for signature request")
        unsigned_commit_sha = state_payload.get("unsigned_commit_sha")
        trusted_object_store_ref = state_payload.get("trusted_object_store_ref")
        canonical_payload_hash = state_payload.get("canonical_payload_hash")
        if not trusted_object_store_ref or not canonical_payload_hash:
            return _json_error("invalid_transition", "workflow has no trusted snapshot for signing")

        signing_request_id = uuid.uuid4().hex
        requested_at = time.time()
        conn.execute(
            """
            INSERT INTO commit_signing_requests (
                signing_request_id, workflow_id, repo_slug, status, signing_method,
                trusted_object_store_ref, canonical_payload_hash, device_id,
                encrypted_key_ref, operator_id, requested_at, completed_at,
                signature_ref, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signing_request_id,
                parsed.workflow_id,
                repo_slug,
                "pending",
                parsed.signing_method,
                trusted_object_store_ref,
                canonical_payload_hash,
                None,
                None,
                parsed.requested_operator_id,
                requested_at,
                None,
                None,
                json.dumps(
                    {
                        "requested_operator_id": parsed.requested_operator_id,
                        "signer_policy_version": parsed.signer_policy_version,
                        "canonical_payload_preview": _request_signature_preview(state_payload),
                    },
                    sort_keys=True,
                ),
            ),
        )
        event_id = uuid.uuid4().hex
        event_payload = {
            "signing_request_id": signing_request_id,
            "signing_method": parsed.signing_method,
            "canonical_payload_hash": canonical_payload_hash,
            "trusted_object_store_ref": trusted_object_store_ref,
            "requested_operator_id": parsed.requested_operator_id,
            "signer_policy_version": parsed.signer_policy_version,
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
                parsed.workflow_id,
                "signature_requested",
                "awaiting_signature",
                requested_at,
                "agent_session",
                session_name,
                session_name,
                repo_slug,
                _row_value(worktree_row, "branch"),
                json.dumps([unsigned_commit_sha] if unsigned_commit_sha else []),
                json.dumps({}),
                state_row.get("content_fingerprint"),
                None,
                None,
                json.dumps(event_payload, sort_keys=True),
            ),
        )
        cdb._rebuild_projection_for_workflow(conn, parsed.workflow_id)
        current_state = _workflow_state_row(conn, parsed.workflow_id)
        if current_state is None:
            raise RuntimeError("signature-request projection missing after append")
        workflow = _workflow_ref_from_state(current_state, actor_id=session_name)
        signing = SigningSummary(
            signing_request_id=signing_request_id,
            status="pending",
            signing_method=parsed.signing_method,
            canonical_payload_hash=str(canonical_payload_hash),
            trusted_object_store_ref=str(trusted_object_store_ref),
            expires_at=None,
        )
        response = CommitRequestSignatureResponse(
            workflow=workflow,
            signing=signing,
            canonical_payload_preview=_request_signature_preview(state_payload),
            local_signer_handoff={
                "signing_request_id": signing_request_id,
                "workflow_id": parsed.workflow_id,
                "repo_slug": repo_slug,
                "signing_method": parsed.signing_method,
                "canonical_payload_hash": canonical_payload_hash,
                "trusted_object_store_ref": trusted_object_store_ref,
            },
            next_action=NextAction(
                action="attach_signature",
                required_actor="local_signer",
                reason="signature request prepared for the local signer",
            ),
            events=[event_id],
        )
        cdb.finalize_idempotency(
            conn,
            idempotency_id=str(idem_record["idempotency_id"]),
            status="completed",
            response_json=response.to_dict(),
            event_ids=[event_id],
            side_effect_ref=signing_request_id,
            updated_at=requested_at,
            expires_at=_response_idempotency_expires_at(idem_state),
        )
        conn.commit()
        return JSONResponse(response.to_dict())
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        return _json_error("duplicate_active_workflow", f"signature request violated workflow uniqueness: {exc}")
    finally:
        conn.close()


def _verify_attached_signature(
    *,
    signing_method: str,
    operator_id: str,
    payload: bytes,
    armored_signature: str,
) -> bool:
    if not armored_signature:
        return False
    verification_key = resolve_verification_key(
        operator_id=operator_id,
        signing_kind=signing_method,
        keystore=_verification_key_store(),
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        payload_path = tmp / "payload"
        signature_path = tmp / "signature.asc"
        payload_path.write_bytes(payload)
        signature_path.write_text(armored_signature, encoding="utf-8")
        if signing_method == "ssh":
            allowed_signers = tmp / "allowed_signers"
            allowed_signers.write_text(
                f"{operator_id} {verification_key.material.decode('utf-8').strip()}\n",
                encoding="utf-8",
            )
            proc = subprocess.run(
                [
                    "ssh-keygen",
                    "-Y",
                    "verify",
                    "-f",
                    str(allowed_signers),
                    "-I",
                    operator_id,
                    "-n",
                    "git",
                    "-s",
                    str(signature_path),
                ],
                input=payload,
                capture_output=True,
            )
            return proc.returncode == 0
        if signing_method == "gpg":
            gpg = shutil.which("gpg")
            if gpg is None:
                return False
            gnupg_home = tmp / "gnupg"
            gnupg_home.mkdir()
            env = os.environ.copy()
            env["GNUPGHOME"] = str(gnupg_home)
            import_proc = subprocess.run(
                [gpg, "--batch", "--yes", "--import"],
                input=verification_key.material,
                capture_output=True,
                env=env,
            )
            if import_proc.returncode != 0:
                return False
            verify_proc = subprocess.run(
                [gpg, "--batch", "--yes", "--verify", str(signature_path), str(payload_path)],
                capture_output=True,
                env=env,
            )
            return verify_proc.returncode == 0
        return False


async def attach_signature(request: Request) -> JSONResponse:
    if not _plugin_enabled():
        return _json_error("route_not_trusted", "commit API plugin is disabled")
    try:
        parsed = CommitAttachSignatureRequest.from_dict(await request.json())
    except Exception as exc:
        return _json_error("invalid_request", f"invalid attach-signature request: {exc}")

    conn = cdb._get_conn()
    try:
        cdb.init_schema_on_connection(conn)
        conn.execute("BEGIN IMMEDIATE")
        signing_row = _signing_request_row(conn, parsed.signing_request_id)
        if signing_row is None:
            return _json_error(
                "signing_request_not_found",
                f"signing request {parsed.signing_request_id!r} not found",
            )
        workflow_id = str(signing_row["workflow_id"])
        workflow_state = _workflow_state_row(conn, workflow_id)
        if workflow_state is None:
            return _json_error("workflow_not_found", f"workflow {workflow_id!r} not found")
        auth_scope = _resolved_workflow_scope(request, workflow_id=workflow_id, repo_slug=str(signing_row["repo_slug"]))
        if isinstance(auth_scope, JSONResponse):
            return auth_scope
        session_name, _session_row, worktree_row, repo_slug, state_row, state_payload = auth_scope
        proposal_request = _proposal_request_for_workflow(conn, workflow_id, state_payload)
        if proposal_request is None:
            return _json_error("invalid_transition", "workflow has no proposal request to attach against")

        request_fields = parsed.to_dict()
        scope_key = cdb.scope_key(repo_slug, workflow_id)
        idem_state = cdb.lookup_idempotency(
            conn,
            actor_type="agent_session",
            actor_id=session_name,
            scope_key=scope_key,
            operation="attach_signature",
            raw_idempotency_key=parsed.idempotency_key,
            request_fields=request_fields,
        )
        if idem_state["kind"] == "conflict":
            return _json_error("idempotency_conflict", "same idempotency key used for a different signature attach")
        if idem_state["kind"] == "in_flight":
            return _json_error("idempotency_in_flight", "a signature attach with this idempotency key is already in flight")
        if idem_state["kind"] == "completed_replay":
            return JSONResponse(idem_state["response_json"])
        if idem_state["kind"] == "failed_retryable":
            return _json_error("idempotency_in_flight", "a previous signature attach with this key is still retryable")
        if idem_state["kind"] == "failed_terminal":
            return _json_error("invalid_transition", "a previous signature attach with this key failed terminally")

        idem_record = cdb.reserve_idempotency(
            conn,
            actor_type="agent_session",
            actor_id=session_name,
            scope_key=scope_key,
            operation="attach_signature",
            raw_idempotency_key=parsed.idempotency_key,
            request_fields=request_fields,
            workflow_id=workflow_id,
        )

        if signing_row["status"] not in {"pending", "failed"}:
            replay_payload = _workflow_state_payload(workflow_state)
            response_json = replay_payload.get("attach_signature_response")
            if isinstance(response_json, dict):
                return JSONResponse(response_json)
            return _json_error("invalid_transition", f"signing request {parsed.signing_request_id!r} is not attachable")

        if parsed.local_signer_attestation.canonical_payload_hash != signing_row["canonical_payload_hash"]:
            return _json_error(
                "signature_verification_failed",
                "local signer attestation does not match the trusted payload hash",
            )
        if not parsed.armored_signature:
            return _json_error("signature_verification_failed", "armored signature is required for attach")
        operator_id = str(signing_row.get("operator_id") or "")
        if not operator_id:
            return _json_error("signature_verification_failed", "signature request is missing a registered operator")
        snapshot_ref = str(signing_row["trusted_object_store_ref"])
        store = _trusted_store()
        snapshot_conn = snapshot_dao._get_conn()
        try:
            snapshot_dao.init_schema_on_connection(snapshot_conn)
            if not verify_snapshot(snapshot_ref=snapshot_ref, store=store, dao_conn=snapshot_conn):
                return _json_error("signature_verification_failed", "trusted snapshot failed integrity verification")

            snapshot = snapshot_dao.get_snapshot(snapshot_conn, snapshot_ref)
            if snapshot is None:
                return _json_error("signature_verification_failed", "trusted snapshot metadata is missing")
            try:
                unsigned_payload = store.get(str(snapshot["canonical_preview_sha256"]))
            except (KeyError, ObjectIntegrityError):
                return _json_error("signature_verification_failed", "trusted snapshot preview failed integrity verification")
            try:
                signature_ok = _verify_attached_signature(
                    signing_method=str(signing_row["signing_method"]),
                    operator_id=operator_id,
                    payload=unsigned_payload,
                    armored_signature=parsed.armored_signature,
                )
            except (VerificationKeyNotRegistered, ValueError, UnicodeDecodeError, OSError, subprocess.CalledProcessError):
                signature_ok = False
            if not signature_ok:
                return _json_error(
                    "signature_verification_failed",
                    "armored signature failed verification against the operator's registered key",
                )
            signed_payload, computed_signed_sha = assemble_signed_commit(
                unsigned_payload,
                fold_gpgsig_block(parsed.armored_signature.encode("utf-8")),
            )
            signed_commit_ref = parsed.signed_commit_object_ref or computed_signed_sha
            if parsed.signed_commit_object_ref and parsed.signed_commit_object_ref != computed_signed_sha:
                return _json_error(
                    "signature_verification_failed",
                    "signed commit object ref does not match the assembled commit",
                )

            verification = {
                "trusted_snapshot_verified": True,
                "canonical_payload_hash": signing_row["canonical_payload_hash"],
                "attestation_device_id": parsed.local_signer_attestation.device_id,
                "attestation_request_nonce": parsed.local_signer_attestation.request_nonce,
                "signature_ref": parsed.signature_ref,
                "armored_signature_present": bool(parsed.armored_signature),
                "signed_commit_object_ref": signed_commit_ref,
                "signed_payload_sha": computed_signed_sha,
                "assembled_from_trusted_payload": True,
            }
            event_id = uuid.uuid4().hex
            workflow = _workflow_ref(
                workflow_id=workflow_id,
                repo_slug=repo_slug,
                status="signed",
                latest_event_id=event_id,
                created_by_session_name=state_row.get("session_name"),
                created_by_actor_id=parsed.local_signer_attestation.device_id,
                mutation_owner_session_name=state_row.get("session_name"),
            )
            signing = SigningSummary(
                signing_request_id=parsed.signing_request_id,
                status="signed",
                signing_method=signing_row["signing_method"],
                canonical_payload_hash=signing_row["canonical_payload_hash"],
                trusted_object_store_ref=snapshot_ref,
                expires_at=None,
            )
            response = CommitAttachSignatureResponse(
                workflow=workflow,
                signing=signing,
                signed_commit_sha=computed_signed_sha,
                verification=verification,
                next_action=NextAction(
                    action="publish",
                    required_actor="dashboard",
                    reason="signed commit is ready to publish",
                ),
                events=[event_id],
            )
            event_payload = {
                "signing_request_id": parsed.signing_request_id,
                "verification": verification,
                "signed_commit_sha": computed_signed_sha,
                "attach_signature_response": response.to_dict(),
                "local_signer_attestation": parsed.local_signer_attestation.to_dict(),
                "signed_payload_sha": commit_object_sha(signed_payload),
            }
            conn.execute(
                "UPDATE commit_signing_requests SET status = ?, completed_at = ?, signature_ref = ?, payload_json = ? WHERE signing_request_id = ?",
                (
                    "signed",
                    time.time(),
                    parsed.signature_ref or parsed.signed_commit_object_ref,
                    json.dumps(event_payload, sort_keys=True),
                    parsed.signing_request_id,
                ),
            )
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
                    "signed",
                    "signed",
                    time.time(),
                    "local_signer",
                    parsed.local_signer_attestation.device_id,
                    session_name,
                    repo_slug,
                    _row_value(worktree_row, "branch"),
                    json.dumps([computed_signed_sha]),
                    json.dumps({}),
                    state_row.get("content_fingerprint"),
                    None,
                    None,
                    json.dumps(event_payload, sort_keys=True),
                ),
            )
            cdb._rebuild_projection_for_workflow(conn, workflow_id)
            cdb.finalize_idempotency(
                conn,
                idempotency_id=str(idem_record["idempotency_id"]),
                status="completed",
                response_json=response.to_dict(),
                event_ids=[event_id],
                side_effect_ref=computed_signed_sha,
                updated_at=time.time(),
                expires_at=_response_idempotency_expires_at(idem_state),
            )
            conn.commit()
            return JSONResponse(response.to_dict())
        finally:
            snapshot_conn.close()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        return _json_error("invalid_transition", f"signature attach violated workflow uniqueness: {exc}")
    finally:
        conn.close()


def _approval_expected_old_sha(conn: sqlite3.Connection, workflow_id: str) -> str | None:
    row = conn.execute(
        """
        SELECT payload_json
        FROM commit_workflow_approvals
        WHERE workflow_id = ?
          AND approval_type = 'force_with_lease'
          AND status = 'approved'
        ORDER BY decided_at DESC, requested_at DESC
        LIMIT 1
        """,
        (workflow_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except Exception:
        return None
    constraints = payload.get("constraints") or {}
    if not isinstance(constraints, dict):
        return None
    expected = constraints.get("expected_ref_sha")
    return str(expected) if expected else None


async def publish(request: Request) -> JSONResponse:
    if not _plugin_enabled():
        return _json_error("route_not_trusted", "commit API plugin is disabled")
    try:
        body = await request.json()
        parsed = CommitPublishRequest.from_dict(body)
    except Exception as exc:
        return _json_error("invalid_request", f"invalid publish request: {exc}")

    auth_scope = _resolved_workflow_scope(request, workflow_id=parsed.workflow_id, repo_slug=parsed.ref_update_intent.repo_slug)
    if isinstance(auth_scope, JSONResponse):
        return auth_scope
    session_name, _session_row, worktree_row, repo_slug, state_row, state_payload = auth_scope

    conn = cdb._get_conn()
    try:
        cdb.init_schema_on_connection(conn)
        conn.execute("BEGIN IMMEDIATE")
        request_fields = parsed.to_dict()
        scope_key = cdb.scope_key(repo_slug, parsed.workflow_id)
        idem_state = cdb.lookup_idempotency(
            conn,
            actor_type="agent_session",
            actor_id=session_name,
            scope_key=scope_key,
            operation="publish",
            raw_idempotency_key=parsed.idempotency_key,
            request_fields=request_fields,
        )
        if idem_state["kind"] == "conflict":
            return _json_error("idempotency_conflict", "same idempotency key used for a different publish request")
        if idem_state["kind"] == "in_flight":
            return _json_error("idempotency_in_flight", "a publish with this idempotency key is already in flight")
        if idem_state["kind"] == "completed_replay":
            return JSONResponse(idem_state["response_json"])
        if idem_state["kind"] == "failed_retryable":
            return _json_error("idempotency_in_flight", "a previous publish with this key is still retryable")
        if idem_state["kind"] == "failed_terminal":
            return _json_error("invalid_transition", "a previous publish with this key failed terminally")

        idem_record = cdb.reserve_idempotency(
            conn,
            actor_type="agent_session",
            actor_id=session_name,
            scope_key=scope_key,
            operation="publish",
            raw_idempotency_key=parsed.idempotency_key,
            request_fields=request_fields,
            workflow_id=parsed.workflow_id,
        )

        if state_row["status"] not in {"signed", "awaiting_publish", "published"}:
            return _json_error("invalid_transition", f"workflow {parsed.workflow_id!r} is not ready to publish")

        current_signed_sha = state_payload.get("signed_commit_sha") or state_payload.get("unsigned_commit_sha")
        if not current_signed_sha:
            return _json_error("invalid_transition", "workflow has no signed commit to publish")
        if parsed.ref_update_intent.new_sha != current_signed_sha:
            return _json_error("ref_update_rejected", "publish intent new_sha does not match the workflow's signed commit")

        approval_expected_old_sha = _approval_expected_old_sha(conn, parsed.workflow_id)
        if approval_expected_old_sha is None:
            return _json_error(
                "ref_update_rejected",
                "publish requires an approved force_with_lease constraint",
            )
        parsed_ref_update = parsed.ref_update_intent
        if parsed_ref_update.expected_old_sha and parsed_ref_update.expected_old_sha != approval_expected_old_sha:
            return _json_error(
                "approval_stale_requires_reapproval",
                "caller supplied lease does not match the stored approval lease",
            )
        parsed_ref_update = CommitPublishRefUpdateIntent(
            provider=parsed_ref_update.provider,
            repo_slug=parsed_ref_update.repo_slug,
            ref=parsed_ref_update.ref,
            operation=parsed_ref_update.operation,
            expected_old_sha=approval_expected_old_sha,
            new_sha=parsed_ref_update.new_sha,
        )

        event_id = uuid.uuid4().hex
        skipped = parsed.publish_mode == "local_only_noop"
        event_type = "publish_skipped_by_policy" if skipped else "published"
        status_after = "awaiting_publish" if skipped else "published"
        ref_update_result = {
            "skipped": skipped,
            "publish_mode": parsed.publish_mode,
            "ref": parsed_ref_update.ref,
            "operation": parsed_ref_update.operation,
            "expected_old_sha": parsed_ref_update.expected_old_sha,
            "new_sha": parsed_ref_update.new_sha,
            "repo_slug": parsed_ref_update.repo_slug,
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
                parsed.workflow_id,
                event_type,
                status_after,
                time.time(),
                "agent_session",
                session_name,
                session_name,
                repo_slug,
                _row_value(worktree_row, "branch"),
                json.dumps([parsed_ref_update.new_sha]),
                json.dumps({}),
                state_row.get("content_fingerprint"),
                parsed_ref_update.provider,
                None,
                json.dumps(
                    {
                        "ref_update_intent": parsed_ref_update.to_dict(),
                        "publish_mode": parsed.publish_mode,
                        "ref_update_result": ref_update_result,
                    },
                    sort_keys=True,
                ),
            ),
        )
        cdb._rebuild_projection_for_workflow(conn, parsed.workflow_id)
        current_state = _workflow_state_row(conn, parsed.workflow_id)
        if current_state is None:
            raise RuntimeError("publish projection missing after append")
        workflow = _workflow_ref_from_state(current_state, actor_id=session_name)
        response = CommitPublishResponse(
            workflow=workflow,
            ref_update_result=ref_update_result,
            provider_url=(
                f"https://github.com/{parsed_ref_update.repo_slug}"
                if parsed_ref_update.provider == "github"
                else None
            ),
            pushed_ref=parsed_ref_update.ref if not skipped else None,
            next_action=NextAction(
                action="none",
                required_actor="dashboard",
                reason="workflow published" if not skipped else "publish skipped by policy",
            ),
            events=[event_id],
        )
        cdb.finalize_idempotency(
            conn,
            idempotency_id=str(idem_record["idempotency_id"]),
            status="completed",
            response_json=response.to_dict(),
            event_ids=[event_id],
            side_effect_ref=parsed_ref_update.ref,
            updated_at=time.time(),
            expires_at=_response_idempotency_expires_at(idem_state),
        )
        conn.commit()
        return JSONResponse(response.to_dict())
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        return _json_error("ref_update_rejected", f"publish violated workflow uniqueness: {exc}")
    finally:
        conn.close()


routes: list[Route] = [
    Route("/api/capabilities/commit/v1/resolve-policy", resolve_policy, methods=["POST"]),
    Route("/api/capabilities/commit/v1/policy/describe", describe_policy, methods=["GET"]),
    Route("/api/capabilities/commit/v1/proposals", propose, methods=["POST"]),
    Route("/api/capabilities/commit/v1/workflows/{workflow_id}/commit", create, methods=["POST"]),
    Route("/api/capabilities/commit/v1/workflows/{workflow_id}/signature-request", request_signature, methods=["POST"]),
    Route("/api/capabilities/commit/v1/signing-requests/{signing_request_id}/attach", attach_signature, methods=["POST"]),
    Route("/api/capabilities/commit/v1/workflows/{workflow_id}/publish", publish, methods=["POST"]),
]
