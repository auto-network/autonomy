"""Schema: ``dashboard.worktree.terminal_fire#1``.

Persisted dedup ledger for Worktrees ``nag_done`` terminal
notifications, keyed by
``<session_name>:<repo_name>:<review_id>:<head_sha>``. One row means
"this exact PR head already fired its terminal CrossTalk". The row is
deleted when the operator re-arms ``nag_done`` or clears the row back
to ``silent``.
"""

from __future__ import annotations

from typing import Any

from .registry import (
    SchemaValidationError,
    SettingSchema,
    field,
    keyed_per_entity,
)


SET_ID = "dashboard.worktree.terminal_fire"
SCHEMA_REVISION = 1

SYNOPSIS = {
    "summary": (
        "Persisted Worktrees nag_done dedup ledger. One row per "
        "(session_name, repo_name, review_id, head_sha) terminal "
        "notification that already fired."
    ),
    "nouns": [
        "terminal fire",
        "nag_done dedup",
        "review head",
        "terminal notification",
    ],
    "related_set_ids": [
        "dashboard.worktree.watch#1",
        "autonomy.source_control.review_state#1",
    ],
}


@keyed_per_entity
class WorktreeTerminalFireV1(SettingSchema):
    """Persisted dedup ledger entry for one terminal notification."""

    set_id = SET_ID
    schema_revision = SCHEMA_REVISION

    fired_at: float = field(
        required=True,
        description="Wall-clock Unix timestamp when the terminal CrossTalk fired",
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
        if "fired_at" not in payload:
            raise SchemaValidationError(
                f"{cls.__name__}: missing required field 'fired_at'"
            )
        fired_at = payload["fired_at"]
        if not isinstance(fired_at, (int, float)) or float(fired_at) <= 0.0:
            raise SchemaValidationError(
                f"{cls.__name__}: 'fired_at' must be a positive number"
            )

