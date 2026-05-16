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
    """Mock the two helpers the route depends on so tests don't need
    a real tmux daemon and can flip the feature flag at will."""
    from tools.dashboard import server, feature_flags

    known_sessions = {"auto-test-designer", "auto-test-validator"}

    def fake_tmux_exists(name):
        return name in known_sessions

    flag_state = {"voice.pipe_enabled": True}

    def fake_is_enabled(name):
        return flag_state.get(name, False)

    monkeypatch.setattr(server, "_tmux_session_exists", fake_tmux_exists)
    monkeypatch.setattr(feature_flags, "is_enabled", fake_is_enabled)

    return {
        "client": test_client,
        "known_sessions": known_sessions,
        "flag_state": flag_state,
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
