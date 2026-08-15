"""Schema: ``dashboard.worktree.watch#1``.

Persisted watch / nag mode for a single Worktrees dashboard row, keyed
by ``<session_name>:<repo_name>``. The payload stores wall-clock expiry
so a fresh dashboard process can reconstruct the in-memory timers after
restart. ``armed_at`` is only present for ``nag_done`` rows so the
smart-cadence polling schedule survives restart as well.

Lifetime: deleted when the row returns to ``silent``.
"""

from __future__ import annotations

from typing import Any

from .registry import (
    SchemaValidationError,
    SettingSchema,
    field,
    keyed_per_entity,
)


SET_ID = "dashboard.worktree.watch"
SCHEMA_REVISION = 1

VALID_MODES = ("silent", "nag_all", "nag_done")

SYNOPSIS = {
    "summary": (
        "Persisted Worktrees watch / nag mode. One row per "
        "(session_name, repo_name); stores expiry and optional armed-at "
        "timestamps so restart can reconstruct polling state."
    ),
    "nouns": [
        "worktree watch",
        "nag mode",
        "watch expiry",
        "armed at",
    ],
    "related_set_ids": [
        "autonomy.worktree.review_binding#1",
        "autonomy.source_control.review_state#1",
    ],
}


@keyed_per_entity(key_strategy="session_name:repo")
class WorktreeWatchV1(SettingSchema):
    """Persisted watch configuration for one Worktrees row."""

    set_id = SET_ID
    schema_revision = SCHEMA_REVISION

    mode: str = field(
        required=True,
        enum=list(VALID_MODES),
        description="Live watch mode for the row",
    )
    expires_at: float = field(
        required=True,
        description="Wall-clock Unix timestamp when the watch auto-expires",
    )
    armed_at: float = field(
        required=False,
        description=(
            "Wall-clock Unix timestamp when nag_done was armed; absent for "
            "other modes."
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
        if "mode" not in payload:
            raise SchemaValidationError(
                f"{cls.__name__}: missing required field 'mode'"
            )
        mode = payload["mode"]
        if mode not in VALID_MODES:
            raise SchemaValidationError(
                f"{cls.__name__}: 'mode' must be one of {VALID_MODES}, got {mode!r}"
            )
        if "expires_at" not in payload:
            raise SchemaValidationError(
                f"{cls.__name__}: missing required field 'expires_at'"
            )
        expires_at = payload["expires_at"]
        if not isinstance(expires_at, (int, float)) or float(expires_at) <= 0.0:
            raise SchemaValidationError(
                f"{cls.__name__}: 'expires_at' must be a positive number"
            )
        armed_at = payload.get("armed_at")
        if armed_at is not None:
            if not isinstance(armed_at, (int, float)) or float(armed_at) <= 0.0:
                raise SchemaValidationError(
                    f"{cls.__name__}: 'armed_at' must be a positive number"
                )
            if mode != "nag_done":
                raise SchemaValidationError(
                    f"{cls.__name__}: 'armed_at' is only valid for 'nag_done'"
                )

