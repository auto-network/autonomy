#!/usr/bin/env python
"""Characterize Whisper's late rewrites relative to a Send/Clear boundary.

Answers the question the suppression ledger hinges on: after you Send (a boundary
at audio position B), Whisper keeps re-transcribing and may reword the sentence or
append a late "thank you". Do those rewritten/filler WORDS carry timestamps BEFORE
B? If yes, a timestamp ledger ("drop any word whose start < B") is a clean hard
filter. If partials lack word timestamps, we need a provisional-partial policy.

Hooks the real WhisperLiveClient._dispatch_segments to capture RAW segments (with
words[]). Streams a full sentence, lets it finalize, fires the boundary, then feeds
SILENCE to provoke the late hallucination — and classifies every emitted word as
PRE/POST/unknown vs the boundary.

  .venv/bin/python -m tools.dashboard.tests.voice_whisper_rewrite_trace
"""
from __future__ import annotations

import asyncio
import time

import numpy as np
import soundfile as sf

from tools.dashboard import voice_whisperlive as vwl

try:
    from tools.dashboard import voice_transcription_settings as _vts
    _CFG = _vts.resolve_transcription_config()
    MODEL, LANG, USE_VAD = _CFG.model, _CFG.language, _CFG.use_vad
    NO_SPEECH, VAD_THRESH = _CFG.no_speech_thresh, _CFG.vad_threshold
except Exception:
    MODEL, LANG, USE_VAD = "large-v3", "en", True
    NO_SPEECH, VAD_THRESH = vwl.WHISPERLIVE_NO_SPEECH_THRESH, vwl.WHISPERLIVE_VAD_THRESHOLD

AUDIO = "data/voice-fixtures/jfk-16k.wav"
FRAME_MS = 64


async def main() -> None:
    captured: list[dict] = []
    t0 = time.monotonic()

    async def noop(_t):  # callbacks are required by the client
        return None

    client = vwl.WhisperLiveClient(
        url=vwl.WHISPERLIVE_URL, uid=f"rwtrace-{int(time.monotonic()*1000)%100000}",
        model=MODEL, language=LANG, use_vad=USE_VAD,
        no_speech_thresh=NO_SPEECH, vad_threshold=VAD_THRESH,
        wire_format=vwl.WHISPERLIVE_WIRE_FORMAT,
        on_partial=noop, on_final=noop, on_error=noop,
    )

    orig_dispatch = client._dispatch_segments

    async def hooked(segments):
        captured.append({
            "t_rel": round(time.monotonic() - t0, 2),
            "audio_ms": round(getattr(client, "_audio_sent_ms", 0.0), 1),
            "segments": [dict(s) for s in segments if isinstance(s, dict)],
        })
        return await orig_dispatch(segments)

    client._dispatch_segments = hooked
    await client.connect_and_wait_ready(ready_timeout=20.0)

    data, sr = sf.read(AUDIO, dtype="int16")
    assert sr == 16000
    frame = int(sr * FRAME_MS / 1000)

    async def stream(buf):
        for i in range(0, len(buf), frame):
            await client.send_audio(buf[i:i + frame].tobytes())
            await asyncio.sleep(FRAME_MS / 1000)

    await stream(data)                 # the sentence
    await asyncio.sleep(2.0)            # let it finalize

    boundary_ms = round(getattr(client, "_audio_sent_ms", 0.0), 1)
    boundary_t = round(time.monotonic() - t0, 2)

    silence = np.zeros(frame, dtype="int16")
    await stream(np.tile(silence, int(6000 / FRAME_MS)))   # ~6s silence → provoke late rewrite
    await asyncio.sleep(2.0)
    await client.close()

    bnd_s = boundary_ms / 1000.0
    print(f"model={MODEL} vad={USE_VAD} no_speech={NO_SPEECH} vad_thresh={VAD_THRESH}")
    print(f"\nBOUNDARY (Send): audio={boundary_ms}ms ({bnd_s:.2f}s), t={boundary_t}s\n")
    print("=== segment dispatches AFTER the boundary (these are the rewrites/filler) ===")
    any_after = False
    for cap in captured:
        if cap["t_rel"] < boundary_t:
            continue
        for seg in cap["segments"]:
            any_after = True
            words = seg.get("words") or []
            wc = []
            for w in words:
                ws = w.get("start")
                cls = "PRE" if (isinstance(ws, (int, float)) and ws < bnd_s) else \
                      ("POST" if isinstance(ws, (int, float)) else "??")
                wc.append(f"{w.get('word','')!r}@{ws}[{cls}]")
            print(f"  t={cap['t_rel']}s emit  start={seg.get('start')} end={seg.get('end')} "
                  f"completed={seg.get('completed')}  text={seg.get('text','')!r}")
            print("      words: " + (" ".join(wc) if words else "(NONE — no word timestamps)"))
    if not any_after:
        print("  (no segments emitted after the boundary)")


if __name__ == "__main__":
    asyncio.run(main())
