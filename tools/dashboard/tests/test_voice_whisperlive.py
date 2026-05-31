"""Tests for ``tools.dashboard.voice_whisperlive`` (S3-4a).

Spec: ``graph://86fd1897-d4d``. Each test boots a tiny in-process
fake WhisperLive WebSocket server, drives the wrapper against it,
and asserts on (a) the init payload shape, (b) the connect-and-wait
ready handshake, (c) audio frame forwarding (bytes preserved
verbatim), (d) segment dedupe across re-sent completed segments,
(e) partial vs final callback dispatch, (f) failure modes (timeout,
DISCONNECT, ERROR, mid-stream disconnect).

The fake server uses the ``websockets`` library's server API and
listens on a port the OS assigns at bind time, so concurrent test
runs don't collide.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
import pytest_asyncio
import websockets

from tools.dashboard import voice_whisperlive as vwl


class _FakeWhisperLive:
    """In-process WhisperLive impersonator.

    The fixture instantiates this, calls ``start`` to bind a server
    on an ephemeral port, exposes ``.url`` for the wrapper to
    connect to, and the test orchestrates the server's behavior
    via the ``on_connect`` async callable. The callable receives
    the WS connection and the parsed init payload; tests use it to
    send SERVER_READY, segments, ERROR, etc., in the order the
    scenario requires.
    """

    def __init__(self, on_connect):
        self._on_connect = on_connect
        self._server = None
        self.url: str = ""
        self.received_inits: list[dict] = []
        self.received_audio_frames: list[bytes] = []

    async def start(self) -> None:
        async def handler(ws):
            # First message is the init payload.
            init_raw = await ws.recv()
            init = json.loads(init_raw)
            self.received_inits.append(init)
            await self._on_connect(ws, init, self)

        self._server = await websockets.serve(handler, "127.0.0.1", 0)
        port = list(self._server.sockets)[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}"

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


@pytest_asyncio.fixture
async def fake_whisperlive():
    """Fixture that boots and tears down the fake WhisperLive."""
    instances: list[_FakeWhisperLive] = []

    async def make(on_connect):
        fake = _FakeWhisperLive(on_connect)
        await fake.start()
        instances.append(fake)
        return fake

    yield make
    for fake in instances:
        await fake.stop()


def _new_client(url: str, *, uid="test-uid", model="large-v3", **kwargs):
    """Convenience wrapper builder with sensible defaults + sink
    callbacks (tests override via kwargs as needed)."""
    return vwl.WhisperLiveClient(
        url=url,
        uid=uid,
        model=model,
        on_partial=kwargs.pop("on_partial", _noop_async),
        on_final=kwargs.pop("on_final", _noop_async),
        on_error=kwargs.pop("on_error", _noop_async),
        **kwargs,
    )


async def _noop_async(*args, **kwargs):
    pass


def _capture_async(bucket: list):
    """Return an async callback that appends every call to bucket."""
    async def cb(*args, **kwargs):
        bucket.append(args[0] if args else None)
    return cb


# ── Constructor validation ──────────────────────────────────


def test_client_requires_non_empty_model():
    with pytest.raises(ValueError, match="model"):
        vwl.WhisperLiveClient(
            url="ws://127.0.0.1:1",
            uid="u",
            model="",
            on_partial=_noop_async,
            on_final=_noop_async,
            on_error=_noop_async,
        )


def test_client_requires_non_empty_uid():
    with pytest.raises(ValueError, match="uid"):
        vwl.WhisperLiveClient(
            url="ws://127.0.0.1:1",
            uid="",
            model="large-v3",
            on_partial=_noop_async,
            on_final=_noop_async,
            on_error=_noop_async,
        )


def test_init_payload_shape():
    """Pinned per S3-4 design: uid, language, task, model, use_vad, plus the
    silence-hallucination gating (no_speech_thresh + vad_parameters) sent so
    WhisperLive doesn't run on its lenient defaults (the constants are the
    defaults #29's graph setting will later override)."""
    client = _new_client("ws://x", uid="u1", model="large-v3")
    payload = client._build_init_payload()
    assert payload == {
        "uid": "u1",
        "language": "en",
        "task": "transcribe",
        "model": "large-v3",
        "use_vad": True,
        "no_speech_thresh": vwl.WHISPERLIVE_NO_SPEECH_THRESH,
        "vad_parameters": {"threshold": vwl.WHISPERLIVE_VAD_THRESHOLD},
    }


def test_init_payload_threads_optional_overrides():
    client = _new_client(
        "ws://x", uid="u1", model="m", language="ja",
    )
    client.use_vad = False
    payload = client._build_init_payload()
    assert payload["language"] == "ja"
    assert payload["use_vad"] is False


# ── connect_and_wait_ready: success path ─────────────────────


@pytest.mark.asyncio
async def test_connect_succeeds_on_server_ready(fake_whisperlive):
    async def on_connect(ws, init, fake):
        await ws.send(json.dumps({"message": "SERVER_READY"}))
        # Keep the WS open so the test's close() teardown works.
        try:
            await asyncio.wait_for(ws.wait_closed(), timeout=5.0)
        except asyncio.TimeoutError:
            pass

    fake = await fake_whisperlive(on_connect)
    client = _new_client(fake.url)
    await client.connect_and_wait_ready(ready_timeout=2.0)
    assert client.state == vwl.READY
    assert client.is_ready() is True
    assert client.is_unavailable() is False
    # Server received the pinned init payload.
    assert fake.received_inits[0] == client._build_init_payload()
    await client.close()


@pytest.mark.asyncio
async def test_connect_raises_on_tcp_connect_failure():
    # Point at a port that's certain to be closed.
    client = _new_client("ws://127.0.0.1:1")
    with pytest.raises(vwl.WhisperLiveConnectError, match="tcp/ws connect failed"):
        await client.connect_and_wait_ready(ready_timeout=2.0)
    assert client.state == vwl.UNAVAILABLE
    assert client.is_unavailable() is True


@pytest.mark.asyncio
async def test_connect_raises_on_server_error_before_ready(fake_whisperlive):
    async def on_connect(ws, init, fake):
        await ws.send(json.dumps({
            "message": "ERROR",
            "reason": "model not found",
        }))
        try:
            await asyncio.wait_for(ws.wait_closed(), timeout=5.0)
        except asyncio.TimeoutError:
            pass

    fake = await fake_whisperlive(on_connect)
    client = _new_client(fake.url)
    with pytest.raises(vwl.WhisperLiveConnectError, match="ERROR.*model not found"):
        await client.connect_and_wait_ready(ready_timeout=2.0)
    assert client.is_unavailable() is True


@pytest.mark.asyncio
async def test_connect_raises_on_disconnect_before_ready(fake_whisperlive):
    async def on_connect(ws, init, fake):
        await ws.send(json.dumps({
            "message": "DISCONNECT",
            "reason": "server overloaded",
        }))

    fake = await fake_whisperlive(on_connect)
    client = _new_client(fake.url)
    with pytest.raises(vwl.WhisperLiveConnectError, match="DISCONNECT"):
        await client.connect_and_wait_ready(ready_timeout=2.0)


@pytest.mark.asyncio
async def test_connect_raises_on_ready_timeout(fake_whisperlive):
    async def on_connect(ws, init, fake):
        # Server accepts the connect + init but never sends SERVER_READY.
        try:
            await asyncio.wait_for(ws.wait_closed(), timeout=5.0)
        except asyncio.TimeoutError:
            pass

    fake = await fake_whisperlive(on_connect)
    client = _new_client(fake.url)
    with pytest.raises(vwl.WhisperLiveConnectError, match="timed out"):
        await client.connect_and_wait_ready(ready_timeout=0.3)
    assert client.is_unavailable() is True


@pytest.mark.asyncio
async def test_connect_twice_raises_runtimeerror(fake_whisperlive):
    """Wrapper is single-shot — the route instantiates a fresh
    client per /ws/voice connection. Calling connect twice on the
    same instance is a transport bug."""
    async def on_connect(ws, init, fake):
        await ws.send(json.dumps({"message": "SERVER_READY"}))
        try:
            await asyncio.wait_for(ws.wait_closed(), timeout=5.0)
        except asyncio.TimeoutError:
            pass

    fake = await fake_whisperlive(on_connect)
    client = _new_client(fake.url)
    await client.connect_and_wait_ready(ready_timeout=2.0)
    with pytest.raises(RuntimeError, match="single-shot"):
        await client.connect_and_wait_ready(ready_timeout=2.0)
    await client.close()


# ── send_audio: byte-preservation + state gates ─────────────


@pytest.mark.asyncio
async def test_send_audio_default_emits_one_frame_per_call(fake_whisperlive):
    """Each send_audio call produces exactly one outbound frame.
    The frame's content depends on wire_format (covered by the
    dedicated conversion tests below); this test only pins the
    1:1 send-to-emit ratio + that the wrapper is forwarding under
    default config."""
    received = []

    async def on_connect(ws, init, fake):
        await ws.send(json.dumps({"message": "SERVER_READY"}))
        async for msg in ws:
            if isinstance(msg, (bytes, bytearray)):
                received.append(bytes(msg))

    fake = await fake_whisperlive(on_connect)
    client = _new_client(fake.url)
    await client.connect_and_wait_ready(ready_timeout=2.0)
    payload_a = b"\x00\x01\x02\x03" * 800  # 3.2KB ~= 100ms 16kHz int16
    payload_b = b"\xff\xfe\xfd\xfc" * 400
    await client.send_audio(payload_a)
    await client.send_audio(payload_b)
    await asyncio.sleep(0.05)
    await client.close()
    assert len(received) == 2  # 1:1 send-to-emit


@pytest.mark.asyncio
async def test_send_audio_silently_dropped_when_not_ready():
    """Defensive guard: wrapper outside READY (e.g. UNAVAILABLE
    after a connect failure) must not crash on send_audio."""
    client = _new_client("ws://127.0.0.1:1")
    # No connect attempted — state remains DISCONNECTED.
    await client.send_audio(b"\x00" * 100)
    # Still DISCONNECTED; nothing thrown.
    assert client.state == vwl.DISCONNECTED


@pytest.mark.asyncio
async def test_send_audio_drops_empty_bytes(fake_whisperlive):
    received = []

    async def on_connect(ws, init, fake):
        await ws.send(json.dumps({"message": "SERVER_READY"}))
        async for msg in ws:
            if isinstance(msg, (bytes, bytearray)):
                received.append(bytes(msg))

    fake = await fake_whisperlive(on_connect)
    client = _new_client(fake.url)
    await client.connect_and_wait_ready(ready_timeout=2.0)
    await client.send_audio(b"")
    await asyncio.sleep(0.05)
    await client.close()
    assert received == []


# ── Transcript callbacks: partial / final ───────────────────


@pytest.mark.asyncio
async def test_completed_segment_fires_on_final(fake_whisperlive):
    finals: list[str] = []

    async def on_connect(ws, init, fake):
        await ws.send(json.dumps({"message": "SERVER_READY"}))
        await ws.send(json.dumps({
            "segments": [
                {"start": 0.0, "end": 1.5, "text": "hello world",
                 "completed": True},
            ],
        }))
        try:
            await asyncio.wait_for(ws.wait_closed(), timeout=2.0)
        except asyncio.TimeoutError:
            pass

    fake = await fake_whisperlive(on_connect)
    client = _new_client(fake.url, on_final=_capture_async(finals))
    await client.connect_and_wait_ready(ready_timeout=2.0)
    await asyncio.sleep(0.1)
    await client.close()
    assert finals == ["hello world"]


@pytest.mark.asyncio
async def test_in_progress_segment_fires_on_partial(fake_whisperlive):
    partials: list[str] = []

    async def on_connect(ws, init, fake):
        await ws.send(json.dumps({"message": "SERVER_READY"}))
        await ws.send(json.dumps({
            "segments": [
                {"start": 0.0, "end": 0.8, "text": "in progress",
                 "completed": False},
            ],
        }))
        try:
            await asyncio.wait_for(ws.wait_closed(), timeout=2.0)
        except asyncio.TimeoutError:
            pass

    fake = await fake_whisperlive(on_connect)
    client = _new_client(fake.url, on_partial=_capture_async(partials))
    await client.connect_and_wait_ready(ready_timeout=2.0)
    await asyncio.sleep(0.1)
    await client.close()
    assert partials == ["in progress"]


@pytest.mark.asyncio
async def test_final_dedupe_across_resent_segments(fake_whisperlive):
    """Critical S3-4 contract: upstream re-sends completed segments
    in subsequent updates. Without dedupe, the buffer would
    accumulate duplicates and the eventual committed text would be
    garbage. We re-send the same completed segment three times and
    assert on_final fires exactly once."""
    finals: list[str] = []

    async def on_connect(ws, init, fake):
        await ws.send(json.dumps({"message": "SERVER_READY"}))
        seg = {"start": 0.0, "end": 1.0, "text": "once and only once",
               "completed": True}
        for _ in range(3):
            await ws.send(json.dumps({"segments": [seg]}))
        try:
            await asyncio.wait_for(ws.wait_closed(), timeout=2.0)
        except asyncio.TimeoutError:
            pass

    fake = await fake_whisperlive(on_connect)
    client = _new_client(fake.url, on_final=_capture_async(finals))
    await client.connect_and_wait_ready(ready_timeout=2.0)
    await asyncio.sleep(0.1)
    await client.close()
    assert finals == ["once and only once"]


@pytest.mark.asyncio
async def test_final_dedupe_distinguishes_segments_by_start_time(fake_whisperlive):
    """Two completed segments with identical text but different
    start times are TWO finals — they're not the same utterance."""
    finals: list[str] = []

    async def on_connect(ws, init, fake):
        await ws.send(json.dumps({"message": "SERVER_READY"}))
        await ws.send(json.dumps({
            "segments": [
                {"start": 0.0, "end": 1.0, "text": "yes",
                 "completed": True},
                {"start": 2.0, "end": 3.0, "text": "yes",
                 "completed": True},
            ],
        }))
        try:
            await asyncio.wait_for(ws.wait_closed(), timeout=2.0)
        except asyncio.TimeoutError:
            pass

    fake = await fake_whisperlive(on_connect)
    client = _new_client(fake.url, on_final=_capture_async(finals))
    await client.connect_and_wait_ready(ready_timeout=2.0)
    await asyncio.sleep(0.1)
    await client.close()
    assert finals == ["yes", "yes"]


@pytest.mark.asyncio
async def test_final_dedupe_distinguishes_corrected_text_at_same_start(fake_whisperlive):
    """Upstream sometimes refines a completed segment's text (later
    pass picks up a clearer transcription). Different text at same
    start → fires a new on_final, not a dedupe skip. Operator gets
    both texts in the buffer; commit picks them up together. This
    is a tradeoff vs. 'last write wins' — but the route doesn't
    have visibility into which is the corrected version, and
    duplicating is recoverable; dropping the corrected version
    isn't."""
    finals: list[str] = []

    async def on_connect(ws, init, fake):
        await ws.send(json.dumps({"message": "SERVER_READY"}))
        await ws.send(json.dumps({
            "segments": [
                {"start": 0.0, "end": 1.0, "text": "helo wrld",
                 "completed": True},
            ],
        }))
        await ws.send(json.dumps({
            "segments": [
                {"start": 0.0, "end": 1.0, "text": "hello world",
                 "completed": True},
            ],
        }))
        try:
            await asyncio.wait_for(ws.wait_closed(), timeout=2.0)
        except asyncio.TimeoutError:
            pass

    fake = await fake_whisperlive(on_connect)
    client = _new_client(fake.url, on_final=_capture_async(finals))
    await client.connect_and_wait_ready(ready_timeout=2.0)
    await asyncio.sleep(0.1)
    await client.close()
    assert finals == ["helo wrld", "hello world"]


@pytest.mark.asyncio
async def test_partial_then_final_with_same_start_both_fire(fake_whisperlive):
    """Operator sees the partial transcript first; the final fires
    when the segment completes. Both should reach their respective
    callbacks even when they share a start time (the typical case
    — finalising the in-flight partial)."""
    partials, finals = [], []

    async def on_connect(ws, init, fake):
        await ws.send(json.dumps({"message": "SERVER_READY"}))
        await ws.send(json.dumps({
            "segments": [
                {"start": 0.0, "end": 0.5, "text": "hello",
                 "completed": False},
            ],
        }))
        await ws.send(json.dumps({
            "segments": [
                {"start": 0.0, "end": 1.0, "text": "hello world",
                 "completed": True},
            ],
        }))
        try:
            await asyncio.wait_for(ws.wait_closed(), timeout=2.0)
        except asyncio.TimeoutError:
            pass

    fake = await fake_whisperlive(on_connect)
    client = _new_client(
        fake.url,
        on_partial=_capture_async(partials),
        on_final=_capture_async(finals),
    )
    await client.connect_and_wait_ready(ready_timeout=2.0)
    await asyncio.sleep(0.1)
    await client.close()
    assert partials == ["hello"]
    assert finals == ["hello world"]


@pytest.mark.asyncio
async def test_segment_with_no_text_or_no_start_dropped(fake_whisperlive):
    """Upstream may emit placeholder segments early in transcription
    (no text yet). The wrapper drops them silently — no on_final,
    no on_error. Tests both 'no text field' and 'no start field'."""
    finals: list[str] = []

    async def on_connect(ws, init, fake):
        await ws.send(json.dumps({"message": "SERVER_READY"}))
        await ws.send(json.dumps({
            "segments": [
                {"start": 0.0, "end": 0.5, "completed": True},  # no text
                {"text": "no start", "completed": True},        # no start
                {"start": "bogus", "end": 1.0, "text": "x",
                 "completed": True},                            # bad start
                {"start": 1.0, "end": 2.0, "text": "real",
                 "completed": True},
            ],
        }))
        try:
            await asyncio.wait_for(ws.wait_closed(), timeout=2.0)
        except asyncio.TimeoutError:
            pass

    fake = await fake_whisperlive(on_connect)
    client = _new_client(fake.url, on_final=_capture_async(finals))
    await client.connect_and_wait_ready(ready_timeout=2.0)
    await asyncio.sleep(0.1)
    await client.close()
    assert finals == ["real"]


@pytest.mark.asyncio
async def test_completed_with_non_bool_completed_field_treated_as_partial(fake_whisperlive):
    """Defensive: `completed` must be `is True`, not just truthy.
    A string 'true' or integer 1 falls into the partial branch.
    Without this, a malformed upstream could slip non-finals into
    the buffer."""
    partials, finals = [], []

    async def on_connect(ws, init, fake):
        await ws.send(json.dumps({"message": "SERVER_READY"}))
        await ws.send(json.dumps({
            "segments": [
                {"start": 0.0, "end": 0.5, "text": "x",
                 "completed": 1},
                {"start": 1.0, "end": 1.5, "text": "y",
                 "completed": "true"},
            ],
        }))
        try:
            await asyncio.wait_for(ws.wait_closed(), timeout=2.0)
        except asyncio.TimeoutError:
            pass

    fake = await fake_whisperlive(on_connect)
    client = _new_client(
        fake.url,
        on_partial=_capture_async(partials),
        on_final=_capture_async(finals),
    )
    await client.connect_and_wait_ready(ready_timeout=2.0)
    await asyncio.sleep(0.1)
    await client.close()
    assert finals == []
    assert partials == ["x", "y"]


# ── Mid-stream failure: on_error + UNAVAILABLE state ────────


@pytest.mark.asyncio
async def test_mid_stream_server_error_fires_on_error(fake_whisperlive):
    errors: list[str] = []

    async def on_connect(ws, init, fake):
        await ws.send(json.dumps({"message": "SERVER_READY"}))
        await asyncio.sleep(0.05)
        await ws.send(json.dumps({
            "message": "ERROR", "reason": "transcription died",
        }))
        try:
            await asyncio.wait_for(ws.wait_closed(), timeout=2.0)
        except asyncio.TimeoutError:
            pass

    fake = await fake_whisperlive(on_connect)
    client = _new_client(fake.url, on_error=_capture_async(errors))
    await client.connect_and_wait_ready(ready_timeout=2.0)
    # Give recv loop time to dispatch the ERROR message.
    await asyncio.sleep(0.15)
    assert client.state == vwl.UNAVAILABLE
    assert any("transcription died" in e for e in errors)
    await client.close()


@pytest.mark.asyncio
async def test_mid_stream_disconnect_fires_on_error(fake_whisperlive):
    errors: list[str] = []

    async def on_connect(ws, init, fake):
        await ws.send(json.dumps({"message": "SERVER_READY"}))
        await asyncio.sleep(0.05)
        # Hard close — recv loop should catch the disconnect and
        # fire on_error.
        await ws.close()

    fake = await fake_whisperlive(on_connect)
    client = _new_client(fake.url, on_error=_capture_async(errors))
    await client.connect_and_wait_ready(ready_timeout=2.0)
    await asyncio.sleep(0.2)
    assert client.state == vwl.UNAVAILABLE
    assert len(errors) >= 1
    await client.close()


# ── close: idempotent + cancels recv loop ───────────────────


@pytest.mark.asyncio
async def test_close_is_idempotent(fake_whisperlive):
    async def on_connect(ws, init, fake):
        await ws.send(json.dumps({"message": "SERVER_READY"}))
        try:
            await asyncio.wait_for(ws.wait_closed(), timeout=2.0)
        except asyncio.TimeoutError:
            pass

    fake = await fake_whisperlive(on_connect)
    client = _new_client(fake.url)
    await client.connect_and_wait_ready(ready_timeout=2.0)
    await client.close()
    await client.close()  # second close must not raise
    assert client._recv_task is None


# ── Malformed upstream messages ─────────────────────────────


@pytest.mark.asyncio
async def test_non_json_message_silently_dropped(fake_whisperlive):
    finals: list[str] = []

    async def on_connect(ws, init, fake):
        await ws.send(json.dumps({"message": "SERVER_READY"}))
        await ws.send("not json")
        await ws.send(json.dumps({
            "segments": [
                {"start": 0.0, "end": 1.0, "text": "real",
                 "completed": True},
            ],
        }))
        try:
            await asyncio.wait_for(ws.wait_closed(), timeout=2.0)
        except asyncio.TimeoutError:
            pass

    fake = await fake_whisperlive(on_connect)
    client = _new_client(fake.url, on_final=_capture_async(finals))
    await client.connect_and_wait_ready(ready_timeout=2.0)
    await asyncio.sleep(0.1)
    await client.close()
    # The non-JSON didn't crash the loop; real segment still landed.
    assert finals == ["real"]


@pytest.mark.asyncio
async def test_unknown_status_messages_are_informational(fake_whisperlive):
    """WAIT / WARNING shouldn't crash or call on_error. Just log."""
    errors: list[str] = []

    async def on_connect(ws, init, fake):
        await ws.send(json.dumps({"message": "WAIT",
                                  "reason": "model loading"}))
        await ws.send(json.dumps({"message": "WARNING",
                                  "reason": "high latency"}))
        await ws.send(json.dumps({"message": "SERVER_READY"}))
        try:
            await asyncio.wait_for(ws.wait_closed(), timeout=2.0)
        except asyncio.TimeoutError:
            pass

    fake = await fake_whisperlive(on_connect)
    client = _new_client(fake.url, on_error=_capture_async(errors))
    await client.connect_and_wait_ready(ready_timeout=2.0)
    await asyncio.sleep(0.05)
    await client.close()
    assert errors == []
    assert client.state in (vwl.READY, vwl.DISCONNECTED)


# ── Segment dedupe key helper ───────────────────────────────


def test_segment_dedupe_key_basic():
    key = vwl._segment_dedupe_key(
        {"start": 1.5, "end": 2.5, "text": "  hello  "}
    )
    assert key == (1500, "hello")


def test_segment_dedupe_key_no_text_returns_none():
    assert vwl._segment_dedupe_key({"start": 0.0}) is None


def test_segment_dedupe_key_no_start_returns_none():
    assert vwl._segment_dedupe_key({"text": "x"}) is None


def test_segment_dedupe_key_bad_start_returns_none():
    assert vwl._segment_dedupe_key({"start": "bogus", "text": "x"}) is None


# ── Wire-format conversion ──────────────────────────────────


import array  # noqa: E402  (test-local import, scoped to this section)


def _samples_to_int16_bytes(samples):
    """Build int16 little-endian bytes from a list of signed ints."""
    return array.array("h", samples).tobytes()


def _float32_bytes_to_floats(buf):
    """Decode float32 little-endian bytes back to a list of floats."""
    return list(array.array("f", buf))


def test_convert_int16_to_float32_full_scale():
    """Boundary values: +32767 → ~+1.0, -32768 → -1.0, 0 → 0.0.
    Pinned because Whisper-family normalisation is the de-facto
    standard; getting the scale wrong silently corrupts every
    transcript."""
    src = _samples_to_int16_bytes([0, 32767, -32768, 16384, -16384])
    out = vwl._convert_int16_le_to_float32_le(src)
    floats = _float32_bytes_to_floats(out)
    assert len(floats) == 5
    assert floats[0] == 0.0
    assert floats[1] == pytest.approx(32767 / 32768.0, abs=1e-6)
    assert floats[2] == pytest.approx(-1.0, abs=1e-6)
    assert floats[3] == pytest.approx(0.5, abs=1e-6)
    assert floats[4] == pytest.approx(-0.5, abs=1e-6)


def test_convert_int16_to_float32_size():
    """int16 is 2 bytes/sample; float32 is 4 bytes/sample. 100ms
    16kHz mono = 1600 samples = 3200 in / 6400 out."""
    src = _samples_to_int16_bytes([1] * 1600)
    out = vwl._convert_int16_le_to_float32_le(src)
    assert len(src) == 3200
    assert len(out) == 6400


def test_convert_int16_to_float32_empty():
    assert vwl._convert_int16_le_to_float32_le(b"") == b""


def test_convert_int16_to_float32_non_bytes():
    """Defensive: a transport bug passing None / int / str must not
    crash the conversion."""
    assert vwl._convert_int16_le_to_float32_le(None) == b""
    assert vwl._convert_int16_le_to_float32_le("nope") == b""


def test_convert_int16_to_float32_accepts_bytearray():
    src = bytearray(_samples_to_int16_bytes([16384]))
    out = vwl._convert_int16_le_to_float32_le(src)
    assert _float32_bytes_to_floats(out) == pytest.approx([0.5], abs=1e-6)


def test_client_rejects_unknown_wire_format():
    with pytest.raises(ValueError, match="wire_format"):
        vwl.WhisperLiveClient(
            url="ws://x", uid="u1", model="m1",
            on_partial=_noop_async, on_final=_noop_async,
            on_error=_noop_async,
            wire_format="bogus",
        )


@pytest.mark.asyncio
async def test_send_audio_converts_int16_to_float32_by_default(fake_whisperlive):
    """Default wire_format is float32_le (matches pip whisper-live
    0.8.0). Send int16 PCM; server should receive float32 bytes."""
    received = []

    async def on_connect(ws, init, fake):
        await ws.send(json.dumps({"message": "SERVER_READY"}))
        async for msg in ws:
            if isinstance(msg, (bytes, bytearray)):
                received.append(bytes(msg))

    fake = await fake_whisperlive(on_connect)
    client = _new_client(fake.url)  # default wire_format
    await client.connect_and_wait_ready(ready_timeout=2.0)
    src = _samples_to_int16_bytes([0, 16384, -16384, 32767])
    await client.send_audio(src)
    await asyncio.sleep(0.05)
    await client.close()
    assert len(received) == 1
    floats = _float32_bytes_to_floats(received[0])
    assert floats == pytest.approx([0.0, 0.5, -0.5, 32767/32768.0], abs=1e-6)


@pytest.mark.asyncio
async def test_send_audio_preserves_int16_when_wire_format_is_int16(fake_whisperlive):
    """For future upstream + --raw_pcm_input deployments the wrapper
    is constructed with wire_format=WIRE_INT16_LE and the
    conversion is skipped. Bytes flow through unchanged (modulo
    the bytes(...) defensive copy)."""
    received = []

    async def on_connect(ws, init, fake):
        await ws.send(json.dumps({"message": "SERVER_READY"}))
        async for msg in ws:
            if isinstance(msg, (bytes, bytearray)):
                received.append(bytes(msg))

    fake = await fake_whisperlive(on_connect)
    client = _new_client(fake.url, wire_format=vwl.WIRE_INT16_LE)
    await client.connect_and_wait_ready(ready_timeout=2.0)
    src = _samples_to_int16_bytes([0, 16384, -16384, 32767])
    await client.send_audio(src)
    await asyncio.sleep(0.05)
    await client.close()
    assert received == [src]


@pytest.mark.asyncio
async def test_set_cutoff_seals_to_audio_clock_not_transcript_clock():
    """A Send/Clear cutoff must drop audio spoken before the click even when
    WhisperLive hasn't emitted a segment for it yet. Sealing only to the last
    EMITTED segment (transcript clock) misses that in-flight audio; sealing to
    the AUDIO clock (bytes forwarded) covers it. Regression: only segment
    A(start=0) is emitted at click time, but a later pre-click segment
    B(start=2000) would otherwise repopulate the buffer.
    """
    finals: list[str] = []

    async def on_final(text):
        finals.append(text)

    async def on_noop(*_a):
        pass

    client = vwl.WhisperLiveClient(
        url="ws://unused",
        uid="cutoff-test",
        model="m",
        on_partial=on_noop,
        on_final=on_final,
        on_error=on_noop,
    )

    class _FakeWS:
        async def send(self, _payload):
            pass

    client._ws = _FakeWS()
    client.state = vwl.READY

    # ~2100 ms of audio forwarded to WhisperLive (16kHz mono int16 -> 32 B/ms).
    await client.send_audio(b"\x00" * (2100 * 32))
    assert int(client._audio_sent_ms) == 2100

    # WhisperLive has only emitted segment A(start=0) so far.
    await client._dispatch_segments(
        [{"start": 0.0, "text": "A", "completed": True}]
    )

    # Operator hits Send/Clear -> seal to the AUDIO clock (2100), not 1 ms.
    client.set_cutoff()
    assert client._cutoff_ms == 2100

    # WhisperLive later emits B(start=2000) for the pre-click audio and
    # C(start=2200) for post-click speech. B must be dropped, C must pass.
    await client._dispatch_segments(
        [
            {"start": 2.0, "text": "B pre-click", "completed": True},
            {"start": 2.2, "text": "C post-click", "completed": True},
        ]
    )
    assert finals == ["A", "C post-click"], finals


@pytest.mark.asyncio
async def test_send_audio_malformed_frame_routes_to_unavailable():
    """An odd/truncated int16 frame fails the int16->float32 conversion
    (np.frombuffer needs an even length). That failure must flow to the same
    UNAVAILABLE + on_error path as a send failure, not escape uncaught — the
    conversion is inside the try. The audio clock must not advance on failure.
    """
    errors: list[str] = []

    async def on_error(msg):
        errors.append(msg)

    async def on_noop(*_a):
        pass

    client = vwl.WhisperLiveClient(
        url="ws://unused",
        uid="malformed-test",
        model="m",
        on_partial=on_noop,
        on_final=on_noop,
        on_error=on_error,
        wire_format=vwl.WIRE_FLOAT32_LE,
    )

    class _FakeWS:
        async def send(self, _payload):
            pass

        async def close(self):
            pass

    client._ws = _FakeWS()
    client.state = vwl.READY

    await client.send_audio(b"\x00" * 3)  # odd length -> conversion ValueError

    assert client.state == vwl.UNAVAILABLE
    assert errors and "send failed" in errors[0]
    assert int(client._audio_sent_ms) == 0  # never advanced past the failure
