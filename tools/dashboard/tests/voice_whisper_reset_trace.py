#!/usr/bin/env python
"""Validate the PIVOT: reset the WhisperLive session on Send instead of a ledger.

Stream sentence 1 (consumed) through client A; at the Send boundary close A and open
a fresh client B (buffer + clock reset); stream the new sentences through B. Proves:
  - B never re-emits sentence 1 (no straddle, no stale re-emit — buffer is gone).
  - How long the reconnect takes (the window where new audio would be clipped).
  - The new utterance renders cleanly on B.

  .venv/bin/python -m tools.dashboard.tests.voice_whisper_reset_trace
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
    _C = _vts.resolve_transcription_config()
    MODEL, LANG, USE_VAD, NO_SPEECH, VAD = _C.model, _C.language, _C.use_vad, _C.no_speech_thresh, _C.vad_threshold
except Exception:
    MODEL, LANG, USE_VAD = "large-v3", "en", True
    NO_SPEECH, VAD = vwl.WHISPERLIVE_NO_SPEECH_THRESH, vwl.WHISPERLIVE_VAD_THRESHOLD

SENT1 = "data/voice-fixtures/jfk-16k.wav"          # consumed
SENT2 = "data/voice-fixtures/ldc.wav"              # new (distinct: "she had your dark suit...")
FRAME_MS = 64


def _mk(tag, sink, t0):
    async def on_final(text):
        sink.append((round(time.monotonic() - t0, 2), tag, "final", text))
    async def on_partial(text):
        sink.append((round(time.monotonic() - t0, 2), tag, "partial", text))
    async def on_err(text):
        sink.append((round(time.monotonic() - t0, 2), tag, "error", text))
    return vwl.WhisperLiveClient(
        url=vwl.WHISPERLIVE_URL, uid=f"reset-{tag}-{int(time.monotonic()*1000)%100000}",
        model=MODEL, language=LANG, use_vad=USE_VAD, no_speech_thresh=NO_SPEECH, vad_threshold=VAD,
        wire_format=vwl.WHISPERLIVE_WIRE_FORMAT, on_partial=on_partial, on_final=on_final, on_error=on_err,
    )


async def _stream(client, buf, sr):
    frame = int(sr * FRAME_MS / 1000)
    for i in range(0, len(buf), frame):
        await client.send_audio(buf[i:i + frame].tobytes())
        await asyncio.sleep(FRAME_MS / 1000)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clear-at", type=float, default=0.0,
                    help="reset MID-sentence-1 at this second (Clear case); 0 = after it finalizes (Send case)")
    args = ap.parse_args()

    events: list = []
    t0 = time.monotonic()
    s1, sr1 = sf.read(SENT1, dtype="int16")
    s2, sr2 = sf.read(SENT2, dtype="int16")
    assert sr1 == 16000 and sr2 == 16000

    a = _mk("A", events, t0)
    await a.connect_and_wait_ready(ready_timeout=20.0)
    if args.clear_at > 0:
        # CLEAR mid-utterance: A hears only the first part; the rest (continuation)
        # plus the next sentence go to the fresh epoch B.
        cut = int(args.clear_at * sr1)
        await _stream(a, s1[:cut], sr1)
        s2 = np.concatenate([s1[cut:], s2])   # continuation + next sentence → B
    else:
        await _stream(a, s1, sr1)
        await asyncio.sleep(1.5)         # let sentence 1 finalize

    # ── SEND boundary → reset the session ──
    reset_t = round(time.monotonic() - t0, 2)
    await a.close()
    b = _mk("B", events, t0)
    await b.connect_and_wait_ready(ready_timeout=20.0)
    ready_t = round(time.monotonic() - t0, 2)
    await _stream(b, s2, sr2)            # the new utterance
    await asyncio.sleep(5.0)             # ensure it finalizes
    await b.close()

    print(f"model={MODEL}")
    print(f"reset(Send) at t={reset_t}s ; new session READY at t={ready_t}s ; "
          f"reconnect window = {round(ready_t - reset_t, 2)}s\n")
    b_all = [e for e in events if e[1] == "B" and e[2] in ("partial", "final")]
    b_finals = [e for e in b_all if e[2] == "final"]
    leaked = [e for e in b_all if "ask not" in e[3].lower() or "fellow americans" in e[3].lower()
              or "your country" in e[3].lower()]
    first_b = min((e[0] for e in b_all), default=None)
    print("=== new session (B) — all transcripts (new utterance must render) ===")
    for e in b_all:
        print(f"  t={e[0]}s  {e[2]:7s} {e[3]!r}")
    print()
    print(f"sentence-1 (JFK) leakage into the new session: {len(leaked)}  "
          f"{'<<< CLEAN' if not leaked else '<<< LEAKED: ' + str(leaked)}")
    if first_b is not None:
        print(f"first new-session transcript at t={first_b}s "
              f"(~{round(first_b - reset_t, 2)}s after Send)")


if __name__ == "__main__":
    asyncio.run(main())
