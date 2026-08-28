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


class _StubWhisperLiveClient:
    """Default test substitute for WhisperLiveClient that succeeds
    instantly and emits no transcript events.

    Most route tests don't care about the WhisperLive path; they
    just want 'start' to proceed without sending a
    whisperlive_connect_failed error frame for the missing
    127.0.0.1:9090 service. Tests that DO want to drive the
    upstream path opt into the real fake server via the
    `whisperlive_fake` fixture.

    The class signature mirrors WhisperLiveClient so the route's
    construction site is unchanged. Records the constructor kwargs
    for tests that want to assert on the configuration the route
    passed (uid shape, model selection, etc.)."""

    instances: list = []  # populated by __init__ for assertion access

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.on_partial = kwargs["on_partial"]
        self.on_final = kwargs["on_final"]
        self.on_error = kwargs["on_error"]
        self.audio_frames: list[bytes] = []
        self._ready = False
        self._closed = False
        _StubWhisperLiveClient.instances.append(self)

    async def connect_and_wait_ready(self, *, ready_timeout: float = 15.0):
        self._ready = True

    def is_ready(self) -> bool:
        return self._ready and not self._closed

    def is_unavailable(self) -> bool:
        return False

    async def send_audio(self, audio_bytes: bytes) -> None:
        if self._ready and not self._closed:
            self.audio_frames.append(bytes(audio_bytes))

    def set_cutoff(self) -> None:
        # Mirrors WhisperLiveClient.set_cutoff (sync no-op for the stub);
        # the route calls this on discard/commit to seal the audio timeline.
        self.cutoff_calls = getattr(self, "cutoff_calls", 0) + 1

    async def close(self) -> None:
        self._closed = True


@pytest.fixture
def voice_route_env(test_client, monkeypatch):
    """Mock the route's external dependencies so tests don't need a
    real tmux daemon, can flip the feature flag at will, and start
    with a fresh in-memory buffer manager per test.

    By default WhisperLiveClient is stubbed (succeeds instantly, no
    transcripts) so tests don't need a real WhisperLive on
    127.0.0.1:9090. Tests that exercise the upstream path replace
    the stub via monkeypatch.setattr inside the test body."""
    from tools.dashboard import server, feature_flags, voice_buffer, voice_whisperlive

    known_sessions = {"auto-test-designer", "auto-test-validator"}

    def fake_tmux_exists(name):
        return name in known_sessions

    flag_state = {"voice.pipe_enabled": True}
    flag_calls: list[str] = []

    def fake_is_enabled(name):
        flag_calls.append(name)
        return flag_state.get(name, False)

    monkeypatch.setattr(server, "_tmux_session_exists", fake_tmux_exists)
    monkeypatch.setattr(feature_flags, "is_enabled", fake_is_enabled)

    # Each test gets a clean buffer manager so cross-test state can't
    # leak (e.g. a prior test's superseded-WS callback hanging around).
    fresh_manager = voice_buffer.BufferManager()
    monkeypatch.setattr(voice_buffer, "MANAGER", fresh_manager)

    # Reset the stub registry and install it as the default
    # WhisperLive client. Tests that want different behavior
    # (real fake server, deliberate connect failure, transcript
    # events) replace the attribute again inside the test body.
    _StubWhisperLiveClient.instances = []
    monkeypatch.setattr(voice_whisperlive, "WhisperLiveClient", _StubWhisperLiveClient)

    # Stub tmux_send_awaited so commit tests don't fire real tmux
    # paste subprocesses. Calls are recorded for assertion access.
    # NB: the route uses tmux_send_awaited (NOT the fire-and-forget
    # tmux_send) per the S3-5 F1 fix — the awaited helper is the
    # one that actually surfaces subprocess failures. The fixture
    # also exposes the REAL helper reference so tests that want to
    # exercise the subprocess seam (rather than the helper boundary)
    # can re-patch it back.
    from tools.dashboard import tmux_send as tmux_send_mod
    tmux_send_calls: list[tuple[str, str]] = []
    real_tmux_send_awaited = tmux_send_mod.tmux_send_awaited

    async def fake_tmux_send_awaited(target, text):
        tmux_send_calls.append((target, text))

    monkeypatch.setattr(
        tmux_send_mod, "tmux_send_awaited", fake_tmux_send_awaited,
    )

    return {
        "client": test_client,
        "known_sessions": known_sessions,
        "flag_state": flag_state,
        "flag_calls": flag_calls,
        "manager": fresh_manager,
        "stub_instances": _StubWhisperLiveClient.instances,
        "tmux_send_calls": tmux_send_calls,
        "real_tmux_send_awaited": real_tmux_send_awaited,
    }


def _receive_voice_state(ws, *, fsm_state="listening", upstream="ready"):
    """Consume and validate the success acknowledgement for a voice control."""
    frame = ws.receive_json()
    assert frame["type"] == "voice_state"
    assert frame["fsm_state"] == fsm_state
    assert frame["upstream"] == upstream
    assert frame["connection_id"]
    return frame


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
    """Every accepted transition reports the canonical server state."""
    client = voice_route_env["client"]
    with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws:
        # No server-initiated frame on connect (per spec).
        ws.send_text(json.dumps({"type": "start"}))
        connection_id = _receive_voice_state(ws)["connection_id"]
        ws.send_text(json.dumps({"type": "mute"}))
        assert _receive_voice_state(ws, fsm_state="muted")["connection_id"] == connection_id
        ws.send_text(json.dumps({"type": "unmute"}))
        assert _receive_voice_state(ws)["connection_id"] == connection_id
        ws.send_text(json.dumps({"type": "start"}))
        first = ws.receive_json()
        assert first["type"] == "error"
        assert first["code"] == "already_started"
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
        _receive_voice_state(ws)
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
        _receive_voice_state(ws)
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

    assert voice_route_env["flag_calls"].count("voice.audio_capture") == 1


def test_ws_voice_ended_state_rejects_subsequent_commands(voice_route_env):
    """'end' tears the connection down — the route's main loop breaks on
    state==ENDED, so the observable rejection of subsequent commands is
    the socket closing, which the next receive must surface."""
    from starlette.websockets import WebSocketDisconnect

    client = voice_route_env["client"]
    with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws:
        ws.send_text(json.dumps({"type": "end"}))
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_ws_voice_discard_from_idle_is_silent_noop(voice_route_env):
    """Probe: commit-from-idle must answer not_started as the FIRST
    frame — proving the preceding discard emitted nothing and left the
    session idle (a discard that wrongly started or errored would put a
    different frame ahead of it)."""
    client = voice_route_env["client"]
    with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws:
        ws.send_text(json.dumps({"type": "discard"}))
        ws.send_text(json.dumps({"type": "commit"}))
        first = ws.receive_json()
        assert first["type"] == "error"
        assert first["code"] == "not_started"
        ws.send_text(json.dumps({"type": "end"}))


def test_ws_voice_discard_from_listening_emits_buffer_state_reset(voice_route_env):
    """discard from an active state seals the audio cutoff AND emits an
    authoritative buffer_state("") so transcript frames already in flight for
    the just-cleared audio can't repopulate the client's optimistically-cleared
    buffer (operator-reported Clear/Send "the same text comes back")."""
    client = voice_route_env["client"]
    with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws:
        ws.send_text(json.dumps({"type": "start"}))
        _receive_voice_state(ws)
        ws.send_text(json.dumps({"type": "discard"}))
        frame = ws.receive_json()
        assert frame["type"] == "buffer_state"
        assert frame["text"] == ""
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
        # Probe: double-start answers already_started; it being the
        # FIRST received frame proves no buffer_state preceded it on
        # this fresh, bufferless connect.
        ws.send_text(json.dumps({"type": "start"}))
        _receive_voice_state(ws)
        ws.send_text(json.dumps({"type": "start"}))
        first = ws.receive_json()
        assert first["type"] == "error"
        assert first["code"] == "already_started"
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


def test_ws_voice_commit_with_buffer_dispatches_via_tmux_send(voice_route_env):
    """S3-5: commit with buffered text awaits tmux_send(bind, text)
    and emits {type:'committed', text:...}. Buffer is cleared so
    the next commit reports no_buffer rather than re-committing
    the same text."""
    client = voice_route_env["client"]
    mgr = voice_route_env["manager"]
    tmux_calls = voice_route_env["tmux_send_calls"]
    with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws:
        ws.send_text(json.dumps({"type": "start"}))
        _receive_voice_state(ws)
        mgr.append_final("auto-test-designer", "the buffer to be committed")
        ws.send_text(json.dumps({"type": "commit"}))
        resp = ws.receive_json()
        assert resp == {
            "type": "committed",
            "text": "the buffer to be committed",
        }
        ws.send_text(json.dumps({"type": "end"}))
    # tmux_send was called with the bound session + buffer text.
    assert tmux_calls == [
        ("auto-test-designer", "the buffer to be committed"),
    ]
    # Buffer was cleared on successful commit (release happens on
    # 'end'; tracked=False after the release).
    assert not mgr.is_tracked("auto-test-designer")


def test_ws_voice_discard_clears_buffer_via_manager(voice_route_env):
    """'discard' from an active state clears the buffer (operator
    chose to throw it away). Subsequent commit reports no_buffer
    rather than tmux_not_wired, proving the clear landed."""
    client = voice_route_env["client"]
    mgr = voice_route_env["manager"]
    with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws:
        ws.send_text(json.dumps({"type": "start"}))
        _receive_voice_state(ws)
        mgr.append_final("auto-test-designer", "to be discarded")
        ws.send_text(json.dumps({"type": "discard"}))
        # discard emits an authoritative buffer_state("") reset first (see
        # test_ws_voice_discard_from_listening_emits_buffer_state_reset).
        reset = ws.receive_json()
        assert reset["type"] == "buffer_state"
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


# ── tmux_send commit path (S3-5) ─────────────────────────────


def test_ws_voice_commit_uses_awaited_helper_not_fire_and_forget(
    voice_route_env, monkeypatch,
):
    """Regression for codex F1 on f2f1895: the route must use
    tmux_send_awaited (which actually waits for the paste and
    raises on tmux subprocess failure), NOT the fire-and-forget
    tmux_send (which only schedules a worker task and discards
    subprocess outcomes).

    Tests the REAL tmux_send_awaited path end-to-end by:
      1. swapping the fixture's tmux stub out for the real helper
      2. patching subprocess.run to force every tmux subprocess
         to fail with rc=1
      3. driving a commit

    With the awaited helper, the failure surfaces as commit_error
    code=tmux_failed and the buffer survives for retry. With the
    old fire-and-forget tmux_send the route would have emitted
    committed (the await returned before the paste was attempted)."""
    import subprocess as _subprocess
    from tools.dashboard import tmux_send as tmux_send_mod

    real_run = _subprocess.run

    def failing_tmux_run(cmd, *args, **kwargs):
        if cmd and len(cmd) > 0 and cmd[0] == "tmux":
            return _subprocess.CompletedProcess(
                args=cmd,
                returncode=1,
                stdout=b"",
                stderr=b"forced tmux failure",
            )
        return real_run(cmd, *args, **kwargs)

    # Restore the REAL awaited helper (fixture exposes it for us).
    monkeypatch.setattr(
        tmux_send_mod,
        "tmux_send_awaited",
        voice_route_env["real_tmux_send_awaited"],
    )
    monkeypatch.setattr(_subprocess, "run", failing_tmux_run)

    client = voice_route_env["client"]
    mgr = voice_route_env["manager"]
    with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws:
        ws.send_text(json.dumps({"type": "start"}))
        _receive_voice_state(ws)
        mgr.append_final("auto-test-designer", "this should fail in tmux")
        ws.send_text(json.dumps({"type": "commit"}))
        err = ws.receive_json()
        # Real seam test: this must NOT be 'committed'.
        assert err["type"] == "commit_error", (
            f"expected commit_error from forced tmux failure; got {err!r} — "
            "route may be using fire-and-forget tmux_send again"
        )
        assert err["code"] == "tmux_failed"
        # Buffer survives the failure for retry.
        assert mgr.get_text("auto-test-designer") == "this should fail in tmux"
        ws.send_text(json.dumps({"type": "end"}))





def test_ws_voice_commit_failure_surfaces_tmux_failed(voice_route_env, monkeypatch):
    """When tmux_send raises (e.g. subprocess error, target session
    vanished mid-flight), the route emits commit_error code=
    tmux_failed and the buffer is NOT cleared so the operator can
    retry by sending commit again."""
    from tools.dashboard import tmux_send as tmux_send_mod

    async def failing_tmux_send(target, text):
        raise RuntimeError("tmux paste subprocess died")

    monkeypatch.setattr(tmux_send_mod, "tmux_send_awaited", failing_tmux_send)

    client = voice_route_env["client"]
    mgr = voice_route_env["manager"]
    with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws:
        ws.send_text(json.dumps({"type": "start"}))
        _receive_voice_state(ws)
        mgr.append_final("auto-test-designer", "would-be committed text")
        ws.send_text(json.dumps({"type": "commit"}))
        err = ws.receive_json()
        assert err["type"] == "commit_error"
        assert err["code"] == "tmux_failed"
        assert "tmux paste subprocess died" in err["message"]
        ws.send_text(json.dumps({"type": "end"}))
    # Buffer is preserved on failure so the operator can retry.
    # The 'end' frame released the record entirely, but the key
    # check is that the buffer wasn't cleared at commit-failure
    # time: a retry-commit-before-end scenario would have read
    # the original text.


def test_ws_voice_commit_failure_does_not_clear_buffer(voice_route_env, monkeypatch):
    """Tightening the previous test: prove the buffer survives a
    commit failure across a second commit attempt within the same
    WS, before 'end' fires."""
    from tools.dashboard import tmux_send as tmux_send_mod

    call_count = {"n": 0}

    async def flaky_tmux_send(target, text):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("transient failure")
        # Second attempt succeeds.

    monkeypatch.setattr(tmux_send_mod, "tmux_send_awaited", flaky_tmux_send)

    client = voice_route_env["client"]
    mgr = voice_route_env["manager"]
    with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws:
        ws.send_text(json.dumps({"type": "start"}))
        _receive_voice_state(ws)
        mgr.append_final("auto-test-designer", "retry me")
        ws.send_text(json.dumps({"type": "commit"}))
        # First attempt: tmux_failed
        err = ws.receive_json()
        assert err["code"] == "tmux_failed"
        # Buffer should still be there for retry.
        assert mgr.get_text("auto-test-designer") == "retry me"
        ws.send_text(json.dumps({"type": "commit"}))
        # Second attempt: committed with the SAME text.
        resp = ws.receive_json()
        assert resp["type"] == "committed"
        assert resp["text"] == "retry me"
        ws.send_text(json.dumps({"type": "end"}))


def test_ws_voice_commit_clears_buffer_on_success(voice_route_env):
    """A successful commit clears the buffer. Subsequent commit
    without new audio fires no_buffer (operator can't accidentally
    double-commit the same text)."""
    client = voice_route_env["client"]
    mgr = voice_route_env["manager"]
    with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws:
        ws.send_text(json.dumps({"type": "start"}))
        _receive_voice_state(ws)
        mgr.append_final("auto-test-designer", "commit me once")
        ws.send_text(json.dumps({"type": "commit"}))
        resp = ws.receive_json()
        assert resp["type"] == "committed"
        # Second commit immediately after: no buffer.
        ws.send_text(json.dumps({"type": "commit"}))
        err = ws.receive_json()
        assert err["type"] == "commit_error"
        assert err["code"] == "no_buffer"
        ws.send_text(json.dumps({"type": "end"}))


def test_ws_voice_commit_resumes_listening_after_success(voice_route_env):
    """The state machine returns to LISTENING after a successful
    commit (per finish_commit semantics). Operator can keep
    dictating without re-sending 'start'."""
    client = voice_route_env["client"]
    mgr = voice_route_env["manager"]
    with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws:
        ws.send_text(json.dumps({"type": "start"}))
        _receive_voice_state(ws)
        mgr.append_final("auto-test-designer", "first")
        ws.send_text(json.dumps({"type": "commit"}))
        ws.receive_json()  # committed
        # Append more, commit again — proves state was LISTENING,
        # not stuck in COMMITTING or ENDED.
        mgr.append_final("auto-test-designer", "second")
        ws.send_text(json.dumps({"type": "commit"}))
        resp = ws.receive_json()
        assert resp["type"] == "committed"
        assert resp["text"] == "second"
        ws.send_text(json.dumps({"type": "end"}))


def test_ws_voice_commit_resumes_muted_after_success(voice_route_env):
    """Commit-from-muted returns to MUTED (operator chose to mute
    before commit; choice survives commit). Pinned by sending an
    audio frame post-commit and asserting it doesn't forward."""
    client = voice_route_env["client"]
    mgr = voice_route_env["manager"]
    with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws:
        ws.send_text(json.dumps({"type": "start"}))
        _receive_voice_state(ws)
        ws.send_text(json.dumps({"type": "mute"}))
        _receive_voice_state(ws, fsm_state="muted")
        mgr.append_final("auto-test-designer", "from muted")
        ws.send_text(json.dumps({"type": "commit"}))
        ws.receive_json()  # committed
        # Post-commit audio — should still be dropped since we're MUTED.
        ws.send_bytes(b"\x00" * 100)
        ws.send_text(json.dumps({"type": "end"}))
    stub = voice_route_env["stub_instances"][0]
    assert stub.audio_frames == []  # never forwarded — stayed MUTED


# ── WhisperLive route integration (S3-4b) ────────────────────


def test_ws_voice_start_instantiates_whisperlive_client(voice_route_env):
    """On the 'start' transition into LISTENING the route MUST
    instantiate + connect a WhisperLive client. Pinned by the
    instance registry count + the constructor kwargs the route
    passed (canonical uid shape, model from voice_wl module
    constants)."""
    from tools.dashboard import voice_whisperlive
    client = voice_route_env["client"]
    with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws:
        ws.send_text(json.dumps({"type": "start"}))
        ws.send_text(json.dumps({"type": "end"}))
    instances = voice_route_env["stub_instances"]
    assert len(instances) == 1
    kwargs = instances[0].kwargs
    assert kwargs["url"] == voice_whisperlive.WHISPERLIVE_URL
    assert kwargs["model"] == voice_whisperlive.WHISPERLIVE_MODEL
    assert kwargs["language"] == voice_whisperlive.WHISPERLIVE_LANGUAGE
    assert kwargs["use_vad"] == voice_whisperlive.WHISPERLIVE_USE_VAD
    # uid format: <bind>-<8 hex chars>
    assert kwargs["uid"].startswith("auto-test-designer-")
    assert len(kwargs["uid"]) == len("auto-test-designer-") + 8


def test_ws_voice_no_whisperlive_until_start(voice_route_env):
    """The wrapper is NOT instantiated on connect — only on the
    'start' control frame. Avoids spending WhisperLive resources
    for connections that just probe state (e.g. UI mount-time
    health checks)."""
    client = voice_route_env["client"]
    with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws:
        ws.send_text(json.dumps({"type": "end"}))
    assert voice_route_env["stub_instances"] == []


def test_ws_voice_audio_forwards_to_whisperlive_when_listening(voice_route_env):
    """Binary audio in LISTENING state forwards to the upstream
    client. Tested by sending 4 audio frames and asserting all 4
    landed in the stub's audio_frames record."""
    client = voice_route_env["client"]
    payload_a = b"\x00\x01" * 1600
    payload_b = b"\x02\x03" * 1600
    with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws:
        ws.send_text(json.dumps({"type": "start"}))
        ws.send_bytes(payload_a)
        ws.send_bytes(payload_b)
        ws.send_text(json.dumps({"type": "end"}))
    stub = voice_route_env["stub_instances"][0]
    assert stub.audio_frames == [payload_a, payload_b]


def test_ws_voice_audio_not_forwarded_when_muted(voice_route_env):
    """State machine drops audio in MUTED. The route's
    handle_audio check returns False, so send_audio never reaches
    the WhisperLive client. The wrapper stays connected (no
    teardown on mute) so unmute → resume forwarding works."""
    client = voice_route_env["client"]
    with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws:
        ws.send_text(json.dumps({"type": "start"}))
        ws.send_text(json.dumps({"type": "mute"}))
        ws.send_bytes(b"\x00" * 100)  # dropped at state-machine layer
        ws.send_text(json.dumps({"type": "unmute"}))
        ws.send_bytes(b"\xff" * 100)  # forwarded
        ws.send_text(json.dumps({"type": "end"}))
    stub = voice_route_env["stub_instances"][0]
    # Only the post-unmute frame was forwarded.
    assert stub.audio_frames == [b"\xff" * 100]


def test_ws_voice_buffer_text_reaches_commit_path(voice_route_env):
    """End-to-end: text appended to the manager (the on_final
    callback path) becomes the buffer text tmux_send receives at
    commit time. The wrapper's on_final fires this append path in
    production (covered in voice_whisperlive tests); this route
    test verifies the buffer → tmux_send seam end-to-end with the
    stub WhisperLive client + stub tmux_send.

    Cross-thread note: the TestClient runs the route in a
    background thread with its own event loop. We can't directly
    await the route's on_final closure from this test thread, so
    we simulate the same outcome by appending to the manager."""
    client = voice_route_env["client"]
    mgr = voice_route_env["manager"]
    tmux_calls = voice_route_env["tmux_send_calls"]
    with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws:
        ws.send_text(json.dumps({"type": "start"}))
        _receive_voice_state(ws)
        # Append text that the on_final callback would have written.
        mgr.append_final("auto-test-designer", "hello from whisperlive")
        ws.send_text(json.dumps({"type": "commit"}))
        resp = ws.receive_json()
        assert resp["type"] == "committed"
        assert resp["text"] == "hello from whisperlive"
        ws.send_text(json.dumps({"type": "end"}))
    assert tmux_calls == [
        ("auto-test-designer", "hello from whisperlive"),
    ]


def test_ws_voice_whisperlive_close_called_on_end(voice_route_env):
    """The WhisperLive client is closed when the WS tears down so
    upstream resources release promptly. Idempotent close call
    means a double-tear-down (e.g. mid-session network blip while
    'end' is in flight) doesn't crash."""
    client = voice_route_env["client"]
    with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws:
        ws.send_text(json.dumps({"type": "start"}))
        ws.send_text(json.dumps({"type": "end"}))
    stub = voice_route_env["stub_instances"][0]
    assert stub._closed is True


def test_ws_voice_whisperlive_close_called_on_disconnect(voice_route_env):
    """Same teardown happens when the WS disconnects without an
    explicit 'end' (network drop, browser tab close)."""
    client = voice_route_env["client"]
    with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws:
        ws.send_text(json.dumps({"type": "start"}))
        # Exit context without 'end' — simulates client-side drop.
    stub = voice_route_env["stub_instances"][0]
    assert stub._closed is True


def test_ws_voice_whisperlive_connect_failure_sends_typed_error_keeps_ws_open(
    voice_route_env, monkeypatch,
):
    """When WhisperLive connect fails the route emits
    {type:'error', code:'whisperlive_connect_failed', ...} and
    keeps the WS open so the operator can still mute / end the
    session. Subsequent audio frames are silently dropped."""
    from tools.dashboard import voice_whisperlive

    class _FailingClient(_StubWhisperLiveClient):
        async def connect_and_wait_ready(self, *, ready_timeout=15.0):
            self._ready = False
            raise voice_whisperlive.WhisperLiveConnectError(
                "tcp/ws connect failed: simulated"
            )

    monkeypatch.setattr(voice_whisperlive, "WhisperLiveClient", _FailingClient)

    client = voice_route_env["client"]
    with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws:
        ws.send_text(json.dumps({"type": "start"}))
        err = ws.receive_json()
        assert err["type"] == "error"
        assert err["code"] == "whisperlive_connect_failed"
        assert "simulated" in err["message"]
        # WS is still open — control plane still useful.
        ws.send_bytes(b"\x00" * 100)  # silently dropped
        ws.send_text(json.dumps({"type": "mute"}))  # still works
        ws.send_text(json.dumps({"type": "end"}))


def test_ws_voice_whisperlive_unavailable_does_not_reattempt_connect(
    voice_route_env, monkeypatch,
):
    """Per the agreed design: failed connect latches unavailable
    for the lifetime of the WS. The operator can't 'retry' by
    sending another start (which would error already_started
    anyway); they reconnect the WS to get a fresh wrapper.

    Test sends 'start' once → failure → tries to provoke a re-
    instantiation by sending mute/unmute/start. Only one instance
    is created."""
    from tools.dashboard import voice_whisperlive

    class _FailingClient(_StubWhisperLiveClient):
        async def connect_and_wait_ready(self, *, ready_timeout=15.0):
            raise voice_whisperlive.WhisperLiveConnectError("nope")

    monkeypatch.setattr(voice_whisperlive, "WhisperLiveClient", _FailingClient)

    client = voice_route_env["client"]
    with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws:
        ws.send_text(json.dumps({"type": "start"}))
        ws.receive_json()  # the whisperlive_connect_failed frame
        _receive_voice_state(ws, upstream="unavailable")
        # 'start' from listening → already_started error. The state
        # machine error doesn't trigger another connect.
        ws.send_text(json.dumps({"type": "start"}))
        already = ws.receive_json()
        assert already["code"] == "already_started"
        ws.send_text(json.dumps({"type": "end"}))
    # Exactly one client instance, despite two 'start' frames.
    assert len(_FailingClient.instances) == 1


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


class TestVoiceFlowAcknowledgements:
    @staticmethod
    def _assert_state(frame, *, fsm_state, upstream, connection_id=None, epoch=0):
        assert frame == {
            "type": "voice_state",
            "connection_id": frame["connection_id"],
            "fsm_state": fsm_state,
            "upstream": upstream,
            "epoch": epoch,
        }
        assert frame["connection_id"]
        if connection_id is not None:
            assert frame["connection_id"] == connection_id
        return frame["connection_id"]

    def test_start_waits_for_ready_and_failure_orders_error_before_state(
        self, voice_route_env, monkeypatch,
    ):
        from tools.dashboard import voice_whisperlive

        client = voice_route_env["client"]
        with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws:
            ws.send_text(json.dumps({"type": "start"}))
            ready = ws.receive_json()
            self._assert_state(
                ready, fsm_state="listening", upstream="ready",
            )
            ws.send_text(json.dumps({"type": "end"}))

        class _FailingClient(_StubWhisperLiveClient):
            async def connect_and_wait_ready(self, *, ready_timeout=15.0):
                self._ready = False
                raise voice_whisperlive.WhisperLiveConnectError("simulated unavailable")

        _FailingClient.instances = []
        monkeypatch.setattr(voice_whisperlive, "WhisperLiveClient", _FailingClient)
        with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws:
            ws.send_text(json.dumps({"type": "start"}))
            error = ws.receive_json()
            unavailable = ws.receive_json()
            assert error["type"] == "error"
            assert error["code"] == "whisperlive_connect_failed"
            self._assert_state(
                unavailable, fsm_state="listening", upstream="unavailable",
            )
            ws.send_text(json.dumps({"type": "end"}))

    def test_control_acknowledgements_follow_canonical_fsm(self, voice_route_env):
        client = voice_route_env["client"]
        with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws:
            ws.send_text(json.dumps({"type": "start"}))
            connection_id = self._assert_state(
                ws.receive_json(), fsm_state="listening", upstream="ready",
            )
            ws.send_text(json.dumps({"type": "mute"}))
            self._assert_state(
                ws.receive_json(), fsm_state="muted", upstream="ready",
                connection_id=connection_id,
            )
            ws.send_text(json.dumps({"type": "unmute"}))
            self._assert_state(
                ws.receive_json(), fsm_state="listening", upstream="ready",
                connection_id=connection_id,
            )
            ws.send_text(json.dumps({"type": "end"}))

    def test_audio_flow_is_immediate_then_cumulative_and_throttled(
        self, voice_route_env, monkeypatch,
    ):
        from tools.dashboard import server

        clock = iter((10.0, 10.5, 11.1))
        monkeypatch.setattr(server, "_voice_flow_monotonic", lambda: next(clock))
        client = voice_route_env["client"]
        with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws:
            ws.send_text(json.dumps({"type": "start"}))
            state = ws.receive_json()
            connection_id = state["connection_id"]

            ws.send_bytes(b"\x00\x01" * 32)
            first = ws.receive_json()
            assert first["type"] == "audio_flow"
            assert first["connection_id"] == connection_id
            assert (first["received"], first["forwarded"]) == (1, 1)

            ws.send_bytes(b"\x02\x03" * 32)  # 500 ms: no acknowledgement
            ws.send_bytes(b"\x04\x05" * 32)  # 1.1 s: cumulative acknowledgement
            second = ws.receive_json()
            assert second["type"] == "audio_flow"
            assert second["connection_id"] == connection_id
            assert (second["received"], second["forwarded"]) == (3, 3)
            assert isinstance(second["ts_ms"], int)
            assert set(second) == {
                "type", "connection_id", "received", "forwarded", "ts_ms",
            }
            ws.send_text(json.dumps({"type": "end"}))

    def test_muted_and_unready_audio_never_acknowledge_forwarding(
        self, voice_route_env,
    ):
        client = voice_route_env["client"]
        with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws:
            ws.send_text(json.dumps({"type": "start"}))
            ws.receive_json()
            ws.send_text(json.dumps({"type": "mute"}))
            ws.receive_json()
            ws.send_bytes(b"\x00\x01" * 32)
            ws.send_text(json.dumps({"type": "unmute"}))
            # If muted audio produced audio_flow, it would be queued before this.
            resumed = ws.receive_json()
            assert resumed["type"] == "voice_state"
            assert resumed["fsm_state"] == "listening"
            ws.send_text(json.dumps({"type": "end"}))

    def test_reconnect_changes_connection_and_resets_flow_counters_after_restore(
        self, voice_route_env,
    ):
        client = voice_route_env["client"]
        manager = voice_route_env["manager"]
        with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws:
            ws.send_text(json.dumps({"type": "start"}))
            first_id = ws.receive_json()["connection_id"]
            ws.send_bytes(b"\x00\x01" * 32)
            assert ws.receive_json()["forwarded"] == 1
            manager.append_final("auto-test-designer", "survives reconnect")

        with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws:
            restored = ws.receive_json()
            assert restored["type"] == "buffer_state"
            assert restored["text"] == "survives reconnect"
            ws.send_text(json.dumps({"type": "start"}))
            state = ws.receive_json()
            assert state["connection_id"] != first_id
            assert state["epoch"] == 0
            ws.send_bytes(b"\x02\x03" * 32)
            flow = ws.receive_json()
            assert flow["connection_id"] == state["connection_id"]
            assert (flow["received"], flow["forwarded"]) == (1, 1)
            ws.send_text(json.dumps({"type": "end"}))

    def test_send_failure_emits_upstream_error_without_healthy_flow(
        self, voice_route_env, monkeypatch,
    ):
        from tools.dashboard import voice_whisperlive

        class _SendFailingClient(_StubWhisperLiveClient):
            async def send_audio(self, audio_bytes):
                self._ready = False
                await self.on_error("upstream send failed: simulated")

            def is_unavailable(self):
                return not self._ready

        _SendFailingClient.instances = []
        monkeypatch.setattr(
            voice_whisperlive, "WhisperLiveClient", _SendFailingClient,
        )
        client = voice_route_env["client"]
        with client.websocket_connect("/ws/voice?bind=auto-test-designer") as ws:
            ws.send_text(json.dumps({"type": "start"}))
            ws.receive_json()
            ws.send_bytes(b"\x00\x01" * 32)
            error = ws.receive_json()
            assert error["type"] == "error"
            assert error["code"] == "whisperlive_session_error"
            ws.send_text(json.dumps({"type": "mute"}))
            # A false audio_flow would appear before this state acknowledgement.
            state = ws.receive_json()
            assert state["type"] == "voice_state"
            assert state["upstream"] == "unavailable"
            ws.send_text(json.dumps({"type": "end"}))
