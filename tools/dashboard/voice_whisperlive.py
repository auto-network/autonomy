"""Async client wrapper for WhisperLive (S3-4a).

Spec: ``graph://86fd1897-d4d``. One :class:`WhisperLiveClient`
instance per ``/ws/voice`` connection. Encapsulates the upstream
WhisperLive WebSocket protocol so the route layer's only concern is
"connect on start; forward audio when ready; route transcript
callbacks to the buffer + the client".

Upstream protocol (verified against collabora/WhisperLive source):

- Client opens a WS to ``ws://127.0.0.1:9090`` (port configurable).
- Client sends ONE JSON init message: ``{uid, language, task,
  model, use_vad, ...}``. Required fields are pinned in
  :meth:`_build_init_payload`; the spec calls for explicit values
  rather than relying on upstream defaults.
- Client then sends binary PCM frames. The server must have been
  started with ``--raw_pcm_input`` for the frames to be interpreted
  as int16 LE; otherwise it expects float32. We assume raw PCM —
  the systemd unit in S3-1 pins that flag.
- Server emits status messages (``SERVER_READY``, ``WAIT``,
  ``ERROR``, ``DISCONNECT``) and segment payloads. The wrapper
  only forwards binary audio after seeing ``SERVER_READY``;
  before that, ``connect_and_wait_ready`` blocks. Per codex's S3-4
  pushback: this synchronous connect-and-wait shape is preferred
  over background-connect + drop-on-pre-ready, because TCP/WS
  buffering already holds the operator's early frames during the
  cold-start window without needing an app-layer queue.

Dedupe: WhisperLive re-sends completed segments across update
messages. Naively calling on_final for every "completed" segment
would duplicate finals into ``voice_buffer.MANAGER`` and corrupt
the eventual commit text. The wrapper tracks the last emitted
segments by ``(start_ms, text)`` and fires on_final only for
genuinely-new completed segments.

This commit (S3-4a) ships the wrapper + unit tests against a fake
upstream server. S3-4b wires the wrapper into ``ws_voice`` in
``server.py``.
"""

from __future__ import annotations

import array
import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import websockets


log = logging.getLogger(__name__)


# Module-level production config. Tests replace these via
# monkeypatch (or replace ``WhisperLiveClient`` outright with a
# stub class) — keeps the route's client construction site
# unchanged for both production and test paths.
WHISPERLIVE_URL = "ws://127.0.0.1:9090"
WHISPERLIVE_MODEL = "large-v3"
WHISPERLIVE_LANGUAGE = "en"
WHISPERLIVE_USE_VAD = True

# Silence-hallucination gating. Whisper invents filler ("thank you", "you",
# noise like "CH-H-H", "Mm-hmm") during silence/faint noise — a known failure
# mode (trained on captioned video where silence maps to those phrases).
# WhisperLive's defaults are lenient (no_speech_thresh 0.45, Silero VAD
# threshold 0.5). Tighten both: a higher VAD threshold keeps faint noise from
# being treated as speech before the model, and a higher no_speech_thresh
# discards segments the model itself marks as likely-silence. Tunable.
WHISPERLIVE_NO_SPEECH_THRESH = 0.6
WHISPERLIVE_VAD_THRESHOLD = 0.6

# Wire format the dashboard sends on the WhisperLive WS.
#
# - ``float32_le``: float32 little-endian in [-1.0, 1.0]. This is
#   what pip ``whisper-live`` 0.8.0 reads unconditionally
#   (upstream server.py:319 does ``np.frombuffer(frame_data,
#   dtype=np.float32)``). The ``--raw_pcm_input`` flag that would
#   switch the server to int16 only exists in upstream GitHub's
#   ``run_server.py`` wrapper, not in the pip release.
# - ``int16_le``: bandwidth-efficient on the browser → dashboard
#   leg. Operators install upstream + --raw_pcm_input to skip the
#   server-side conversion.
#
# Default ``float32_le`` matches the pip 0.8.0 deployment the
# operator's host is running today.
WHISPERLIVE_WIRE_FORMAT = "float32_le"


# Wire-format constants exported for use as ``wire_format`` kwarg
# on :class:`WhisperLiveClient`. Kept as string literals (not an
# enum) to match the module-level config constant shape.
WIRE_FLOAT32_LE = "float32_le"
WIRE_INT16_LE = "int16_le"


def _convert_int16_le_to_float32_le(audio_bytes: bytes) -> bytes:
    """Convert int16 little-endian PCM bytes to float32 little-endian
    in [-1.0, 1.0].

    Uses the stdlib ``array`` module rather than numpy (which isn't
    in the dashboard image). For a 100ms 16kHz mono frame (1600
    samples / 3200 bytes int16 → 6400 bytes float32) the conversion
    cost is ~1600 float divisions per frame; well under the 10
    frames/sec audio rate.

    Returns float32_le bytes ready to forward to a WhisperLive
    server that expects ``np.frombuffer(..., dtype=np.float32)``.

    Normalises via division by 32768.0 for both signs — equivalent
    to the de-facto Whisper-family int16→float32 normalisation
    pattern (numpy's ``.astype(np.float32) / 32768.0``).
    """
    if not isinstance(audio_bytes, (bytes, bytearray)) or not audio_bytes:
        return b""
    samples = array.array("h")  # 'h' = signed short (int16)
    samples.frombytes(bytes(audio_bytes))
    floats = array.array("f", (s / 32768.0 for s in samples))
    return floats.tobytes()


# Wrapper state enum. Single-line evolution: disconnected ->
# connecting -> ready | unavailable. No back-edges; a wrapper that
# went unavailable stays unavailable until the route discards it
# and instantiates a fresh wrapper on the next operator-driven
# reconnect.
DISCONNECTED = "disconnected"
CONNECTING = "connecting"
READY = "ready"
UNAVAILABLE = "unavailable"


# Upstream status messages we recognise. Anything else falls into
# the segment-payload branch which the dedupe + callback logic
# handles.
_STATUS_SERVER_READY = "SERVER_READY"
_STATUS_DISCONNECT = "DISCONNECT"
_STATUS_ERROR = "ERROR"
_STATUS_WAIT = "WAIT"
_STATUS_WARNING = "WARNING"


class WhisperLiveConnectError(Exception):
    """Raised by :meth:`WhisperLiveClient.connect_and_wait_ready`
    when the TCP / WS handshake fails OR the server emits a fatal
    status (DISCONNECT / ERROR) before SERVER_READY. The caller
    catches this and sends a typed error frame to the operator.
    """


@dataclass
class _EmittedFinal:
    """One completed segment we've already fired on_final for.

    Stored by ``(start_ms, text)`` rather than by index because
    WhisperLive may renumber segments across updates. ``text`` is
    the post-strip transcript so trailing-whitespace differences
    don't bypass the dedupe.
    """

    start_ms: int
    text: str


def _segment_dedupe_key(segment: dict) -> tuple[int, str] | None:
    """Build the dedupe key from a segment payload. Returns ``None``
    when the segment is malformed in a way that makes it unusable
    (no start time, no text) — these are silently dropped rather
    than firing on_error, since the upstream may emit
    placeholder/empty segments before the real transcription lands.
    """
    start_raw = segment.get("start")
    text = segment.get("text")
    if text is None or not isinstance(text, str):
        return None
    try:
        start_ms = int(float(start_raw) * 1000)
    except (TypeError, ValueError):
        return None
    return (start_ms, text.strip())


def _is_completed(segment: dict) -> bool:
    """Upstream uses ``completed: True`` on finalised segments.
    Defensive about absent / non-bool values: only ``is True``
    counts so a missing field can't slip a partial through as
    a final."""
    return segment.get("completed") is True


@dataclass
class WhisperLiveClient:
    """Async client for a single /ws/voice connection's audio
    pipeline through WhisperLive.

    ``model`` is required. Per upstream contract, the server's
    initialisation breaks without it; explicit param rather than
    relying on a default keeps the failure at construction time
    rather than at first server message.

    Callbacks are async — the route awaits them, so an on_final
    that appends to the buffer manager runs in the same task as the
    incoming-message loop.
    """

    url: str
    uid: str
    model: str
    on_partial: Callable[[str], Awaitable[None]]
    on_final: Callable[[str], Awaitable[None]]
    on_error: Callable[[str], Awaitable[None]]
    language: str = "en"
    use_vad: bool = True
    # Silence-hallucination gating, sent in the init payload. Default to the
    # tuned module constants; #29 will pass operator-set values from the
    # dashboard.voice graph setting at connect time.
    no_speech_thresh: float = WHISPERLIVE_NO_SPEECH_THRESH
    vad_threshold: float = WHISPERLIVE_VAD_THRESHOLD
    # Wire format the wrapper produces on the WhisperLive WS. Default
    # ``float32_le`` matches the pip whisper-live 0.8.0 deployment
    # (unconditional np.float32 read in upstream server.py:319);
    # ``int16_le`` is for future upstream+--raw_pcm_input installs.
    # send_audio converts browser-int16 frames to this on the wire.
    wire_format: str = WIRE_FLOAT32_LE
    state: str = field(default=DISCONNECTED, init=False)
    _ws: Any = field(default=None, init=False, repr=False)
    _recv_task: asyncio.Task | None = field(default=None, init=False, repr=False)
    _ready_event: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    _emitted_finals: list[_EmittedFinal] = field(default_factory=list, init=False, repr=False)
    _connect_error: str | None = field(default=None, init=False, repr=False)
    _connect_factory: Any = field(default=None, init=False, repr=False)
    # Audio-timeline cutoff (ms). Segments whose start is < _cutoff_ms are
    # dropped in _dispatch_segments — they're WhisperLive re-emitting audio
    # already consumed by a Send/Clear/Rebind. set_cutoff() seals to the
    # AUDIO clock (_audio_sent_ms: total audio forwarded to WhisperLive), NOT
    # the transcript clock — audio already sent but not yet emitted as a
    # segment at click time must still be sealed, or it repopulates the buffer
    # when WhisperLive transcribes it a beat later. _max_start_ms (latest
    # emitted segment start) is kept as a belt-and-braces lower bound.
    _cutoff_ms: int = field(default=0, init=False, repr=False)
    _max_start_ms: int = field(default=0, init=False, repr=False)
    _audio_sent_ms: float = field(default=0.0, init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model:
            raise ValueError(
                "WhisperLiveClient requires a non-empty `model` "
                "(upstream server initialisation breaks without it)"
            )
        if not isinstance(self.uid, str) or not self.uid:
            raise ValueError("WhisperLiveClient requires a non-empty `uid`")
        if self.wire_format not in (WIRE_FLOAT32_LE, WIRE_INT16_LE):
            raise ValueError(
                f"WhisperLiveClient.wire_format must be {WIRE_FLOAT32_LE!r} "
                f"or {WIRE_INT16_LE!r}; got {self.wire_format!r}"
            )

    # ── Public API ──────────────────────────────────────────

    def is_ready(self) -> bool:
        """True when SERVER_READY has been observed and the wrapper
        is forwarding audio."""
        return self.state == READY

    def is_unavailable(self) -> bool:
        """True when an unrecoverable failure has occurred. The
        route uses this to short-circuit subsequent audio frames
        without re-attempting the connect."""
        return self.state == UNAVAILABLE

    def _build_init_payload(self) -> dict:
        """Pin the init payload shape per S3-4 design + spec.

        Returns the dict to JSON-serialise + send as the first frame
        to the upstream server. Extracted as a method so tests can
        assert on the exact payload shape without having to round-
        trip through json.loads on the fake server.
        """
        return {
            "uid": self.uid,
            "language": self.language,
            "task": "transcribe",
            "model": self.model,
            "use_vad": self.use_vad,
            # Silence-hallucination gating (see constants). These are the
            # DEFAULTS that the planned dashboard.voice graph setting (#29)
            # will override at connect time once it's wired; hard-coded here
            # for now so the operator gets immediate relief.
            "no_speech_thresh": self.no_speech_thresh,
            "vad_parameters": {"threshold": self.vad_threshold},
        }

    async def connect_and_wait_ready(self, *, ready_timeout: float = 15.0) -> None:
        """TCP connect + send init payload + await SERVER_READY.

        Synchronous from the route's perspective — the route awaits
        this during 'start' processing so the next receive loop
        iteration finds a wrapper that's ready (or unavailable).
        TCP/WS buffering holds any client frames that arrive during
        the cold-start window; no app-layer queue needed.

        Raises :class:`WhisperLiveConnectError` on TCP/WS failure,
        on fatal server status (DISCONNECT / ERROR) before
        SERVER_READY, or on timeout. The state transitions to
        UNAVAILABLE on failure so subsequent send_audio calls
        short-circuit silently.
        """
        if self.state != DISCONNECTED:
            raise RuntimeError(
                f"connect_and_wait_ready called in state={self.state!r}; "
                "wrapper is single-shot — instantiate a fresh client per "
                "/ws/voice connection"
            )
        self.state = CONNECTING
        try:
            # The connect factory is exposed for tests to inject a
            # fake transport; production goes through websockets.connect.
            connect_fn = self._connect_factory or websockets.connect
            self._ws = await connect_fn(self.url)
        except Exception as exc:
            self.state = UNAVAILABLE
            raise WhisperLiveConnectError(
                f"tcp/ws connect failed: {exc}"
            ) from exc

        try:
            await self._ws.send(json.dumps(self._build_init_payload()))
        except Exception as exc:
            self.state = UNAVAILABLE
            await self._safe_close_ws()
            raise WhisperLiveConnectError(
                f"init payload send failed: {exc}"
            ) from exc

        # Spawn the receive loop. It will drive the state transition
        # to READY on SERVER_READY, then continue dispatching
        # transcript events via callbacks. Spawning here (rather
        # than after the await below) means messages arriving
        # between our send and the wait don't get dropped on the
        # floor.
        self._recv_task = asyncio.create_task(
            self._recv_loop(), name=f"whisperlive_recv:{self.uid}",
        )

        try:
            await asyncio.wait_for(
                self._ready_event.wait(), timeout=ready_timeout,
            )
        except asyncio.TimeoutError as exc:
            self.state = UNAVAILABLE
            await self._safe_close_ws()
            raise WhisperLiveConnectError(
                f"timed out waiting for SERVER_READY after {ready_timeout:.1f}s"
            ) from exc

        # If the recv loop set _connect_error before ready, surface it.
        # (Race: ERROR arrived right as we transitioned out of the wait.)
        if self.state != READY:
            err = self._connect_error or "ready handshake failed"
            self.state = UNAVAILABLE
            await self._safe_close_ws()
            raise WhisperLiveConnectError(err)

    async def send_audio(self, audio_bytes: bytes) -> None:
        """Forward an audio frame to upstream. No-op when the
        wrapper isn't READY (transient pre-ready frames are
        impossible because of the sync connect-and-wait; this
        guard is a defensive net for the unavailable branch).

        The browser forwards int16 little-endian PCM as the
        bandwidth-efficient on-the-wire format from browser →
        dashboard. This method converts to ``wire_format`` before
        sending to WhisperLive — by default ``float32_le`` to match
        pip ``whisper-live`` 0.8.0 (which reads
        ``np.frombuffer(..., dtype=np.float32)`` unconditionally).
        For future upstream+--raw_pcm_input deployments the wrapper
        is constructed with ``wire_format=WIRE_INT16_LE`` and the
        conversion is skipped.

        Logs only byte counts, never content, per spec privacy rule.
        """
        if self.state != READY or self._ws is None:
            return
        if not isinstance(audio_bytes, (bytes, bytearray)) or not audio_bytes:
            return
        # Conversion is INSIDE the try: an odd/truncated frame fails the
        # int16->float32 conversion (np.frombuffer needs an even length), and
        # that must flow to the same UNAVAILABLE/on_error path as a send
        # failure rather than escaping uncaught. payload is pre-bound so the
        # error log's len(payload) can't NameError when conversion is what threw.
        payload = b""
        try:
            if self.wire_format == WIRE_FLOAT32_LE:
                payload = _convert_int16_le_to_float32_le(audio_bytes)
            else:
                payload = bytes(audio_bytes)
            await self._ws.send(payload)
            # Advance the audio clock by this frame's duration. Browser PCM is
            # 16kHz mono int16 → 2 bytes/sample, 16 samples/ms → 32 bytes/ms.
            # set_cutoff() seals to this so audio sent before a Send/Clear but
            # transcribed after it still gets dropped.
            self._audio_sent_ms += len(audio_bytes) / 32.0
        except Exception as exc:
            log.warning(
                "WhisperLiveClient[%s] send_audio failed bytes_in=%d "
                "bytes_out=%d wire=%s: %s",
                self.uid, len(audio_bytes), len(payload),
                self.wire_format, exc,
            )
            self.state = UNAVAILABLE
            await self.on_error(f"upstream send failed: {exc}")
            await self._safe_close_ws()

    async def close(self) -> None:
        """Idempotent teardown. Cancels the recv loop and closes the
        upstream WS. Safe to call multiple times."""
        if self._recv_task is not None and not self._recv_task.done():
            self._recv_task.cancel()
            try:
                await self._recv_task
            except (asyncio.CancelledError, Exception):
                pass
        self._recv_task = None
        await self._safe_close_ws()
        if self.state != UNAVAILABLE:
            self.state = DISCONNECTED

    # ── Internal: receive loop + segment dedupe ─────────────

    async def _safe_close_ws(self) -> None:
        if self._ws is None:
            return
        try:
            await self._ws.close()
        except Exception:
            pass
        self._ws = None

    async def _recv_loop(self) -> None:
        """Drain messages from the upstream WS until it closes.

        Each text frame is JSON; we dispatch on status messages
        (SERVER_READY → set ready event; DISCONNECT / ERROR →
        unavailable + on_error) and on segment payloads (dedupe +
        fire on_partial / on_final). Unknown shapes are logged at
        debug and ignored — the upstream may add fields the
        wrapper doesn't recognise without that being a bug.

        The loop ends in three ways:

        - asyncio.CancelledError (route called close()): re-raised
          so close() awaits cleanly.
        - Exception (mid-stream WS failure): fires on_error +
          UNAVAILABLE if we were READY.
        - Natural exit (upstream closed cleanly): if we were READY,
          that's still a mid-stream loss from the route's
          perspective — fires on_error + UNAVAILABLE so the
          operator sees the disconnect rather than thinking audio
          is being processed.
        """
        loss_reason: str | None = None
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    # Upstream shouldn't send binary back to us.
                    continue
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    log.debug(
                        "WhisperLiveClient[%s] non-JSON message dropped: %r",
                        self.uid, raw[:80],
                    )
                    continue
                if not isinstance(msg, dict):
                    continue
                await self._dispatch_message(msg)
            loss_reason = "upstream closed connection"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(
                "WhisperLiveClient[%s] recv loop exited: %s", self.uid, exc,
            )
            loss_reason = f"upstream disconnected: {exc}"

        # Either clean-close path or exception path: if we were
        # READY when the loop ended, treat this as mid-stream loss.
        if loss_reason is not None and self.state == READY:
            self.state = UNAVAILABLE
            try:
                await self.on_error(loss_reason)
            except Exception:
                log.exception(
                    "WhisperLiveClient[%s] on_error callback raised",
                    self.uid,
                )

    async def _dispatch_message(self, msg: dict) -> None:
        # Status messages from upstream.
        status = msg.get("message")
        if isinstance(status, str):
            if status == _STATUS_SERVER_READY:
                self.state = READY
                self._ready_event.set()
                return
            if status in (_STATUS_DISCONNECT, _STATUS_ERROR):
                # Fatal — capture for the connect path AND surface
                # via on_error if we were already past ready.
                reason = msg.get("reason") or status
                if not self._ready_event.is_set():
                    self._connect_error = f"{status}: {reason}"
                    # Wake the connect waiter so it can raise.
                    self._ready_event.set()
                else:
                    self.state = UNAVAILABLE
                    try:
                        await self.on_error(f"{status}: {reason}")
                    except Exception:
                        log.exception(
                            "WhisperLiveClient[%s] on_error raised",
                            self.uid,
                        )
                return
            if status in (_STATUS_WAIT, _STATUS_WARNING):
                # Informational — log at debug; no callback.
                log.debug(
                    "WhisperLiveClient[%s] upstream %s: %r",
                    self.uid, status, msg.get("reason"),
                )
                return

        # Segment payload.
        segments = msg.get("segments")
        if isinstance(segments, list):
            await self._dispatch_segments(segments)

    def set_cutoff(self) -> None:
        """Seal everything transcribed so far as consumed.

        WhisperLive re-transcribes a sliding window and re-emits segments
        for audio already spoken; after a Send/Clear/Rebind that re-emit
        repopulates the operator's box with text they already sent or
        cleared. Seals to the AUDIO clock (total audio forwarded to
        WhisperLive), so audio spoken before the click but not yet emitted as
        a segment is also covered — the case the transcript clock alone would
        miss. The latest emitted segment start is a belt-and-braces lower
        bound. Makes :meth:`_dispatch_segments` drop any later segment at or
        before that audio position. Idempotent; only ever advances.
        """
        self._cutoff_ms = max(
            self._cutoff_ms,
            int(self._audio_sent_ms),
            self._max_start_ms + 1,
        )

    async def _dispatch_segments(self, segments: list) -> None:
        """Process a segment array from upstream. Completed segments
        that we haven't already emitted fire on_final and get added
        to the dedupe table. The trailing in-progress segment (if
        any) fires on_partial — there's only one in-flight at a
        time per upstream contract.

        Segments whose start is before the active cutoff (set by a
        Send/Clear/Rebind) are dropped — they are WhisperLive
        re-emitting already-consumed audio and would otherwise
        repopulate the operator's buffer.
        """
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            key = _segment_dedupe_key(seg)
            if key is None:
                continue
            if key[0] > self._max_start_ms:
                self._max_start_ms = key[0]
            if key[0] < self._cutoff_ms:
                # Re-emitted audio from before a Send/Clear/Rebind cutoff.
                continue
            if _is_completed(seg):
                if any(
                    e.start_ms == key[0] and e.text == key[1]
                    for e in self._emitted_finals
                ):
                    # Dedupe — already fired for this exact segment.
                    continue
                self._emitted_finals.append(
                    _EmittedFinal(start_ms=key[0], text=key[1]),
                )
                if key[1]:
                    try:
                        await self.on_final(key[1])
                    except Exception:
                        log.exception(
                            "WhisperLiveClient[%s] on_final raised",
                            self.uid,
                        )
            else:
                if key[1]:
                    try:
                        await self.on_partial(key[1])
                    except Exception:
                        log.exception(
                            "WhisperLiveClient[%s] on_partial raised",
                            self.uid,
                        )
