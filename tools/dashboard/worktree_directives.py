"""Worktree directive schemas — operator/agent coordination channels for
the worktree review UI.

Sibling channels for the dashboard's worktree review flow:

* :class:`RebaseDirectiveV1` — operator-written inbound request asking a
  live session to rebase its worktree. The directive renders the
  concrete message server-side from live worktree metadata, then delivers
  it over CrossTalk.
* :class:`WorktreeRebaseStatusV1` — agent-written outbound progress for an
  in-flight rebase. The dashboard subscribes via ``Schema.alpine.onChange``
  and reflects each state transition into the per-row Request Rebase /
  Merge button. Read-only from the dashboard's perspective: the mediator
  has no action — the agent is the sole writer.

The corresponding inbound dashboard→agent nudge (``RebaseDirectiveV1``)
lands in a sibling bead; both schemas live in this module so the round-trip
is co-located.

Key shape: ``"<session_name>/<repo_name>"`` — mirrors the dashboard's
``rowKey(row)`` so the agent and the JS resolver address the same row
without a translation step.
"""

from __future__ import annotations

import asyncio
from typing import Any

from agents.workspace_manager import WorkspaceError, get_session_worktree_rebase_info
from tools.dashboard.crosstalk_directive import CrosstalkDirective
from tools.graph import settings_ops
from tools.graph.schemas.registry import (
    SchemaValidationError,
    SettingSchema,
    action,
    field,
    keyed_per_entity,
)


WORKTREE_NAMESPACE = "dashboard.session.worktree"
WORKTREE_REBASE_STATUS_REVISION = 1
WORKTREE_REBASE_DIRECTIVE_REVISION = 1


VALID_REBASE_STATES = ("in_progress", "done", "failed")


SYNOPSIS = {
    "summary": (
        "Inbound rebase requests and outbound rebase progress for the "
        "worktree review UI. The operator writes a Crosstalk directive "
        "scoped to one (session, repo) row; the agent writes progress "
        "back keyed as '<session>/<repo>' so the dashboard can drive the "
        "Request Rebase / Merge button state machine."
    ),
    "nouns": [
        "rebase directive", "rebase status", "worktree rebase",
        "rebase progress", "agent status",
    ],
    "related_set_ids": [],
}


def _rebase_status_key(session_name: str, repo_name: str) -> str:
    return f"{session_name}/{repo_name}"


def render_rebase_prompt(info: dict[str, Any]) -> str:
    """Render the exact operator-facing rebase prompt from structured info."""
    target_branch = str(info["target_branch"])
    commits_behind = int(info["commits_behind"])
    fork_sha = str(info["fork_sha"])[:7]
    noun = "commit" if commits_behind == 1 else "commits"
    message = (
        "Rebase required before your commit can be merged via the dashboard.\n"
        f"{target_branch} has advanced {commits_behind} {noun} beyond your fork point ({fork_sha}).\n\n"
        "Run in your worktree:\n"
        f"git rebase {target_branch}\n"
    )
    if info.get("is_dirty"):
        message += "\nIf you have uncommitted changes, stash or commit them before rebasing.\n"
    message += "\nThen refresh the Worktrees page — the updated commit will be ff-eligible."
    return message


class WorktreeDirective(CrosstalkDirective):
    """Abstract CrosstalkDirective scoped to one Worktrees row."""

    set_id_suffix = "worktree"
    repo: str = field(
        required=True,
        description=(
            "Repository name for the target worktree row. Combined with "
            "target_session to address one dashboard worktree row."
        ),
    )


class RebaseDirectiveV1(WorktreeDirective):
    """Operator-issued request asking a session to rebase one worktree."""

    set_id_suffix = "rebase"
    schema_revision = WORKTREE_REBASE_DIRECTIVE_REVISION
    body: str = field(
        default="",
        description=(
            "Unused input field. The directive renders the concrete "
            "message server-side from live worktree metadata."
        ),
    )

    @action
    async def deliver(row, svc):
        session_name = row["target_session"]
        repo_name = row["repo"]
        try:
            info = await asyncio.to_thread(
                get_session_worktree_rebase_info,
                session_name,
                repo_name,
                sync_managed_clone_target=True,
            )
            if not info.get("session_live"):
                raise WorkspaceError(f"session is not live: {session_name}")
            await svc.session_send(session_name, render_rebase_prompt(info))
        except WorkspaceError as exc:
            settings_ops.upsert_by_key(
                WORKTREE_REBASE_STATUS_SET_ID,
                WORKTREE_REBASE_STATUS_REVISION,
                _rebase_status_key(session_name, repo_name),
                {"state": "failed", "error": str(exc)},
                org=row.org or "autonomy",
            )
            svc.log.warning(
                "rebase directive delivery failed for %s/%s: %s",
                session_name,
                repo_name,
                exc,
            )


class WorktreeStatusSchema(SettingSchema):
    """Abstract namespace root for dashboard.session.worktree.* status rows."""

    set_id = WORKTREE_NAMESPACE


@keyed_per_entity(key_strategy="session_name/repo_name")
class WorktreeRebaseStatusV1(WorktreeStatusSchema):
    """Agent-written rebase progress, watched by the worktree review UI.

    Key: ``<session_name>/<repo_name>`` — mirrors the dashboard's
    ``rowKey(row)``. The substrate auto-stamps ``updated_at`` and
    ``revision_seq``; the payload carries semantics only.

    Read-only from the dashboard's perspective: the mediator does NOT
    expose a write action — the agent is the sole writer, the dashboard
    only reads + subscribes.
    """

    set_id_suffix = "rebase_status"
    schema_revision = WORKTREE_REBASE_STATUS_REVISION

    state: str = field(
        default="",
        enum=list(VALID_REBASE_STATES),
        description=(
            "Current rebase state: 'in_progress' on ack, 'done' on "
            "successful rebase, 'failed' on conflict."
        ),
    )
    error: str = field(
        default="",
        description=(
            "Short human-readable reason when state == 'failed'; empty "
            "string otherwise."
        ),
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise SchemaValidationError(
                f"{cls.__name__}: payload must be a dict, "
                f"got {type(payload).__name__}"
            )
        extra = set(payload) - set(cls._field_metadata)
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown field(s): {sorted(extra)}"
            )
        state = payload.get("state", "")
        if not isinstance(state, str):
            raise SchemaValidationError(
                f"{cls.__name__}: 'state' must be a string, "
                f"got {type(state).__name__}"
            )
        if state and state not in VALID_REBASE_STATES:
            raise SchemaValidationError(
                f"{cls.__name__}: 'state' must be one of "
                f"{VALID_REBASE_STATES} (or empty), got {state!r}"
            )
        error = payload.get("error", "")
        if not isinstance(error, str):
            raise SchemaValidationError(
                f"{cls.__name__}: 'error' must be a string, "
                f"got {type(error).__name__}"
            )


WORKTREE_REBASE_STATUS_SET_ID = WorktreeRebaseStatusV1.set_id
