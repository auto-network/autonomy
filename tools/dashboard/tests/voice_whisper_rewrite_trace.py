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

import argparse
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


def _flt(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


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

    ap = argparse.ArgumentParser()
    ap.add_argument("--clear-at", type=float, default=0.0,
                    help="seconds into the audio to fire the boundary; 0 = after it finalizes")
    ap.add_argument("--audio", default=AUDIO)
    args = ap.parse_args()

    data, sr = sf.read(args.audio, dtype="int16")
    assert sr == 16000
    frame = int(sr * FRAME_MS / 1000)
    boundary_ms = None
    boundary_t = None

    async def stream(buf, fire_at=None):
        nonlocal boundary_ms, boundary_t
        sent_s = 0.0
        for i in range(0, len(buf), frame):
            await client.send_audio(buf[i:i + frame].tobytes())
            sent_s += min(frame, len(buf) - i) / sr
            if fire_at is not None and boundary_ms is None and sent_s >= fire_at:
                boundary_ms = round(getattr(client, "_audio_sent_ms", 0.0), 1)
                boundary_t = round(time.monotonic() - t0, 2)
            await asyncio.sleep(FRAME_MS / 1000)

    if args.clear_at > 0:
        # MID-utterance boundary, then KEEP speaking (the continuation = "new speech").
        await stream(data, fire_at=args.clear_at)
        await asyncio.sleep(3.0)       # let the rest + rewrites land
    else:
        await stream(data)             # full sentence
        await asyncio.sleep(2.0)       # let it finalize
        boundary_ms = round(getattr(client, "_audio_sent_ms", 0.0), 1)
        boundary_t = round(time.monotonic() - t0, 2)
        silence = np.zeros(frame, dtype="int16")
        await stream(np.tile(silence, int(6000 / FRAME_MS)))   # silence → provoke late rewrite
        await asyncio.sleep(2.0)
    await client.close()

    bnd_s = boundary_ms / 1000.0
    print(f"model={MODEL} vad={USE_VAD} no_speech={NO_SPEECH} vad_thresh={VAD_THRESH}")
    print(f"clear_at={args.clear_at}  BOUNDARY (Send): audio={boundary_ms}ms ({bnd_s:.2f}s), t={boundary_t}s")
    print("\nLedger rule: DROP end<=B | STRADDLE start<B<end (suppressed, can't trim) | FRESH start>=B (render)\n")
    print("=== segment dispatches AFTER the boundary ===")
    first_fresh_t = None
    fresh_texts = set()
    straddle_texts = set()
    for cap in captured:
        if cap["t_rel"] < (boundary_t or 0):
            continue
        for seg in cap["segments"]:
            st = seg.get("start"); en = seg.get("end"); txt = seg.get("text", "")
            st_s = _flt(st); en_s = _flt(en)
            if en_s is not None and en_s <= bnd_s:
                cls = "DROP"
            elif st_s is not None and st_s < bnd_s < (en_s if en_s is not None else 1e9):
                cls = "STRADDLE"; straddle_texts.add(txt.strip())
            elif st_s is not None and st_s >= bnd_s:
                cls = "FRESH"; fresh_texts.add(txt.strip())
                if first_fresh_t is None:
                    first_fresh_t = cap["t_rel"]
            else:
                cls = "??"
            print(f"  t={cap['t_rel']}s  {cls:9s} start={st} end={en} completed={seg.get('completed')}  {txt!r}")
    print("\n=== STRADDLE COST (how long conservative suppression hides continuation) ===")
    if first_fresh_t is not None and boundary_t is not None:
        print(f"  first FRESH (start>=B) segment at t={first_fresh_t}s  →  ~{round(first_fresh_t - boundary_t, 2)}s after the boundary")
    else:
        print("  no FRESH (start>=B) segment appeared in the window")
    print(f"  text hidden in STRADDLE segments (would be suppressed): {sorted(straddle_texts)}")
    print(f"  text that WOULD render (FRESH): {sorted(fresh_texts)}")


if __name__ == "__main__":
    asyncio.run(main())
