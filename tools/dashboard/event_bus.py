"""EventBus — in-process global pub/sub for SSE delivery.

All subscribers receive ALL topics. Each broadcast is tagged with the topic
name so SSE clients can set the event type and route locally.

Usage::

    bus = EventBus()

    # Subscribe — returns a Queue that receives (topic, data) tuples for ALL topics.
    q = bus.subscribe()

    # Broadcast to all subscribers (deduped — skipped if data unchanged per topic).
    await bus.broadcast("dispatch", {"active": [], "waiting": []})

    # Unsubscribe on client disconnect.
    bus.unsubscribe(q)
"""

import asyncio
import json
from typing import Any


class EventBus:
    def __init__(self) -> None:
        # Global list of subscriber queues — every queue receives every topic.
        self._subscribers: list[asyncio.Queue] = []
        # topic -> last-broadcast JSON string (for dedup)
        self._last: dict[str, str] = {}

    def subscribe(self) -> asyncio.Queue:
        """Subscribe to all topics.

        Returns a Queue that will receive (topic, data) tuples for every
        future broadcast. All cached topic states are immediately enqueued
        so the first SSE frame arrives without waiting for the next poll.
        """
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(q)
        # Replay cached state for all known topics.
        for topic, serialised in self._last.items():
            q.put_nowait((topic, json.loads(serialised)))
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

        for q in list(self._subscribers):  # snapshot — don't hold lock across put()
            await q.put((topic, data))
        return len(self._subscribers)

    def all_cached_topics(self) -> list[str]:
        """Return topics that have cached state."""
        return list(self._last.keys())


# Module-level singleton — imported by server.py
event_bus = EventBus()
