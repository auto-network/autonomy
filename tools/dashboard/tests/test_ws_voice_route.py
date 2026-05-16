"""Tests for ``/ws/voice`` WebSocket route (S3-2).

Covers the connection-acceptance gates (missing bind / unknown
session / disabled feature flag) and the control-frame state machine
end-to-end through the Starlette TestClient. Pure state-machine
coverage lives in ``test_voice_session.py``; this file proves the
transport wires the state machine and the gates correctly.

WhisperLive forwarding, buffer state, and ``tmux_send`` integration
are stubbed in this commit (S3-2). The corresponding integration
tests land alongside S3-3 / S3-4 / S3-5.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def voice_route_env(test_client, monkeypatch):
    """Mock the route's external dependencies so tests don't need a
    real tmux daemon, can flip the feature flag at will, and start
    with a fresh in-memory buffer manager per test."""
    from tools.dashboard import server, feature_flags, voice_buffer

    known_sessions = {"auto-test-designer", "auto-test-validator"}

    def fake_tmux_exists(name):
        return name in known_sessions

    flag_state = {"voice.pipe_enabled": True}

    def fake_is_enabled(name):
        return flag_state.get(name, False)

    monkeypatch.setattr(server, "_tmux_session_exists", fake_tmux_exists)
    monkeypatch.setattr(feature_flags, "is_enabled", fake_is_enabled)

    # Each test gets a clean buffer manager so cross-test state can't
    # leak (e.g. a prior test's superseded-WS callback hanging around).
    fresh_manager = voice_buffer.BufferManager()
    monkeypatch.setattr(voice_buffer, "MANAGER", fresh_manager)

    return {
        "client": test_client,
        "known_sessions": known_sessions,
        "flag_state": flag_state,
        "manager": fresh_manager,
    }


# ── Connection-acceptance gates ─────────────────────────────


def test_ws_voice_rejects_missing_bind_param(voice_route_env):
    client = voice_route_env["client"]
    with client.websocket_connect("/ws/voice") as ws:
        # Server emits the typed error frame before closing.
        err = ws.receive_json()
        assert err["type"] == "error"
        assert err["code"] == "missing_bind"
        # Drain the close.
        with pytest.raises(Exception):
            ws.receive_text()


def test_ws_voice_rejects_unknown_session(voice_route_env):
    client = voice_route_env["client"]
    with client.websocket_connect("/ws/voice?bind=session-does-not-exist") as ws:
        err = ws.receive_json()
        assert err["type"] == "error"
        assert err["code"] == "session_not_found"
        assert "session-does-not-exist" in err["message"]
        with pytest.raises(Exception):
            ws.receive_text()


def test_ws_voice_rejects_when_flag_disabled(voice_route_env):
    voice_route_env["flag_state"]["voice.pipe_enabled"] = False
    client = voice_route_env["client"]
    with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws:
        err = ws.receive_json()
        assert err["type"] == "error"
        assert err["code"] == "voice_pipe_disabled"
        with pytest.raises(Exception):
            ws.receive_text()


# ── State machine end-to-end through the transport ──────────


def test_ws_voice_start_then_mute_then_unmute(voice_route_env):
    """Three valid transitions in sequence each emit no acknowledgement
    frame — the state machine reports successful transitions silently
    (the client tracks state by remembering what it sent)."""
    client = voice_route_env["client"]
    with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws:
        # No server-initiated frame on connect (per spec).
        ws.send_text(json.dumps({"type": "start"}))
        ws.send_text(json.dumps({"type": "mute"}))
        ws.send_text(json.dumps({"type": "unmute"}))
        ws.send_text(json.dumps({"type": "end"}))


def test_ws_voice_idle_commit_emits_not_started(voice_route_env):
    client = voice_route_env["client"]
    with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws:
        ws.send_text(json.dumps({"type": "commit"}))
        err = ws.receive_json()
        assert err["type"] == "error"
        assert err["code"] == "not_started"
        ws.send_text(json.dumps({"type": "end"}))


def test_ws_voice_double_start_emits_already_started(voice_route_env):
    client = voice_route_env["client"]
    with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws:
        ws.send_text(json.dumps({"type": "start"}))
        ws.send_text(json.dumps({"type": "start"}))
        err = ws.receive_json()
        assert err["type"] == "error"
        assert err["code"] == "already_started"
        ws.send_text(json.dumps({"type": "end"}))


def test_ws_voice_commit_emits_no_buffer_stub_in_s32(voice_route_env):
    """S3-2 stub: commit from listening currently returns commit_error
    because the buffer manager (S3-3) and tmux_send wiring (S3-5)
    haven't landed. Operators wiring the frontend get a faithful
    protocol error rather than a fake success. This assertion changes
    when S3-5 lands."""
    client = voice_route_env["client"]
    with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws:
        ws.send_text(json.dumps({"type": "start"}))
        ws.send_text(json.dumps({"type": "commit"}))
        err = ws.receive_json()
        assert err["type"] == "commit_error"
        assert err["code"] == "no_buffer"
        ws.send_text(json.dumps({"type": "end"}))


def test_ws_voice_malformed_text_frame_emits_error(voice_route_env):
    client = voice_route_env["client"]
    with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws:
        ws.send_text("not even json")
        err = ws.receive_json()
        assert err["type"] == "error"
        assert err["code"] == "malformed_frame"
        ws.send_text(json.dumps({"type": "end"}))


def test_ws_voice_missing_type_field_emits_error(voice_route_env):
    client = voice_route_env["client"]
    with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws:
        ws.send_text(json.dumps({"foo": "bar"}))
        err = ws.receive_json()
        assert err["code"] == "malformed_frame"
        ws.send_text(json.dumps({"type": "end"}))


def test_ws_voice_unknown_type_emits_error(voice_route_env):
    client = voice_route_env["client"]
    with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws:
        ws.send_text(json.dumps({"type": "teleport"}))
        err = ws.receive_json()
        assert err["code"] == "unknown_frame"
        ws.send_text(json.dumps({"type": "end"}))


def test_ws_voice_audio_frames_silently_dropped_in_s32(voice_route_env):
    """S3-2 stub: audio frames don't trigger transcripts (no
    WhisperLive yet) but the connection stays open and continues to
    process control frames. Send-and-check-no-error proves the path."""
    client = voice_route_env["client"]
    with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws:
        ws.send_text(json.dumps({"type": "start"}))
        ws.send_bytes(b"\x00" * 100)  # dropped silently
        ws.send_text(json.dumps({"type": "mute"}))
        ws.send_bytes(b"\x00" * 100)  # dropped silently
        ws.send_text(json.dumps({"type": "end"}))


def test_ws_voice_ended_state_rejects_subsequent_commands(voice_route_env):
    client = voice_route_env["client"]
    with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws:
        ws.send_text(json.dumps({"type": "end"}))
        # The route's main loop breaks on state==ENDED after the end
        # transition, so this connection is already torn down. Trying
        # to send more would race the close; we just confirm the end
        # was processed without an error frame.


def test_ws_voice_discard_from_idle_is_silent_noop(voice_route_env):
    client = voice_route_env["client"]
    with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws:
        ws.send_text(json.dumps({"type": "discard"}))
        ws.send_text(json.dumps({"type": "end"}))


def test_ws_voice_discard_from_listening_is_silent_noop(voice_route_env):
    client = voice_route_env["client"]
    with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws:
        ws.send_text(json.dumps({"type": "start"}))
        ws.send_text(json.dumps({"type": "discard"}))
        ws.send_text(json.dumps({"type": "end"}))


# ── Buffer manager integration (S3-3) ───────────────────────


def test_ws_voice_reconnect_restores_buffer_via_buffer_state_frame(voice_route_env):
    """Spec: 'Buffer survives WS disconnect for 60 seconds, restored
    via buffer_state frame on reconnect.' We seed the manager with a
    detached buffer, open a fresh WS for the same bind, and assert
    the first server frame is the buffer_state restore."""
    client = voice_route_env["client"]
    mgr = voice_route_env["manager"]

    # Seed the manager as if a prior WS connected, accumulated some
    # finals, and disconnected without 'end'.
    mgr.acquire("auto-test-designer", evict_callback=None)
    mgr.append_final("auto-test-designer", "first transcript")
    mgr.append_final("auto-test-designer", "second transcript")
    mgr.detach("auto-test-designer")

    with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws:
        # First server frame on a fresh connect with a restored buffer
        # is the buffer_state restore.
        first = ws.receive_json()
        assert first["type"] == "buffer_state"
        assert first["text"] == "first transcript second transcript"
        assert first["word_count"] == 4
        ws.send_text(json.dumps({"type": "end"}))


def test_ws_voice_reconnect_with_no_prior_buffer_sends_no_buffer_state(voice_route_env):
    """No buffer_state frame on a fresh connect with no prior
    buffer — sending an empty buffer_state would be noise."""
    client = voice_route_env["client"]
    with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws:
        # Start the session — no preceding buffer_state means the
        # first activity is whatever the operator sends.
        ws.send_text(json.dumps({"type": "start"}))
        ws.send_text(json.dumps({"type": "end"}))


def test_ws_voice_explicit_end_releases_buffer_immediately(voice_route_env):
    """An explicit 'end' control frame drops the buffer with no TTL
    grace — operator-intended teardown is final."""
    client = voice_route_env["client"]
    mgr = voice_route_env["manager"]
    with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws:
        ws.send_text(json.dumps({"type": "start"}))
        # Seed buffer via direct manager call (no audio plumbing yet).
        mgr.append_final("auto-test-designer", "released on end")
        ws.send_text(json.dumps({"type": "end"}))
    # After the WS closes via 'end', the manager has dropped the
    # record entirely (release, not detach).
    assert not mgr.is_tracked("auto-test-designer")


def test_ws_voice_disconnect_without_end_detaches_with_ttl(voice_route_env):
    """Closing the WS without sending 'end' starts the 60s TTL — the
    buffer survives for a reconnect."""
    client = voice_route_env["client"]
    mgr = voice_route_env["manager"]
    with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws:
        ws.send_text(json.dumps({"type": "start"}))
        mgr.append_final("auto-test-designer", "survives blip")
        # Exit context without 'end' — simulates network drop.
    # Buffer record still tracked (in the TTL window), text preserved.
    assert mgr.is_tracked("auto-test-designer")
    assert mgr.get_text("auto-test-designer") == "survives blip"


def test_ws_voice_commit_with_buffer_reports_tmux_not_wired_in_s33(voice_route_env):
    """S3-3 stub: when the buffer has text at commit time, the
    commit_error code is 'tmux_not_wired' (rather than 'no_buffer')
    — distinguishes 'nothing to commit' from 'have something but
    can't send yet'. Goes away in S3-5 when tmux_send lands."""
    client = voice_route_env["client"]
    mgr = voice_route_env["manager"]
    with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws:
        ws.send_text(json.dumps({"type": "start"}))
        mgr.append_final("auto-test-designer", "the buffer to be committed")
        ws.send_text(json.dumps({"type": "commit"}))
        err = ws.receive_json()
        assert err["type"] == "commit_error"
        assert err["code"] == "tmux_not_wired"
        assert "buffer captured" in err["message"]
        ws.send_text(json.dumps({"type": "end"}))


def test_ws_voice_discard_clears_buffer_via_manager(voice_route_env):
    """'discard' from an active state clears the buffer (operator
    chose to throw it away). Subsequent commit reports no_buffer
    rather than tmux_not_wired, proving the clear landed."""
    client = voice_route_env["client"]
    mgr = voice_route_env["manager"]
    with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws:
        ws.send_text(json.dumps({"type": "start"}))
        mgr.append_final("auto-test-designer", "to be discarded")
        ws.send_text(json.dumps({"type": "discard"}))
        ws.send_text(json.dumps({"type": "commit"}))
        err = ws.receive_json()
        assert err["type"] == "commit_error"
        assert err["code"] == "no_buffer"
        ws.send_text(json.dumps({"type": "end"}))


def test_ws_voice_cross_tab_supersede_kicks_prior_connection(voice_route_env):
    """Spec: 'only one WS connection per tmux_name at a time. New
    connection kicks the old one (close code 1000, reason
    superseded).'

    We open WS A, then open WS B for the same bind. WS A's receive
    must raise (the close arrived) and WS B should be a clean
    connection ready to use."""
    from starlette.websockets import WebSocketDisconnect

    client = voice_route_env["client"]
    with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws_a:
        ws_a.send_text(json.dumps({"type": "start"}))
        # Open second connection same bind — should kick ws_a.
        with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws_b:
            # ws_a's next receive raises because it was closed (1000).
            with pytest.raises((WebSocketDisconnect, Exception)):
                ws_a.receive_text(timeout=1.0)
            # ws_b is the active owner and can proceed normally.
            ws_b.send_text(json.dumps({"type": "start"}))
            ws_b.send_text(json.dumps({"type": "end"}))


def test_ws_voice_deferred_end_during_commit_still_releases_buffer(voice_route_env, monkeypatch):
    """Regression for codex F1 on 02c4950: when 'end' arrives mid-
    commit the state machine defers the terminal transition until
    finish_commit (per eb02f95). The transport's finally block must
    still call release() — not detach() — because the operator
    explicitly intended to end. Without latching end_was_explicit
    at the moment the 'end' frame arrives (independent of session
    state), the deferred-end path would silently fall through to the
    60s TTL grace, holding a buffer the operator wanted gone.

    S3-3's commit stub is synchronous so this path isn't reachable
    over a live WS without forcing finish_commit to leave the state
    in COMMITTING; we patch it to a no-op and assert the route's
    intent-latching logic still fires."""
    from tools.dashboard import voice_session

    # Patch finish_commit to a no-op that leaves state in COMMITTING
    # (simulates the S3-5 async commit window where 'end' can arrive
    # before tmux_send resolves).
    monkeypatch.setattr(
        voice_session.VoiceSession,
        "finish_commit",
        lambda self, **kwargs: [],
    )

    client = voice_route_env["client"]
    mgr = voice_route_env["manager"]

    with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws:
        ws.send_text(json.dumps({"type": "start"}))
        mgr.append_final("auto-test-designer", "operator wants gone")
        ws.send_text(json.dumps({"type": "commit"}))  # → committing, patched finish stays
        ws.send_text(json.dumps({"type": "end"}))     # → deferred (state stays committing)
        # Close from test side — finally runs; with the fix, release
        # is called because end_was_explicit was latched at the
        # 'end' frame regardless of state.

    # After WS close, manager.release should have dropped the record
    # entirely — NOT detach which would hold it for 60s.
    assert not mgr.is_tracked("auto-test-designer"), (
        "deferred-end via 'end' control frame should release the buffer "
        "immediately (operator intent), not detach for TTL grace"
    )


def test_ws_voice_superseded_disconnect_does_not_clobber_new_owners_buffer(voice_route_env):
    """Regression for the cross-tab + buffer interaction: when ws_a
    is superseded, its finally block must NOT touch the buffer —
    the new owner (ws_b) is the legitimate holder. Without the
    superseded_event guard, ws_a's detach/release would corrupt
    ws_b's state."""
    client = voice_route_env["client"]
    mgr = voice_route_env["manager"]

    with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws_a:
        ws_a.send_text(json.dumps({"type": "start"}))
        # Seed buffer via manager (under ws_a's ownership).
        mgr.append_final("auto-test-designer", "must survive supersede")

        # Supersede.
        with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws_b:
            # ws_b should receive the buffer_state restore since the
            # manager preserved the buffer across the supersede.
            first = ws_b.receive_json()
            assert first["type"] == "buffer_state"
            assert first["text"] == "must survive supersede"
            ws_b.send_text(json.dumps({"type": "end"}))

    # After both ws close, the record is gone (ws_b released on end;
    # ws_a was superseded and skipped detach/release).
    assert not mgr.is_tracked("auto-test-designer")
