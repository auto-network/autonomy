"""Worktree directive schemas — operator/agent coordination channels for
the worktree review UI.

Sibling channels for the dashboard's worktree review flow:

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

from typing import Any

from tools.graph.schemas.registry import (
    SchemaValidationError,
    SettingSchema,
    field,
    keyed_per_entity,
)


WORKTREE_REBASE_STATUS_SET_ID = "dashboard.session.worktree.rebase_status"
WORKTREE_REBASE_STATUS_REVISION = 1


VALID_REBASE_STATES = ("in_progress", "done", "failed")


SYNOPSIS = {
    "summary": (
        "Agent-written rebase progress for the worktree review UI. "
        "One row per (session, repo) keyed as '<session>/<repo>'; the "
        "dashboard subscribes via Schema.onChange and reflects state "
        "transitions into the Request Rebase / Merge button."
    ),
    "nouns": [
        "rebase status", "worktree rebase", "rebase progress",
        "agent status",
    ],
    "related_set_ids": [],
}


@keyed_per_entity
class WorktreeRebaseStatusV1(SettingSchema):
    """Agent-written rebase progress, watched by the worktree review UI.

    Key: ``<session_name>/<repo_name>`` — mirrors the dashboard's
    ``rowKey(row)``. The substrate auto-stamps ``updated_at`` and
    ``revision_seq``; the payload carries semantics only.

    Read-only from the dashboard's perspective: the mediator does NOT
    expose a write action — the agent is the sole writer, the dashboard
    only reads + subscribes.
    """

    set_id = WORKTREE_REBASE_STATUS_SET_ID
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
