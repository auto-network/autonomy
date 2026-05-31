"""auto-rsvzk: per-session ring buffer of in-flight outbound messages.

Holds ``(client_id, text, ts)`` triples for messages POSTed to
``/api/session/send`` so the inotify tailer can attach a matching
``client_id`` to the echoed user-turn broadcast on ``session:messages``.
The frontend uses that round-tripped id to promote its locally-rendered
"sending" entry to "confirmed" rather than appending a duplicate row.

Lives in its own module rather than ``server.py`` or
``session_monitor.py`` because both need to import it — if it lived in
either, the other would create the circular import recorded at
``graph://e9f37614-60a``.

Per-session FIFO ring with ``MAX_PENDING_PER_SESSION`` slots; eviction
is oldest-first. Process memory only; not persisted across dashboard
restarts (a restart-survival mode would need a SQLite-backed buffer and
is deferred).

Idempotency-by-id is what makes retry-with-same-client-id safe: the
server's :func:`is_in_flight` check before invoking ``tmux_send`` is
the gate that prevents a retry from double-pasting into the harness
while the original send is still echoing back.
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from threading import Lock
from typing import Optional


# Per-session ring depth. The harness rarely queues more than a handful
# of in-flight sends; 32 is comfortable headroom even under the
# auto-7v712 queued-then-flushed scenario where multiple messages are
# fired in close succession at the moment the session reaches ready.
MAX_PENDING_PER_SESSION = 32


@dataclass
class _Pending:
    client_id: str
    text: str
    ts: float


# Per-session ordered map of client_id → _Pending. OrderedDict gives us
# both fast id lookup and FIFO eviction semantics. Wrapped in a single
# global lock — contention is negligible at typical send rates and a
# global lock is simpler than per-session locks for a buffer this small.
_buffers: dict[str, OrderedDict[str, _Pending]] = {}
_lock = Lock()


def is_in_flight(tmux_name: str, client_id: str) -> bool:
    """True iff ``(tmux_name, client_id)`` has a pending entry.

    The server checks this BEFORE invoking ``tmux_send`` so a retry POST
    carrying the same ``client_id`` does not pile a second paste onto
    the harness while the first is still in flight (echo not yet
    received). This is what makes the optimistic-outbound retry path
    idempotent end-to-end.
    """
    if not client_id:
        return False
    with _lock:
        buf = _buffers.get(tmux_name)
        return bool(buf and client_id in buf)


def record_send(tmux_name: str, client_id: str, text: str) -> None:
    """Stash a freshly-issued send so the inotify tailer can match it.

    No-op when ``client_id`` is empty (callers that don't carry a
    ``client_id`` opt out of the optimistic-send flow entirely; the
    server still pastes their text into the harness normally).

    Ring eviction: when a session already holds
    ``MAX_PENDING_PER_SESSION`` entries, the oldest is dropped before
    the new one is inserted. Eviction is fine — it means the oldest
    in-flight message timed out long enough ago that its echo will
    never arrive carrying a client_id; the frontend has already moved
    it to ``failed`` (T_echo gate).
    """
    if not client_id:
        return
    with _lock:
        buf = _buffers.setdefault(tmux_name, OrderedDict())
        # Re-insert moves to the end (LRU-update semantics) but for a
        # fresh client_id this is just an append.
        if client_id in buf:
            buf.move_to_end(client_id)
            buf[client_id] = _Pending(client_id, text, time.time())
            return
        if len(buf) >= MAX_PENDING_PER_SESSION:
            buf.popitem(last=False)
        buf[client_id] = _Pending(client_id, text, time.time())


def match_and_consume(tmux_name: str, text: str) -> Optional[str]:
    """Return + remove the ``client_id`` of the oldest pending entry
    whose ``text`` exactly matches *text*.

    Called by the inotify tailer when it parses a new user-turn from
    JSONL. The match is text-equality (not substring) — Claude/Codex
    echo the operator's typed text verbatim. Two identical sends
    (e.g. two ``"ok"``s in a row) get matched FIFO: the first echo
    consumes the first pending entry, the second echo consumes the
    second.

    Returns ``None`` when no match exists (the user-turn was typed at
    the harness directly, or the optimistic flow opted out, or
    eviction already cleared it).
    """
    if not text:
        return None
    with _lock:
        buf = _buffers.get(tmux_name)
        if not buf:
            return None
        for client_id, pending in buf.items():
            if pending.text == text:
                del buf[client_id]
                return client_id
        return None


def clear_session(tmux_name: str) -> None:
    """Drop every pending entry for *tmux_name*.

    Called when a session is explicitly stopped (``auto-imaxu``) so a
    later resume can't pick up stale pending entries from the prior
    incarnation. Idempotent — clearing a session with no buffer is a
    no-op.
    """
    with _lock:
        _buffers.pop(tmux_name, None)


def pending_count(tmux_name: str) -> int:
    """Diagnostic. Returns the number of in-flight pending entries."""
    with _lock:
        buf = _buffers.get(tmux_name)
        return len(buf) if buf else 0


def snapshot(tmux_name: str) -> list[dict]:
    """Diagnostic. Returns a list of ``{client_id, text, ts}`` dicts."""
    with _lock:
        buf = _buffers.get(tmux_name)
        if not buf:
            return []
        return [
            {"client_id": p.client_id, "text": p.text, "ts": p.ts}
            for p in buf.values()
        ]
