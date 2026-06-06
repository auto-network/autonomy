#!/usr/bin/env python
"""Reconnect-time distribution for the Send/Clear reset pivot.

A Send/Clear reset closes the upstream WhisperLive session and opens a fresh one.
The reconnect window is exactly how long the dashboard must buffer browser audio so
the first post-Send words aren't clipped. Measure connect+SERVER_READY over N cycles
and report p50/p95/p99/max so the audio-queue cap can be sized with headroom.

  .venv/bin/python -m tools.dashboard.tests.voice_whisper_reconnect_bench --n 25
"""
from __future__ import annotations

import argparse
import asyncio
import time

from tools.dashboard import voice_whisperlive as vwl

try:
    from tools.dashboard import voice_transcription_settings as _vts
    _C = _vts.resolve_transcription_config()
    MODEL, LANG, USE_VAD, NO_SPEECH, VAD = _C.model, _C.language, _C.use_vad, _C.no_speech_thresh, _C.vad_threshold
except Exception:
    MODEL, LANG, USE_VAD = "large-v3", "en", True
    NO_SPEECH, VAD = vwl.WHISPERLIVE_NO_SPEECH_THRESH, vwl.WHISPERLIVE_VAD_THRESHOLD


async def _noop(_t):
    return None


def _pct(xs, p):
    if not xs:
        return None
    s = sorted(xs)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
    return round(s[k], 1)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=25)
    args = ap.parse_args()

    times = []
    fails = 0
    for i in range(args.n):
        c = vwl.WhisperLiveClient(
            url=vwl.WHISPERLIVE_URL, uid=f"bench-{i}-{int(time.monotonic()*1000)%100000}",
            model=MODEL, language=LANG, use_vad=USE_VAD, no_speech_thresh=NO_SPEECH, vad_threshold=VAD,
            wire_format=vwl.WHISPERLIVE_WIRE_FORMAT, on_partial=_noop, on_final=_noop, on_error=_noop,
        )
        t0 = time.monotonic()
        try:
            await c.connect_and_wait_ready(ready_timeout=20.0)
            times.append((time.monotonic() - t0) * 1000.0)
        except Exception as e:
            fails += 1
            print(f"  reconnect {i} FAILED: {e}")
        finally:
            await c.close()
        await asyncio.sleep(0.2)   # small gap between cycles

    print(f"\nmodel={MODEL}  reconnects={len(times)}/{args.n}  failures={fails}")
    if times:
        print(f"  connect→READY ms:  min={round(min(times),1)}  p50={_pct(times,50)}  "
              f"p95={_pct(times,95)}  p99={_pct(times,99)}  max={round(max(times),1)}")
        print(f"  → audio-queue cap should comfortably exceed p99 ({_pct(times,99)}ms); "
              f"e.g. a {max(500, int(_pct(times,99) or 0) * 3)}ms cap leaves wide headroom.")


if __name__ == "__main__":
    asyncio.run(main())
