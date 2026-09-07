"""Compact event-loop stall reporting.

The stall sampler (``server._loop_stall_sampler``) reads the loop thread's
stack every 0.4 s while the loop is blocked. Raw, that was up to sixty full
tracebacks per episode — 58% of them the same ``_reconstruct_read_state``
prefix replay — and made up most of the dashboard log by volume. This module
turns the samples into:

* one stack per DISTINCT consecutive stack, trimmed to start at the first
  frame inside this repository (the asyncio/uvicorn frames above it never
  vary and never name the culprit);
* one greppable summary line per episode when the loop recovers::

      STALL 3.2s leaf=tools/graph/db.py:412 via _reconstruct_read_state samples=8 distinct=2

  ``leaf`` is the innermost repository frame of the dominant stack, ``via`` its
  function;
* a rollup every ten minutes naming the top leaves by time blocked.

Pure logic with an injected logger and clock so it is unit-testable without
blocking anything.
"""

from __future__ import annotations

import logging
import time
import traceback
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

REPO_ROOT = str(Path(__file__).resolve().parents[2])


@dataclass(frozen=True)
class Frame:
    file: str
    line: int
    func: str

    def rel(self, repo_root: str) -> str:
        if self.file.startswith(repo_root):
            return self.file[len(repo_root):].lstrip("/")
        return self.file


def frames_from(frame) -> list[Frame]:
    """A live frame object → outermost-first list of :class:`Frame`."""
    return [
        Frame(fs.filename, fs.lineno or 0, fs.name)
        for fs in traceback.extract_stack(frame)
    ]


def compact(frames: Sequence[Frame], repo_root: str = REPO_ROOT) -> list[Frame]:
    """Drop frames OUTSIDE (above) the first repository frame."""
    for i, f in enumerate(frames):
        if f.file.startswith(repo_root):
            return list(frames[i:])
    return list(frames)


def leaf(frames: Sequence[Frame], repo_root: str = REPO_ROOT) -> Frame | None:
    """Innermost repository frame, else the innermost frame of all."""
    for f in reversed(frames):
        if f.file.startswith(repo_root):
            return f
    return frames[-1] if frames else None


def signature(frames: Sequence[Frame]) -> tuple:
    return tuple((f.file, f.line, f.func) for f in frames)


def format_frames(frames: Iterable[Frame], repo_root: str = REPO_ROOT) -> str:
    return "\n".join(f"  {f.rel(repo_root)}:{f.line} in {f.func}" for f in frames)


class Rollup:
    """Top stall leaves by time blocked, emitted once per period."""

    def __init__(self, logger: logging.Logger, *, period_s: float = 600.0,
                 top: int = 5, clock=time.monotonic) -> None:
        self._logger = logger
        self.period_s = period_s
        self.top = top
        self._clock = clock
        self._started = clock()
        self._time: Counter = Counter()
        self._count: Counter = Counter()

    def add(self, leaf_key: str, duration_s: float) -> None:
        self._time[leaf_key] += duration_s
        self._count[leaf_key] += 1

    def maybe_emit(self, now: float | None = None) -> bool:
        now = self._clock() if now is None else now
        if now - self._started < self.period_s:
            return False
        self._started = now
        if not self._time:
            return False
        parts = [
            f"{key} n={self._count[key]} total={total:.1f}s"
            for key, total in self._time.most_common(self.top)
        ]
        self._logger.warning(
            "STALL ROLLUP %dm: %d episode(s), %.1fs blocked; top leaves: %s",
            round(self.period_s / 60), sum(self._count.values()),
            sum(self._time.values()), "; ".join(parts),
        )
        self._time.clear()
        self._count.clear()
        return True


class EpisodeTracker:
    """Fold a stream of stall samples into distinct stacks + one summary."""

    def __init__(self, logger: logging.Logger, *, repo_root: str = REPO_ROOT,
                 max_dumps: int = 60, resample_s: float = 0.4,
                 rollup: Rollup | None = None) -> None:
        self._logger = logger
        self._repo_root = repo_root
        self.max_dumps = max_dumps
        self.resample_s = resample_s
        self.rollup = rollup
        self._episode_hb: float | None = None
        self._reset_episode()

    # -- episode state ------------------------------------------------------

    def _reset_episode(self) -> None:
        self._dumps = 0
        self._last_dump_t = 0.0
        self._last_sig: tuple | None = None
        self._stacks: dict[tuple, list[Frame]] = {}
        self._counts: Counter = Counter()
        self._collapsed = 0
        self._last_stalled = 0.0

    @property
    def active(self) -> bool:
        return self._episode_hb is not None

    # -- inputs --------------------------------------------------------------

    def observe(self, hb: float, now: float, stalled: float, frames: Sequence[Frame]) -> bool:
        """One sample while the loop is stalled. Returns True if a stack was logged."""
        if hb != self._episode_hb:
            if self._episode_hb is not None:
                # The loop ticked and stalled again before we saw it idle:
                # close the previous episode with what we know.
                self._finish(duration=self._last_stalled, ended_at=hb)
            self._episode_hb = hb
            self._reset_episode()
        self._last_stalled = stalled
        if self._dumps >= self.max_dumps or (now - self._last_dump_t) < self.resample_s:
            return False
        self._dumps += 1
        self._last_dump_t = now
        stack = compact(frames, self._repo_root)
        sig = signature(stack)
        self._counts[sig] += 1
        self._stacks.setdefault(sig, stack)
        if sig == self._last_sig:
            self._collapsed += 1
            return False
        self._last_sig = sig
        self._logger.error(
            "EVENT-LOOP STALL STACK #%d (stalled %.2fs, loop thread mid-call):\n%s",
            self._dumps, stalled, format_frames(stack, self._repo_root) or "  <no frames>",
        )
        return True

    def idle(self, hb: float, now: float) -> None:
        """The loop is ticking again. Closes an active episode."""
        if self._episode_hb is not None:
            duration = hb - self._episode_hb if hb > self._episode_hb else self._last_stalled
            self._finish(duration=duration, ended_at=now)
            self._episode_hb = None
            self._reset_episode()
        if self.rollup is not None:
            self.rollup.maybe_emit(now)

    # -- output --------------------------------------------------------------

    def _finish(self, *, duration: float, ended_at: float) -> None:
        if not self._counts:
            self._logger.warning("STALL %.1fs (no stack sampled)", duration)
            return
        dominant_sig, dominant_n = self._counts.most_common(1)[0]
        dominant = self._stacks[dominant_sig]
        lf = leaf(dominant, self._repo_root)
        leaf_key = f"{lf.rel(self._repo_root)}:{lf.line}" if lf else "<unknown>"
        via = lf.func if lf else "<unknown>"
        self._logger.warning(
            "STALL %.1fs leaf=%s via %s samples=%d distinct=%d dominant=%d collapsed=%d",
            duration, leaf_key, via, self._dumps, len(self._counts),
            dominant_n, self._collapsed,
        )
        if self.rollup is not None:
            self.rollup.add(f"{leaf_key} via {via}", duration)
