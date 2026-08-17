"""Setting schemas owned by the coordinator-board plugin.

The plugin owns eleven Settings:

* ``dashboard.coordinator`` — explicit singleton binding for the live
  coordinator session that should receive operator messages and board-
  level refresh requests. Key: ``default``.

* ``dashboard.coordinator-canvas`` — coordinator's primary output: a
  single perfectly-framed question with just enough context to be
  answerable, plus optional pre-canned quick-reply pills.
  Key: ``<coord-session>``.
* ``dashboard.operator-message-to-coordinator`` — operator's most
  recent message back; latest-write-wins (the UI surfaces only the
  newest member).  Key: ``default``.
* ``dashboard.coordinator-tile`` — per-peer-session editorial card
  (One thing tab). Bead auto-1aef5 rekeys this from
  ``<coord-session>:<peer-session>`` (v1) to ``<peer-session>``
  alone (v2): peers self-publish under their own session id, and
  the coordinator curates via the publication-state machine
  (auto-xhimi). Coordinator handoff becomes seamless — the new
  coordinator inherits whatever each peer last wrote.
* ``dashboard.coordinator-thread`` — per-peer-session editorial
  thread (Tracking tab). Same rekey as tile.
* ``dashboard.coordinator-decision`` — append-only event log of
  operator taps on a coordinator tile (thumb yes/no, choice, custom
  reply, sitrep request, refresh request). Keyed by uuid. Variant
  subclasses match the ``kind`` discriminator one-to-one
  (``ThumbYes`` → ``thumb_yes`` etc.) — codegen consumers walk the
  tree to expose per-kind typed methods.
* ``dashboard.coordinator-sprint`` — coordinator-owned multi-session
  arc. Key ``<sprint-id>``.
* ``dashboard.coordinator-bead`` — coordinator-curated bead landing /
  closing summary (Tracking tab). Key ``<bead-id>``.
* ``dashboard.coordinator-convergent-decision`` — coordinator-curated
  cross-session design decision (Tracking tab). Key ``<title-slug>``.
* ``dashboard.coordinator-open-followup`` — coordinator-curated
  follow-up that hasn't been beaded yet (Tracking tab). Key uuid.
* ``dashboard.coordinator-docs`` — singleton docs pointer
  (coord map + walkthrough source ids). Key ``default``.

Read paths flow through the standard ``/api/graph/settings/...``
endpoints; write paths POST to ``/api/graph/setting``. Sprints,
beads, convergent-decisions, open-followups, and docs are
coordinator-only writes (cross-session editorial); tiles + threads
are peer-self-published with coordinator curation via the
publication-state machine.

Migrated to the Phase 1 typed-field declaration shape (bead 4A) —
``_field_metadata`` is derived from typed annotations + ``field()``
helpers via ``SettingSchema.__init_subclass__``. Access patterns are
declared via decorator (``@append_only_log`` /  ``@singleton`` /
``@keyed_per_entity``); decision variants use the variant-subclass
pattern. Imperative ``validate()`` methods continue to enforce
cross-field shape rules the typed metadata cannot yet describe
(non-empty required strings, enum membership, kind-conditional
choice requirement, structured-detail shape, list-of-strings
content, extra-field rejection).
"""
from __future__ import annotations

from typing import Any

from tools.graph.schemas.registry import (
    home,
    home,
    home,
    home,
    home,
    home,
    home,
    home,
    home,
    home,
    home,
    home,
    SchemaValidationError,
    SettingSchema,
    append_only_log,
    field,
    keyed_per_entity,
    singleton,
)

COORDINATOR_SET_ID = "dashboard.coordinator"
COORDINATOR_CANVAS_SET_ID = "dashboard.coordinator-canvas"
OPERATOR_MESSAGE_SET_ID = "dashboard.operator-message-to-coordinator"
COORDINATOR_TILE_SET_ID = "dashboard.coordinator-tile"
COORDINATOR_THREAD_SET_ID = "dashboard.coordinator-thread"
COORDINATOR_DECISION_SET_ID = "dashboard.coordinator-decision"
COORDINATOR_SPRINT_SET_ID = "dashboard.coordinator-sprint"
COORDINATOR_BEAD_SET_ID = "dashboard.coordinator-bead"
COORDINATOR_CONVERGENT_DECISION_SET_ID = (
    "dashboard.coordinator-convergent-decision"
)
COORDINATOR_OPEN_FOLLOWUP_SET_ID = "dashboard.coordinator-open-followup"
COORDINATOR_DOCS_SET_ID = "dashboard.coordinator-docs"

# Most schemas live at revision 1. Tile + thread were bumped to revision 2
# by bead auto-1aef5 (peer-session-only keying); bead auto-fwwfu bumps tile
# and thread to revision 3 and sprint to revision 2 — both drop ``ageMin``
# from the payload (page derives relative time from ``member.updated_at``).
# Prior revisions stay registered for the migration window.
SCHEMA_REVISION = 1
TILE_SCHEMA_REVISION = 3
THREAD_SCHEMA_REVISION = 3
SPRINT_SCHEMA_REVISION = 2

# The now-prior revisions for tile + thread + sprint, preserved for
# the upconvert chain and the ``drop_legacy_age_min`` migration helper.
TILE_PRIOR_AGE_MIN_REVISION = 2
THREAD_PRIOR_AGE_MIN_REVISION = 2
SPRINT_PRIOR_AGE_MIN_REVISION = 1


VALID_TILE_ASKS = ("yes_no", "decide", "merge", "approve", "fyi")
VALID_TILE_UPDATE_KINDS = ("refresh", "discovery")
VALID_THREAD_STATUSES = (
    "shipping", "blocked", "designing",
    "researching", "investigating", "paused", "compacted",
)
VALID_DECISION_KINDS = (
    "thumb_yes", "thumb_no", "choice", "custom",
    "sitrep_request", "refresh_request",
)
VALID_SPRINT_STATUSES = (
    "active", "shipping", "parked", "design", "done", "nascent",
)
VALID_BEAD_STATUSES = ("landed", "closed-duplicate", "specified")


SYNOPSIS = {
    "summary": (
        "Coordinator board Settings: coordinator binding, canvas (the "
        "banger), operator message back, tile + thread editorial cards "
        "(peer-session keyed), decision log, sprints, beads, "
        "convergent decisions, open follow-ups, docs"
    ),
    "nouns": [
        "coordinator board", "coordinator", "coordinator canvas",
        "operator message", "coordinator tile", "coordinator thread",
        "coordinator decision", "coordinator sprint",
        "coordinator bead", "coordinator convergent decision",
        "coordinator open follow-up", "coordinator docs",
        "thumb yes", "thumb no", "sitrep", "tile refresh",
    ],
    "related_set_ids": [],
}


# ── Coordinator binding ──────────────────────────────────────────────


#: Not forced into any one store. This records that the question was
#: ASKED -- must this live in the operator's own database, or on
#: this machine alone? -- and answered no, which is different
#: from nobody having considered it.
#:
#: It is not a prohibition. The operator owns workspaces, so
#: their database is the organizational home of their own
#: things; reading this as "anywhere but personal" refuses
#: writes that are correct.
@home("organization")
@singleton(key="default")
class CoordinatorV1(SettingSchema):
    """Singleton live coordinator binding. Key: ``default``."""

    set_id = COORDINATOR_SET_ID
    schema_revision = SCHEMA_REVISION

    session_id: str = field(
        required=True,
        description="Bound coordinator session id for board-level routing",
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise SchemaValidationError(
                f"{cls.__name__}: payload must be a dict, "
                f"got {type(payload).__name__}"
            )
        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            raise SchemaValidationError(
                f"{cls.__name__}: 'session_id' must be a non-empty string"
            )
        extra = set(payload) - {"session_id"}
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown field(s): {sorted(extra)}"
            )


# ── Canvas ───────────────────────────────────────────────────────────


@keyed_per_entity(key_strategy="session_name")
class CoordinatorCanvasV1(SettingSchema):
    """``{ageMin, question, context, quickReplies[]}`` — coordinator's banger."""

    set_id = COORDINATOR_CANVAS_SET_ID
    schema_revision = SCHEMA_REVISION

    question: str = field(
        required=True,
        description=(
            "The one well-framed question the coordinator wants the "
            "operator to answer right now"
        ),
    )
    context: str = field(
        required=False,
        description=(
            "Just-enough context to make the question answerable; "
            "supports inline ``[label](href)`` markdown links"
        ),
    )
    ageMin: int = field(
        required=False,
        description="Age in minutes since the question was published",
    )
    quickReplies: list = field(
        required=False,
        description=(
            "Pre-canned reply suggestions; tapping a pill populates "
            "the operator composer verbatim"
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


@singleton(key="default")
class OperatorMessageToCoordinatorV1(SettingSchema):
    """``{text, sentAt}`` — operator's latest message back to coordinator."""

    set_id = OPERATOR_MESSAGE_SET_ID
    schema_revision = SCHEMA_REVISION

    text: str = field(
        required=True,
        description="Operator's reply body, sent verbatim to the coordinator session",
    )
    sentAt: str = field(
        required=False,
        description="ISO-8601 send timestamp",
    )

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


# ── Tile (v1, retained for upconvert chain) ──────────────────────────


def _validate_tile_common(cls, payload: Any) -> None:
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


@keyed_per_entity(key_strategy="session_name[:tile_id]")
class CoordinatorTileV1(SettingSchema):
    """Per-(coordinator, peer) tile. Key: ``<coord>:<peer-session>``.

    Retained so existing v1 rows remain readable while migration runs.
    Use :class:`CoordinatorTileV2` for new writes.
    """

    set_id = COORDINATOR_TILE_SET_ID
    schema_revision = SCHEMA_REVISION

    label: str = field(required=True, description="Tile label (peer's working title)")
    role: str = field(required=True, description="Peer's session role")
    thing: str = field(required=True,
                       description="Editorial summary of what the peer is doing")
    asks: str = field(required=True, enum=list(VALID_TILE_ASKS),
                      description="What the tile is asking the operator for")
    ageMin: int = field(required=False,
                        description="Age in minutes since the tile last updated")
    updateKind: str = field(required=False, enum=list(VALID_TILE_UPDATE_KINDS),
                            description="Refresh vs discovery update")
    detail: str = field(required=False,
                        description="Optional longer-form supporting detail (string)")

    @classmethod
    def validate(cls, payload: Any) -> None:
        _validate_tile_common(cls, payload)
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


# ── Tile (v2 — peer-session keyshape, structured detail) ─────────────


@keyed_per_entity(key_strategy="session_name[:tile_id]")
class CoordinatorTileV2(SettingSchema):
    """Per-peer-session tile. Key: ``<peer-session>``.

    Differences from v1:
    - Key is the peer session id alone (no coord prefix).
    - ``detail`` is an object ``{context, choices[]}`` instead of a
      bare string. The detail panel renders ``context`` + a list of
      ``choices`` operator can pick from.

    Retained for the v2→v3 upconvert chain (bead auto-fwwfu drops
    ``ageMin``). Use :class:`CoordinatorTileV3` for new writes.
    """

    set_id = COORDINATOR_TILE_SET_ID
    schema_revision = TILE_PRIOR_AGE_MIN_REVISION

    label: str = field(required=True, description="Tile label (peer's working title)")
    role: str = field(required=True, description="Peer's session role")
    thing: str = field(required=True,
                       description="Editorial summary of what the peer is doing")
    asks: str = field(required=True, enum=list(VALID_TILE_ASKS),
                      description="What the tile is asking the operator for")
    ageMin: int = field(required=False,
                        description="Age in minutes since the tile last updated")
    updateKind: str = field(required=False, enum=list(VALID_TILE_UPDATE_KINDS),
                            description="Refresh vs discovery update")
    detail: dict = field(
        required=False,
        description=(
            "Optional structured detail; renders the expanded panel. "
            "``context`` carries longer-form prose; ``choices`` is a "
            "list of resolution-choice strings the operator can tap."
        ),
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        _validate_tile_common(cls, payload)
        if "detail" in payload and payload["detail"] is not None:
            d = payload["detail"]
            if not isinstance(d, dict):
                raise SchemaValidationError(
                    f"{cls.__name__}: 'detail' must be an object or null"
                )
            if "context" in d and d["context"] is not None \
                    and not isinstance(d["context"], str):
                raise SchemaValidationError(
                    f"{cls.__name__}: 'detail.context' must be a string or null"
                )
            if "choices" in d:
                ch = d["choices"]
                if not isinstance(ch, list) \
                        or not all(isinstance(c, str) for c in ch):
                    raise SchemaValidationError(
                        f"{cls.__name__}: 'detail.choices' must be a list of strings"
                    )
            extra = set(d) - {"context", "choices"}
            if extra:
                raise SchemaValidationError(
                    f"{cls.__name__}: unknown 'detail' field(s): {sorted(extra)}"
                )
        extra = set(payload) - set(cls._field_metadata)
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown field(s): {sorted(extra)}"
            )

    @classmethod
    def upconvert_from_prev(cls, payload: dict) -> dict:
        """Wrap a v1 tile payload (string ``detail``) into v2 shape.

        The v1 ``detail`` was a free-text supporting blurb. v2 promotes it
        to ``detail.context`` and adds an empty ``choices`` list — old rows
        surface in the new detail panel without a Resolution-choices
        section, just the prose.
        """
        out = dict(payload)
        detail = payload.get("detail")
        if detail is None:
            out.pop("detail", None)
        elif isinstance(detail, str):
            out["detail"] = {"context": detail, "choices": []}
        return out


# ── Tile (v3 — drops ``ageMin``; relative time derived from member.updated_at) ──


@keyed_per_entity(key_strategy="session_name[:tile_id]")
class CoordinatorTileV3(SettingSchema):
    """Per-peer-session tile, ``ageMin``-free. Key: ``<peer-session>``.

    Differences from v2:
    - ``ageMin`` is gone. The page derives the per-tile "Nm ago" label
      from ``member.updated_at`` via the ``relativeTime`` helper, so the
      stored payload no longer carries a stale stamp the writer has to
      keep refreshing.
    """

    set_id = COORDINATOR_TILE_SET_ID
    schema_revision = TILE_SCHEMA_REVISION

    label: str = field(required=True, description="Tile label (peer's working title)")
    role: str = field(required=True, description="Peer's session role")
    thing: str = field(required=True,
                       description="Editorial summary of what the peer is doing")
    asks: str = field(required=True, enum=list(VALID_TILE_ASKS),
                      description="What the tile is asking the operator for")
    updateKind: str = field(required=False, enum=list(VALID_TILE_UPDATE_KINDS),
                            description="Refresh vs discovery update")
    detail: dict = field(
        required=False,
        description=(
            "Optional structured detail; renders the expanded panel. "
            "``context`` carries longer-form prose; ``choices`` is a "
            "list of resolution-choice strings the operator can tap."
        ),
    )

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
        if "updateKind" in payload \
                and payload["updateKind"] not in VALID_TILE_UPDATE_KINDS:
            raise SchemaValidationError(
                f"{cls.__name__}: 'updateKind' must be one of "
                f"{VALID_TILE_UPDATE_KINDS}"
            )
        if "detail" in payload and payload["detail"] is not None:
            d = payload["detail"]
            if not isinstance(d, dict):
                raise SchemaValidationError(
                    f"{cls.__name__}: 'detail' must be an object or null"
                )
            if "context" in d and d["context"] is not None \
                    and not isinstance(d["context"], str):
                raise SchemaValidationError(
                    f"{cls.__name__}: 'detail.context' must be a string or null"
                )
            if "choices" in d:
                ch = d["choices"]
                if not isinstance(ch, list) \
                        or not all(isinstance(c, str) for c in ch):
                    raise SchemaValidationError(
                        f"{cls.__name__}: 'detail.choices' must be a list of strings"
                    )
            extra = set(d) - {"context", "choices"}
            if extra:
                raise SchemaValidationError(
                    f"{cls.__name__}: unknown 'detail' field(s): {sorted(extra)}"
                )
        extra = set(payload) - set(cls._field_metadata)
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown field(s): {sorted(extra)}"
            )

    @classmethod
    def upconvert_from_prev(cls, payload: dict) -> dict:
        """Drop ``ageMin`` — v3 derives it from ``member.updated_at``."""
        out = dict(payload)
        out.pop("ageMin", None)
        return out


# ── Thread (v1, retained for upconvert chain) ────────────────────────


def _validate_thread_common(cls, payload: Any) -> None:
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


#: Not forced into any one store. This records that the question was
#: ASKED -- must this live in the operator's own database, or on
#: this machine alone? -- and answered no, which is different
#: from nobody having considered it.
#:
#: It is not a prohibition. The operator owns workspaces, so
#: their database is the organizational home of their own
#: things; reading this as "anywhere but personal" refuses
#: writes that are correct.
@home("organization")
@keyed_per_entity(key_strategy="session_name")
class CoordinatorThreadV1(SettingSchema):
    """Per-(coordinator, peer) thread. Key: ``<coord>:<peer-session>``.

    Retained for the v1→v2 upconvert chain.
    """

    set_id = COORDINATOR_THREAD_SET_ID
    schema_revision = SCHEMA_REVISION

    label: str = field(required=True, description="Thread label (working title)")
    role: str = field(required=True,
                      description="Session role driving the thread")
    status: str = field(required=True, enum=list(VALID_THREAD_STATUSES),
                        description="Thread status; drives the badge")
    lead: str = field(required=True,
                      description="Editorial lead (one-sentence summary)")
    bullets: list = field(
        required=False, element=str,
        description="Supporting bullets shown beneath the lead",
    )
    ageMin: int = field(
        required=False,
        description="Age in minutes since the thread last updated",
    )
    totalTurns: int = field(
        required=False, description="Total turn count for the thread",
    )
    needs: str = field(
        required=False,
        description=(
            "When set, the thread is asking the operator for "
            "something specific — surfaced as a callout"
        ),
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        _validate_thread_common(cls, payload)
        extra = set(payload) - set(cls._field_metadata)
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown field(s): {sorted(extra)}"
            )


@keyed_per_entity(key_strategy="session_name")
class CoordinatorThreadV2(SettingSchema):
    """Per-peer-session thread. Key: ``<peer-session>``.

    Same payload shape as v1; only the keyshape changed (peers
    self-publish under their own session id; coordinator curates
    via the publication-state machine).

    Retained for the v2→v3 upconvert chain (bead auto-fwwfu drops
    ``ageMin``). Use :class:`CoordinatorThreadV3` for new writes.
    """

    set_id = COORDINATOR_THREAD_SET_ID
    schema_revision = THREAD_PRIOR_AGE_MIN_REVISION

    label: str = field(required=True, description="Thread label (working title)")
    role: str = field(required=True,
                      description="Session role driving the thread")
    status: str = field(required=True, enum=list(VALID_THREAD_STATUSES),
                        description="Thread status; drives the badge")
    lead: str = field(required=True,
                      description="Editorial lead (one-sentence summary)")
    bullets: list = field(
        required=False, element=str,
        description="Supporting bullets shown beneath the lead",
    )
    ageMin: int = field(
        required=False,
        description="Age in minutes since the thread last updated",
    )
    totalTurns: int = field(
        required=False, description="Total turn count for the thread",
    )
    needs: str = field(
        required=False,
        description=(
            "When set, the thread is asking the operator for "
            "something specific — surfaced as a callout"
        ),
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        _validate_thread_common(cls, payload)
        extra = set(payload) - set(cls._field_metadata)
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown field(s): {sorted(extra)}"
            )

    @classmethod
    def upconvert_from_prev(cls, payload: dict) -> dict:
        """Pass through — the payload shape is identical between v1 and v2."""
        return dict(payload)


# ── Thread (v3 — drops ``ageMin``) ───────────────────────────────────


@keyed_per_entity(key_strategy="session_name")
class CoordinatorThreadV3(SettingSchema):
    """Per-peer-session thread, ``ageMin``-free. Key: ``<peer-session>``.

    Differences from v2: ``ageMin`` is gone. The page derives "Nm ago"
    from ``member.updated_at`` via ``relativeTime``.
    """

    set_id = COORDINATOR_THREAD_SET_ID
    schema_revision = THREAD_SCHEMA_REVISION

    label: str = field(required=True, description="Thread label (working title)")
    role: str = field(required=True,
                      description="Session role driving the thread")
    status: str = field(required=True, enum=list(VALID_THREAD_STATUSES),
                        description="Thread status; drives the badge")
    lead: str = field(required=True,
                      description="Editorial lead (one-sentence summary)")
    bullets: list = field(
        required=False, element=str,
        description="Supporting bullets shown beneath the lead",
    )
    totalTurns: int = field(
        required=False, description="Total turn count for the thread",
    )
    needs: str = field(
        required=False,
        description=(
            "When set, the thread is asking the operator for "
            "something specific — surfaced as a callout"
        ),
    )

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
        if "totalTurns" in payload \
                and not isinstance(payload["totalTurns"], (int, float)):
            raise SchemaValidationError(
                f"{cls.__name__}: 'totalTurns' must be a number"
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

    @classmethod
    def upconvert_from_prev(cls, payload: dict) -> dict:
        """Drop ``ageMin`` — v3 derives it from ``member.updated_at``."""
        out = dict(payload)
        out.pop("ageMin", None)
        return out


# ── Decision (append-only event log; variants per kind) ──────────────


#: Not forced into any one store. This records that the question was
#: ASKED -- must this live in the operator's own database, or on
#: this machine alone? -- and answered no, which is different
#: from nobody having considered it.
#:
#: It is not a prohibition. The operator owns workspaces, so
#: their database is the organizational home of their own
#: things; reading this as "anywhere but personal" refuses
#: writes that are correct.
@home("organization")
@append_only_log(key="uuid_v4")
class CoordinatorDecisionV1(SettingSchema):
    """Append-only operator decision row. Key: uuid, payload describes the tap.

    Variant subclasses (``ThumbYes``, ``ThumbNo``, ``Choice``, ``Custom``,
    ``SitrepRequest``, ``RefreshRequest``) match ``VALID_DECISION_KINDS``
    one-to-one. Codegen consumers walk the variants tree to expose
    per-kind typed methods on the JS proxy (``Decision.thumb_yes(...)``
    etc.). The base ``validate()`` handles every kind via the existing
    imperative checks; variants exist for codegen and for the union-
    of-fields unknown-field guard below.
    """

    set_id = COORDINATOR_DECISION_SET_ID
    schema_revision = SCHEMA_REVISION

    tile_id: str = field(
        required=True,
        description=(
            "Identifier of the tile the operator interacted with "
            "(matches a coordinator-tile member's session segment)"
        ),
    )
    kind: str = field(
        required=True, enum=list(VALID_DECISION_KINDS),
        description="Decision kind; the action handler routes on this field",
    )
    sentAt: str = field(
        required=False,
        description="ISO-8601 timestamp of the operator tap",
    )
    target_session: str = field(
        required=True,
        description="Session that should receive the decision via session_send",
    )
    choice: str = field(
        required=False,
        description=(
            "The option the operator picked. Optional in general and "
            "required by validate() when kind is 'choice' or 'custom', a "
            "conditional the declarative form cannot express"
        ),
    )

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
        # Allow any field declared on the base OR any registered variant.
        # Variants extend the field set (Choice / Custom add ``choice``);
        # this union keeps the unknown-field guard valid across them.
        all_fields = set(cls._field_metadata)
        for variant_cls in cls._variants.values():
            all_fields |= set(variant_cls._field_metadata)
        extra = set(payload) - all_fields
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown field(s): {sorted(extra)}"
            )


class ThumbYes(CoordinatorDecisionV1):
    """Operator approves the tile's ask."""


class ThumbNo(CoordinatorDecisionV1):
    """Operator declines the tile's ask."""


class SitrepRequest(CoordinatorDecisionV1):
    """Operator requests a fresh sitrep on the tile's owning session."""


class RefreshRequest(CoordinatorDecisionV1):
    """Operator requests the tile's owner refresh its state."""


class Choice(CoordinatorDecisionV1):
    """Operator picked one of the canvas's pre-canned quickReply pills."""

    choice: str = field(
        required=True,
        description="The pill text the operator picked, verbatim.",
    )


class Custom(CoordinatorDecisionV1):
    """Operator typed a free-text reply targeting this tile."""

    choice: str = field(
        required=True,
        description="The operator's free-text body, verbatim.",
    )


# ── Sprint (coordinator-owned editorial arc) ─────────────────────────


#: Not forced into any one store. This records that the question was
#: ASKED -- must this live in the operator's own database, or on
#: this machine alone? -- and answered no, which is different
#: from nobody having considered it.
#:
#: It is not a prohibition. The operator owns workspaces, so
#: their database is the organizational home of their own
#: things; reading this as "anywhere but personal" refuses
#: writes that are correct.
@home("organization")
@keyed_per_entity(key_strategy="sprint_id")
class CoordinatorSprintV1(SettingSchema):
    """Per-sprint-id editorial card. Key: ``<sprint-id>``.

    Sprints are inherently cross-session arcs that no single peer sees
    the full shape of; coordinator is the only writer.

    Retained for the v1→v2 upconvert chain (bead auto-fwwfu drops
    ``ageMin``). Use :class:`CoordinatorSprintV2` for new writes.
    """

    set_id = COORDINATOR_SPRINT_SET_ID
    schema_revision = SPRINT_PRIOR_AGE_MIN_REVISION

    title: str = field(required=True, description="Editorial sprint headline")
    status: str = field(required=True, enum=list(VALID_SPRINT_STATUSES),
                        description="Sprint status; drives the badge")
    ageMin: int = field(required=False,
                        description="Age in minutes since last update")
    participants: list = field(
        required=False, element=str,
        description="Sessions involved in this sprint",
    )
    commitCount: int = field(
        required=False,
        description="Commit count surfaced in the meta-row",
    )
    beadCount: int = field(
        required=False,
        description="Bead count surfaced in the meta-row",
    )
    arc: str = field(
        required=False,
        description=(
            "Editorial 1–2 sentence arc; supports inline "
            "``[label](href)`` markdown links"
        ),
    )
    shipped: list = field(
        required=False, element=str,
        description="Landed work lines (with optional inline links)",
    )
    inFlight: list = field(
        required=False, element=str, description="Active work lines",
    )
    needs: str = field(
        required=False,
        description=(
            "What would keep this sprint from falling by the "
            "wayside; surfaced as an amber callout"
        ),
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise SchemaValidationError(
                f"{cls.__name__}: payload must be a dict, "
                f"got {type(payload).__name__}"
            )
        for required in ("title", "status"):
            v = payload.get(required)
            if not isinstance(v, str) or not v:
                raise SchemaValidationError(
                    f"{cls.__name__}: missing or empty required field {required!r}"
                )
        if payload["status"] not in VALID_SPRINT_STATUSES:
            raise SchemaValidationError(
                f"{cls.__name__}: 'status' must be one of "
                f"{VALID_SPRINT_STATUSES}, got {payload['status']!r}"
            )
        for num_field in ("ageMin", "commitCount", "beadCount"):
            if num_field in payload \
                    and not isinstance(payload[num_field], (int, float)):
                raise SchemaValidationError(
                    f"{cls.__name__}: {num_field!r} must be a number"
                )
        for list_field in ("participants", "shipped", "inFlight"):
            if list_field in payload:
                v = payload[list_field]
                if not isinstance(v, list) \
                        or not all(isinstance(s, str) for s in v):
                    raise SchemaValidationError(
                        f"{cls.__name__}: {list_field!r} must be a list of strings"
                    )
        for str_field in ("arc", "needs"):
            if str_field in payload and payload[str_field] is not None \
                    and not isinstance(payload[str_field], str):
                raise SchemaValidationError(
                    f"{cls.__name__}: {str_field!r} must be a string or null"
                )
        extra = set(payload) - set(cls._field_metadata)
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown field(s): {sorted(extra)}"
            )


# ── Sprint (v2 — drops ``ageMin``) ───────────────────────────────────


@keyed_per_entity(key_strategy="sprint_id")
class CoordinatorSprintV2(SettingSchema):
    """Per-sprint-id editorial card, ``ageMin``-free. Key: ``<sprint-id>``.

    Differences from v1: ``ageMin`` is gone. The page derives "Nm ago"
    from ``member.updated_at`` via ``relativeTime``.
    """

    set_id = COORDINATOR_SPRINT_SET_ID
    schema_revision = SPRINT_SCHEMA_REVISION

    title: str = field(required=True, description="Editorial sprint headline")
    status: str = field(required=True, enum=list(VALID_SPRINT_STATUSES),
                        description="Sprint status; drives the badge")
    participants: list = field(
        required=False, element=str,
        description="Sessions involved in this sprint",
    )
    commitCount: int = field(
        required=False,
        description="Commit count surfaced in the meta-row",
    )
    beadCount: int = field(
        required=False,
        description="Bead count surfaced in the meta-row",
    )
    arc: str = field(
        required=False,
        description=(
            "Editorial 1–2 sentence arc; supports inline "
            "``[label](href)`` markdown links"
        ),
    )
    shipped: list = field(
        required=False, element=str,
        description="Landed work lines (with optional inline links)",
    )
    inFlight: list = field(
        required=False, element=str, description="Active work lines",
    )
    needs: str = field(
        required=False,
        description=(
            "What would keep this sprint from falling by the "
            "wayside; surfaced as an amber callout"
        ),
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise SchemaValidationError(
                f"{cls.__name__}: payload must be a dict, "
                f"got {type(payload).__name__}"
            )
        for required in ("title", "status"):
            v = payload.get(required)
            if not isinstance(v, str) or not v:
                raise SchemaValidationError(
                    f"{cls.__name__}: missing or empty required field {required!r}"
                )
        if payload["status"] not in VALID_SPRINT_STATUSES:
            raise SchemaValidationError(
                f"{cls.__name__}: 'status' must be one of "
                f"{VALID_SPRINT_STATUSES}, got {payload['status']!r}"
            )
        for num_field in ("commitCount", "beadCount"):
            if num_field in payload \
                    and not isinstance(payload[num_field], (int, float)):
                raise SchemaValidationError(
                    f"{cls.__name__}: {num_field!r} must be a number"
                )
        for list_field in ("participants", "shipped", "inFlight"):
            if list_field in payload:
                v = payload[list_field]
                if not isinstance(v, list) \
                        or not all(isinstance(s, str) for s in v):
                    raise SchemaValidationError(
                        f"{cls.__name__}: {list_field!r} must be a list of strings"
                    )
        for str_field in ("arc", "needs"):
            if str_field in payload and payload[str_field] is not None \
                    and not isinstance(payload[str_field], str):
                raise SchemaValidationError(
                    f"{cls.__name__}: {str_field!r} must be a string or null"
                )
        extra = set(payload) - set(cls._field_metadata)
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown field(s): {sorted(extra)}"
            )

    @classmethod
    def upconvert_from_prev(cls, payload: dict) -> dict:
        """Drop ``ageMin`` — v2 derives it from ``member.updated_at``."""
        out = dict(payload)
        out.pop("ageMin", None)
        return out


# ── Bead (coordinator-curated landing/closing summary) ───────────────


@keyed_per_entity(key_strategy="bead_id")
class CoordinatorBeadV1(SettingSchema):
    """Coordinator-curated bead summary. Key: ``<bead-id>``."""

    set_id = COORDINATOR_BEAD_SET_ID
    schema_revision = SCHEMA_REVISION

    commit: str = field(
        required=False,
        description="Landing commit short-sha (or ``(host)`` for host work)",
    )
    scope: str = field(
        required=True, description="One-line scope description",
    )
    status: str = field(
        required=True, enum=list(VALID_BEAD_STATUSES),
        description="Bead status; drives the row glyph",
    )
    note: str = field(required=False, description="Optional editorial note")

    @classmethod
    def validate(cls, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise SchemaValidationError(
                f"{cls.__name__}: payload must be a dict, "
                f"got {type(payload).__name__}"
            )
        for required in ("scope", "status"):
            v = payload.get(required)
            if not isinstance(v, str) or not v:
                raise SchemaValidationError(
                    f"{cls.__name__}: missing or empty required field {required!r}"
                )
        if payload["status"] not in VALID_BEAD_STATUSES:
            raise SchemaValidationError(
                f"{cls.__name__}: 'status' must be one of "
                f"{VALID_BEAD_STATUSES}, got {payload['status']!r}"
            )
        for str_field in ("commit", "note"):
            if str_field in payload and payload[str_field] is not None \
                    and not isinstance(payload[str_field], str):
                raise SchemaValidationError(
                    f"{cls.__name__}: {str_field!r} must be a string or null"
                )
        extra = set(payload) - set(cls._field_metadata)
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown field(s): {sorted(extra)}"
            )


# ── Convergent decision (coordinator-curated cross-session call) ─────


@keyed_per_entity(key_strategy="decision_id")
class CoordinatorConvergentDecisionV1(SettingSchema):
    """Coordinator-curated convergent design decision. Key: ``<title-slug>``."""

    set_id = COORDINATOR_CONVERGENT_DECISION_SET_ID
    schema_revision = SCHEMA_REVISION

    title: str = field(
        required=True, description="Headline of the convergent decision",
    )
    raisedBy: list = field(
        required=True, element=str,
        description="Sessions that surfaced the same problem",
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise SchemaValidationError(
                f"{cls.__name__}: payload must be a dict, "
                f"got {type(payload).__name__}"
            )
        title = payload.get("title")
        if not isinstance(title, str) or not title:
            raise SchemaValidationError(
                f"{cls.__name__}: missing or empty required field 'title'"
            )
        raised = payload.get("raisedBy")
        if not isinstance(raised, list) or not raised \
                or not all(isinstance(s, str) and s for s in raised):
            raise SchemaValidationError(
                f"{cls.__name__}: 'raisedBy' must be a non-empty list of strings"
            )
        extra = set(payload) - set(cls._field_metadata)
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown field(s): {sorted(extra)}"
            )


# ── Open follow-up (coordinator-curated, not yet beaded) ─────────────


#: Not forced into any one store. This records that the question was
#: ASKED -- must this live in the operator's own database, or on
#: this machine alone? -- and answered no, which is different
#: from nobody having considered it.
#:
#: It is not a prohibition. The operator owns workspaces, so
#: their database is the organizational home of their own
#: things; reading this as "anywhere but personal" refuses
#: writes that are correct.
@home("organization")
@keyed_per_entity(key_strategy="followup_id")
class CoordinatorOpenFollowupV1(SettingSchema):
    """Coordinator-curated open follow-up. Key: uuid."""

    set_id = COORDINATOR_OPEN_FOLLOWUP_SET_ID
    schema_revision = SCHEMA_REVISION

    text: str = field(
        required=True,
        description=(
            "Follow-up body; supports inline ``[label](href)`` "
            "markdown links"
        ),
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise SchemaValidationError(
                f"{cls.__name__}: payload must be a dict, "
                f"got {type(payload).__name__}"
            )
        text = payload.get("text")
        if not isinstance(text, str) or not text:
            raise SchemaValidationError(
                f"{cls.__name__}: missing or empty required field 'text'"
            )
        extra = set(payload) - {"text"}
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown field(s): {sorted(extra)}"
            )


# ── Docs (singleton coordMap + walkthrough pointers) ─────────────────


#: Not forced into any one store. This records that the question was
#: ASKED -- must this live in the operator's own database, or on
#: this machine alone? -- and answered no, which is different
#: from nobody having considered it.
#:
#: It is not a prohibition. The operator owns workspaces, so
#: their database is the organizational home of their own
#: things; reading this as "anywhere but personal" refuses
#: writes that are correct.
@home("organization")
@singleton(key="default")
class CoordinatorDocsV1(SettingSchema):
    """Singleton coord-map + walkthrough doc pointers. Key: ``default``."""

    set_id = COORDINATOR_DOCS_SET_ID
    schema_revision = SCHEMA_REVISION

    coordMap: str = field(required=False, description="Coord-map note source id")
    walkthrough: str = field(
        required=False, description="Walkthrough note source id",
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise SchemaValidationError(
                f"{cls.__name__}: payload must be a dict, "
                f"got {type(payload).__name__}"
            )
        for str_field in ("coordMap", "walkthrough"):
            if str_field in payload and payload[str_field] is not None \
                    and not isinstance(payload[str_field], str):
                raise SchemaValidationError(
                    f"{cls.__name__}: {str_field!r} must be a string or null"
                )
        extra = set(payload) - set(cls._field_metadata)
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown field(s): {sorted(extra)}"
            )


# Schemas auto-register via ``SettingSchema.__init_subclass__``.
