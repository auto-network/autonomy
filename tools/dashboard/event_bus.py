"""EventBus — in-process global pub/sub for SSE delivery.

All subscribers receive ALL topics. Each broadcast is tagged with the topic
name so SSE clients can set the event type and route locally.

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
import time
from collections import deque
from dataclasses import dataclass
from typing import Any


@dataclass
class _BufferEntry:
    seq: int
    topic: str
    serialised: str
    timestamp: float
    size: int  # len(serialised)


class EventBus:
    _BUFFER_MAX_BYTES = 2 * 1024 * 1024  # 2 MB
    _BUFFER_MAX_AGE = 30.0               # seconds

    def __init__(self) -> None:
        # Global list of subscriber queues — every queue receives every topic.
        self._subscribers: list[asyncio.Queue] = []
        # topic -> last-broadcast JSON string (for dedup + topic replay)
        self._last: dict[str, str] = {}
        # topic -> seq of last broadcast (for subscribe replay)
        self._last_seq: dict[str, int] = {}
        # Global monotonic sequence counter
        self._seq: int = 0
        # Chronological ring buffer for gap replay
        self._buffer: deque[_BufferEntry] = deque()
        self._buffer_bytes: int = 0  # running total of serialised sizes

    def subscribe(self) -> asyncio.Queue:
        """Subscribe to all topics.

        Returns a Queue that will receive (topic, data, seq) tuples for every
        future broadcast.  All cached topic states are immediately enqueued
        so the first SSE frame arrives without waiting for the next poll.
        """
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(q)
        # Replay cached state for all known topics.
        # seq=0 signals "cached state, not a live event" — prevents the client's
        # gap detector from seeing non-contiguous seqs and firing a false alarm.
        for topic, serialised in self._last.items():
            q.put_nowait((topic, json.loads(serialised), 0))
        return q

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        """Remove a subscriber queue (call on client disconnect)."""
        try:
            self._subscribers.remove(queue)
        except ValueError:
            pass

    async def broadcast(self, topic: str, data: Any, dedup: bool = True) -> int:
        """Broadcast data to all subscribers tagged with topic name.

        Skips the broadcast if the serialised data is identical to the last
        broadcast for this topic (dedup).  Pass ``dedup=False`` for topics
        where every message is unique (e.g. streaming session entries).

        Returns the number of subscribers that received the message.
        """
        serialised = json.dumps(data, separators=(",", ":"), sort_keys=True)
        if dedup and self._last.get(topic) == serialised:
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

        # Push 3-tuple to all subscribers
        for q in list(self._subscribers):  # snapshot — don't hold lock across put()
            await q.put((topic, data, seq))
        return len(self._subscribers)

    def _trim_buffer(self) -> None:
        """Evict oldest entries when memory budget is exceeded."""
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

        # Complete if we found the first requested seq
        complete = bool(events) and events[0]["seq"] == from_seq
        return events, complete

    def all_cached_topics(self) -> list[str]:
        """Return topics that have cached state."""
        return list(self._last.keys())


# Module-level singleton — imported by server.py
event_bus = EventBus()

# Server epoch — set once at import time, changes on process restart.
# Clients compare this to detect restarts and reset stale seq counters.
_SERVER_EPOCH = int(time.time())
