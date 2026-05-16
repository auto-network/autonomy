"""Per-tmux_name buffer state manager for ``/ws/voice`` (S3-3).

Spec: ``graph://86fd1897-d4d``. Holds the accumulated final
transcripts for each bound tmux session between the operator's
``start`` and the next ``commit`` / ``discard`` / ``end``. The
buffer survives WebSocket disconnect for **60 seconds** so a brief
network blip doesn't drop the operator's in-flight dictation; on
reconnect the transport sends a ``buffer_state`` frame with the
restored text.

Also enforces the **cross-tab guard**: only one WS connection per
``tmux_name`` at a time. A new connection for an already-bound
``tmux_name`` returns the prior owner's evict callback to the
caller, which closes the prior WS with code 1000 reason
``superseded``. Concurrent operation across tabs is intentionally
unsupported — the operator-owned environment makes second-wins the
right behavior.

Design notes:

- All operations are synchronous. The state lives in-memory and the
  WS handler is single-threaded async; no I/O happens inside the
  manager, so no async surface is needed.
- TTL is implemented via lazy eviction: every public method calls
  :meth:`_prune_stale_locked` first to drop records whose
  ``detached_at_ms`` is older than ``ttl_ms``. No background task,
  no leaked timers.
- The clock is injected for deterministic testing — production
  uses ``int(time.time() * 1000)``; tests pass a fake clock to
  exercise TTL boundaries without sleep.
- Eviction callbacks are stored opaquely (just ``Any``); the
  manager doesn't await them. The transport receives the prior
  callback from :meth:`acquire` and awaits it itself. This keeps
  the manager sync-only and side-effect-free.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable


# Default TTL: how long a buffer survives after the WS disconnects
# without an explicit ``end`` control frame. 60 seconds per spec —
# long enough to ride out a brief network hiccup or tab refresh,
# short enough that stale buffers don't accumulate.
DEFAULT_TTL_MS = 60_000


@dataclass
class _BufferRecord:
    """Per-tmux_name in-memory state.

    ``text`` is the accumulated final-transcript text (partials
    aren't persisted — only finals contribute, so a brief
    disconnect mid-utterance loses the trailing partial but the
    operator can re-dictate the missing words).

    ``detached_at_ms`` is None while a WS is attached; set to the
    detach timestamp when the WS disconnects. Reattachment clears
    it back to None.

    ``evict_callback`` is whatever the transport passed at acquire
    time — the manager treats it opaquely. Cleared on detach so a
    dead WS's callback never fires.
    """

    text: str = ""
    detached_at_ms: int | None = None
    evict_callback: Any = None


def _word_count(text: str) -> int:
    """Whitespace-split word count, used in ``buffer_state`` frames.

    Empty / whitespace-only text yields 0 rather than 1 (which
    ``len([''])`` would produce naively).
    """
    return len(text.split())


def buffer_state_frame(text: str) -> dict:
    """Build the ``{"type": "buffer_state", ...}`` frame the
    transport sends after reconnect with restored text."""
    return {
        "type": "buffer_state",
        "text": text,
        "word_count": _word_count(text),
    }


@dataclass
class AcquireResult:
    """Returned by :meth:`BufferManager.acquire`.

    ``buffer_text`` is the restored buffer (empty string for a
    fresh tmux_name or one whose TTL expired). The transport
    should send a ``buffer_state`` frame if non-empty.

    ``prior_evict_callback`` is the evict callback the previous
    owner registered, or None if there was no prior owner. The
    transport awaits it (close the prior WS with code 1000 reason
    ``superseded``) before treating itself as the active owner.
    """

    buffer_text: str
    prior_evict_callback: Any


class BufferManager:
    """In-memory buffer state with TTL + cross-tab guard.

    Single instance per server process (one in ``server.py``
    module-global). Production callers should not instantiate
    additional managers; tests construct their own with a fake
    clock + smaller TTL for deterministic boundary coverage.
    """

    def __init__(
        self,
        *,
        ttl_ms: int = DEFAULT_TTL_MS,
        clock: Callable[[], int] | None = None,
    ):
        self._buffers: dict[str, _BufferRecord] = {}
        self._ttl_ms = int(ttl_ms)
        self._clock = clock or (lambda: int(time.time() * 1000))

    # ── Public API ──────────────────────────────────────────

    def acquire(
        self,
        tmux_name: str,
        *,
        evict_callback: Any,
    ) -> AcquireResult:
        """Register ownership of ``tmux_name`` by the calling WS.

        If a prior WS was attached, returns its ``evict_callback``
        in the result; the transport must close that WS (code
        1000, reason 'superseded') before treating itself as the
        active owner.

        If a buffer exists from a prior connection within the TTL
        window, its text is returned for the transport to emit as
        a ``buffer_state`` frame.

        If no prior buffer exists (or it TTL-expired), a fresh
        empty record is created and an empty text is returned.
        """
        self._prune_stale()
        rec = self._buffers.get(tmux_name)
        if rec is None:
            self._buffers[tmux_name] = _BufferRecord(
                text="",
                detached_at_ms=None,
                evict_callback=evict_callback,
            )
            return AcquireResult(buffer_text="", prior_evict_callback=None)
        prior_evict = rec.evict_callback
        existing_text = rec.text
        rec.evict_callback = evict_callback
        rec.detached_at_ms = None
        return AcquireResult(
            buffer_text=existing_text,
            prior_evict_callback=prior_evict,
        )

    def append_final(self, tmux_name: str, text: str) -> None:
        """Append a final transcript to ``tmux_name``'s buffer.

        Finals are joined with a single space — WhisperLive emits
        finals as utterance-level chunks, so a space between is the
        right join in the common case. The transport is welcome to
        pre-normalize text before calling.

        No-op if the buffer doesn't exist (e.g. a transcript arrives
        after release/TTL-eviction — drop rather than re-create
        state that has no owner).
        """
        self._prune_stale()
        rec = self._buffers.get(tmux_name)
        if rec is None:
            return
        if not isinstance(text, str) or not text:
            return
        if rec.text:
            rec.text = rec.text + " " + text
        else:
            rec.text = text

    def clear(self, tmux_name: str) -> None:
        """Empty ``tmux_name``'s buffer (commit / discard).

        Ownership and TTL state preserved — the operator is still
        attached and may keep dictating. No-op for an unknown
        tmux_name.
        """
        rec = self._buffers.get(tmux_name)
        if rec is not None:
            rec.text = ""

    def detach(self, tmux_name: str) -> None:
        """Mark the WS for ``tmux_name`` as disconnected without an
        explicit ``end`` control frame. Starts the TTL countdown.

        Subsequent calls within the TTL window can reattach via
        :meth:`acquire`; after the window, lazy pruning drops the
        record entirely.

        The eviction callback is cleared — there's no live WS to
        evict if a new connection arrives within the TTL window
        (acquire returns prior_evict_callback=None in that case).
        """
        rec = self._buffers.get(tmux_name)
        if rec is None:
            return
        rec.detached_at_ms = self._clock()
        rec.evict_callback = None

    def release(self, tmux_name: str) -> None:
        """Drop ``tmux_name``'s buffer and ownership immediately.

        Called when the WS sends an explicit ``end`` control frame
        — the operator deliberately ended their session, so we
        don't honor the 60s TTL. No-op for an unknown tmux_name.
        """
        self._buffers.pop(tmux_name, None)

    def get_text(self, tmux_name: str) -> str:
        """Read the current buffer text. Used at commit time to
        capture what to send to tmux. Empty string if no buffer
        exists or it's been cleared."""
        self._prune_stale()
        rec = self._buffers.get(tmux_name)
        return rec.text if rec is not None else ""

    def is_tracked(self, tmux_name: str) -> bool:
        """True if a buffer record exists for ``tmux_name`` (live
        or in TTL window). Diagnostics only; not used by the
        transport's control flow."""
        self._prune_stale()
        return tmux_name in self._buffers

    def active_count(self) -> int:
        """Count of currently-tracked buffers (after pruning).
        Diagnostics only."""
        self._prune_stale()
        return len(self._buffers)

    # ── Internal: lazy TTL eviction ─────────────────────────

    def _prune_stale(self) -> None:
        """Drop records whose detached_at_ms is older than the TTL.

        Called at the head of every public method so callers always
        see a TTL-respecting view without a background task. The
        cost is O(N) per call where N is the total tracked count;
        in practice N is bounded by the operator's concurrent voice
        sessions (single-digit).
        """
        now = self._clock()
        to_drop = [
            name
            for name, rec in self._buffers.items()
            if rec.detached_at_ms is not None
            and (now - rec.detached_at_ms) > self._ttl_ms
        ]
        for name in to_drop:
            self._buffers.pop(name, None)


# Module-level singleton used by ``ws_voice`` in production.
# Tests that need a fake clock or alternate TTL replace it via
# ``monkeypatch.setattr(voice_buffer, 'MANAGER', ...)`` so the route
# under test sees the test-controlled manager without forking the
# transport's lookup path.
MANAGER = BufferManager()
