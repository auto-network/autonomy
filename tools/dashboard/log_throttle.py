"""Once-per-state-change logging with ONE aggregated repeat line per subsystem.

For conditions that hold for a while and would otherwise emit one identical
line per occurrence (an org-bound caller hammering a refused route, an event
proxy that stays behind for an hour, twenty worktrees preserved on every
cleanup pass), a :class:`StateChangeLogger` logs:

* the first time a key enters a state (per key);
* every time a key changes state (per key, with how many quiet repeats the
  old state had);
* otherwise NOTHING per key. Quiet repeats across ALL keys roll up into one
  summary line per ``interval_s`` — so a throttle whose key cardinality scales
  with sessions costs one line a minute, not one per key per minute
  (host-0906-222509, 2026-09-07: per-key repeat lines made "worktree
  preserved" 60 → 84 per 15 min with ~20 preserved worktrees).

Thread-safe; the dashboard logs from the loop thread and from ``to_thread``
workers alike.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Hashable

DEFAULT_SUMMARY = "{keys} key(s) unchanged ({repeats} repeat(s) in the last {interval:.0f}s)"


@dataclass
class _Entry:
    state: Hashable
    suppressed: int = 0


class StateChangeLogger:
    """``emit`` decides whether a (key, state) observation is worth a line.

    ``summary`` is the aggregated repeat line's template; it may use
    ``{keys}`` (keys with quiet repeats since the last summary), ``{repeats}``
    (their total) and ``{interval}`` (seconds).
    """

    def __init__(self, interval_s: float = 60.0, *, summary: str = DEFAULT_SUMMARY,
                 clock=time.monotonic) -> None:
        self.interval_s = interval_s
        self.summary = summary
        self._clock = clock
        self._entries: dict[Hashable, _Entry] = {}
        self._lock = threading.Lock()
        self._last_summary = clock()

    # -- decisions -----------------------------------------------------------

    def decide(self, key: Hashable, state: Hashable) -> tuple[str | None, int]:
        """Per-key decision: ``("new", 0)``, ``("changed", old_state_repeats)``
        or ``(None, repeats_so_far)`` when the state is unchanged."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._entries[key] = _Entry(state=state)
                return "new", 0
            if entry.state != state:
                suppressed = entry.suppressed
                entry.state = state
                entry.suppressed = 0
                return "changed", suppressed
            entry.suppressed += 1
            return None, entry.suppressed

    def take_summary(self, now: float | None = None) -> tuple[int, int] | None:
        """``(keys, repeats)`` once per interval when anything was suppressed,
        resetting the counters; else None."""
        now = self._clock() if now is None else now
        with self._lock:
            if now - self._last_summary < self.interval_s:
                return None
            self._last_summary = now
            keys = 0
            repeats = 0
            for entry in self._entries.values():
                if entry.suppressed:
                    keys += 1
                    repeats += entry.suppressed
                    entry.suppressed = 0
            return (keys, repeats) if repeats else None

    # -- logging -------------------------------------------------------------

    def emit(
        self,
        logger: logging.Logger,
        level: int,
        key: Hashable,
        state: Hashable,
        msg: str,
        *args: Any,
        **kwargs: Any,
    ) -> bool:
        """Log ``msg`` if this observation is a first or a change for ``key``;
        otherwise count it. Then, once per interval, log the aggregated summary
        of every key's quiet repeats. Returns whether the per-key line was
        logged."""
        reason, suppressed = self.decide(key, state)
        emitted = False
        if reason is not None:
            if reason == "changed" and suppressed:
                msg = f"{msg} (previous state repeated {suppressed}×)"
            logger.log(level, msg, *args, **kwargs)
            emitted = True
        summary = self.take_summary()
        if summary is not None:
            keys, repeats = summary
            logger.log(level, self.summary.format(
                keys=keys, repeats=repeats, interval=self.interval_s,
            ))
        return emitted

    def forget(self, key: Hashable) -> None:
        with self._lock:
            self._entries.pop(key, None)
