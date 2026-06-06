#!/usr/bin/env python
"""End-to-end proof for #43 Send/Clear suppression — REAL audio, REAL WhisperLive,
REAL client renderer. No synthetic frames anywhere.

For each scenario it captures the actual WhisperLive frame stream two ways:
  - reset OFF: one continuous session (today's behaviour — server clears its own
    buffer but never resets WhisperLive). Reproduces the operator's bug.
  - reset  ON: close + reopen the session at the Send boundary (the #43 fix). The
    upstream audio buffer is gone, so the just-sent speech CANNOT re-emit.
Each captured frame stream is then replayed through the production
voice-capture.js (voice_capture_replay.js) with the Send/Clear injected at the
recorded boundary, and we assert what bufferText the real client actually shows.

PASS criteria:
  reset OFF  -> sent text REAPPEARS after Send   (bug reproduced end-to-end)
  reset ON   -> sent text NEVER reappears        (fix works at the source)
             -> new speech still renders          (fix doesn't over-reach)

  .venv/bin/python -m tools.dashboard.tests.voice_send_suppression_harness
  .venv/bin/python -m tools.dashboard.tests.voice_send_suppression_harness --scenario clear-mid-partial
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
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

FRAME_MS = 64
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
REPLAY_JS = os.path.join(REPO_ROOT, "tools/dashboard/tests/voice_capture_replay.js")

SENT1 = "data/voice-fixtures/jfk-16k.wav"   # "...ask not what your country can do for you..."
SENT2 = "data/voice-fixtures/ldc.wav"       # "she had your dark suit in greasy wash water..."

# Per-scenario oracle. "cleared" = phrases that were removed by Send/Clear and must
# NOT reappear after it. "new" = phrases the operator legitimately speaks next, which
# MUST still render (suppression must not over-reach).
SCENARIO_MARKERS = {
    # BUG-REPRO: full sentence 1 is sent (segments finalized); a distinct sentence 2
    # follows. reset OFF re-emits the last finalized segment (the operator's reported
    # "last thing reappears"); reset ON suppresses it. This is the reported bug.
    "send-after-final": {
        "kind": "bug-repro",
        "cleared": ["fellow americans", "ask not", "your country", "do for you"],
        "new":     ["dark suit", "greasy", "wash water", "all year"],
    },
    # SAFETY (no over-reach): cleared mid-utterance after "...my fellow Americans",
    # then the SAME sentence continues. Empirically the cleared PARTIAL does NOT
    # re-surface even with reset OFF (WhisperLive's decoder window rolls forward), so
    # there's no bug to reproduce here — the point is that the fix must not suppress
    # the continuation. cleared = pre-clear words (must stay gone); new = continuation.
    "clear-mid-partial": {
        "kind": "safety",
        "cleared": ["and so", "fellow americans"],
        "new":     ["ask not", "your country", "do for you"],
    },
}


def _node() -> str:
    n = shutil.which("node")
    if n:
        return n
    import glob
    cands = sorted(glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin/node")))
    if cands:
        return cands[-1]
    raise RuntimeError("node not found")


def _mk(uid_tag, sink, t0):
    async def on_final(text):
        sink.append({"t": round(time.monotonic() - t0, 3), "kind": "final", "text": text})

    async def on_partial(text):
        sink.append({"t": round(time.monotonic() - t0, 3), "kind": "partial", "text": text})

    async def on_err(text):
        sink.append({"t": round(time.monotonic() - t0, 3), "kind": "error", "text": str(text)})

    return vwl.WhisperLiveClient(
        url=vwl.WHISPERLIVE_URL, uid=f"supp-{uid_tag}-{int(time.monotonic()*1000)%100000}",
        model=MODEL, language=LANG, use_vad=USE_VAD, no_speech_thresh=NO_SPEECH, vad_threshold=VAD,
        wire_format=vwl.WHISPERLIVE_WIRE_FORMAT, on_partial=on_partial, on_final=on_final, on_error=on_err,
    )


async def _stream(client, buf, sr):
    frame = int(sr * FRAME_MS / 1000)
    for i in range(0, len(buf), frame):
        await client.send_audio(buf[i:i + frame].tobytes())
        await asyncio.sleep(FRAME_MS / 1000)


async def capture(scenario: str, reset: bool):
    """Return (frames, boundary_index, timing) for one arm.

    frames: ordered [{kind,text,t}], the real WhisperLive output the browser sees.
    boundary_index: where the operator pressed Send/Clear (frame index to inject it).
    """
    s1, sr1 = sf.read(SENT1, dtype="int16")
    s2, sr2 = sf.read(SENT2, dtype="int16")
    assert sr1 == 16000 and sr2 == 16000

    frames: list = []
    t0 = time.monotonic()
    a = _mk("A", frames, t0)
    await a.connect_and_wait_ready(ready_timeout=20.0)

    if scenario == "send-after-final":
        # Speak sentence 1, let it FINALIZE, then Send.
        await _stream(a, s1, sr1)
        await asyncio.sleep(1.5)
        post_audio = s2
    elif scenario == "clear-mid-partial":
        # Speak enough of sentence 1 that a PARTIAL is on screen (no final yet),
        # let it render, then Clear mid-utterance and KEEP TALKING THE SAME sentence.
        # This is the real reappearance trap: reset OFF, WhisperLive finalizes the
        # whole sentence so the cleared "And so, my fellow Americans" comes back glued
        # to the continuation; reset ON, the buffer is flushed so only the
        # continuation ("...ask not what your country...") is heard fresh. The oracle
        # keys "must-not-reappear" off the PRE-CLEAR words ("and so"/"fellow
        # americans"), so the continuation counts as legitimate new speech.
        cut = int(2.2 * sr1)
        await _stream(a, s1[:cut], sr1)
        await asyncio.sleep(0.4)   # let the partial actually render before the Clear
        post_audio = s1[cut:]      # continuation of the SAME utterance
    else:
        raise SystemExit(f"unknown scenario {scenario!r}")

    reset_t = round(time.monotonic() - t0, 3)
    boundary_index = len(frames)   # everything captured so far is pre-Send

    if reset:
        # #43: close + reopen at the Send boundary. New buffer, new clock.
        await a.close()
        b = _mk("B", frames, t0)
        await b.connect_and_wait_ready(ready_timeout=20.0)
        ready_t = round(time.monotonic() - t0, 3)
        await _stream(b, post_audio, sr2)
        await asyncio.sleep(5.0)
        await b.close()
    else:
        # today: keep the same session; the just-sent audio stays buffered and
        # re-transcribes as more audio flows in.
        ready_t = reset_t
        await _stream(a, post_audio, sr2)
        await asyncio.sleep(5.0)
        await a.close()

    timing = {"reset_t": reset_t, "ready_t": ready_t,
              "reconnect_window_s": round(ready_t - reset_t, 3)}
    return frames, boundary_index, timing


def replay(frames, boundary_index):
    """Feed the real frame stream through the production client; return its output."""
    payload = json.dumps({
        "frames": [{"kind": f["kind"], "text": f["text"]} for f in frames if f["kind"] in ("final", "partial", "buffer_state")],
        "clearAtIndex": boundary_index,
    })
    out = subprocess.run([_node(), REPLAY_JS], input=payload, capture_output=True, text=True,
                         env={**os.environ, "REPO_ROOT": REPO_ROOT})
    if out.returncode != 0:
        raise RuntimeError(f"replay failed: {out.stderr}")
    return json.loads(out.stdout)


def _has(markers, text):
    low = (text or "").lower()
    return any(m in low for m in markers)


async def run_scenario(scenario: str):
    print(f"\n{'='*72}\nSCENARIO: {scenario}   model={MODEL}\n{'='*72}")
    cleared_markers = SCENARIO_MARKERS[scenario]["cleared"]
    new_markers = SCENARIO_MARKERS[scenario]["new"]
    results = {}
    for reset in (False, True):
        arm = "reset ON " if reset else "reset OFF"
        frames, bidx, timing = await capture(scenario, reset)
        r = replay(frames, bidx)

        sent = r["sentText"]
        post = r["postClearRenders"]
        reappeared = any(_has(cleared_markers, x) for x in post)
        new_shown = any(_has(new_markers, x) for x in post)

        print(f"\n--- {arm} ---")
        print(f"  reset/reconnect window: {timing['reconnect_window_s']}s")
        print(f"  sent (in box at Send):  {sent!r}")
        print(f"  frames captured: {len(frames)} (boundary at #{bidx})")
        print(f"  renders AFTER Send ({len(post)}):")
        for x in post[:12]:
            tag = " <-- SENT TEXT REAPPEARED" if _has(cleared_markers, x) else ("  (new speech)" if _has(new_markers, x) else "")
            print(f"      {x!r}{tag}")
        if len(post) > 12:
            print(f"      ... (+{len(post)-12} more)")
        print(f"  >> sent-text reappeared: {reappeared}   new-speech rendered: {new_shown}")
        results[reset] = {"reappeared": reappeared, "new_shown": new_shown, "timing": timing}

    kind = SCENARIO_MARKERS[scenario]["kind"]
    print(f"\n{'-'*72}\nVERDICT ({scenario}, kind={kind}):")
    off, on = results[False], results[True]
    fix_ok = (not on["reappeared"]) and on["new_shown"]
    print(f"  reset ON suppresses cleared text AND keeps new speech: {'YES ✓' if fix_ok else 'NO ✗'}")
    print(f"  reset ON reconnect window: {on['timing']['reconnect_window_s']}s")
    if kind == "bug-repro":
        bug_ok = off["reappeared"]
        print(f"  reset OFF reproduces the bug (cleared text reappears): {'YES ✓' if bug_ok else 'NO ✗'}")
        ok = bug_ok and fix_ok
        print(f"  RESULT: {'PASS — reproduces the bug AND proves the fix' if ok else 'INCONCLUSIVE — see above'}")
    else:  # safety / no-over-reach
        print(f"  reset OFF reappearance (informational, timing-dependent): {off['reappeared']}")
        ok = fix_ok
        print(f"  RESULT: {'PASS — fix does not suppress the continuation (no over-reach)' if ok else 'FAIL — fix over-reached or dropped new speech'}")
    return ok


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="all",
                    choices=["all", "send-after-final", "clear-mid-partial"])
    args = ap.parse_args()
    scenarios = ["send-after-final", "clear-mid-partial"] if args.scenario == "all" else [args.scenario]
    allok = True
    for sc in scenarios:
        allok = await run_scenario(sc) and allok
    print(f"\n{'='*72}\nOVERALL: {'ALL PASS' if allok else 'NOT ALL PASS'}\n{'='*72}")


if __name__ == "__main__":
    asyncio.run(main())
