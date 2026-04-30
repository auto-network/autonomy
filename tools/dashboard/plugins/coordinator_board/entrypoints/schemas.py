"""Setting schemas owned by the coordinator-board plugin.

Five Settings drive the live data flow:

* ``dashboard.coordinator-canvas`` — the coordinator's primary output: a
  single perfectly-framed question with just enough context to be
  answerable, plus optional pre-canned quick-reply pills.
* ``dashboard.operator-message-to-coordinator`` — the operator's most
  recent message back; latest-write-wins (the UI surfaces only the
  newest member).
* ``dashboard.coordinator-tile`` — per-(coordinator, tile) editorial
  card published by the coordinator session. Keyed
  ``<coord-session>:<tile-session>``.
* ``dashboard.coordinator-thread`` — per-(coordinator, thread)
  editorial thread on the Tracking tab. Keyed
  ``<coord-session>:<thread-session>``.
* ``dashboard.coordinator-decision`` — append-only event log of
  operator taps on a coordinator tile (thumb yes/no, choice, custom
  reply, sitrep request, refresh request). Keyed by uuid.

Read paths flow through the standard ``/api/graph/settings/...``
endpoints; write paths POST to ``/api/graph/setting``. No plugin-side
facade — the coordinator board page is a thin client over the
Settings substrate (bead auto-lffg5 retired the v1 ``api.py`` facade).
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
COORDINATOR_TILE_SET_ID = "dashboard.coordinator-tile"
COORDINATOR_THREAD_SET_ID = "dashboard.coordinator-thread"
COORDINATOR_DECISION_SET_ID = "dashboard.coordinator-decision"
SCHEMA_REVISION = 1


VALID_TILE_ASKS = ("yes_no", "decide", "merge", "approve", "fyi")
VALID_TILE_UPDATE_KINDS = ("refresh", "discovery")
VALID_THREAD_STATUSES = (
    "shipping", "blocked", "designing",
    "researching", "investigating", "paused",
)
VALID_DECISION_KINDS = (
    "thumb_yes", "thumb_no", "choice", "custom",
    "sitrep_request", "refresh_request",
)


SYNOPSIS = {
    "summary": (
        "Coordinator board Settings: canvas (the banger), operator "
        "message back, tile + thread editorial cards, append-only "
        "decision log driving session_send via the action substrate"
    ),
    "nouns": [
        "coordinator board", "coordinator canvas", "operator message",
        "coordinator tile", "coordinator thread", "coordinator decision",
        "thumb yes", "thumb no", "sitrep", "tile refresh",
    ],
    "related_set_ids": [],
}


# ── Canvas ───────────────────────────────────────────────────────────


class CoordinatorCanvasV1(SettingSchema):
    """``{ageMin, question, context, quickReplies[]}`` — coordinator's banger."""

    set_id = COORDINATOR_CANVAS_SET_ID
    schema_revision = SCHEMA_REVISION

    _field_metadata: dict[str, dict] = {
        "question": {
            "type": "string",
            "required": True,
            "description": (
                "The one well-framed question the coordinator wants the "
                "operator to answer right now"
            ),
        },
        "context": {
            "type": "string",
            "description": (
                "Just-enough context to make the question answerable; "
                "supports inline ``[label](href)`` markdown links"
            ),
        },
        "ageMin": {
            "type": "integer",
            "description": "Age in minutes since the question was published",
        },
        "quickReplies": {
            "type": "array",
            "description": (
                "Pre-canned reply suggestions; tapping a pill populates "
                "the operator composer verbatim"
            ),
            "element": {"type": "string"},
        },
    }

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


# ── Operator message ─────────────────────────────────────────────────


class OperatorMessageToCoordinatorV1(SettingSchema):
    """``{text, sentAt}`` — operator's latest message back to coordinator."""

    set_id = OPERATOR_MESSAGE_SET_ID
    schema_revision = SCHEMA_REVISION

    _field_metadata: dict[str, dict] = {
        "text": {
            "type": "string",
            "required": True,
            "description": "Operator's reply body, sent verbatim to the coordinator session",
        },
        "sentAt": {
            "type": "string",
            "description": "ISO-8601 send timestamp",
        },
    }

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


# ── Tile ─────────────────────────────────────────────────────────────


class CoordinatorTileV1(SettingSchema):
    """Per-(coordinator, tile) card. Key: ``<coord>:<tile-session>``."""

    set_id = COORDINATOR_TILE_SET_ID
    schema_revision = SCHEMA_REVISION

    _field_metadata: dict[str, dict] = {
        "label": {
            "type": "string",
            "required": True,
            "description": "Human-readable tile label (the session's working title)",
        },
        "role": {
            "type": "string",
            "required": True,
            "description": "Session role (implementer / pair / researcher / coordinator / ...)",
        },
        "thing": {
            "type": "string",
            "required": True,
            "description": "Coordinator's editorial summary of what this session is doing right now",
        },
        "asks": {
            "type": "string",
            "required": True,
            "enum": list(VALID_TILE_ASKS),
            "description": "What the tile is asking the operator for; drives the action affordances",
        },
        "ageMin": {
            "type": "integer",
            "description": "Age in minutes since the tile last updated",
        },
        "updateKind": {
            "type": "string",
            "enum": list(VALID_TILE_UPDATE_KINDS),
            "description": "Whether the latest update was a routine refresh or a discovery",
        },
        "detail": {
            "type": "string",
            "description": "Optional longer-form supporting detail",
        },
    }

    @classmethod
    def validate(cls, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise SchemaValidationError(
                f"{cls.__name__}: payload must be a dict, "
                f"got {type(payload).__name__}"
            )
        for required in ("label", "role", "thing", "asks"):
            v = payload.get(required)
            if not isinstance(v, str) or not v:
                raise SchemaValidationError(
                    f"{cls.__name__}: missing or empty required field {required!r}"
                )
        if payload["asks"] not in VALID_TILE_ASKS:
            raise SchemaValidationError(
                f"{cls.__name__}: 'asks' must be one of {VALID_TILE_ASKS}, "
                f"got {payload['asks']!r}"
            )
        if "ageMin" in payload and not isinstance(payload["ageMin"], (int, float)):
            raise SchemaValidationError(
                f"{cls.__name__}: 'ageMin' must be a number"
            )
        if "updateKind" in payload \
                and payload["updateKind"] not in VALID_TILE_UPDATE_KINDS:
            raise SchemaValidationError(
                f"{cls.__name__}: 'updateKind' must be one of "
                f"{VALID_TILE_UPDATE_KINDS}"
            )
        if "detail" in payload and payload["detail"] is not None \
                and not isinstance(payload["detail"], str):
            raise SchemaValidationError(
                f"{cls.__name__}: 'detail' must be a string or null"
            )
        extra = set(payload) - set(cls._field_metadata)
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown field(s): {sorted(extra)}"
            )


# ── Thread ───────────────────────────────────────────────────────────


class CoordinatorThreadV1(SettingSchema):
    """Per-(coordinator, thread) tracking card. Key: ``<coord>:<thread-session>``."""

    set_id = COORDINATOR_THREAD_SET_ID
    schema_revision = SCHEMA_REVISION

    _field_metadata: dict[str, dict] = {
        "label": {
            "type": "string",
            "required": True,
            "description": "Human-readable thread label (working title)",
        },
        "role": {
            "type": "string",
            "required": True,
            "description": "Session role driving the thread",
        },
        "status": {
            "type": "string",
            "required": True,
            "enum": list(VALID_THREAD_STATUSES),
            "description": "Current status of the thread; drives the status badge",
        },
        "lead": {
            "type": "string",
            "required": True,
            "description": "Editorial lead — the one-sentence summary of what the thread is about",
        },
        "bullets": {
            "type": "array",
            "description": "Supporting bullets shown beneath the lead",
            "element": {"type": "string"},
        },
        "ageMin": {
            "type": "integer",
            "description": "Age in minutes since the thread last updated",
        },
        "totalTurns": {
            "type": "integer",
            "description": "Total turn count for the thread (sort signal)",
        },
        "needs": {
            "type": "string",
            "description": (
                "When set, the thread is asking the operator for "
                "something specific — surfaced as a callout"
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
        for required in ("label", "role", "status", "lead"):
            v = payload.get(required)
            if not isinstance(v, str) or not v:
                raise SchemaValidationError(
                    f"{cls.__name__}: missing or empty required field {required!r}"
                )
        if payload["status"] not in VALID_THREAD_STATUSES:
            raise SchemaValidationError(
                f"{cls.__name__}: 'status' must be one of "
                f"{VALID_THREAD_STATUSES}, got {payload['status']!r}"
            )
        if "bullets" in payload:
            bullets = payload["bullets"]
            if not isinstance(bullets, list) \
                    or not all(isinstance(b, str) for b in bullets):
                raise SchemaValidationError(
                    f"{cls.__name__}: 'bullets' must be a list of strings"
                )
        for num_field in ("ageMin", "totalTurns"):
            if num_field in payload \
                    and not isinstance(payload[num_field], (int, float)):
                raise SchemaValidationError(
                    f"{cls.__name__}: {num_field!r} must be a number"
                )
        if "needs" in payload and payload["needs"] is not None \
                and not isinstance(payload["needs"], str):
            raise SchemaValidationError(
                f"{cls.__name__}: 'needs' must be a string or null"
            )
        extra = set(payload) - set(cls._field_metadata)
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown field(s): {sorted(extra)}"
            )


# ── Decision (append-only event log) ─────────────────────────────────


class CoordinatorDecisionV1(SettingSchema):
    """Append-only operator decision row. Key: uuid, payload describes the tap."""

    set_id = COORDINATOR_DECISION_SET_ID
    schema_revision = SCHEMA_REVISION

    _field_metadata: dict[str, dict] = {
        "tile_id": {
            "type": "string",
            "required": True,
            "description": (
                "Identifier of the tile the operator interacted with "
                "(matches a coordinator-tile member's session segment)"
            ),
        },
        "kind": {
            "type": "string",
            "required": True,
            "enum": list(VALID_DECISION_KINDS),
            "description": "Decision kind; the action handler routes on this field",
        },
        "choice": {
            "type": "string",
            "description": (
                "Operator's chosen text — the resolution label for "
                "``choice``, the free-text body for ``custom``"
            ),
        },
        "sentAt": {
            "type": "string",
            "description": "ISO-8601 timestamp of the operator tap",
        },
        "target_session": {
            "type": "string",
            "required": True,
            "description": "Session that should receive the decision via session_send",
        },
    }

    @classmethod
    def validate(cls, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise SchemaValidationError(
                f"{cls.__name__}: payload must be a dict, "
                f"got {type(payload).__name__}"
            )
        for required in ("tile_id", "kind", "target_session"):
            v = payload.get(required)
            if not isinstance(v, str) or not v:
                raise SchemaValidationError(
                    f"{cls.__name__}: missing or empty required field {required!r}"
                )
        if payload["kind"] not in VALID_DECISION_KINDS:
            raise SchemaValidationError(
                f"{cls.__name__}: 'kind' must be one of "
                f"{VALID_DECISION_KINDS}, got {payload['kind']!r}"
            )
        # ``choice`` carries the operator's chosen text for ``choice`` /
        # ``custom`` kinds; other kinds may omit it. When supplied it
        # must be a string.
        if "choice" in payload and payload["choice"] is not None \
                and not isinstance(payload["choice"], str):
            raise SchemaValidationError(
                f"{cls.__name__}: 'choice' must be a string or null"
            )
        if payload["kind"] in ("choice", "custom"):
            choice = payload.get("choice")
            if not isinstance(choice, str) or not choice:
                raise SchemaValidationError(
                    f"{cls.__name__}: kind={payload['kind']!r} requires a "
                    f"non-empty 'choice'"
                )
        if "sentAt" in payload and payload["sentAt"] is not None \
                and not isinstance(payload["sentAt"], str):
            raise SchemaValidationError(
                f"{cls.__name__}: 'sentAt' must be an ISO-8601 string or null"
            )
        extra = set(payload) - set(cls._field_metadata)
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown field(s): {sorted(extra)}"
            )


register_schema(COORDINATOR_CANVAS_SET_ID, SCHEMA_REVISION, CoordinatorCanvasV1)
register_schema(OPERATOR_MESSAGE_SET_ID, SCHEMA_REVISION, OperatorMessageToCoordinatorV1)
register_schema(COORDINATOR_TILE_SET_ID, SCHEMA_REVISION, CoordinatorTileV1)
register_schema(COORDINATOR_THREAD_SET_ID, SCHEMA_REVISION, CoordinatorThreadV1)
register_schema(COORDINATOR_DECISION_SET_ID, SCHEMA_REVISION, CoordinatorDecisionV1)
