"""Structured, persistent, per-session startup trace.

The dashboard already emits ~22 ``phase-trace:`` markers during session
create/boot — but only as ``logger.info`` lines in ``data/dashboard.log``,
which rotates hourly and is unstructured. So "what happened during this
session's startup, with timings" is only answerable by grepping a moving
target across rotated ``.gz`` files — unreliable by construction.

This module captures the SAME events into a durable, per-session record:
one JSONL file per session at ``data/session-traces/<tmux_name>.jsonl``.

  - per-session: no cross-session grep, no log interleaving
  - append-only, NEVER rotated: the full startup is always there
  - structured: each line is ``{"t": <iso>, "mono_ms": <int>, "phase":
    <str>, ...detail}`` so it parses trivially
  - trivially readable: ``cat data/session-traces/<tmux>.jsonl`` or the
    ``/api/session/{tmux}/startup-trace`` endpoint

``mono_ms`` is elapsed since the first event recorded for that session in
this process, so deltas between phases are exact even if wall clocks skew.
Every call is best-effort and never raises — tracing must not be able to
break the launch path it instruments.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

TRACE_DIR = Path(__file__).resolve().parents[2] / "data" / "session-traces"

# First-monotonic per session, so mono_ms is "since trace start" regardless
# of which code path logged the first event. Bounded cleanup keeps it from
# growing without limit over a long-lived process.
_starts: dict[str, float] = {}
_MAX_TRACKED = 2000


def trace(tmux_name: str, phase: str, **detail) -> None:
    """Append one structured startup-trace event for ``tmux_name``.

    Best-effort: any failure (disk, encoding, race) is swallowed so the
    instrumented launch path is never affected.
    """
    if not tmux_name:
        return
    try:
        now = time.monotonic()
        if tmux_name not in _starts:
            if len(_starts) >= _MAX_TRACKED:
                # Drop the oldest few so the dict can't grow unbounded.
                for k in list(_starts)[:100]:
                    _starts.pop(k, None)
            _starts[tmux_name] = now
        rec: dict = {
            "t": datetime.now(timezone.utc).isoformat(),
            "mono_ms": int((now - _starts[tmux_name]) * 1000),
            "phase": phase,
        }
        if detail:
            rec.update(detail)
        TRACE_DIR.mkdir(parents=True, exist_ok=True)
        with (TRACE_DIR / f"{tmux_name}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        logger.debug("session_trace: write failed for %s/%s", tmux_name, phase, exc_info=True)


def read_trace(tmux_name: str) -> list[dict]:
    """Return the parsed trace events for ``tmux_name`` (oldest first)."""
    path = TRACE_DIR / f"{tmux_name}.jsonl"
    out: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    except FileNotFoundError:
        return []
    except Exception:
        logger.debug("session_trace: read failed for %s", tmux_name, exc_info=True)
    return out
