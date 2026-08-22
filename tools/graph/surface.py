"""Surface Presence + OperatorActivity substrate library — v1 API surface.

Multiplayer-shaped substrate primitive for live dashboard surfaces.
Once the substrate ships these primitives, every plugin gets:

* **Surface presence** — operators and agents on the same page see each
  other, with state, position, and intent.
* **Operator activity** — single-row primitive answering "has the
  operator typed anywhere recently?" Independent of any session or
  surface; one timestamp, one writer.
* **Summons (pinging)** — explicit, ID-targeted, delivered via CrossTalk.

Live behind the API today:

* :meth:`OperatorActivity.is_idle`, :meth:`OperatorActivity.last_user_input`
  — read the singleton ``dashboard.operator.activity#1`` row written by
  ``tools/dashboard/session_monitor.py`` whenever any session parses a
  ``user`` or ``crosstalk`` turn. ``is_idle`` treats a missing row as
  idle; ``last_user_input`` returns ``None`` in that case.

Heartbeat / state writes route through
:func:`tools.graph.settings_ops.upsert_by_key`, so the per-(surface,
participant) row stays a single, evolving base under
``BEGIN IMMEDIATE`` — concurrent writers in the same process serialize
cleanly. Closed substrate gap #2 (signpost ``graph://dff97eec-c59``).

Provenance:

* Signpost: ``graph://dff97eec-c59``
* Anchor: ``graph://cddb1c00-6a4`` (Settings Nexus)
* Principle: ``graph://213c7c83-39a`` (Use shared infrastructure)
* Pitfall (claim-vs-presentation): ``graph://73af2694-562``
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import uuid4

from tools.graph.schemas.registry import (
    publication_band,
    home,
    home,
    SchemaValidationError,
    SettingSchema,
    append_only_log,
    field,
    keyed_per_entity,
    singleton,
)


logger = logging.getLogger(__name__)


SURFACE_PRESENCE_SET_ID = "dashboard.surface.presence"
SURFACE_PING_SET_ID = "dashboard.surface.ping"
OPERATOR_ACTIVITY_SET_ID = "dashboard.operator.activity"

SCHEMA_REVISION = 1


#: "guest" (operator-approved 2026-08-07, program record: OSS Insights P2)
#: is a shim-resolved external visitor — Mission Control's identity shim
#: (``resolve_visitor(token) -> {participant_id, participant_label}``) is
#: the first producer. A guest row's ``participant_id``/``participant_label``
#: come from that resolver, NOT from a dashboard session or an enrolled
#: operator identity. Consumers MUST NOT infer from ``participant_kind ==
#: "guest"``:
#:   * dashboard access — a guest has never unlocked the dashboard;
#:   * session existence — there is no tmux/container session behind the
#:     id, so anything keyed on session lookup (e.g. a "click to open
#:     /session/<id>" affordance) does not apply;
#:   * CrossTalk reachability — a browser tab has no CrossTalk endpoint,
#:     so :meth:`Presence.summon` / :class:`SurfacePingV1` cannot reach a
#:     guest. Consumers that filter summon targets by kind (e.g. Nexus's
#:     ``participant_kind === 'agent'`` template guard) already exclude
#:     guests by construction — keep that filter, don't widen it.
VALID_PARTICIPANT_KINDS = ("operator", "agent", "guest")
VALID_PRESENCE_STATES = ("present", "working")
VALID_POSITION_KINDS_PRESENCE = ("none", "tile", "zone", "coord", "label")
VALID_POSITION_KINDS_PING = ("tile", "zone", "coord", "label")


# SYNOPSIS must be defined ABOVE the schema classes that auto-register —
# ``flush_schema_meta`` reads ``sys.modules[model_cls.__module__].SYNOPSIS``
# when the DB connection is first opened. See pitfall ``graph://4f142305-6fb``.
SYNOPSIS = {
    "summary": (
        "Multiplayer surface substrate: per-(surface, participant) "
        "presence rows (dashboard.surface.presence), explicit "
        "participant-id-targeted pings (dashboard.surface.ping), and "
        "a singleton operator-activity timestamp answering 'has the "
        "operator typed anywhere recently?' (dashboard.operator.activity)"
    ),
    "nouns": [
        "surface presence", "surface ping", "operator activity",
        "summons", "ping", "presence row", "heartbeat",
        "multiplayer", "follow-mode", "spectator",
    ],
    "related_set_ids": [],
}


# ── SurfacePresence ──────────────────────────────────────────


#: An organization's own store. Observed: rows live only in
#: organization databases and never in the operator's. Declaring
#: it refuses a write into the personal or machine store, which
#: would be invisible to every member who needs the value.
@publication_band(min="raw", max="curated")
@home("organization")
@keyed_per_entity(key_strategy="surface_id:participant_id")
class SurfacePresenceV1(SettingSchema):
    """Persistent row per ``(surface_id, participant_id)``.

    Key: ``<surface_id>:<participant_id>``. The row exists permanently
    once claimed; ``state``, ``position_*``, ``intent``, and
    ``heartbeat_at`` change over time. Display logic visually ages a
    stale heartbeat to "away" — the row itself does NOT expire, so the
    participant remains findable for pings even when not currently
    present.
    """

    set_id = SURFACE_PRESENCE_SET_ID
    schema_revision = SCHEMA_REVISION

    surface_id: str = field(
        required=True,
        description="The page/surface this row is for, e.g. 'settings-nexus'",
    )
    participant_kind: str = field(
        required=True,
        enum=list(VALID_PARTICIPANT_KINDS),
        description=(
            "operator | agent | guest. A guest is a shim-resolved "
            "external visitor (see VALID_PARTICIPANT_KINDS docs above) "
            "-- never implies dashboard access, a session, or CrossTalk "
            "reachability."
        ),
    )
    participant_id: str = field(
        required=True,
        description=(
            "Session id (agent) or operator id; the explicit target "
            "for pings"
        ),
    )
    participant_label: str = field(
        required=True,
        description=(
            "Claimed display name, e.g. 'Settings Nexus' or 'Jeremy'"
        ),
    )
    accepts_pings: bool = field(
        default=True,
        description=(
            "Subscription opt-in; set false to be findable but "
            "unpingable"
        ),
    )
    state: str = field(
        required=True,
        enum=list(VALID_PRESENCE_STATES),
        description=(
            "Self-reported state. The view layer overlays 'away' "
            "when heartbeat_at is stale."
        ),
    )
    position_kind: str = field(
        default="none",
        enum=list(VALID_POSITION_KINDS_PRESENCE),
        description=(
            "How to interpret position_value; 'none' when the "
            "participant isn't pointed anywhere"
        ),
    )
    position_value: str = field(
        default="",
        description=(
            "tile id | zone id | 'x,y' | free text — interpretation "
            "by position_kind"
        ),
    )
    intent: str = field(
        default="",
        description="What the participant is doing right now, free text",
    )
    heartbeat_at: str = field(
        required=True,
        description=(
            "ISO timestamp; staleness drives view-side 'away' "
            "rendering"
        ),
    )
    last_ping_id: str = field(
        default="",
        description="UUID of most recent acknowledged ping",
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise SchemaValidationError(
                f"{cls.__name__}: payload must be a dict, "
                f"got {type(payload).__name__}"
            )
        for required_field in (
            "surface_id", "participant_kind", "participant_id",
            "participant_label", "state", "heartbeat_at",
        ):
            v = payload.get(required_field)
            if not isinstance(v, str) or not v:
                raise SchemaValidationError(
                    f"{cls.__name__}: missing or empty required field "
                    f"{required_field!r}"
                )
        if payload["participant_kind"] not in VALID_PARTICIPANT_KINDS:
            raise SchemaValidationError(
                f"{cls.__name__}: 'participant_kind' must be one of "
                f"{VALID_PARTICIPANT_KINDS}, got "
                f"{payload['participant_kind']!r}"
            )
        if payload["state"] not in VALID_PRESENCE_STATES:
            raise SchemaValidationError(
                f"{cls.__name__}: 'state' must be one of "
                f"{VALID_PRESENCE_STATES}, got {payload['state']!r}"
            )
        if "position_kind" in payload \
                and payload["position_kind"] not in VALID_POSITION_KINDS_PRESENCE:
            raise SchemaValidationError(
                f"{cls.__name__}: 'position_kind' must be one of "
                f"{VALID_POSITION_KINDS_PRESENCE}, got "
                f"{payload['position_kind']!r}"
            )
        if "accepts_pings" in payload \
                and not isinstance(payload["accepts_pings"], bool):
            raise SchemaValidationError(
                f"{cls.__name__}: 'accepts_pings' must be a bool"
            )
        for str_field in ("position_value", "intent", "last_ping_id"):
            if str_field in payload \
                    and not isinstance(payload[str_field], str):
                raise SchemaValidationError(
                    f"{cls.__name__}: {str_field!r} must be a string"
                )
        extra = set(payload) - set(cls._field_metadata)
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown field(s): {sorted(extra)}"
            )


# ── SurfacePing ──────────────────────────────────────────────


@publication_band(min="raw", max="curated")
@append_only_log(key="uuid_v4")
class SurfacePingV1(SettingSchema):
    """Directed summons. Append-only event log.

    Targeting is by explicit ``to_participant_id`` — never by role
    lookup. The page reads :class:`SurfacePresenceV1` rows to discover
    who is pingable, presents them in the UI, then passes the chosen
    participant_id into the ping write.
    """

    set_id = SURFACE_PING_SET_ID
    schema_revision = SCHEMA_REVISION

    surface_id: str = field(
        required=True,
        description="The page/surface this ping was sent on, e.g. 'settings-nexus'",
    )
    from_participant_id: str = field(
        required=True,
        description="participant_id of whoever sent the summons",
    )
    to_participant_id: str = field(
        required=True,
        description="Exact participant_id from a SurfacePresence row",
    )
    position_kind: str = field(
        required=True,
        enum=list(VALID_POSITION_KINDS_PING),
        description=(
            "How to interpret position_value — the place on the surface the "
            "recipient is being summoned to"
        ),
    )
    position_value: str = field(
        required=True,
        description=(
            "tile id | zone id | 'x,y' | free text — interpretation depends "
            "on position_kind"
        ),
    )
    message: str = field(
        default="",
        description="Optional free text shown with the summons",
    )
    sent_at: str = field(
        required=True,
        description="ISO-8601 UTC timestamp the ping was written",
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise SchemaValidationError(
                f"{cls.__name__}: payload must be a dict, "
                f"got {type(payload).__name__}"
            )
        for required_field in (
            "surface_id", "from_participant_id", "to_participant_id",
            "position_kind", "position_value", "sent_at",
        ):
            v = payload.get(required_field)
            if not isinstance(v, str) or not v:
                raise SchemaValidationError(
                    f"{cls.__name__}: missing or empty required field "
                    f"{required_field!r}"
                )
        if payload["position_kind"] not in VALID_POSITION_KINDS_PING:
            raise SchemaValidationError(
                f"{cls.__name__}: 'position_kind' must be one of "
                f"{VALID_POSITION_KINDS_PING}, got "
                f"{payload['position_kind']!r}"
            )
        if "message" in payload \
                and not isinstance(payload["message"], str):
            raise SchemaValidationError(
                f"{cls.__name__}: 'message' must be a string"
            )
        extra = set(payload) - set(cls._field_metadata)
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown field(s): {sorted(extra)}"
            )


# ── OperatorActivity ─────────────────────────────────────────


#: The operator's own store. Observed: every stored row lives there
#: and none in any organization's database. Declared so it is
#: enforced rather than agreed -- an undeclared home refuses
#: nothing, and a plain read looks in the caller's own store and
#: reports nothing for rows sitting one database over.
@publication_band(min="raw", max="curated")
@home("personal")
@singleton(key="operator")
class OperatorActivityV1(SettingSchema):
    """Has the operator typed anywhere recently? Singleton, one writer.

    The use case is global: "is the human currently driving any
    session?" — we don't care which session received the input, only
    that the operator was active. One row, fixed key ``operator``,
    latest write wins.

    Written by ``tools/dashboard/session_monitor.py`` whenever any
    session parses a ``user`` or ``crosstalk`` turn.
    """

    set_id = OPERATOR_ACTIVITY_SET_ID
    schema_revision = SCHEMA_REVISION

    last_input_at: str = field(
        default="",
        description="ISO timestamp of the operator's most recent input",
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise SchemaValidationError(
                f"{cls.__name__}: payload must be a dict, "
                f"got {type(payload).__name__}"
            )
        if "last_input_at" in payload \
                and not isinstance(payload["last_input_at"], str):
            raise SchemaValidationError(
                f"{cls.__name__}: 'last_input_at' must be a string"
            )
        extra = set(payload) - set(cls._field_metadata)
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown field(s): {sorted(extra)}"
            )


# ── Presence context manager ─────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 10.0


class Presence:
    """Context-managed participation in a surface.

    Writes the initial presence row on ``__enter__``, starts a
    background heartbeat thread that touches ``heartbeat_at`` on a
    cadence (default ~10s), and on ``__exit__`` signals the thread to
    stop, joins it, and writes a final ``state="present"`` row so the
    participant remains findable for future pings.

    Concurrent writers (same process) serialize cleanly: writes route
    through :func:`settings_ops.upsert_by_key`, which holds SQLite's
    reserved-write lock across SELECT + INSERT/UPDATE so the presence
    row stays a single, evolving base.

    Args:
        surface_id: Page identifier (e.g. ``"settings-nexus"``).
        participant_kind: ``"operator"``, ``"agent"``, or ``"guest"``
            (shim-resolved external visitor — see
            ``VALID_PARTICIPANT_KINDS`` for what consumers must not
            infer about a guest row).
        participant_id: Session id (agent) or operator id — the
            explicit target for pings.
        label: Display name.
        org: Settings org for storage. Defaults to ``"personal"``.
        heartbeat_interval: Seconds between heartbeat writes. Override
            in tests to avoid spurious writes during short test runs.
    """

    def __init__(
        self,
        *,
        surface_id: str,
        participant_kind: str,
        participant_id: str,
        label: str,
        org: str = "personal",
        accepts_pings: bool = True,
        heartbeat_interval: float = _DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    ) -> None:
        if participant_kind not in VALID_PARTICIPANT_KINDS:
            raise ValueError(
                f"participant_kind must be one of "
                f"{VALID_PARTICIPANT_KINDS}, got {participant_kind!r}"
            )
        self.surface_id = surface_id
        self.participant_kind = participant_kind
        self.participant_id = participant_id
        self.label = label
        self.org = org
        self.accepts_pings = bool(accepts_pings)
        self._heartbeat_interval = float(heartbeat_interval)
        self._key = f"{surface_id}:{participant_id}"
        self._state = "present"
        self._position_kind = "none"
        self._position_value = ""
        self._intent = ""
        self._last_ping_id = ""
        self._stop_event: threading.Event | None = None
        self._heartbeat_thread: threading.Thread | None = None

    @property
    def key(self) -> str:
        """Composite Settings key for this participant on this surface."""
        return self._key

    def _build_payload(self) -> dict:
        return {
            "surface_id": self.surface_id,
            "participant_kind": self.participant_kind,
            "participant_id": self.participant_id,
            "participant_label": self.label,
            "accepts_pings": self.accepts_pings,
            "state": self._state,
            "position_kind": self._position_kind,
            "position_value": self._position_value,
            "intent": self._intent,
            "heartbeat_at": _now_iso(),
            "last_ping_id": self._last_ping_id,
        }

    def _write_presence(self) -> None:
        """Persist the current row state. Swallows errors in heartbeat
        path so a transient DB hiccup doesn't kill the surface session.

        Imports :mod:`tools.graph.settings_ops` lazily so importing this
        module doesn't pull in the whole settings stack at server boot.

        Routes through :func:`settings_ops.upsert_by_key` so the
        per-``(surface, participant)`` row stays a single, evolving
        base — heartbeat and ``set_state`` writes update the same row
        instead of appending. Closes substrate gap #2 for this surface.
        """
        from tools.graph import settings_ops

        payload = self._build_payload()
        settings_ops.upsert_by_key(
            SURFACE_PRESENCE_SET_ID, SCHEMA_REVISION, self._key, payload,
            org=self.org,
        )

    def _heartbeat_loop(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.wait(self._heartbeat_interval):
            try:
                self._write_presence()
            except Exception:
                logger.exception(
                    "presence heartbeat write failed for %s", self._key,
                )

    def __enter__(self) -> "Presence":
        self._write_presence()
        self._stop_event = threading.Event()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"presence-heartbeat:{self._key}",
            daemon=True,
        )
        self._heartbeat_thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # Signal the heartbeat thread to stop and join it BEFORE the
        # final write so we don't race with our own background writer.
        if self._stop_event is not None:
            self._stop_event.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(
                timeout=max(self._heartbeat_interval * 2, 5.0),
            )
            if self._heartbeat_thread.is_alive():
                logger.warning(
                    "presence heartbeat thread did not exit within "
                    "join timeout for %s", self._key,
                )
        # Final write: clear position/intent and mark back to 'present'
        # so the row remains discoverable for future pings.
        self._state = "present"
        self._position_kind = "none"
        self._position_value = ""
        self._intent = ""
        try:
            self._write_presence()
        except Exception:
            logger.exception(
                "presence final write failed for %s", self._key,
            )

    def set_state(
        self,
        state: str,
        *,
        position_kind: str | None = None,
        position_value: str | None = None,
        intent: str | None = None,
    ) -> None:
        """Update self-reported state. Restamps ``heartbeat_at``."""
        if state not in VALID_PRESENCE_STATES:
            raise ValueError(
                f"state must be one of {VALID_PRESENCE_STATES}, "
                f"got {state!r}"
            )
        self._state = state
        if position_kind is not None:
            if position_kind not in VALID_POSITION_KINDS_PRESENCE:
                raise ValueError(
                    f"position_kind must be one of "
                    f"{VALID_POSITION_KINDS_PRESENCE}, "
                    f"got {position_kind!r}"
                )
            self._position_kind = position_kind
        if position_value is not None:
            self._position_value = position_value
        if intent is not None:
            self._intent = intent
        self._write_presence()

    def acknowledge_ping(self, ping_id: str) -> None:
        """Record that this participant has handled a ping."""
        self._last_ping_id = ping_id
        self._write_presence()

    def summon(
        self,
        target_id: str,
        position_kind: str,
        position_value: str,
        message: str = "",
    ) -> str:
        """Write a :class:`SurfacePingV1` row directed at *target_id*.

        Returns the new ping's setting id (also serves as the ping id
        for downstream :meth:`acknowledge_ping` calls).
        """
        if position_kind not in VALID_POSITION_KINDS_PING:
            raise ValueError(
                f"position_kind must be one of "
                f"{VALID_POSITION_KINDS_PING}, got {position_kind!r}"
            )
        from tools.graph import settings_ops

        ping_key = str(uuid4())
        payload = {
            "surface_id": self.surface_id,
            "from_participant_id": self.participant_id,
            "to_participant_id": target_id,
            "position_kind": position_kind,
            "position_value": position_value,
            "message": message,
            "sent_at": _now_iso(),
        }
        return settings_ops.add_setting(
            SURFACE_PING_SET_ID, SCHEMA_REVISION, ping_key, payload,
            org=self.org,
        )

    @staticmethod
    def participant_color(participant_id: str) -> str:
        """Deterministic color for a participant id.

        Same id always returns the same color across all sessions and
        viewers. Lives view-side per pitfall ``graph://73af2694-562``;
        surfaced here for Python parity with the JS helper.
        """
        h = sum(
            ord(c) * 31 ** i for i, c in enumerate(participant_id)
        ) & 0xFFFFFFFF
        return f"hsl({h % 360} 70% 60%)"


# ── OperatorActivity helpers — singleton-row readers ────────


class OperatorActivity:
    """Static readers for the singleton ``dashboard.operator.activity`` row.

    Answers "did the operator say anything anywhere in the last N
    minutes?" — independent of any specific session or surface. The
    singleton row is written by ``tools/dashboard/session_monitor.py``
    whenever any session parses a ``user`` or ``crosstalk`` turn.

    Distinct from :class:`Presence`, which tracks per-(surface,
    participant) multiplayer state.
    """

    @staticmethod
    def is_idle(
        threshold: timedelta = timedelta(minutes=30),
    ) -> bool:
        """Has the operator been idle longer than *threshold*?

        Treats 'no row yet' as idle (True).
        """
        last = OperatorActivity.last_user_input()
        if last is None:
            return True
        return (datetime.now(timezone.utc) - last) > threshold

    @staticmethod
    def last_user_input() -> Optional[datetime]:
        """ISO timestamp of the operator's most recent input.

        Returns a tz-aware UTC :class:`~datetime.datetime`, or ``None``
        when no row exists or its ``last_input_at`` is empty/unparseable.
        """
        from tools.graph import settings_ops

        rows = settings_ops.read_set(
            OPERATOR_ACTIVITY_SET_ID, org="personal", peers=[],
        )
        if not rows.members:
            return None
        last_iso = rows.members[0].payload.get("last_input_at", "")
        if not last_iso:
            return None
        try:
            return datetime.fromisoformat(
                last_iso.replace("Z", "+00:00")
            )
        except ValueError:
            return None


# Schemas auto-register via ``SettingSchema.__init_subclass__``.
