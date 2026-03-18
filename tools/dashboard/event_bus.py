"""EventBus — in-process topic-based pub/sub for SSE delivery.

Usage::

    bus = EventBus()

    # Subscribe to a topic (returns a Queue)
    q = bus.subscribe("dispatch")

    # Broadcast to all subscribers (deduped — skipped if data unchanged)
    await bus.broadcast("dispatch", {"active": [], "waiting": []})

    # Unsubscribe on client disconnect
    bus.unsubscribe(q)

    # Check active subscriber count
    n = bus.subscriber_count("dispatch")
"""

import asyncio
import json
from collections import defaultdict
from typing import Any


class EventBus:
    def __init__(self) -> None:
        # topic -> list of asyncio.Queue
        self._subscribers: dict[str, list[asyncio.Queue]] = defaultdict(list)
        # topic -> last-broadcast JSON string (for dedup)
        self._last: dict[str, str] = {}

    def subscribe(self, topic: str) -> asyncio.Queue:
        """Subscribe to a topic. Returns a Queue that will receive broadcast data.

        If there is a cached state for the topic, it is immediately enqueued so
        the first SSE client message arrives without waiting for the next broadcast.
        """
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers[topic].append(q)
        if topic in self._last:
            q.put_nowait(json.loads(self._last[topic]))
        return q

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        """Remove a subscriber queue from all topics (call on client disconnect)."""
        for subs in self._subscribers.values():
            try:
                subs.remove(queue)
            except ValueError:
                pass

    async def broadcast(self, topic: str, data: Any) -> int:
        """Broadcast data to all subscribers for topic.

        Skips the broadcast if the serialised data is identical to the last
        broadcast for this topic (dedup).

        Returns the number of subscribers that received the message.
        """
        serialised = json.dumps(data, separators=(",", ":"), sort_keys=True)
        if self._last.get(topic) == serialised:
            return 0
        self._last[topic] = serialised

        subs = self._subscribers.get(topic, [])
        for q in list(subs):  # snapshot — don't hold lock across put()
            await q.put(data)
        return len(subs)

    def subscriber_count(self, topic: str) -> int:
        """Return the number of active subscribers for a topic."""
        return len(self._subscribers.get(topic, []))

    def all_topics(self) -> list[str]:
        """Return topics that have at least one subscriber."""
        return [t for t, subs in self._subscribers.items() if subs]


# Module-level singleton — imported by server.py
event_bus = EventBus()
