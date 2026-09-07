"""Voice protocol engine shared by the stable gateway and legacy dashboard.

Dependencies are injected by the hosting application; this module never imports
the dashboard ASGI application. Each socket owns its FSM and WhisperLive client.
"""
import asyncio
import json
import logging
import time
import uuid

from starlette.websockets import WebSocket, WebSocketDisconnect
from tools.dashboard import voice_transcription_settings as _voice_transcription_settings

logger = logging.getLogger("tools.dashboard.server.voice")
_VOICE_INCOMING_MAX_MESSAGES = 64
_VOICE_AUDIO_MAX_FRAME_BYTES = 64 * 1024

async def serve_voice(
    websocket: WebSocket, *, session_exists, commit_text,
    capture_enabled=lambda: False, open_capture=lambda bind: None,
    monotonic=time.monotonic, buffer_owner: str | None = None,
    incoming_max_messages: int = _VOICE_INCOMING_MAX_MESSAGES,
    audio_max_frame_bytes: int = _VOICE_AUDIO_MAX_FRAME_BYTES,
):
    """WebSocket endpoint for the voice pipe canary (S3).

    Spec: ``graph://86fd1897-d4d``. As of S3-4b, the WhisperLive
    audio pipeline is wired: on ``start`` the route opens an
    upstream WhisperLive client (sync connect + SERVER_READY wait
    so TCP/WS buffering absorbs operator audio that arrives during
    the cold-start window), then on each binary frame in LISTENING
    state forwards bytes to upstream. Transcript callbacks route
    finals into :data:`voice_buffer.MANAGER` and surface
    partial+final ``transcript`` frames back to the operator.
    ``tmux_send`` integration on commit lands in S3-5.

    Query params:
      bind — existing tmux session name (required)

    Gate failures close the connection with WebSocket close code
    1008 (Policy Violation):
      - missing ``bind`` query param
      - bound tmux session does not exist
      - ``voice.pipe_enabled`` feature flag is disabled

    Until WhisperLive (S3-4), binary audio frames are silently
    dropped per spec. Until the buffer manager (S3-3) and
    ``tmux_send`` integration (S3-5), ``commit`` actions resolve
    with ``commit_error`` code ``no_buffer`` — operators wiring
    frontends against this stub can exercise the full state machine
    and the error path; the success path becomes reachable in S3-5.
    """
    from tools.dashboard import voice_session as voice_mod
    from tools.dashboard import voice_buffer as voice_buffer_mod
    from tools.dashboard import voice_whisperlive as voice_wl
    from tools.dashboard import feature_flags

    _tmux_session_exists = session_exists
    _voice_audio_capture_enabled = capture_enabled
    _open_voice_audio_capture = open_capture
    _voice_flow_monotonic = monotonic
    await websocket.accept()

    bind = websocket.query_params.get("bind")
    if not bind:
        await websocket.send_json({
            "type": "error",
            "code": "missing_bind",
            "message": "?bind=<tmux_session> query param is required",
        })
        await websocket.close(code=1008, reason="missing bind param")
        return

    # New clients advertise the ordered recovery acknowledgement. Legacy PWA
    # tabs omit this query parameter and retain the old automatic reopen path
    # until they reload the versioned static bundle.
    audio_ready_required = websocket.query_params.get("audio_ack") == "1"

    if not _tmux_session_exists(bind):
        await websocket.send_json({
            "type": "error",
            "code": "session_not_found",
            "message": f"tmux session {bind!r} does not exist",
        })
        await websocket.close(code=1008, reason="bind session not found")
        return

    if not feature_flags.is_enabled("voice.pipe_enabled"):
        await websocket.send_json({
            "type": "error",
            "code": "voice_pipe_disabled",
            "message": (
                "voice.pipe_enabled feature flag is off; enable it via the "
                "Settings UI to use /ws/voice"
            ),
        })
        await websocket.close(code=1008, reason="voice.pipe_enabled disabled")
        return

    # Cross-tab guard + buffer reattach. The evict callback closes
    # THIS WS if a later connection for the same bind arrives. The
    # superseded_event lets the finally block distinguish a normal
    # disconnect (detach buffer for TTL grace) from being kicked
    # (don't touch buffer; the new owner took it).
    superseded_event = asyncio.Event()

    async def _evict_me():
        superseded_event.set()
        try:
            await websocket.close(code=1000, reason="superseded")
        except Exception:
            pass

    buffer_key = json.dumps([buffer_owner, bind]) if buffer_owner else bind
    acq = voice_buffer_mod.MANAGER.acquire(buffer_key, evict_callback=_evict_me)

    if acq.prior_evict_callback is not None:
        # A prior WS owned this bind. Close it (code 1000 'superseded'
        # per spec) before treating ourselves as the active owner.
        try:
            await acq.prior_evict_callback()
        except Exception:
            logger.exception("ws_voice: prior evict failed bind=%s", bind)

    if acq.buffer_text:
        # Restore the in-flight buffer that survived the disconnect.
        # Sent BEFORE any subsequent server frame so the client's
        # buffer state is correct before audio / transcripts resume.
        await websocket.send_json(
            voice_buffer_mod.buffer_state_frame(acq.buffer_text),
        )

    session = voice_mod.VoiceSession(tmux_name=bind)
    connection_id = uuid.uuid4().hex
    end_was_explicit = False
    # WhisperLive client is created on the first 'start' frame
    # (sync connect + SERVER_READY wait), then reused for the
    # lifetime of this WS. If connect fails, whisperlive_unavailable
    # latches True and subsequent 'start' attempts re-send a typed
    # error frame rather than re-attempting connect — operators
    # disconnect+reconnect to retry.
    whisperlive_client: voice_wl.WhisperLiveClient | None = None
    whisperlive_unavailable = False
    # #43: per-connection transcript-acceptance epoch. Bumped on each explicit
    # Send/Clear reset (_handle_voice_reset); every transcript/buffer_state frame
    # carries it so the client drops the re-emit of just-sent/cleared speech still
    # draining from the pre-reset WhisperLive buffer. Stays 0 (harmless) when the
    # voice.reset_suppression flag is off.
    voice_epoch = 0
    audio_received = 0
    audio_forwarded = 0
    last_audio_flow_at: float | None = None
    # Audio intake and processing are deliberately separate. Control work such
    # as reset/reconnect or tmux commit may await for long enough that more
    # browser frames arrive. A serial receive/process loop cannot tell that
    # those frames arrived while capture was suppressed: ASGI queues them and
    # hands them to us only after the await finishes. The receiver below tags
    # each binary frame with the gate state at arrival time, preserving wire
    # order while preventing stale queued audio from becoming "healthy" later.
    audio_intake_open = False
    audio_intake_generation = 0
    pending_audio_ready_token: str | None = None
    incoming: asyncio.Queue[tuple[dict, bool, int, str | None]] = asyncio.Queue(
        maxsize=incoming_max_messages,
    )

    def _close_audio_intake() -> None:
        nonlocal audio_intake_open, audio_intake_generation
        audio_intake_open = False
        audio_intake_generation += 1

    def _sync_audio_intake(
        *, allow_open: bool = False, expected_generation: int | None = None,
    ) -> None:
        nonlocal audio_intake_open
        can_open = bool(
            session.state == voice_mod.LISTENING
            and whisperlive_client is not None
            and whisperlive_client.is_ready()
        )
        if not can_open:
            audio_intake_open = False
        elif (
            allow_open
            and expected_generation is not None
            and audio_intake_generation == expected_generation
        ):
            audio_intake_open = True

    async def _receive_voice_messages() -> None:
        """Continuously receive and tag frames with arrival-time eligibility."""
        nonlocal audio_intake_open, audio_received, pending_audio_ready_token
        try:
            while True:
                msg = await websocket.receive()
                msg_type = msg.get("type")
                accepted_at_intake = False
                boundary_token: str | None = None
                if "bytes" in msg and msg["bytes"] is not None:
                    audio_received += 1
                    audio_bytes = msg["bytes"]
                    accepted_at_intake = bool(
                        audio_bytes
                        and len(audio_bytes) <= audio_max_frame_bytes
                        and audio_intake_open
                    )
                    # Closed/empty/oversized audio is accounted for but never
                    # retained. If the bounded processor queue is saturated,
                    # drop this audio frame rather than removing backpressure
                    # for controls or growing PCM memory without limit.
                    if not accepted_at_intake or incoming.full():
                        continue
                elif "text" in msg and msg["text"] is not None:
                    try:
                        control = json.loads(msg["text"])
                    except Exception:
                        control = None
                    # Close on receipt, not after the potentially blocking
                    # control handler. Start/unmute never open here: only the
                    # canonical processor may reopen after upstream readiness.
                    if (
                        isinstance(control, dict)
                        and control.get("type")
                        in {"mute", "commit", "reset", "end"}
                    ):
                        _close_audio_intake()
                        if control.get("type") in {"commit", "reset"}:
                            boundary_token = uuid.uuid4().hex
                            pending_audio_ready_token = boundary_token
                        else:
                            pending_audio_ready_token = None
                    # Reset/commit reopen only after the browser has observed
                    # the server result. This client acknowledgement is ordered
                    # after every binary frame the browser sent during the
                    # closed interval, so ASGI backlog cannot be reclassified
                    # as post-recovery audio.
                    if isinstance(control, dict) and control.get("type") == "audio_ready":
                        epoch = control.get("epoch")
                        connection = control.get("connection_id")
                        token = control.get("token")
                        if (
                            connection == connection_id
                            and isinstance(token, str)
                            and token == pending_audio_ready_token
                            and isinstance(epoch, int)
                            and not isinstance(epoch, bool)
                            and epoch == voice_epoch
                            and session.state == voice_mod.LISTENING
                            and whisperlive_client is not None
                            and whisperlive_client.is_ready()
                        ):
                            audio_intake_open = True
                            pending_audio_ready_token = None
                        continue
                await incoming.put((
                    msg, accepted_at_intake, audio_intake_generation,
                    boundary_token,
                ))
                if msg_type == "websocket.disconnect":
                    return
        except WebSocketDisconnect:
            await incoming.put((
                {"type": "websocket.disconnect"}, False,
                audio_intake_generation, None,
            ))
        except Exception as exc:
            await incoming.put((
                {"type": "voice.receive.error", "error": exc},
                False, audio_intake_generation, None,
            ))

    def _upstream_state() -> str:
        """Project the existing WhisperLive client into the wire contract."""
        if whisperlive_unavailable:
            return "unavailable"
        if whisperlive_client is not None:
            if whisperlive_client.is_ready():
                return "ready"
            if whisperlive_client.is_unavailable():
                return "unavailable"
        return "not_ready"

    def _voice_state_frame() -> dict:
        return {
            "type": "voice_state",
            "connection_id": connection_id,
            "fsm_state": session.state,
            "upstream": _upstream_state(),
            "epoch": voice_epoch,
        }

    async def _on_partial(text: str) -> None:
        # Partials are operator-visible feedback but not persisted
        # to the buffer (spec: only finals contribute). ts_ms is
        # the wall-clock at callback time; sufficient for client
        # ordering and not pretending to be a more authoritative
        # timestamp than we actually have.
        try:
            await websocket.send_json({
                "type": "transcript",
                "kind": "partial",
                "text": text,
                "ts_ms": int(time.time() * 1000),
                "epoch": voice_epoch,
            })
        except Exception:
            logger.debug("ws_voice: on_partial send failed bind=%s", bind)

    async def _on_final(text: str) -> None:
        # Finals contribute to the buffer (dedupe happened in the
        # wrapper, so this is guaranteed-new text) AND surface to
        # the operator as a transcript:final frame.
        voice_buffer_mod.MANAGER.append_final(buffer_key, text)
        logger.info("ws_voice DIAG: FINAL transcript bind=%s text=%r", bind, text)
        try:
            await websocket.send_json({
                "type": "transcript",
                "kind": "final",
                "text": text,
                "ts_ms": int(time.time() * 1000),
                "epoch": voice_epoch,
            })
        except Exception:
            logger.debug("ws_voice: on_final send failed bind=%s", bind)

    async def _on_whisperlive_error(message: str) -> None:
        nonlocal whisperlive_unavailable
        whisperlive_unavailable = True
        _close_audio_intake()
        try:
            await websocket.send_json({
                "type": "error",
                "code": "whisperlive_session_error",
                "message": message,
            })
        except Exception:
            logger.debug("ws_voice: on_error send failed bind=%s", bind)

    async def _ensure_whisperlive_connected() -> bool:
        """Idempotent: instantiate + connect WhisperLive on the
        first 'start'. Returns True on ready, False on failure
        (typed error frame already sent to operator).

        Subsequent calls on the same WS return whisperlive_client.
        is_ready() — no re-attempt. Operators retry by reconnecting.
        """
        nonlocal whisperlive_client, whisperlive_unavailable
        if whisperlive_client is not None:
            return whisperlive_client.is_ready()
        if whisperlive_unavailable:
            return False
        # Resolve the live-tunable transcription knobs from the
        # ``dashboard.voice.transcription`` graph setting, read fresh per mic
        # connection (falls back to the voice_whisperlive module defaults if
        # the row is absent — never raises). Lets the operator tune the
        # silence-gating thresholds / language / VAD without a code change.
        _tx_cfg = _voice_transcription_settings.resolve_transcription_config()
        whisperlive_client = voice_wl.WhisperLiveClient(
            url=voice_wl.WHISPERLIVE_URL,
            uid=f"{bind}-{uuid.uuid4().hex[:8]}",
            model=_tx_cfg.model,
            language=_tx_cfg.language,
            use_vad=_tx_cfg.use_vad,
            no_speech_thresh=_tx_cfg.no_speech_thresh,
            vad_threshold=_tx_cfg.vad_threshold,
            wire_format=voice_wl.WHISPERLIVE_WIRE_FORMAT,
            on_partial=_on_partial,
            on_final=_on_final,
            on_error=_on_whisperlive_error,
        )
        logger.info("ws_voice DIAG: 'start' received → connecting WhisperLive bind=%s url=%s", bind, voice_wl.WHISPERLIVE_URL)
        try:
            await whisperlive_client.connect_and_wait_ready()
        except voice_wl.WhisperLiveConnectError as exc:
            logger.warning("ws_voice DIAG: WhisperLive connect FAILED bind=%s err=%s", bind, exc)
            whisperlive_unavailable = True
            try:
                await websocket.send_json({
                    "type": "error",
                    "code": "whisperlive_connect_failed",
                    "message": str(exc),
                })
            except Exception:
                pass
            # Wrapper went to UNAVAILABLE inside connect_and_wait_ready;
            # we leave the reference so close() in finally is a no-op
            # rather than re-instantiating.
            return False
        logger.info("ws_voice DIAG: WhisperLive READY bind=%s", bind)
        return True

    async def _handle_voice_reset(
        arrival_generation: int, boundary_token: str | None,
    ) -> None:
        """#43: explicit Send/Clear suppression control. Flag-gated.

        ON (``voice.reset_suppression``): flush the WhisperLive session at the
        source — close + reopen (~15ms), which drops its buffered audio + clock —
        so the just-sent/cleared speech can NEVER re-emit; bump the epoch; and tell
        the client the new epoch via an empty buffer_state. Audio frames that arrive
        during the ~15ms reopen are simply not forwarded (wrapper not ready) — a
        bounded clip, never a hang (proven by voice_whisper_reset_trace.py).

        OFF: behave exactly like a legacy ``discard`` — clear the manager buffer,
        emit buffer_state(""), leave WhisperLive untouched. Reversible by flag.
        """
        nonlocal voice_epoch, whisperlive_client
        voice_buffer_mod.MANAGER.clear(buffer_key)
        if not feature_flags.is_enabled("voice.reset_suppression"):
            frame = voice_buffer_mod.buffer_state_frame("")
            if audio_ready_required and boundary_token:
                frame["audio_ready_token"] = boundary_token
            await websocket.send_json(frame)
            _sync_audio_intake(
                allow_open=not audio_ready_required,
                expected_generation=arrival_generation,
            )
            return
        voice_epoch += 1
        old = whisperlive_client
        whisperlive_client = None
        if old is not None:
            try:
                await old.close()
            except Exception:
                logger.debug("ws_voice: reset close failed bind=%s", bind)
        # Reopen a fresh session (the idempotent helper re-instantiates since we
        # nulled the ref). Buffer + audio clock are gone at the source.
        await _ensure_whisperlive_connected()
        frame = voice_buffer_mod.buffer_state_frame("")
        frame["epoch"] = voice_epoch
        if audio_ready_required and boundary_token:
            frame["audio_ready_token"] = boundary_token
        await websocket.send_json(frame)
        _sync_audio_intake(
            allow_open=not audio_ready_required,
            expected_generation=arrival_generation,
        )
        logger.info("ws_voice DIAG: reset → epoch=%d bind=%s", voice_epoch, bind)

    logger.info(
        "ws_voice: connected bind=%s state=%s restored_buffer_chars=%d",
        bind, session.state, len(acq.buffer_text),
    )
    _audio_frames = 0
    _audio_capture_enabled = _voice_audio_capture_enabled()
    _audio_capture_wav = None   # debug WAV writer (lazily opened if capture flag set)

    receiver_task = asyncio.create_task(_receive_voice_messages())
    try:
        while True:
            (
                msg, accepted_at_intake, arrival_generation, boundary_token,
            ) = await incoming.get()
            msg_type = msg.get("type")
            if msg_type == "websocket.disconnect":
                logger.info(
                    "ws_voice: disconnected bind=%s code=%s reason=%r",
                    bind,
                    msg.get("code"),
                    msg.get("reason", ""),
                )
                break
            if msg_type == "voice.receive.error":
                raise msg["error"]
            if "text" in msg and msg["text"] is not None:
                # #43: 'reset' is the explicit Send/Clear suppression control. It is
                # NOT a state-machine control, so intercept it before
                # parse_control_frame (which would reject the unknown type). The
                # flag gate lives inside _handle_voice_reset (off => legacy discard
                # semantics, no WhisperLive reset).
                try:
                    _ctrl = json.loads(msg["text"])
                except Exception:
                    _ctrl = None
                if isinstance(_ctrl, dict) and _ctrl.get("type") == "reset":
                    await _handle_voice_reset(
                        arrival_generation, boundary_token,
                    )
                    continue
                frame_type, payload = voice_mod.parse_control_frame(msg["text"])
                if frame_type is None:
                    # parse_control_frame already built the error frame.
                    await websocket.send_json(payload)
                    continue
                # 'discard' from an active state clears the buffer.
                # Catch it BEFORE handle_control runs because the
                # state machine intentionally treats discard as a
                # no-op-from-its-perspective (state unchanged); the
                # buffer-clearing side effect lives in the transport.
                if frame_type == "discard" and session.state in voice_mod.ACTIVE_STATES:
                    voice_buffer_mod.MANAGER.clear(buffer_key)
                    # NOTE: we deliberately do NOT seal the WhisperLive audio
                    # timeline (set_cutoff) here. A controlled repro
                    # (tools/dashboard/tests/voice_whisper_repro.py) proved the
                    # cutoff STALLS dictation ~2.6s and hangs 3/4 of the time:
                    # the in-flight segment straddling the clear has no word
                    # timestamps yet, so its partials are dropped wholesale until
                    # the segment finalizes. Clear/Send must not touch the audio
                    # stream — suppression of the just-cleared text is purely
                    # client-side (voice-capture remembers the displayed text and
                    # strips its re-emit). buffer_state("") still wipes immediate
                    # in-flight stragglers; the client handles the later re-emit.
                    await websocket.send_json(
                        voice_buffer_mod.buffer_state_frame("")
                    )
                responses = session.handle_control(frame_type)
                logger.info("ws_voice DIAG: ctrl=%s → state=%s bind=%s", frame_type, session.state, bind)
                for resp in responses:
                    await websocket.send_json(resp)
                control_accepted = not any(
                    resp.get("type") == "error" for resp in responses
                )
                # 'start' from IDLE triggered the LISTENING
                # transition — that's when we connect WhisperLive
                # (sync wait for SERVER_READY so subsequent audio
                # frames find the wrapper ready). Failure sends a
                # typed error frame from inside the helper; the WS
                # stays open so the operator can still mute / end
                # the session cleanly.
                if (
                    frame_type == "start"
                    and control_accepted
                    and session.state == voice_mod.LISTENING
                ):
                    await _ensure_whisperlive_connected()
                    await websocket.send_json(_voice_state_frame())
                    _sync_audio_intake(
                        allow_open=True,
                        expected_generation=arrival_generation,
                    )
                elif frame_type in ("mute", "unmute") and control_accepted:
                    await websocket.send_json(_voice_state_frame())
                    _sync_audio_intake(
                        allow_open=True,
                        expected_generation=arrival_generation,
                    )
                # Real commit path (S3-5): when the state machine
                # transitioned into COMMITTING, read the accumulated
                # buffer and dispatch via tmux_send.
                #
                # Empty buffer → commit_error code=no_buffer (no
                # text to send; operator commit was a no-op).
                #
                # Non-empty buffer → await tmux_send(bind, text).
                # tmux_send is fire-and-forget (schedules a worker
                # task that paste-and-double-Enters); the await
                # returns immediately. We then clear the buffer and
                # emit committed. If tmux_send itself raises (e.g.
                # subprocess error), surface as commit_error
                # code=tmux_failed; the buffer is NOT cleared so
                # the operator can retry by sending commit again.
                if session.state == voice_mod.COMMITTING:
                    pending_text = voice_buffer_mod.MANAGER.get_text(buffer_key)
                    if not pending_text:
                        finish = session.finish_commit(
                            success=False,
                            error_code=voice_mod.COMMIT_ERR_NO_BUFFER,
                            error_message="no buffer accumulated",
                        )
                    else:
                        # Use the AWAITED tmux helper, not the
                        # fire-and-forget tmux_send. The latter only
                        # schedules a worker task — awaiting it tells
                        # us nothing about whether the paste actually
                        # landed. tmux_send_awaited runs the paste +
                        # first Enter inline and raises TmuxSendError
                        # on any non-zero tmux returncode, so the
                        # committed/commit_error frame reflects the
                        # real outcome.
                        try:
                            await commit_text(bind, pending_text)
                        except Exception as exc:
                            logger.exception(
                                "ws_voice: tmux_send_awaited failed bind=%s",
                                bind,
                            )
                            finish = session.finish_commit(
                                success=False,
                                error_code=voice_mod.COMMIT_ERR_TMUX_FAILED,
                                error_message=f"tmux send failed: {exc}",
                            )
                        else:
                            voice_buffer_mod.MANAGER.clear(buffer_key)
                            # As with 'discard': do NOT set_cutoff on commit — it
                            # stalls dictation after Send (proven by the repro
                            # harness). Re-emit suppression of the just-sent text
                            # is client-side; the audio stream is left untouched.
                            finish = session.finish_commit(
                                success=True,
                                committed_text=pending_text,
                            )
                    for resp in finish:
                        if audio_ready_required and boundary_token:
                            resp = dict(resp)
                            resp["audio_ready_token"] = boundary_token
                        await websocket.send_json(resp)
                    _sync_audio_intake(
                        allow_open=not audio_ready_required,
                        expected_generation=arrival_generation,
                    )
                if frame_type == "end":
                    # Explicit operator 'end' — drop the buffer
                    # immediately (no TTL grace), regardless of what
                    # state the session ended up in. The state may
                    # still be COMMITTING (deferred-end latch path
                    # from eb02f95): when finish_commit eventually
                    # transitions to ENDED, the finally block must
                    # still see end_was_explicit=True so it calls
                    # release(), not detach(). Latching here captures
                    # operator intent at the moment they sent the
                    # frame, independent of state-machine timing.
                    end_was_explicit = True
                if session.state == voice_mod.ENDED:
                    break
            elif "bytes" in msg and msg["bytes"] is not None:
                audio_bytes = msg["bytes"]
                if not audio_bytes:
                    continue
                should_forward = (
                    accepted_at_intake and session.handle_audio(audio_bytes)
                )
                _audio_frames += 1
                # Debug: capture the RAW browser PCM (real mic, ambient room tone)
                # to a WAV when enabled at WS start. Never consult Settings from
                # the per-frame path.
                if _audio_capture_wav is None and _audio_capture_enabled:
                    _audio_capture_wav = _open_voice_audio_capture(bind)
                if _audio_capture_wav is not None:
                    try:
                        _audio_capture_wav.writeframes(audio_bytes)
                    except Exception:
                        pass
                if (
                    should_forward
                    and whisperlive_client is not None
                    and whisperlive_client.is_ready()
                ):
                    was_forwarded = await whisperlive_client.send_audio(audio_bytes)
                    # send_audio reports the actual upstream write. Readiness
                    # alone is insufficient: empty/invalid frames may be a
                    # deliberate no-op while the wrapper remains READY.
                    if was_forwarded:
                        audio_forwarded += 1
                        now = _voice_flow_monotonic()
                        if (
                            last_audio_flow_at is None
                            or now - last_audio_flow_at >= 1.0
                        ):
                            await websocket.send_json({
                                "type": "audio_flow",
                                "connection_id": connection_id,
                                "received": audio_received,
                                "forwarded": audio_forwarded,
                                "ts_ms": int(time.time() * 1000),
                            })
                            last_audio_flow_at = now
                    elif not whisperlive_client.is_ready():
                        _close_audio_intake()
                # else: state machine said no (muted / committing /
                # ended) or wrapper not ready / unavailable.
                # Silently drop — spec says audio outside LISTENING
                # is dropped without an error frame, and the
                # whisperlive_connect_failed / whisperlive_session_error
                # frame already informed the operator if the upstream
                # is the reason.
    except WebSocketDisconnect as exc:
        logger.info(
            "ws_voice: disconnected bind=%s code=%s reason=%r",
            bind,
            getattr(exc, "code", None),
            getattr(exc, "reason", ""),
        )
    except Exception:
        logger.exception("ws_voice: unexpected error bind=%s", bind)
    finally:
        if not receiver_task.done():
            receiver_task.cancel()
            try:
                await receiver_task
            except (asyncio.CancelledError, Exception):
                pass
        if _audio_capture_wav is not None:
            try:
                _audio_capture_wav.close()
                logger.info("ws_voice: AUDIO CAPTURE closed (%d frames) bind=%s", _audio_frames, bind)
            except Exception:
                pass
        session.force_end()
        if whisperlive_client is not None:
            # Idempotent; safe to call even if already torn down.
            # Awaited so the recv-loop task finishes before the
            # route returns and the test fixture's event loop can
            # reach quiescence.
            try:
                await whisperlive_client.close()
            except Exception:
                logger.debug("ws_voice: whisperlive close failed bind=%s", bind)
        if superseded_event.is_set():
            # We were kicked by a later connection — that connection
            # already owns the buffer. Don't detach (would clear the
            # new owner's evict callback) or release (would drop
            # their buffer). Just close our socket and return.
            pass
        elif end_was_explicit:
            voice_buffer_mod.MANAGER.release(buffer_key)
        else:
            voice_buffer_mod.MANAGER.detach(buffer_key)
        try:
            await websocket.close()
        except Exception:
            pass
