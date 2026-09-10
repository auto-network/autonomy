"""EventBus — in-process global pub/sub for SSE delivery.

All subscribers receive ALL topics. Each broadcast is tagged with the topic
name so SSE clients can set the event type and route locally.

TODO(auto-p5rbu): the ``setting.changed`` topic is emitted in-process only.
When uvicorn moves to multi-worker, Settings writes from worker A won't
reach SSE subscribers on worker B. Either (a) hoist the bus onto a
process-shared transport (Redis pub/sub, NATS) or (b) keep workers
single-process for the dashboard.

Usage::

    bus = EventBus()

    # Subscribe — returns a Queue that receives (topic, data, seq) tuples for ALL topics.
    q = bus.subscribe()

    # Broadcast to all subscribers (deduped — skipped if data unchanged per topic).
    await bus.broadcast("dispatch", {"active": [], "waiting": []})

    # Unsubscribe on client disconnect.
    bus.unsubscribe(q)

    # Replay missed events after reconnect.
    events, complete = bus.replay(from_seq=5, to_seq=10)
"""

import asyncio
import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

_SNAPSHOT_VERSION = 1
_RECENT_BROADCASTS_PER_TOPIC = 10
_RESTORE_HISTORY_MAX = 5


@dataclass
class _BufferEntry:
    seq: int
    topic: str
    serialised: str
    timestamp: float
    size: int  # len(serialised)


@dataclass
class _SubscriberMeta:
    """Per-subscriber tracking for /api/diag visibility into backpressure."""
    subscribed_at: float  # wall-clock seconds (Unix epoch)
    dropped_count: int = 0
    client_id: str | None = None  # set by the SSE handler if known
    connection_id: str | None = None  # opaque id derived from queue identity


class EventBus:
    _BUFFER_MAX_BYTES = 32 * 1024 * 1024  # 32 MB

    def __init__(self) -> None:
        # Global list of subscriber queues — every queue receives every topic.
        self._subscribers: list[asyncio.Queue] = []
        # Per-subscriber metadata, parallel-indexed to ``self._subscribers``.
        self._subscriber_meta: dict[int, _SubscriberMeta] = {}
        # topic -> last-broadcast JSON string (for dedup + topic replay)
        self._last: dict[str, str] = {}
        # topic -> seq of last broadcast (for subscribe replay)
        self._last_seq: dict[str, int] = {}
        # Global monotonic sequence counter
        self._seq: int = 0
        # Chronological ring buffer for gap replay
        self._buffer: deque[_BufferEntry] = deque()
        self._buffer_bytes: int = 0  # running total of serialised sizes
        # Wall-clock timestamps of the last 60s of broadcasts (for /api/diag).
        # Evicted lazily on read, never persisted in snapshots.
        self._broadcast_log: deque[float] = deque()
        # Running count of broadcasts skipped via dedup (never reset).
        self._dedup_skipped_total: int = 0
        # Per-topic recent-broadcast deque (last N entries) for /api/diag.
        # Each entry: {seq, topic, ts, byte_size, dedup_skipped}.
        self._recent_broadcasts: dict[str, deque[dict]] = {}
        # Last RESTORE_HISTORY_MAX restore() calls — circular buffer.
        # Each entry: {ts, success, seq_after, epoch_after}.
        self._restore_history: deque[dict] = deque(maxlen=_RESTORE_HISTORY_MAX)
        # Guards every traversal and every mutation of the shared cache state
        # (_seq, _last, _last_seq, _buffer, _buffer_bytes).
        #
        # This bus is NOT single-threaded: broadcast_sync exists precisely so a
        # caller in a sync context (a settings_ops commit-then-emit hook on a
        # worker thread) can publish without hopping to the event loop. Its
        # ``self._buffer.append`` therefore races every reader that walks the
        # deque. CPython raises "deque mutated during iteration" for exactly
        # that race, and the reader it hit was the startup privacy scrub
        # (since deleted — it was a migration guard wired as a permanent
        # fail-closed boot step). discard_cached died, _on_startup refused to
        # serve, and the worker exited. Live on sjc-2 2026-09-09 that turned
        # every hot reload into a silent no-op — uvicorn kept the incumbent
        # worker on 41-minute-old code while the deployed fix sat unread on
        # disk. The remaining readers (replay, snapshot, _discard_restart_
        # event_cache) race exactly the same way, which is why the lock stays.
        #
        # Reentrant because the guarded paths nest (broadcast_sync holds it
        # across _trim_buffer).
        self._lock = threading.RLock()

    def subscribe(self, client_id: str | None = None) -> asyncio.Queue:
        """Subscribe to all topics.

        Returns a Queue that will receive (topic, data, seq) tuples for every
        future broadcast.  All cached topic states are immediately enqueued
        so the first SSE frame arrives without waiting for the next poll.

        ``client_id`` is optional and only used by /api/diag for correlating
        SSE subscriptions back to the diag client_id of a given tab.
        """
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(q)
        meta = _SubscriberMeta(
            subscribed_at=time.time(),
            client_id=client_id,
            connection_id=f"sub-{id(q):x}",
        )
        self._subscriber_meta[id(q)] = meta
        # Replay cached state for all known topics.
        # seq=0 signals "cached state, not a live event" — prevents the client's
        # gap detector from seeing non-contiguous seqs and firing a false alarm.
        with self._lock:
            cached = list(self._last.items())
        for topic, serialised in cached:
            q.put_nowait((topic, json.loads(serialised), 0))
        return q

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        """Remove a subscriber queue (call on client disconnect)."""
        try:
            self._subscribers.remove(queue)
        except ValueError:
            pass
        self._subscriber_meta.pop(id(queue), None)

    def set_subscriber_client_id(self, queue: asyncio.Queue, client_id: str) -> None:
        """Associate an SSE subscription queue with the tab's diag client_id.

        Used after the tab POSTs to /api/diag/client and we want subsequent
        diag responses to correlate the SSE queue to the tab. No-op if the
        queue is no longer registered.
        """
        meta = self._subscriber_meta.get(id(queue))
        if meta is not None:
            meta.client_id = client_id

    def update_cache(self, topic: str, data: Any) -> None:
        """Update the cached state for a topic without broadcasting.

        New subscribers receive this via subscribe() replay. Existing
        subscribers are not notified — use this when live updates are
        already delivered through a different topic (e.g. session:messages
        carries activity_state, so session:registry cache just needs to
        stay fresh for new connections).
        """
        with self._lock:
            self._last[topic] = json.dumps(
                data, separators=(",", ":"), sort_keys=True,
            )

    async def broadcast(self, topic: str, data: Any, dedup: bool = True) -> int:
        """Broadcast data to all subscribers tagged with topic name.

        Skips the broadcast if the serialised data is identical to the last
        broadcast for this topic (dedup).  Pass ``dedup=False`` for topics
        where every message is unique (e.g. streaming session entries).

        Returns the number of subscribers that received the message.
        """
        return self.broadcast_sync(topic, data, dedup=dedup)

    def broadcast_sync(self, topic: str, data: Any, dedup: bool = True) -> int:
        """Sync variant of :meth:`broadcast`.

        Subscriber queues are unbounded ``asyncio.Queue`` instances, so
        ``put_nowait`` is functionally equivalent to ``await put`` —
        nothing ever blocks. Exposed so callers running in a sync context
        (e.g. ``settings_ops`` mutator commit-then-emit hooks) can publish
        without hopping back to the event loop, which is what makes
        commit-then-emit ordering observable to subscribers.
        """
        serialised = json.dumps(data, separators=(",", ":"), sort_keys=True)
        with self._lock:
            if dedup and self._last.get(topic) == serialised:
                self._dedup_skipped_total += 1
                self._record_recent_broadcast(
                    topic=topic, seq=self._last_seq.get(topic, 0),
                    size=len(serialised), dedup_skipped=True,
                )
                return 0
            self._last[topic] = serialised

            # Assign global seq
            self._seq += 1
            seq = self._seq
            self._last_seq[topic] = seq

            # Store in ring buffer
            entry = _BufferEntry(
                seq=seq, topic=topic, serialised=serialised,
                timestamp=time.monotonic(), size=len(serialised),
            )
            self._buffer.append(entry)
            self._buffer_bytes += entry.size
            self._trim_buffer()

        # Track wall-clock broadcast time for /api/diag rate stats.
        self._broadcast_log.append(time.time())
        self._evict_old_broadcasts()
        self._record_recent_broadcast(
            topic=topic, seq=seq, size=len(serialised), dedup_skipped=False,
        )

        # Push 3-tuple to all subscribers (unbounded queues never block).
        for q in list(self._subscribers):  # snapshot
            try:
                q.put_nowait((topic, data, seq))
            except asyncio.QueueFull:
                # Defensive: a subscriber must use an unbounded queue.
                meta = self._subscriber_meta.get(id(q))
                if meta is not None:
                    meta.dropped_count += 1
        return len(self._subscribers)

    def _record_recent_broadcast(
        self, *, topic: str, seq: int, size: int, dedup_skipped: bool,
    ) -> None:
        """Track this broadcast in the per-topic recent-broadcasts deque."""
        bucket = self._recent_broadcasts.get(topic)
        if bucket is None:
            bucket = deque(maxlen=_RECENT_BROADCASTS_PER_TOPIC)
            self._recent_broadcasts[topic] = bucket
        bucket.append({
            "seq": seq,
            "topic": topic,
            "ts": time.time(),
            "byte_size": size,
            "dedup_skipped": dedup_skipped,
        })

    def _evict_old_broadcasts(self) -> None:
        """Drop broadcast log entries older than 60s."""
        cutoff = time.time() - 60.0
        while self._broadcast_log and self._broadcast_log[0] < cutoff:
            self._broadcast_log.popleft()

    def broadcasts_last_60s(self) -> int:
        """Number of broadcasts in the last 60s of wall-clock time."""
        self._evict_old_broadcasts()
        return len(self._broadcast_log)

    def subscribers_count(self) -> int:
        """Number of active SSE subscriber queues."""
        return len(self._subscribers)

    def subscribers_metadata(self) -> list[dict]:
        """Per-subscriber snapshot for /api/diag.

        Returns a list of dicts ordered to match ``self._subscribers``.
        Each entry: {connection_id, client_id, age_s, queue_depth, dropped_count}.
        ``queue_depth`` reflects pending messages on the subscriber queue
        (high values signal a slow client / backpressure).
        """
        out: list[dict] = []
        now = time.time()
        for q in self._subscribers:
            meta = self._subscriber_meta.get(id(q))
            if meta is None:
                continue
            try:
                qsize = q.qsize()
            except Exception:
                qsize = -1
            out.append({
                "connection_id": meta.connection_id,
                "client_id": meta.client_id,
                "age_s": max(0.0, now - meta.subscribed_at),
                "queue_depth": qsize,
                "dropped_count": meta.dropped_count,
            })
        return out

    def recent_broadcasts(self, topic: str | None = None, limit: int | None = None) -> list[dict]:
        """Return recent broadcasts.

        With no ``topic``, returns broadcasts merged across all topics, in
        seq order, capped at ``limit`` (default: most recent 10 across all
        topics).  With ``topic``, returns only that topic's deque.
        """
        if topic is not None:
            bucket = self._recent_broadcasts.get(topic)
            if not bucket:
                return []
            items = list(bucket)
        else:
            items = []
            for bucket in self._recent_broadcasts.values():
                items.extend(bucket)
            items.sort(key=lambda e: e["seq"])
        if limit is not None and limit >= 0:
            items = items[-limit:]
        return items

    def dedup_skipped_total(self) -> int:
        """Running count of broadcasts skipped via dedup since process start."""
        return self._dedup_skipped_total

    def restore_history(self) -> list[dict]:
        """Last RESTORE_HISTORY_MAX restore() calls (most recent last)."""
        return list(self._restore_history)

    def buffer_window(self) -> tuple[int | None, int | None, float | None, float | None]:
        """Return (first_seq, last_seq, first_ts, last_ts) for the buffer.

        Returns (None, None, None, None) when the buffer is empty.
        Timestamps are wall-clock seconds (Unix epoch), converted from
        ``_BufferEntry.timestamp`` (monotonic) using the current offset.
        """
        with self._lock:
            if not self._buffer:
                return (None, None, None, None)
            first = self._buffer[0]
            last = self._buffer[-1]
        offset = time.time() - time.monotonic()
        return (first.seq, last.seq, first.timestamp + offset, last.timestamp + offset)

    def _trim_buffer(self) -> None:
        """Evict oldest entries when memory budget is exceeded."""
        with self._lock:
            while self._buffer and self._buffer_bytes > self._BUFFER_MAX_BYTES:
                evicted = self._buffer.popleft()
                self._buffer_bytes -= evicted.size

    def replay(self, from_seq: int, to_seq: int) -> tuple[list[dict], bool]:
        """Return events in [from_seq, to_seq] range from buffer.

        Returns (events_list, complete).
        complete=True if buffer covers the full requested range.
        complete=False if events have been evicted — caller should
        fall back to full re-fetch from disk.
        """
        events = []
        with self._lock:
            for entry in self._buffer:
                if entry.seq < from_seq:
                    continue
                if entry.seq > to_seq:
                    break
                events.append({
                    "seq": entry.seq,
                    "topic": entry.topic,
                    "data": json.loads(entry.serialised),
                })

        # A scrubbed or otherwise missing entry in the middle of the requested
        # range is just as incomplete as an evicted first entry.  Checking only
        # the first sequence used to label a range complete even when a startup
        # privacy migration deliberately removed a later cached event.
        complete = (
            bool(events)
            and events[0]["seq"] == from_seq
            and events[-1]["seq"] == to_seq
            and all(
                current["seq"] == previous["seq"] + 1
                for previous, current in zip(events, events[1:])
            )
        )
        return events, complete

    def discard_cached(
        self,
        predicate: Callable[[str, Any, bool], bool],
    ) -> int:
        """Discard matching cached/replay entries without renumbering.

        This is a startup migration primitive, not a broadcast filter.  The
        caller receives ``(topic, decoded_data, decoded_ok)`` and decides
        whether an entry is unsafe to retain.  Malformed JSON is surfaced as
        ``decoded_ok=False`` with ``decoded_data=None`` so a migration can fail
        closed for one topic without discarding malformed state from every
        unrelated topic.

        The global sequence is intentionally unchanged.  Any removed middle
        sequence therefore becomes an honest replay gap and ``replay`` reports
        the requested range incomplete.
        """
        if not callable(predicate):
            raise TypeError("discard predicate must be callable")

        def should_discard(topic: str, serialised: str) -> bool:
            try:
                decoded = json.loads(serialised)
                decoded_ok = True
            except Exception:
                decoded = None
                decoded_ok = False
            return bool(predicate(topic, decoded, decoded_ok))

        removed = 0
        # One critical section for the whole scrub. Held across the rebuild so
        # a concurrent broadcast_sync cannot append into the buffer being
        # walked (the crash) nor land in the old deque that the reassignment
        # below discards (silent event loss). Predicate evaluation happens
        # inside it, which is deliberate: the predicate is a pure decode-and-
        # test over one entry, it does not call back into the bus.
        with self._lock:
            for topic, serialised in list(self._last.items()):
                if should_discard(topic, serialised):
                    self._last.pop(topic, None)
                    self._last_seq.pop(topic, None)
                    removed += 1

            kept: deque[_BufferEntry] = deque()
            kept_bytes = 0
            for entry in self._buffer:
                if should_discard(entry.topic, entry.serialised):
                    removed += 1
                    continue
                kept.append(entry)
                kept_bytes += entry.size
            self._buffer = kept
            self._buffer_bytes = kept_bytes
        return removed

    def all_cached_topics(self) -> list[str]:
        """Return topics that have cached state."""
        with self._lock:
            return list(self._last.keys())

    def snapshot(self, path: str | Path) -> None:
        """Persist bus state + module epoch to ``path`` atomically.

        Failures are logged but never raised — snapshot loss falls back to
        the fresh-epoch boot path (clients see the "Server restarted" banner).
        """
        try:
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                state = {
                    "version": _SNAPSHOT_VERSION,
                    "epoch": _SERVER_EPOCH,
                    "seq": self._seq,
                    "last_seq": dict(self._last_seq),
                    "last": dict(self._last),
                    "buffer": [
                        {
                            "seq": e.seq,
                            "topic": e.topic,
                            "serialised": e.serialised,
                            "timestamp": e.timestamp,
                            "size": e.size,
                        }
                        for e in self._buffer
                    ],
                }
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_text(json.dumps(state))
            tmp.replace(target)
        except Exception:
            logger.exception("EventBus.snapshot(%s) failed", path)

    def restore(self, path: str | Path) -> bool:
        """Restore bus state from ``path``. Returns True if a valid snapshot was loaded.

        Missing, corrupt, or version-mismatched files restore nothing and
        never raise — the caller is expected to continue with the empty bus,
        which causes the client banner to fire (correct behaviour for
        unclean restarts).
        """
        global _SERVER_EPOCH

        def _record(success: bool) -> None:
            self._restore_history.append({
                "ts": time.time(),
                "success": success,
                "seq_after": self._seq,
                "epoch_after": _SERVER_EPOCH,
            })

        try:
            raw = Path(path).read_text()
        except (FileNotFoundError, OSError):
            _record(False)
            return False
        except Exception:
            logger.exception("EventBus.restore(%s) failed reading file", path)
            _record(False)
            return False
        try:
            state = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("EventBus.restore(%s): corrupt JSON, ignoring", path)
            _record(False)
            return False
        if not isinstance(state, dict) or state.get("version") != _SNAPSHOT_VERSION:
            logger.warning(
                "EventBus.restore(%s): version mismatch (got %r, want %d), ignoring",
                path, state.get("version") if isinstance(state, dict) else None,
                _SNAPSHOT_VERSION,
            )
            _record(False)
            return False
        try:
            new_seq = int(state["seq"])
            new_epoch = int(state["epoch"])
            new_last_seq = {str(k): int(v) for k, v in state["last_seq"].items()}
            new_last = {str(k): str(v) for k, v in state["last"].items()}
            new_buffer: deque[_BufferEntry] = deque()
            new_buffer_bytes = 0
            for raw_entry in state["buffer"]:
                serialised = str(raw_entry["serialised"])
                size = int(raw_entry["size"])
                entry = _BufferEntry(
                    seq=int(raw_entry["seq"]),
                    topic=str(raw_entry["topic"]),
                    serialised=serialised,
                    timestamp=float(raw_entry["timestamp"]),
                    size=size,
                )
                new_buffer.append(entry)
                new_buffer_bytes += size
        except (KeyError, TypeError, ValueError):
            logger.exception("EventBus.restore(%s): malformed payload, ignoring", path)
            _record(False)
            return False
        with self._lock:
            self._seq = new_seq
            self._last_seq = new_last_seq
            self._last = new_last
            self._buffer = new_buffer
            self._buffer_bytes = new_buffer_bytes
        # A clean reload keeps the replay buffer and sequence, but must still
        # be observable by connected clients.  Increment the persisted epoch
        # (or keep the newer process timestamp) so the next SSE frame triggers
        # the existing "Server restarted" reload banner.
        _SERVER_EPOCH = max(_SERVER_EPOCH, new_epoch + 1)
        _record(True)
        return True


# Module-level singleton — imported by server.py
event_bus = EventBus()

# Server epoch — set once at import time, changes on process restart.
# Clients compare this to detect restarts and reset stale seq counters.
# EventBus.restore() advances this beyond the persisted value so a clean
# uvicorn reload is visible to connected clients without losing replay state.
_SERVER_EPOCH = int(time.time())


def current_server_epoch() -> int:
    """Return the live module-level server epoch.

    Callers must use this rather than importing ``_SERVER_EPOCH`` directly,
    so values restored from snapshot are visible after startup.
    """
    return _SERVER_EPOCH
