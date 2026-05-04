"""Activity tab notifications substrate — Setting schemas only.

Four schemas backing the Activity surface's Notifications tab — the
"one-thought" coordinator-style asks panel:

* ``dashboard.activity.ask#1`` — :class:`SessionAskV1`. One row per
  source session that currently has an outstanding ask for an
  operator. Caller key = ``session_id``; heartbeat-style rewrites
  must route through :func:`tools.graph.settings_ops.upsert_by_key`
  so the row count stays at one per session.
* ``dashboard.activity.ask_vote#1`` — :class:`AskVoteV1`. One row per
  ``(ask_id, voter_id)`` pair. Shared (anyone can read the count); v1
  doesn't render counts inline. View-side aggregates from a member
  walk — schema stores no aggregates.
* ``dashboard.activity.ask_refresh#1`` — :class:`AskRefreshRequestV1`.
  One row per ask at any time, requesting the source session re-prove
  its ask is still live. ``target_revision`` pins the
  ``revision_seq`` this refresh expects to surpass; the "requested"
  state clears only when the source session bumps ``revision_seq`` or
  drops the ask, never on operator re-clicks / reloads / heartbeats.
* ``dashboard.activity.operator_dismissed#1`` —
  :class:`OperatorDismissedAsksV1`. Operator-local singleton holding
  ask ids muted from THIS operator's inbox. Other operators are
  unaffected. Notifications-tab badge count =
  ``outstanding_asks - dismissed_ask_ids``.

Lives alongside Surface Presence (``graph://dff97eec-c59``) — presence
answers "who is here, what are they focused on?", asks answer "who is
currently asking for operator resolution?" Two different claim
families; UI composes both.

The substrate stays thin: schemas + decorators + length-cap
validation. Real-time delivery rides on the existing
``setting.changed`` SSE channel (auto-5mz65); no new plumbing.
Notifications tab UI ships in the companion bead (re-file of
auto-140q0.6).

Provenance:

* Bead: auto-5u8zb (re-filed from auto-140q0.5)
* Design: ``graph://75c03f1d-4cd`` (Unified Activity Surface) —
  claim-vs-presentation cut, explicit-id targeting, separation
  from Presence
* Substrate primitive: ``upsert_by_key`` (auto-nqlzg)
* SSE seam: ``setting.changed`` channel (auto-5mz65)
"""
from __future__ import annotations

from typing import Any

from tools.graph.schemas.registry import (
    SchemaValidationError,
    SettingSchema,
    field,
    keyed_per_entity,
    singleton,
)


SESSION_ASK_SET_ID = "dashboard.activity.ask"
ASK_VOTE_SET_ID = "dashboard.activity.ask_vote"
ASK_REFRESH_SET_ID = "dashboard.activity.ask_refresh"
OPERATOR_DISMISSED_SET_ID = "dashboard.activity.operator_dismissed"

SCHEMA_REVISION = 1

# Hard cap on SessionAskV1.text. ~2KB measured in UTF-8 bytes; 2048
# accepts a full 2KB write, 2049 rejects. Tested at the boundaries.
ASK_TEXT_MAX_BYTES = 2048


VALID_VOTE_DIRECTIONS = ("up", "down")


# SYNOPSIS must be defined ABOVE the schema classes that auto-register —
# ``flush_schema_meta`` reads ``sys.modules[model_cls.__module__].SYNOPSIS``
# when the DB connection is first opened (pitfall ``graph://4f142305-6fb``).
SYNOPSIS = {
    "summary": (
        "Activity tab notifications substrate: per-session asks "
        "(dashboard.activity.ask), shared per-(ask, voter) votes "
        "(dashboard.activity.ask_vote), per-ask refresh requests with "
        "revision_seq pinning (dashboard.activity.ask_refresh), and an "
        "operator-local dismissed-ids singleton "
        "(dashboard.activity.operator_dismissed). Lives alongside "
        "Surface Presence — different claim families, UI composes both."
    ),
    "nouns": [
        "session ask", "activity ask", "notifications tab",
        "ask vote", "vote count", "ask refresh", "refresh request",
        "operator dismissed", "muted asks", "inbox",
        "one-thought", "coordinator ask",
    ],
    "related_set_ids": [
        "dashboard.surface.presence",
        "dashboard.surface.ping",
    ],
}


# ── SessionAskV1 ─────────────────────────────────────────────


@keyed_per_entity
class SessionAskV1(SettingSchema):
    """One row per source session with an outstanding operator-ask.

    Caller key = ``session_id``. Heartbeat-style rewrites must route
    through :func:`tools.graph.settings_ops.upsert_by_key` so the row
    count stays at one per session — multiple base rows for the same
    session are a writer bug, not a presentation choice.

    ``revision_seq`` is monotonic per session: the source bumps it
    every time the ask body changes meaningfully. Operators trigger
    refreshes by writing :class:`AskRefreshRequestV1` rows pinned to a
    ``target_revision``; the source session "answers" by writing a
    new ``SessionAskV1`` row with ``revision_seq > target_revision``.

    ``to_participant_id`` is explicit-id targeting only (no role
    lookup) — empty string means an ambient ask anyone can pick up.
    """

    set_id = SESSION_ASK_SET_ID
    schema_revision = SCHEMA_REVISION

    session_id: str = field(
        default="",
        description=(
            "Source session id. Matches the natural key for upsert "
            "writes; one row per session by construction."
        ),
    )
    to_participant_id: str = field(
        default="",
        description=(
            "Explicit recipient participant id (e.g. operator id), or "
            "empty string for an ambient ask. No role lookup logic."
        ),
    )
    text: str = field(
        default="",
        description=(
            "Ask body, capped at ~2KB (UTF-8 bytes). Oversized writes "
            "are rejected by validate()."
        ),
    )
    created_at: str = field(
        default="",
        description="ISO-8601 timestamp the ask was first written",
    )
    revision_seq: int = field(
        default=0,
        description=(
            "Monotonic per-session revision counter. AskRefreshRequest "
            "rows pin a target_revision; the requested state clears "
            "only when this value surpasses the target."
        ),
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise SchemaValidationError(
                f"{cls.__name__}: payload must be a dict, "
                f"got {type(payload).__name__}"
            )
        for str_field in (
            "session_id", "to_participant_id", "text", "created_at",
        ):
            if str_field in payload \
                    and not isinstance(payload[str_field], str):
                raise SchemaValidationError(
                    f"{cls.__name__}: {str_field!r} must be a string"
                )
        text = payload.get("text", "")
        if isinstance(text, str):
            byte_len = len(text.encode("utf-8"))
            if byte_len > ASK_TEXT_MAX_BYTES:
                raise SchemaValidationError(
                    f"{cls.__name__}: 'text' exceeds the "
                    f"{ASK_TEXT_MAX_BYTES}-byte cap "
                    f"({byte_len} bytes)"
                )
        if "revision_seq" in payload \
                and not isinstance(payload["revision_seq"], int):
            raise SchemaValidationError(
                f"{cls.__name__}: 'revision_seq' must be an int"
            )
        # bool is a subclass of int — exclude it explicitly.
        if isinstance(payload.get("revision_seq"), bool):
            raise SchemaValidationError(
                f"{cls.__name__}: 'revision_seq' must be an int, not bool"
            )
        extra = set(payload) - set(cls._field_metadata)
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown field(s): {sorted(extra)}"
            )


# ── AskVoteV1 ────────────────────────────────────────────────


@keyed_per_entity
class AskVoteV1(SettingSchema):
    """One row per ``(ask_id, voter_id)`` pair. Shared, schema-clean.

    No count fields, no aggregation. View-side aggregates by walking
    members. v1 single-operator deployments don't render counts inline
    (would just be noise); the count is available for future UIs.

    Caller-supplied composite key: ``f"{ask_id}:{voter_id}"`` is the
    canonical convention. The schema doesn't enforce key shape.
    """

    set_id = ASK_VOTE_SET_ID
    schema_revision = SCHEMA_REVISION

    ask_id: str = field(
        default="",
        description="Setting id of the SessionAsk row this vote targets",
    )
    voter_id: str = field(
        default="",
        description="Operator/agent id casting the vote",
    )
    direction: str = field(
        default="",
        enum=list(VALID_VOTE_DIRECTIONS),
        description="'up' or 'down'",
    )
    voted_at: str = field(
        default="",
        description="ISO-8601 timestamp the vote was cast",
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise SchemaValidationError(
                f"{cls.__name__}: payload must be a dict, "
                f"got {type(payload).__name__}"
            )
        for str_field in ("ask_id", "voter_id", "voted_at"):
            if str_field in payload \
                    and not isinstance(payload[str_field], str):
                raise SchemaValidationError(
                    f"{cls.__name__}: {str_field!r} must be a string"
                )
        if "direction" in payload:
            direction = payload["direction"]
            if not isinstance(direction, str):
                raise SchemaValidationError(
                    f"{cls.__name__}: 'direction' must be a string"
                )
            # Empty default permitted for partial writes; non-empty
            # values must be one of the enum values.
            if direction and direction not in VALID_VOTE_DIRECTIONS:
                raise SchemaValidationError(
                    f"{cls.__name__}: 'direction' must be one of "
                    f"{VALID_VOTE_DIRECTIONS}, got {direction!r}"
                )
        extra = set(payload) - set(cls._field_metadata)
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown field(s): {sorted(extra)}"
            )


# ── AskRefreshRequestV1 ──────────────────────────────────────


@keyed_per_entity
class AskRefreshRequestV1(SettingSchema):
    """One row per ask. Pins a ``target_revision`` for the refresh.

    Caller key = ``ask_id``. The "requested" state lives until ONE of:

    1. Source session writes a new :class:`SessionAskV1` row with
       ``revision_seq > target_revision`` (the source revised in
       response).
    2. Source session deletes/dismisses the ask (the source dropped).
    3. Another operator writes this row again with a new
       ``target_revision`` (chain-clear).

    The "requested" state must NOT clear via operator re-clicks,
    local reloads, heartbeats, or time-based timeouts. This is the
    "trust the source actually responded" invariant — covered by
    ``test_refresh_state_machine``.
    """

    set_id = ASK_REFRESH_SET_ID
    schema_revision = SCHEMA_REVISION

    ask_id: str = field(
        default="",
        description=(
            "Setting id of the SessionAsk row this refresh targets; "
            "matches the natural key for upsert writes"
        ),
    )
    requested_at: str = field(
        default="",
        description="ISO-8601 timestamp the refresh was requested",
    )
    requested_by: str = field(
        default="",
        description="Voter/operator id that clicked refresh",
    )
    target_revision: int = field(
        default=0,
        description=(
            "The SessionAsk revision_seq this refresh targets. The "
            "requested state clears only when the source session "
            "writes a SessionAsk row with revision_seq > "
            "target_revision (or drops the ask entirely)."
        ),
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise SchemaValidationError(
                f"{cls.__name__}: payload must be a dict, "
                f"got {type(payload).__name__}"
            )
        for str_field in ("ask_id", "requested_at", "requested_by"):
            if str_field in payload \
                    and not isinstance(payload[str_field], str):
                raise SchemaValidationError(
                    f"{cls.__name__}: {str_field!r} must be a string"
                )
        if "target_revision" in payload:
            if isinstance(payload["target_revision"], bool):
                raise SchemaValidationError(
                    f"{cls.__name__}: 'target_revision' must be an int, "
                    f"not bool"
                )
            if not isinstance(payload["target_revision"], int):
                raise SchemaValidationError(
                    f"{cls.__name__}: 'target_revision' must be an int"
                )
        extra = set(payload) - set(cls._field_metadata)
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown field(s): {sorted(extra)}"
            )


# ── OperatorDismissedAsksV1 ──────────────────────────────────


@singleton(key="dismissed")
class OperatorDismissedAsksV1(SettingSchema):
    """Operator-local: ask ids THIS operator has muted from their inbox.

    Singleton key ``dismissed`` — one row per operator's per-org DB.
    Other operators see their own row only; dismissal does not
    propagate. Each operator's Notifications-tab badge count is
    ``outstanding_asks - dismissed_ask_ids``.
    """

    set_id = OPERATOR_DISMISSED_SET_ID
    schema_revision = SCHEMA_REVISION

    dismissed_ask_ids: list[str] = field(
        default_factory=list,
        description=(
            "Ask Setting-ids muted from this operator's inbox. Append "
            "to dismiss; remove to undo. Order is not significant."
        ),
        element=str,
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise SchemaValidationError(
                f"{cls.__name__}: payload must be a dict, "
                f"got {type(payload).__name__}"
            )
        if "dismissed_ask_ids" in payload:
            ids = payload["dismissed_ask_ids"]
            if not isinstance(ids, list):
                raise SchemaValidationError(
                    f"{cls.__name__}: 'dismissed_ask_ids' must be a list"
                )
            for i, item in enumerate(ids):
                if not isinstance(item, str):
                    raise SchemaValidationError(
                        f"{cls.__name__}: 'dismissed_ask_ids[{i}]' must "
                        f"be a string"
                    )
        extra = set(payload) - set(cls._field_metadata)
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown field(s): {sorted(extra)}"
            )


# Schemas auto-register via ``SettingSchema.__init_subclass__``.
