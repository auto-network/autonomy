"""``graph commit policy`` commands."""

from __future__ import annotations

import os
import sys
from typing import Any

from . import ops
from .client import get_client
from .commit_policy import (
    AUTONOMY_PROFILE,
    WorkspaceCapabilityContext,
    describe_commit_policy,
    describe_commit_policy_json,
    resolve_commit_policy_from_members,
)
from .schemas.commit_policy import (
    COMMIT_POLICY_REVISION,
    COMMIT_POLICY_SET_ID,
)


def _capability_context_for_workspace(
    workspace_id: str | None, org: str | None
) -> WorkspaceCapabilityContext | None:
    """Best-effort real capability context for a describe call.

    Falls through to ``None`` (all-False context) if the workspace or its
    capabilities can't be resolved - a policy that doesn't actually require
    issue linkage still describes cleanly either way.
    """
    if not workspace_id:
        return None
    try:
        from agents.workspace_settings import resolve_capabilities
        caps = resolve_capabilities(workspace_id, org=org)
    except Exception:
        return None
    return WorkspaceCapabilityContext(
        issue_tracker_enabled=any(cap.contract == "issue_tracker" for cap in caps),
    )


def _describe_org(workspace_id: str | None, explicit_org: str | None) -> str | None:
    """Resolve which org DB a describe call should read from.

    An explicit ``--org`` always wins. Otherwise, when a workspace id is
    given, follow that workspace's real owning org (``graph_project``) -
    a ``workspace:<id>`` policy row lives in whichever org DB registered
    the workspace, which is not necessarily the caller's own org (e.g. the
    Anchore workspaces live in the ``anchore`` org DB). Falling back to the
    caller's org here is exactly what silently resolves to
    ``built-in:safe.default`` for a workspace registered in a different
    org. Falls back to the caller org when the workspace can't be found
    (e.g. it doesn't exist yet), or when no workspace id was given at all.
    """
    if explicit_org:
        return explicit_org
    if workspace_id:
        try:
            from agents.workspace_settings import get_workspace
            return get_workspace(workspace_id).graph_project
        except KeyError:
            pass
    return ops.CALLER_ORG


def _display_org(explicit_org: str | None) -> str:
    return explicit_org or "personal"


def cmd_commit_policy_describe(args: Any) -> None:
    org = _describe_org(args.workspace, getattr(args, "org", None))
    client = get_client()
    members = client.read_set(
        COMMIT_POLICY_SET_ID,
        org=org,
        target_revision=COMMIT_POLICY_REVISION,
    ).to_dict()
    resolved = resolve_commit_policy_from_members(
        members=members,
        workspace_id=args.workspace,
        repo_slug=args.repo,
        org=org,
        context=_capability_context_for_workspace(args.workspace, org),
    )
    if args.workspace and f"workspace:{args.workspace}" not in members:
        print(
            f"Error: no workspace {args.workspace!r} found in organization {_display_org(getattr(args, 'org', None))!r}",
            file=sys.stderr,
        )
        sys.exit(1)
    if args.json:
        sys.stdout.write(describe_commit_policy_json(resolved) + "\n")
    else:
        sys.stdout.write(describe_commit_policy(resolved))


def cmd_commit_policy_seed(args: Any) -> None:
    client = get_client()
    org = getattr(args, "org", None) or ops.CALLER_ORG
    members = client.read_set(
        COMMIT_POLICY_SET_ID,
        org=org,
        target_revision=COMMIT_POLICY_REVISION,
    ).to_dict()
    key = f"workspace:{args.workspace}"
    if key in members:
        action = "exists"
    else:
        client.add_setting(
            COMMIT_POLICY_SET_ID,
            COMMIT_POLICY_REVISION,
            key,
            {
                "workspace_id": args.workspace,
                "applies_to": "workspace",
                "profile": args.profile,
                "override_mode": "none",
            },
            org=org,
            state="canonical",
        )
        action = "inserted"
    print(
        f"{action}: autonomy.commit.policy#1 workspace:{args.workspace} "
        f"profile={args.profile}"
    )


def attach_commit_subparser(sub) -> None:
    p_commit = sub.add_parser(
        "commit",
        help="Commit workflow policy tools",
    )
    commit_sub = p_commit.add_subparsers(dest="commit_cmd")
    commit_sub.required = True

    p_policy = commit_sub.add_parser(
        "policy",
        help="Inspect or repair commit workflow policy Settings",
    )
    policy_sub = p_policy.add_subparsers(dest="commit_policy_cmd")
    policy_sub.required = True

    p_describe = policy_sub.add_parser(
        "describe",
        help="Describe the commit workflow policy for a workspace/repo",
    )
    p_describe.add_argument(
        "--workspace",
        default=None,
        help="Workspace id. When set, it must exist in the selected org.",
    )
    p_describe.add_argument(
        "--repo",
        default=None,
        help="Canonical repo slug for repo-scoped policy lookup.",
    )
    p_describe.add_argument(
        "--org",
        default=None,
        help="Read policy from this org DB. Defaults to caller org.",
    )
    p_describe.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON including the text projection.",
    )
    p_describe.set_defaults(func=cmd_commit_policy_describe)

    p_seed = policy_sub.add_parser(
        "seed",
        help="Repair/testing helper: ensure a workspace commit policy row exists",
    )
    p_seed.add_argument("--workspace", required=True, help="Workspace id")
    p_seed.add_argument("--org", default=None, help="Org DB to write")
    p_seed.add_argument(
        "--profile",
        default=AUTONOMY_PROFILE,
        help=f"Profile to seed (default: {AUTONOMY_PROFILE})",
    )
    p_seed.set_defaults(func=cmd_commit_policy_seed)
