#!/usr/bin/env python
"""Characterize what WhisperLive emits on SILENCE / WHITE NOISE (hallucinations).

Streams a noise/silence wav and dumps every raw segment with the fields that drive
noise suppression (no_speech_prob, avg_logprob, completed) so we can see the "thank
you"/filler hallucinations and find the right VAD / no_speech threshold.

  .venv/bin/python -m tools.dashboard.tests.voice_whisper_noise_trace --audio data/voice-fixtures/silence-20s-16k.wav
"""
from __future__ import annotations

import argparse
import asyncio
import time

import soundfile as sf

from tools.dashboard import voice_whisperlive as vwl

try:
    from tools.dashboard import voice_transcription_settings as _vts
    _C = _vts.resolve_transcription_config()
    MODEL, LANG, USE_VAD, NO_SPEECH, VAD = _C.model, _C.language, _C.use_vad, _C.no_speech_thresh, _C.vad_threshold
except Exception:
    MODEL, LANG, USE_VAD = "large-v3", "en", True
    NO_SPEECH, VAD = vwl.WHISPERLIVE_NO_SPEECH_THRESH, vwl.WHISPERLIVE_VAD_THRESHOLD

FRAME_MS = 64


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", default="data/voice-fixtures/silence-20s-16k.wav")
    args = ap.parse_args()

    seen: list = []
    t0 = time.monotonic()

    async def noop(_t):
        return None

    client = vwl.WhisperLiveClient(
        url=vwl.WHISPERLIVE_URL, uid=f"noise-{int(time.monotonic()*1000)%100000}",
        model=MODEL, language=LANG, use_vad=USE_VAD, no_speech_thresh=NO_SPEECH, vad_threshold=VAD,
        wire_format=vwl.WHISPERLIVE_WIRE_FORMAT, on_partial=noop, on_final=noop, on_error=noop,
    )
    orig = client._dispatch_segments

    async def hooked(segments):
        for s in segments:
            if isinstance(s, dict):
                seen.append((round(time.monotonic() - t0, 2), dict(s)))
        return await orig(segments)

    client._dispatch_segments = hooked
    await client.connect_and_wait_ready(ready_timeout=20.0)

    data, sr = sf.read(args.audio, dtype="int16")
    assert sr == 16000
    frame = int(sr * FRAME_MS / 1000)
    for i in range(0, len(data), frame):
        await client.send_audio(data[i:i + frame].tobytes())
        await asyncio.sleep(FRAME_MS / 1000)
    await asyncio.sleep(3.0)
    await client.close()

    print(f"audio={args.audio}  model={MODEL}  vad={USE_VAD} vad_thresh={VAD} no_speech_thresh={NO_SPEECH}")
    print(f"raw segments emitted: {len(seen)}")
    # unique texts + the gating fields
    keys_seen = set()
    for _, s in seen:
        keys_seen |= set(s.keys())
    print(f"segment fields present: {sorted(keys_seen)}\n")
    uniq = {}
    for t, s in seen:
        txt = (s.get("text") or "").strip()
        nsp = s.get("no_speech_prob"); alp = s.get("avg_logprob"); comp = s.get("completed")
        key = txt
        if key not in uniq:
            uniq[key] = {"first_t": t, "n": 0, "no_speech_prob": nsp, "avg_logprob": alp, "completed": comp}
        uniq[key]["n"] += 1
    print("=== distinct hallucinated texts on this noise/silence ===")
    for txt, info in sorted(uniq.items(), key=lambda kv: -kv[1]["n"]):
        print(f"  x{info['n']:<4} no_speech_prob={info['no_speech_prob']} avg_logprob={info['avg_logprob']} "
              f"completed={info['completed']}  {txt!r}")
    if not uniq:
        print("  (nothing emitted — VAD/threshold fully suppressed it)")


if __name__ == "__main__":
    asyncio.run(main())
