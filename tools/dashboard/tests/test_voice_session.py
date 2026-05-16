"""Tests for ``tools.dashboard.voice_session``.

Covers the full transition matrix from S3 spec ``graph://86fd1897-d4d``
without touching a real WebSocket transport. Each of the eight named
error scenarios from the spec is pinned with an explicit assertion on
the response frame's ``code`` so a copy change to the error message
can't silently change the protocol contract.
"""

from __future__ import annotations

import pytest

from tools.dashboard import voice_session as vs


def _new_session() -> vs.VoiceSession:
    return vs.VoiceSession(tmux_name="auto-test-0001")


# ── Valid transitions ───────────────────────────────────────


def test_initial_state_is_idle():
    assert _new_session().state == vs.IDLE


def test_start_from_idle_transitions_to_listening():
    s = _new_session()
    assert s.handle_control("start") == []
    assert s.state == vs.LISTENING


def test_mute_from_listening_transitions_to_muted():
    s = _new_session()
    s.handle_control("start")
    assert s.handle_control("mute") == []
    assert s.state == vs.MUTED


def test_unmute_from_muted_transitions_to_listening():
    s = _new_session()
    s.handle_control("start")
    s.handle_control("mute")
    assert s.handle_control("unmute") == []
    assert s.state == vs.LISTENING


def test_mute_from_muted_is_noop():
    s = _new_session()
    s.handle_control("start")
    s.handle_control("mute")
    assert s.handle_control("mute") == []
    assert s.state == vs.MUTED


def test_unmute_from_listening_is_noop():
    s = _new_session()
    s.handle_control("start")
    assert s.handle_control("unmute") == []
    assert s.state == vs.LISTENING


def test_discard_from_listening_keeps_state():
    """discard clears the buffer (a transport-side side effect) but
    the state stays listening."""
    s = _new_session()
    s.handle_control("start")
    assert s.handle_control("discard") == []
    assert s.state == vs.LISTENING


def test_discard_from_muted_keeps_state():
    s = _new_session()
    s.handle_control("start")
    s.handle_control("mute")
    assert s.handle_control("discard") == []
    assert s.state == vs.MUTED


def test_discard_from_idle_is_noop():
    """Buffer is already empty in idle; no error."""
    s = _new_session()
    assert s.handle_control("discard") == []
    assert s.state == vs.IDLE


def test_end_from_idle_transitions_to_ended():
    s = _new_session()
    assert s.handle_control("end") == []
    assert s.state == vs.ENDED


def test_end_from_listening_transitions_to_ended():
    s = _new_session()
    s.handle_control("start")
    assert s.handle_control("end") == []
    assert s.state == vs.ENDED


def test_end_from_muted_transitions_to_ended():
    s = _new_session()
    s.handle_control("start")
    s.handle_control("mute")
    assert s.handle_control("end") == []
    assert s.state == vs.ENDED


def test_end_from_ended_is_noop():
    s = _new_session()
    s.handle_control("end")
    assert s.handle_control("end") == []
    assert s.state == vs.ENDED


# ── Invalid transitions: named error frames ─────────────────


@pytest.mark.parametrize("action", ["mute", "unmute", "commit"])
def test_idle_rejects_action_with_not_started(action):
    s = _new_session()
    frames = s.handle_control(action)
    assert len(frames) == 1
    assert frames[0]["type"] == "error"
    assert frames[0]["code"] == vs.ERR_NOT_STARTED
    assert s.state == vs.IDLE  # unchanged


def test_listening_rejects_start_with_already_started():
    s = _new_session()
    s.handle_control("start")
    frames = s.handle_control("start")
    assert frames[0]["code"] == vs.ERR_ALREADY_STARTED
    assert s.state == vs.LISTENING


def test_muted_rejects_start_with_already_started():
    s = _new_session()
    s.handle_control("start")
    s.handle_control("mute")
    frames = s.handle_control("start")
    assert frames[0]["code"] == vs.ERR_ALREADY_STARTED
    assert s.state == vs.MUTED


@pytest.mark.parametrize("action", ["start", "mute", "unmute", "commit", "discard"])
def test_committing_rejects_actions_with_commit_in_flight(action):
    s = _new_session()
    s.handle_control("start")
    s.handle_control("commit")  # enter committing
    assert s.state == vs.COMMITTING
    frames = s.handle_control(action)
    assert frames[0]["code"] == vs.ERR_COMMIT_IN_FLIGHT
    # State unchanged by the rejected action.
    assert s.state == vs.COMMITTING


def test_committing_end_defers_until_commit_resolves():
    """Regression for codex F1 on 1536e1d: spec graph://86fd1897-d4d
    says 'end from committing → ended (after current commit returns)'.
    The state machine must stay in COMMITTING when 'end' arrives so
    the transport can still complete the in-flight tmux_send and emit
    the final committed/commit_error frame. Without the deferral, an
    operator clicking 'end' mid-commit would tear down the WS before
    the result frame went out, swallowing the response and corrupting
    the protocol."""
    s = _new_session()
    s.handle_control("start")
    s.handle_control("commit")
    assert s.state == vs.COMMITTING
    frames = s.handle_control("end")
    # No frames emitted on the deferred-end (the latch is silent).
    assert frames == []
    # State stays in COMMITTING so finish_commit is still callable.
    assert s.state == vs.COMMITTING
    # finish_commit honors the latch and transitions to ENDED.
    finish_frames = s.finish_commit(success=True)
    assert finish_frames == [{"type": "committed"}]
    assert s.state == vs.ENDED


def test_committing_end_defers_through_commit_error():
    """The deferred-end transition fires whether the in-flight commit
    succeeded or failed — operator's intent to end was registered
    before the result, so we honor it either way."""
    s = _new_session()
    s.handle_control("start")
    s.handle_control("commit")
    s.handle_control("end")
    finish_frames = s.finish_commit(
        success=False,
        error_code=vs.COMMIT_ERR_TMUX_FAILED,
        error_message="tmux send raised",
    )
    assert finish_frames[0]["type"] == "commit_error"
    assert s.state == vs.ENDED


def test_committing_repeated_end_idempotent_during_commit():
    """Multiple 'end' frames arriving while still committing must not
    error — the latch is already set, so further 'end's are silent
    no-ops. Without this, a jittery client could spam 'end' and
    receive spurious error frames."""
    s = _new_session()
    s.handle_control("start")
    s.handle_control("commit")
    assert s.handle_control("end") == []
    assert s.handle_control("end") == []
    assert s.handle_control("end") == []
    assert s.state == vs.COMMITTING
    s.finish_commit(success=True)
    assert s.state == vs.ENDED


def test_committing_end_then_finish_commit_still_resumes_correctly_without_latch():
    """Sanity check: without the deferred-end latch, finish_commit
    resumes to the prior active state as normal. Pinning the
    no-latch branch separately from the latched branch ensures the
    latch is genuinely opt-in rather than always-on."""
    s = _new_session()
    s.handle_control("start")
    s.handle_control("commit")
    # No 'end' frame here — finish_commit should resume LISTENING.
    s.finish_commit(success=True)
    assert s.state == vs.LISTENING


def test_committing_end_latch_resets_after_finish_commit():
    """If the operator starts a new session after a deferred-end ran
    its course (e.g. fresh WS connection that reuses the same
    in-memory test object), the latch must be cleared. force_end +
    a fresh session is the production path; this test just pins that
    finish_commit clears its own state."""
    s = _new_session()
    s.handle_control("start")
    s.handle_control("commit")
    s.handle_control("end")
    s.finish_commit(success=True)
    assert s._end_after_commit is False


@pytest.mark.parametrize("action", ["start", "mute", "unmute", "commit", "discard"])
def test_ended_rejects_actions_with_session_ended(action):
    s = _new_session()
    s.handle_control("end")
    frames = s.handle_control(action)
    assert frames[0]["code"] == vs.ERR_SESSION_ENDED
    assert s.state == vs.ENDED


# ── Unknown / malformed control frames ──────────────────────


def test_unknown_control_frame_emits_error_without_state_change():
    s = _new_session()
    s.handle_control("start")
    frames = s.handle_control("teleport")
    assert frames[0]["type"] == "error"
    assert frames[0]["code"] == vs.ERR_UNKNOWN_FRAME
    assert s.state == vs.LISTENING  # unchanged


# ── handle_audio: forwarding gate ───────────────────────────


def test_audio_dropped_in_idle():
    s = _new_session()
    assert s.handle_audio(b"PCM!") is False


def test_audio_forwarded_only_in_listening():
    s = _new_session()
    s.handle_control("start")
    assert s.handle_audio(b"PCM!") is True


def test_audio_dropped_in_muted():
    s = _new_session()
    s.handle_control("start")
    s.handle_control("mute")
    assert s.handle_audio(b"PCM!") is False


def test_audio_dropped_in_committing():
    s = _new_session()
    s.handle_control("start")
    s.handle_control("commit")
    assert s.handle_audio(b"PCM!") is False


def test_audio_dropped_in_ended():
    s = _new_session()
    s.handle_control("end")
    assert s.handle_audio(b"PCM!") is False


def test_audio_non_bytes_dropped():
    """Defensive: a transport that surfaces a str as audio (a bug)
    must not pass the forward gate."""
    s = _new_session()
    s.handle_control("start")
    assert s.handle_audio("not bytes") is False  # type: ignore[arg-type]


# ── finish_commit: two-phase commit semantics ───────────────


def test_finish_commit_success_resumes_listening():
    s = _new_session()
    s.handle_control("start")
    s.handle_control("commit")
    frames = s.finish_commit(success=True)
    assert frames == [{"type": "committed"}]
    assert s.state == vs.LISTENING


def test_finish_commit_success_resumes_muted_when_commit_came_from_muted():
    """Regression for commit-from-muted: after a successful commit we
    must return to MUTED, not LISTENING — the operator chose to mute
    before commit and that choice should survive the commit."""
    s = _new_session()
    s.handle_control("start")
    s.handle_control("mute")
    s.handle_control("commit")
    frames = s.finish_commit(success=True)
    assert frames == [{"type": "committed"}]
    assert s.state == vs.MUTED


def test_finish_commit_success_includes_committed_text_when_provided():
    s = _new_session()
    s.handle_control("start")
    s.handle_control("commit")
    frames = s.finish_commit(
        success=True,
        committed_text="hello world",
    )
    assert frames[0]["text"] == "hello world"


def test_finish_commit_failure_emits_commit_error_frame():
    s = _new_session()
    s.handle_control("start")
    s.handle_control("commit")
    frames = s.finish_commit(
        success=False,
        error_code=vs.COMMIT_ERR_TMUX_FAILED,
        error_message="tmux send raised TimeoutError",
    )
    assert frames == [{
        "type": "commit_error",
        "code": vs.COMMIT_ERR_TMUX_FAILED,
        "message": "tmux send raised TimeoutError",
    }]
    # On failure, we still resume the prior active state — the WS
    # stays open so the operator can retry.
    assert s.state == vs.LISTENING


def test_finish_commit_failure_resumes_muted_when_appropriate():
    s = _new_session()
    s.handle_control("start")
    s.handle_control("mute")
    s.handle_control("commit")
    s.finish_commit(success=False, error_code="x", error_message="y")
    assert s.state == vs.MUTED


def test_finish_commit_outside_committing_raises_runtimeerror():
    """Calling finish_commit from any state other than committing is a
    transport-layer bug — fail loudly rather than corrupting state."""
    s = _new_session()
    s.handle_control("start")
    with pytest.raises(RuntimeError, match="committing"):
        s.finish_commit(success=True)


# ── force_end: WS disconnect / superseded ───────────────────


def test_force_end_from_any_state_transitions_to_ended():
    for setup in [
        lambda x: None,
        lambda x: x.handle_control("start"),
        lambda x: (x.handle_control("start"), x.handle_control("mute")),
        lambda x: (x.handle_control("start"), x.handle_control("commit")),
    ]:
        s = _new_session()
        setup(s)
        s.force_end()
        assert s.state == vs.ENDED


def test_force_end_is_idempotent():
    s = _new_session()
    s.force_end()
    s.force_end()
    assert s.state == vs.ENDED


# ── parse_control_frame ─────────────────────────────────────


def test_parse_control_frame_returns_type_and_payload():
    typ, payload = vs.parse_control_frame('{"type": "start", "extra": 1}')
    assert typ == "start"
    assert payload == {"type": "start", "extra": 1}


def test_parse_control_frame_rejects_non_json():
    typ, frame = vs.parse_control_frame("not json at all")
    assert typ is None
    assert frame["type"] == "error"
    assert frame["code"] == vs.ERR_MALFORMED_FRAME


def test_parse_control_frame_rejects_non_object():
    typ, frame = vs.parse_control_frame('["array", "not", "object"]')
    assert typ is None
    assert frame["code"] == vs.ERR_MALFORMED_FRAME


def test_parse_control_frame_rejects_missing_type():
    typ, frame = vs.parse_control_frame('{"foo": "bar"}')
    assert typ is None
    assert frame["code"] == vs.ERR_MALFORMED_FRAME


def test_parse_control_frame_rejects_non_string_type():
    typ, frame = vs.parse_control_frame('{"type": 42}')
    assert typ is None
    assert frame["code"] == vs.ERR_MALFORMED_FRAME


def test_parse_control_frame_accepts_unknown_type_string():
    """Parse only enforces shape; the state machine rejects unknown
    types via handle_control. Splits the concern: parse = JSON shape,
    state machine = type semantics."""
    typ, payload = vs.parse_control_frame('{"type": "teleport"}')
    assert typ == "teleport"
    assert payload == {"type": "teleport"}


# ── Cross-state matrix completeness ──────────────────────────


def test_transition_matrix_covers_every_state_action_pair():
    """Regression guard: if a future commit adds a new state or new
    control type without updating the matrix, this test fails noisily
    rather than silently dropping the cell into the unknown-frame
    fallback."""
    expected_states = (vs.IDLE, vs.LISTENING, vs.MUTED, vs.COMMITTING, vs.ENDED)
    for state in expected_states:
        for action in vs.CONTROL_TYPES:
            assert (state, action) in vs._TRANSITION_MATRIX, (
                f"missing transition cell: ({state}, {action})"
            )
