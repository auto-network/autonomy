#!/usr/bin/env python
"""Controlled reproduction harness for the clear/send-vs-dictation interference.

Streams REAL speech audio into the live WhisperLive (via the same WhisperLiveClient
the dashboard uses), fires set_cutoff() — the exact thing a Clear/Send triggers —
mid-stream, then keeps streaming the REST of the audio (= "new speech after the
clear"). Records every partial/final with a relative timestamp so we can answer:

  1. Does NEW speech after the cutoff still produce transcripts, or does dictation
     HANG (the operator-reported "hang after clear/send")?
  2. Does the just-removed text REAPPEAR after the cutoff?
  3. How long is the gap between the cutoff and the next transcript?

Run MANY times to characterize the interface, not once. Requires WhisperLive up
(ws://127.0.0.1:9090) and a 16kHz mono PCM_16 wav.

Usage:
  .venv/bin/python -m tools.dashboard.tests.voice_whisper_repro \
      --audio data/voice-fixtures/jfk-x3-16k.wav --clear-at 13 --runs 5
"""
from __future__ import annotations

import argparse
import asyncio
import time

import soundfile as sf

from tools.dashboard import voice_whisperlive as vwl

try:
    from tools.dashboard import voice_transcription_settings as _vts
    _CFG = _vts.resolve_transcription_config()
    MODEL = _CFG.model
    LANGUAGE = _CFG.language
    USE_VAD = _CFG.use_vad
    NO_SPEECH = _CFG.no_speech_thresh
    VAD_THRESH = _CFG.vad_threshold
except Exception:  # fall back to module defaults
    MODEL = "small.en"
    LANGUAGE = "en"
    USE_VAD = True
    NO_SPEECH = vwl.WHISPERLIVE_NO_SPEECH_THRESH
    VAD_THRESH = vwl.WHISPERLIVE_VAD_THRESHOLD


async def one_run(audio_path: str, clear_at_s: float, frame_ms: int = 64,
                  do_clear: bool = True) -> dict:
    events: list[tuple[float, str, str]] = []
    t0 = time.monotonic()

    def rel() -> float:
        return time.monotonic() - t0

    async def on_partial(text: str) -> None:
        events.append((rel(), "partial", text))

    async def on_final(text: str) -> None:
        events.append((rel(), "final", text))

    async def on_error(text: str) -> None:
        events.append((rel(), "error", text))

    client = vwl.WhisperLiveClient(
        url=vwl.WHISPERLIVE_URL,
        uid=f"repro-{int(time.monotonic()*1000)%100000}",
        model=MODEL,
        language=LANGUAGE,
        use_vad=USE_VAD,
        no_speech_thresh=NO_SPEECH,
        vad_threshold=VAD_THRESH,
        wire_format=vwl.WHISPERLIVE_WIRE_FORMAT,
        on_partial=on_partial,
        on_final=on_final,
        on_error=on_error,
    )
    await client.connect_and_wait_ready(ready_timeout=20.0)

    data, sr = sf.read(audio_path, dtype="int16")
    assert sr == 16000, f"expected 16kHz, got {sr}"
    frame = int(sr * frame_ms / 1000)
    cleared = False
    clear_info: dict = {}

    sent_s = 0.0
    for i in range(0, len(data), frame):
        chunk = data[i:i + frame]
        await client.send_audio(chunk.tobytes())
        sent_s += len(chunk) / sr
        if not cleared and sent_s >= clear_at_s:
            if do_clear:
                client.set_cutoff()         # <-- the Clear/Send action under test
            cleared = True
            clear_info = {
                "at_s": round(sent_s, 2),
                "cutoff_ms": getattr(client, "_cutoff_ms", None),
                "audio_sent_ms": round(getattr(client, "_audio_sent_ms", 0.0), 1),
                "did_clear": do_clear,
            }
            events.append((rel(), "CLEAR", f"cutoff_ms={clear_info['cutoff_ms']} did_clear={do_clear}"))
        await asyncio.sleep(frame_ms / 1000)  # real-time pacing

    # let trailing transcripts arrive
    await asyncio.sleep(4.0)
    await client.close()

    # Characterize: what came AFTER the clear, and the gap to first post-clear event.
    after = [(t, k, v) for (t, k, v) in events if k in ("partial", "final") and t > _clear_rel(events)]
    finals_after = [v for (t, k, v) in after if k == "final"]
    first_after_gap = None
    cr = _clear_rel(events)
    post = [t for (t, k, v) in events if k in ("partial", "final") and t > cr]
    if cr is not None and post:
        first_after_gap = round(min(post) - cr, 2)

    return {
        "clear": clear_info,
        "events": events,
        "finals_after_clear": finals_after,
        "n_transcripts_after_clear": len(after),
        "gap_to_first_after_clear_s": first_after_gap,
    }


def _clear_rel(events):
    for (t, k, v) in events:
        if k == "CLEAR":
            return t
    return None


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", default="data/voice-fixtures/jfk-x3-16k.wav")
    ap.add_argument("--clear-at", type=float, default=13.0)
    ap.add_argument("--runs", type=int, default=3)
    args = ap.parse_args()

    print(f"model={MODEL} vad={USE_VAD} no_speech={NO_SPEECH} vad_thresh={VAD_THRESH}")
    print(f"audio={args.audio} clear_at={args.clear_at}s runs={args.runs}\n")

    def summarize(label, results):
        gaps = [r["gap_to_first_after_clear_s"] for r in results
                if r["gap_to_first_after_clear_s"] is not None]
        nafter = [r["n_transcripts_after_clear"] for r in results]
        hangs = sum(1 for r in results
                    if r["n_transcripts_after_clear"] == 0
                    or (r["gap_to_first_after_clear_s"] or 0) > 3.0)
        avg = round(sum(gaps) / len(gaps), 2) if gaps else None
        print(f"[{label}] runs={len(results)} avg_gap_after_point={avg}s "
              f"gaps={gaps} transcripts_after={nafter} hangs={hangs}")
        return hangs

    control, cleared = [], []
    for n in range(args.runs):
        control.append(await one_run(args.audio, args.clear_at, do_clear=False))
        cleared.append(await one_run(args.audio, args.clear_at, do_clear=True))

    print("\n=== CHARACTERIZATION (gap from the clear-point to the next transcript) ===")
    ch = summarize("CONTROL (no cutoff)", control)
    hh = summarize("CLEAR   (set_cutoff)", cleared)
    print(f"\nVERDICT: control hangs={ch}/{args.runs}  clear hangs={hh}/{args.runs}")
    print("If CLEAR hangs >> CONTROL hangs, the cutoff is interfering with dictation.")


if __name__ == "__main__":
    asyncio.run(main())
