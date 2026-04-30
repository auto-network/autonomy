"""Setting schemas owned by the settings-mediator substrate.

Two schemas track per-set loop state:

* ``dashboard.action-registry-cursor#1`` — keyed ``<set_id>``, one row per
  registered set. Payload ``{lastRowId, lastSeenAt}`` records the last
  Setting row the loop has advanced past, so a process restart resumes
  in order rather than re-firing every prior row.
* ``dashboard.action-registry-state#1`` — keyed
  ``<set_id>:<row_id>:<action_name>``, one row per (row, handler)
  dispatch attempt. Payload ``{processedAt, status, error?}``. The
  presence of a marker row is the idempotency guarantee: re-polls and
  multi-handler-per-set fan-out both consult these markers before
  invoking a handler.

Both Settings live in the calling org's DB. Cursors and markers are
per-process state, not cross-org content — peers never need to read
them. The schemas are registered for completeness (so ``graph set
schema`` surfaces them) and for type-checked writes through the
settings_ops layer.
"""
from __future__ import annotations

from typing import Any

from tools.graph.schemas.registry import (
    SchemaValidationError,
    SettingSchema,
    register_schema,
)


CURSOR_SET_ID = "dashboard.action-registry-cursor"
CURSOR_REVISION = 1

STATE_SET_ID = "dashboard.action-registry-state"
STATE_REVISION = 1


# Single shared synopsis covering both schemas. The schema-meta flush
# walks the registry per ``set_id`` and uses the module-level SYNOPSIS
# constant, so both ``...registry-cursor#1`` and ``...registry-state#1``
# end up surfaced under the same nouns. ``graph set find action
# registry`` returns both schemas — this is the desired behavior, since
# they are two halves of the same substrate.
SYNOPSIS = {
    "summary": (
        "Settings-mediator substrate state — per-set cursor "
        "(action-registry-cursor) and per-(row, handler) idempotency "
        "markers (action-registry-state) for the dashboard's action "
        "dispatch loop"
    ),
    "nouns": [
        "action registry", "settings mediator", "action loop",
        "action loop cursor", "action marker", "idempotency marker",
        "dispatch cursor", "dispatch marker", "register_action",
    ],
    "related_set_ids": [],
}


_CURSOR_FIELDS = {"lastRowId", "lastSeenAt"}


class ActionRegistryCursorV1(SettingSchema):
    """``{lastRowId, lastSeenAt}`` — settings-mediator per-set cursor."""

    set_id = CURSOR_SET_ID
    schema_revision = CURSOR_REVISION

    _field_metadata: dict[str, dict] = {
        "lastRowId": {
            "type": "string",
            "required": True,
            "description": (
                "Setting id of the last row the loop has advanced past "
                "for this set"
            ),
        },
        "lastSeenAt": {
            "type": "string",
            "required": True,
            "description": (
                "ISO-8601 created_at of the last row the loop has "
                "advanced past — paired with lastRowId for stable "
                "ordering across rows with identical timestamps"
            ),
        },
    }

    @classmethod
    def validate(cls, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise SchemaValidationError(
                f"{cls.__name__}: payload must be a dict, "
                f"got {type(payload).__name__}"
            )
        for required in ("lastRowId", "lastSeenAt"):
            if required not in payload:
                raise SchemaValidationError(
                    f"{cls.__name__}: missing required field {required!r}"
                )
            if not isinstance(payload[required], str):
                raise SchemaValidationError(
                    f"{cls.__name__}: {required!r} must be a string"
                )
        extra = set(payload) - _CURSOR_FIELDS
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown field(s): {sorted(extra)}"
            )


_STATE_FIELDS = {"processedAt", "status", "error"}
_STATE_VALID_STATUSES = ("ok", "failed", "filtered")


class ActionRegistryStateV1(SettingSchema):
    """``{processedAt, status, error?}`` — per-(row, handler) marker."""

    set_id = STATE_SET_ID
    schema_revision = STATE_REVISION

    _field_metadata: dict[str, dict] = {
        "processedAt": {
            "type": "string",
            "required": True,
            "description": "ISO-8601 timestamp the handler completed (or raised)",
        },
        "status": {
            "type": "string",
            "required": True,
            "description": (
                "Outcome of the handler call: 'ok', 'failed', or "
                "'filtered' (predicate rejected the row)"
            ),
            "enum": list(_STATE_VALID_STATUSES),
        },
        "error": {
            "type": "string",
            "description": (
                "Repr of the exception when status == 'failed'; absent "
                "otherwise"
            ),
        },
    }

    @classmethod
    def validate(cls, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise SchemaValidationError(
                f"{cls.__name__}: payload must be a dict, "
                f"got {type(payload).__name__}"
            )
        for required in ("processedAt", "status"):
            if required not in payload:
                raise SchemaValidationError(
                    f"{cls.__name__}: missing required field {required!r}"
                )
            if not isinstance(payload[required], str):
                raise SchemaValidationError(
                    f"{cls.__name__}: {required!r} must be a string"
                )
        if payload["status"] not in _STATE_VALID_STATUSES:
            raise SchemaValidationError(
                f"{cls.__name__}: status must be one of "
                f"{_STATE_VALID_STATUSES}, got {payload['status']!r}"
            )
        if "error" in payload and payload["error"] is not None \
                and not isinstance(payload["error"], str):
            raise SchemaValidationError(
                f"{cls.__name__}: 'error' must be a string or null"
            )
        extra = set(payload) - _STATE_FIELDS
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown field(s): {sorted(extra)}"
            )


register_schema(CURSOR_SET_ID, CURSOR_REVISION, ActionRegistryCursorV1)
register_schema(STATE_SET_ID, STATE_REVISION, ActionRegistryStateV1)
