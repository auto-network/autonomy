"""Setting schemas owned by the coordinator-board plugin.

Two Settings drive the live data flow:

* ``dashboard.coordinator-canvas`` — the coordinator's primary output: a
  single perfectly-framed question with just enough context to be
  answerable, plus optional pre-canned quick-reply pills.
* ``dashboard.operator-message-to-coordinator`` — the operator's most
  recent message back; latest-write-wins (the UI surfaces only the
  newest member).

Both Settings are keyed per-coordinator-session so multiple coordinators
can publish without trampling each other. v1 reads the most recent
member regardless of key — last writer wins, as per the bead's
"multi-coordinator out of scope" line.
"""
from __future__ import annotations

from typing import Any

from tools.graph.schemas.registry import (
    SchemaValidationError,
    SettingSchema,
    register_schema,
)


COORDINATOR_CANVAS_SET_ID = "dashboard.coordinator-canvas"
OPERATOR_MESSAGE_SET_ID = "dashboard.operator-message-to-coordinator"
SCHEMA_REVISION = 1


class CoordinatorCanvasV1(SettingSchema):
    """``{ageMin, question, context, quickReplies[]}`` — coordinator's banger."""

    set_id = COORDINATOR_CANVAS_SET_ID
    schema_revision = SCHEMA_REVISION

    @classmethod
    def validate(cls, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise SchemaValidationError(
                f"{cls.__name__}: payload must be a dict, "
                f"got {type(payload).__name__}"
            )
        for required in ("question",):
            if required not in payload:
                raise SchemaValidationError(
                    f"{cls.__name__}: missing required field {required!r}"
                )
        if not isinstance(payload["question"], str) or not payload["question"].strip():
            raise SchemaValidationError(
                f"{cls.__name__}: 'question' must be a non-empty string"
            )
        if "ageMin" in payload and not isinstance(payload["ageMin"], (int, float)):
            raise SchemaValidationError(
                f"{cls.__name__}: 'ageMin' must be a number"
            )
        if "context" in payload and payload["context"] is not None \
                and not isinstance(payload["context"], str):
            raise SchemaValidationError(
                f"{cls.__name__}: 'context' must be a string or null"
            )
        replies = payload.get("quickReplies")
        if replies is not None:
            if not isinstance(replies, list):
                raise SchemaValidationError(
                    f"{cls.__name__}: 'quickReplies' must be a list"
                )
            for r in replies:
                if not isinstance(r, str):
                    raise SchemaValidationError(
                        f"{cls.__name__}: quickReplies entries must be strings"
                    )
        extra = set(payload) - {"ageMin", "question", "context", "quickReplies"}
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown field(s): {sorted(extra)}"
            )


class OperatorMessageToCoordinatorV1(SettingSchema):
    """``{text, sentAt}`` — operator's latest message back to coordinator."""

    set_id = OPERATOR_MESSAGE_SET_ID
    schema_revision = SCHEMA_REVISION

    @classmethod
    def validate(cls, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise SchemaValidationError(
                f"{cls.__name__}: payload must be a dict, "
                f"got {type(payload).__name__}"
            )
        if "text" not in payload:
            raise SchemaValidationError(
                f"{cls.__name__}: missing required field 'text'"
            )
        if not isinstance(payload["text"], str):
            raise SchemaValidationError(
                f"{cls.__name__}: 'text' must be a string"
            )
        if "sentAt" in payload and payload["sentAt"] is not None \
                and not isinstance(payload["sentAt"], str):
            raise SchemaValidationError(
                f"{cls.__name__}: 'sentAt' must be an ISO-8601 string or null"
            )
        extra = set(payload) - {"text", "sentAt"}
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown field(s): {sorted(extra)}"
            )


register_schema(COORDINATOR_CANVAS_SET_ID, SCHEMA_REVISION, CoordinatorCanvasV1)
register_schema(OPERATOR_MESSAGE_SET_ID, SCHEMA_REVISION, OperatorMessageToCoordinatorV1)
