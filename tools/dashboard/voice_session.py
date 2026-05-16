"""Server-side state machine for a single ``/ws/voice`` connection.

The state machine is decoupled from the WebSocket transport so the
full transition matrix (S3 spec ``graph://86fd1897-d4d``) can be
unit-tested without spinning up a real WebSocket or WhisperLive
client. The transport layer (``ws_voice`` in ``server.py``) drives
this object by calling :meth:`VoiceSession.handle_control` for text
control frames and :meth:`VoiceSession.handle_audio` for binary
audio frames; the session returns the frames the transport should
send back and whether audio should be forwarded to WhisperLive.

States: ``idle`` → ``listening`` → (``muted`` ↔ ``listening``) →
``committing`` → (back to prior active state) → ... → ``ended``.

The full matrix lives in :data:`_TRANSITION_MATRIX` so it's a single
source of truth for both the runtime behavior and the test assertions.

Commit is two-phase to support real async dispatch via ``tmux_send``
in S3-5 while letting tests observe the ``committing`` state in
isolation:

1. The transport receives a ``commit`` control frame and calls
   :meth:`handle_control` — the session transitions to ``committing``
   and the transport begins the actual commit work (no frames sent
   yet).
2. When the commit work resolves (success or error), the transport
   calls :meth:`finish_commit` to transition back to the prior
   active state and emit the ``committed`` or ``commit_error`` frame.

This commit goes only as far as the state machine + frame parsing
(S3-2 in the slice plan). WhisperLive (S3-4) and the buffer manager
(S3-3) and ``tmux_send`` integration (S3-5) land separately.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field


# Public state enum, ordered roughly by lifecycle progression.
IDLE = "idle"
LISTENING = "listening"
MUTED = "muted"
COMMITTING = "committing"
ENDED = "ended"

ACTIVE_STATES = (LISTENING, MUTED)
TERMINAL_STATE = ENDED

# Documented control frame types (per spec).
CONTROL_TYPES = ("start", "mute", "unmute", "commit", "discard", "end")

# Documented error codes (per spec).
ERR_NOT_STARTED = "not_started"
ERR_ALREADY_STARTED = "already_started"
ERR_COMMIT_IN_FLIGHT = "commit_in_flight"
ERR_SESSION_ENDED = "session_ended"
ERR_UNKNOWN_FRAME = "unknown_frame"
ERR_MALFORMED_FRAME = "malformed_frame"

# Documented commit-error codes (returned by finish_commit on failure).
COMMIT_ERR_NO_BUFFER = "no_buffer"      # empty buffer at commit time
COMMIT_ERR_TMUX_FAILED = "tmux_failed"   # tmux_send raised


@dataclass
class _Transition:
    """One cell in the state-transition matrix.

    ``next_state`` is the state to move to. ``response`` is the frame
    to emit back to the client (None for no-response transitions).
    ``forward_audio`` is meaningful only for the audio handler, not
    for control frames; cell defines it for symmetry.
    """

    next_state: str | None  # None means "stay in current state"
    response: dict | None = None


def _err(code: str, message: str) -> dict:
    """Build a generic ``{"type": "error", ...}`` response frame."""
    return {"type": "error", "code": code, "message": message}


# Static transition matrix indexed by (current_state, control_type).
# The eight named-error scenarios from the spec are encoded here as
# `response` payloads. Cells that transition between states leave the
# response None (transport emits no acknowledgement for valid
# state-only moves like mute / unmute / end).
#
# Special cases NOT encodable as a single _Transition:
#   - 'commit' from listening/muted enters committing; finishing the
#     commit is a separate transport call. The matrix encodes the
#     ENTRY into committing only; finish_commit() encodes the EXIT.
#   - 'discard' from listening/muted has a side effect (clear buffer)
#     that the transport performs against its buffer manager — the
#     matrix records the no-state-change transition.
_TRANSITION_MATRIX: dict[tuple[str, str], _Transition] = {
    # idle
    (IDLE, "start"): _Transition(next_state=LISTENING),
    (IDLE, "mute"): _Transition(
        None, _err(ERR_NOT_STARTED, "session has not been started"),
    ),
    (IDLE, "unmute"): _Transition(
        None, _err(ERR_NOT_STARTED, "session has not been started"),
    ),
    (IDLE, "commit"): _Transition(
        None, _err(ERR_NOT_STARTED, "session has not been started"),
    ),
    (IDLE, "discard"): _Transition(next_state=None),  # no-op
    (IDLE, "end"): _Transition(next_state=ENDED),

    # listening
    (LISTENING, "start"): _Transition(
        None, _err(ERR_ALREADY_STARTED, "session already started"),
    ),
    (LISTENING, "mute"): _Transition(next_state=MUTED),
    (LISTENING, "unmute"): _Transition(next_state=None),  # no-op
    (LISTENING, "commit"): _Transition(next_state=COMMITTING),
    (LISTENING, "discard"): _Transition(next_state=None),  # clear buffer in transport
    (LISTENING, "end"): _Transition(next_state=ENDED),

    # muted
    (MUTED, "start"): _Transition(
        None, _err(ERR_ALREADY_STARTED, "session already started"),
    ),
    (MUTED, "mute"): _Transition(next_state=None),  # no-op
    (MUTED, "unmute"): _Transition(next_state=LISTENING),
    (MUTED, "commit"): _Transition(next_state=COMMITTING),
    (MUTED, "discard"): _Transition(next_state=None),  # clear buffer in transport
    (MUTED, "end"): _Transition(next_state=ENDED),

    # committing — every non-end action errors; end transitions but
    # the transport must wait for the in-flight commit to resolve
    # before fully tearing down.
    (COMMITTING, "start"): _Transition(
        None, _err(ERR_COMMIT_IN_FLIGHT, "commit in progress"),
    ),
    (COMMITTING, "mute"): _Transition(
        None, _err(ERR_COMMIT_IN_FLIGHT, "commit in progress"),
    ),
    (COMMITTING, "unmute"): _Transition(
        None, _err(ERR_COMMIT_IN_FLIGHT, "commit in progress"),
    ),
    (COMMITTING, "commit"): _Transition(
        None, _err(ERR_COMMIT_IN_FLIGHT, "commit in progress"),
    ),
    (COMMITTING, "discard"): _Transition(
        None, _err(ERR_COMMIT_IN_FLIGHT, "commit in progress"),
    ),
    (COMMITTING, "end"): _Transition(next_state=ENDED),

    # ended — everything errors (terminal state)
    (ENDED, "start"): _Transition(
        None, _err(ERR_SESSION_ENDED, "session ended"),
    ),
    (ENDED, "mute"): _Transition(
        None, _err(ERR_SESSION_ENDED, "session ended"),
    ),
    (ENDED, "unmute"): _Transition(
        None, _err(ERR_SESSION_ENDED, "session ended"),
    ),
    (ENDED, "commit"): _Transition(
        None, _err(ERR_SESSION_ENDED, "session ended"),
    ),
    (ENDED, "discard"): _Transition(
        None, _err(ERR_SESSION_ENDED, "session ended"),
    ),
    (ENDED, "end"): _Transition(next_state=None),  # idempotent no-op
}


@dataclass
class VoiceSession:
    """In-memory state machine for one ``/ws/voice`` connection.

    ``tmux_name`` is the bound tmux session this connection commits
    into. It's stored on the session so the transport doesn't have
    to thread it through every commit call.

    ``state`` exposes the current state for tests and the transport;
    do NOT mutate it directly — call :meth:`handle_control` or
    :meth:`finish_commit`.
    """

    tmux_name: str
    state: str = IDLE
    _prior_active_state: str | None = field(default=None, init=False, repr=False)

    def handle_control(self, frame_type: str) -> list[dict]:
        """Process one control frame. Returns the list of frames the
        transport should send back to the client.

        Unknown frame types produce an :data:`ERR_UNKNOWN_FRAME`
        response without changing state. Valid no-op transitions (e.g.
        ``unmute`` while already listening) return ``[]``.

        Entering ``committing`` returns ``[]`` — the transport is
        responsible for performing the actual commit work and calling
        :meth:`finish_commit` to emit the ``committed`` /
        ``commit_error`` frame.
        """
        if frame_type not in CONTROL_TYPES:
            return [
                _err(
                    ERR_UNKNOWN_FRAME,
                    f"unknown control frame type {frame_type!r}",
                )
            ]
        cell = _TRANSITION_MATRIX[(self.state, frame_type)]
        if cell.next_state == COMMITTING and self.state in ACTIVE_STATES:
            # Remember which active state to resume in after the commit
            # resolves; without this, finish_commit would have no way
            # to choose between LISTENING and MUTED. The matrix
            # transitions out of either, so the prior state can't be
            # recovered from the static table.
            self._prior_active_state = self.state
        if cell.next_state is not None:
            self.state = cell.next_state
        return [cell.response] if cell.response is not None else []

    def handle_audio(self, audio_bytes: bytes) -> bool:
        """Return True if the audio frame should be forwarded to
        WhisperLive. False if it must be silently dropped (per the
        spec: any non-``listening`` state drops audio without an error
        frame — client bugs that send audio outside the listening
        window aren't worth fatally failing on).
        """
        if not isinstance(audio_bytes, (bytes, bytearray)):
            return False
        return self.state == LISTENING

    def finish_commit(
        self,
        *,
        success: bool,
        committed_text: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> list[dict]:
        """Resolve a previously-initiated commit. Transitions back to
        the prior active state (LISTENING or MUTED) the session was
        in before ``commit`` arrived. Emits a single
        ``{"type": "committed"}`` (success) or
        ``{"type": "commit_error", ...}`` (failure) frame.

        Must be called from the COMMITTING state — calling from any
        other state is a programming error in the transport layer and
        raises :class:`RuntimeError` so the bug surfaces loudly rather
        than silently desynchronising the session state.
        """
        if self.state != COMMITTING:
            raise RuntimeError(
                f"finish_commit called while state={self.state!r}; only "
                "valid from 'committing'"
            )
        resume_state = self._prior_active_state or LISTENING
        self._prior_active_state = None
        self.state = resume_state
        if success:
            frame: dict = {"type": "committed"}
            if committed_text is not None:
                frame["text"] = committed_text
            return [frame]
        return [{
            "type": "commit_error",
            "code": error_code or "unknown",
            "message": error_message or "commit failed",
        }]

    def force_end(self) -> None:
        """Mark the session terminally ENDED — used by the transport
        when the WebSocket disconnects without an explicit ``end``
        control frame, or when the cross-tab guard supersedes this
        connection. Idempotent."""
        self.state = ENDED


def parse_control_frame(raw_text: str) -> tuple[str | None, dict]:
    """Parse a text frame as a JSON control message.

    Returns ``(frame_type, raw_payload)`` on success.
    Returns ``(None, error_frame)`` on parse failure — the second
    element is a ready-to-send error frame the transport can forward
    to the client without further processing.

    The protocol requires every text frame be a JSON object with a
    string ``type`` field. Anything else is :data:`ERR_MALFORMED_FRAME`.
    """
    try:
        obj = json.loads(raw_text)
    except (json.JSONDecodeError, ValueError):
        return None, _err(
            ERR_MALFORMED_FRAME, "frame must be a JSON object",
        )
    if not isinstance(obj, dict):
        return None, _err(
            ERR_MALFORMED_FRAME, "frame must be a JSON object",
        )
    frame_type = obj.get("type")
    if not isinstance(frame_type, str):
        return None, _err(
            ERR_MALFORMED_FRAME,
            "frame must have a string 'type' field",
        )
    return frame_type, obj
