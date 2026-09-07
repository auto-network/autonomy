"""Once-per-state-change logging with a periodic repeat count.

For conditions that hold for a while and would otherwise emit one identical
line per occurrence (an org-bound caller hammering a refused route, an event
proxy that stays behind for an hour, a worktree preserved on every cleanup
pass), log:

* the first time a key enters a state;
* every time the key changes state (with how many repeats the old state had);
* otherwise at most once per ``interval_s``, as a count of suppressed repeats.

Thread-safe; the dashboard logs from the loop thread and from ``to_thread``
workers alike.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Hashable


@dataclass
class _Entry:
    state: Hashable
    last_emit: float
    suppressed: int = 0
    entered: float = field(default=0.0)


class StateChangeLogger:
    """``emit`` decides whether a (key, state) observation is worth a line."""

    def __init__(self, interval_s: float = 60.0, *, clock=time.monotonic) -> None:
        self.interval_s = interval_s
        self._clock = clock
        self._entries: dict[Hashable, _Entry] = {}
        self._lock = threading.Lock()

    def decide(self, key: Hashable, state: Hashable) -> tuple[str | None, int]:
        """Return ``(reason, suppressed)``.

        ``reason`` is ``"new"`` (first observation), ``"changed"`` (state
        differs from the last one; ``suppressed`` counts the OLD state's quiet
        repeats), ``"recurring"`` (same state, interval elapsed; ``suppressed``
        counts repeats since the last line), or ``None`` (say nothing).
        """
        now = self._clock()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._entries[key] = _Entry(state=state, last_emit=now, entered=now)
                return "new", 0
            if entry.state != state:
                suppressed = entry.suppressed
                entry.state = state
                entry.last_emit = now
                entry.entered = now
                entry.suppressed = 0
                return "changed", suppressed
            if now - entry.last_emit >= self.interval_s:
                # This observation is logged; the count is the quiet repeats
                # between the previous line and this one.
                suppressed = entry.suppressed
                entry.suppressed = 0
                entry.last_emit = now
                return "recurring", suppressed
            entry.suppressed += 1
            return None, entry.suppressed

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
        """Log ``msg`` if this observation deserves a line. Returns whether it did.

        A recurring line gets ``" (×N in the last Ts)"`` appended; a state
        change that follows quiet repeats of the previous state gets
        ``" (previous state repeated N×)"``.
        """
        reason, suppressed = self.decide(key, state)
        if reason is None:
            return False
        if reason == "recurring":
            msg = f"{msg} (×{suppressed} in the last {self.interval_s:.0f}s)"
        elif reason == "changed" and suppressed:
            msg = f"{msg} (previous state repeated {suppressed}×)"
        logger.log(level, msg, *args, **kwargs)
        return True

    def forget(self, key: Hashable) -> None:
        with self._lock:
            self._entries.pop(key, None)
