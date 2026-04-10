"""EventBus replay and buffer tests — server-side, no browser.

Tests the EventBus ring buffer, replay logic, eviction, seq monotonicity,
and the /api/events/replay HTTP endpoint.

Uses asyncio.run() since pytest-asyncio is not installed.
"""

import asyncio
import os
import sqlite3

import pytest


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def bus():
    from tools.dashboard.event_bus import EventBus
    return EventBus()


@pytest.fixture
def setup_env(tmp_path):
    """Minimal env so server module can load (needs DASHBOARD_DB)."""
    db_path = tmp_path / "dashboard.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """CREATE TABLE IF NOT EXISTS tmux_sessions (
            tmux_name TEXT PRIMARY KEY, session_uuid TEXT,
            graph_source_id TEXT, type TEXT NOT NULL, project TEXT NOT NULL,
            jsonl_path TEXT, bead_id TEXT, created_at REAL NOT NULL,
            is_live INTEGER DEFAULT 1, file_offset INTEGER DEFAULT 0,
            last_activity REAL, last_message TEXT DEFAULT '',
            entry_count INTEGER DEFAULT 0, context_tokens INTEGER DEFAULT 0,
            label TEXT DEFAULT '', topics TEXT DEFAULT '[]',
            role TEXT DEFAULT '', nag_enabled INTEGER DEFAULT 0,
            nag_interval INTEGER DEFAULT 15, nag_message TEXT DEFAULT '',
            nag_last_sent REAL DEFAULT 0, dispatch_nag INTEGER DEFAULT 0,
            resolution_dir TEXT, session_uuids TEXT DEFAULT '[]',
            curr_jsonl_file TEXT
        )"""
    )
    conn.commit()
    conn.close()
    os.environ["DASHBOARD_DB"] = str(db_path)
    yield
    os.environ.pop("DASHBOARD_DB", None)


async def _broadcast_n(bus, n, topic="test"):
    """Broadcast n events with dedup=False."""
    for i in range(n):
        await bus.broadcast(topic, {"n": i}, dedup=False)


async def _broadcast_large(bus, n, payload_size=1024):
    """Broadcast n events with large payloads."""
    payload = "x" * payload_size
    for i in range(n):
        await bus.broadcast("test", {"d": payload, "n": i}, dedup=False)


# ── TestReplayCoversGap ──────────────────────────────────────────────


class TestReplayCoversGap:
    """Replay returns the correct events when buffer covers the range."""

    def test_replay_returns_events_in_range(self, bus):
        """Broadcast 10 events. Replay(5, 8) returns events 5-8."""
        asyncio.run(_broadcast_n(bus, 10))

        events, complete = bus.replay(5, 8)
        seqs = [e["seq"] for e in events]
        assert seqs == [5, 6, 7, 8]
        for ev in events:
            assert ev["topic"] == "test"
            assert "n" in ev["data"]

    def test_replay_complete_flag_true(self, bus):
        """Replay range fully covered -> complete=True."""
        asyncio.run(_broadcast_n(bus, 10))

        _, complete = bus.replay(1, 10)
        assert complete is True

    def test_replay_empty_range(self, bus):
        """Replay range with no matching events -> empty, complete=False."""
        asyncio.run(_broadcast_n(bus, 5))

        events, complete = bus.replay(100, 200)
        assert events == []
        assert complete is False


# ── TestBufferOverflow ───────────────────────────────────────────────


class TestBufferOverflow:
    """Buffer eviction under the 2MB size limit."""

    def test_eviction_on_size_limit(self, bus):
        """Broadcast large payloads until buffer exceeds 2MB.
        Assert oldest events evicted, buffer stays under limit."""
        asyncio.run(_broadcast_large(bus, 3000))

        assert bus._buffer_bytes <= bus._BUFFER_MAX_BYTES
        assert bus._buffer[0].seq > 1

    def test_replay_incomplete_after_eviction(self, bus):
        """Fill buffer, evict old events. Replay evicted range -> complete=False."""
        asyncio.run(_broadcast_large(bus, 3000))

        events, complete = bus.replay(1, 5)
        assert complete is False

    def test_replay_partial_coverage(self, bus):
        """Some events in range evicted, some still in buffer.
        Returns available events, complete=False."""
        asyncio.run(_broadcast_large(bus, 3000))

        oldest_seq = bus._buffer[0].seq
        from_seq = oldest_seq - 10
        to_seq = oldest_seq + 5

        events, complete = bus.replay(from_seq, to_seq)
        assert complete is False
        assert len(events) > 0
        assert all(e["seq"] >= oldest_seq for e in events)


# ── TestReplayEndpoint ───────────────────────────────────────────────


class TestReplayEndpoint:
    """HTTP /api/events/replay endpoint via TestClient."""

    def _get_server(self):
        import importlib
        from tools.dashboard import server as server_mod
        importlib.reload(server_mod)
        return server_mod

    def test_replay_endpoint_returns_json(self, setup_env):
        """GET /api/events/replay?from=1&to=5 returns JSON with events and complete."""
        from tools.dashboard.event_bus import EventBus
        from starlette.testclient import TestClient

        server_mod = self._get_server()
        fresh_bus = EventBus()
        original_bus = server_mod.event_bus
        server_mod.event_bus = fresh_bus

        try:
            asyncio.run(_broadcast_n(fresh_bus, 5))

            with TestClient(server_mod.app) as client:
                resp = client.get("/api/events/replay?from=1&to=5")
                assert resp.status_code == 200
                body = resp.json()
                assert "events" in body
                assert "complete" in body
        finally:
            server_mod.event_bus = original_bus

    def test_replay_endpoint_complete(self, setup_env):
        """Broadcast 10 events, replay 3-7. complete=true, 5 events."""
        from tools.dashboard.event_bus import EventBus
        from starlette.testclient import TestClient

        server_mod = self._get_server()
        fresh_bus = EventBus()
        original_bus = server_mod.event_bus
        server_mod.event_bus = fresh_bus

        try:
            asyncio.run(_broadcast_n(fresh_bus, 10))

            with TestClient(server_mod.app) as client:
                resp = client.get("/api/events/replay?from=3&to=7")
                assert resp.status_code == 200
                body = resp.json()
                assert body["complete"] is True
                assert len(body["events"]) == 5
                seqs = [e["seq"] for e in body["events"]]
                assert seqs == [3, 4, 5, 6, 7]
        finally:
            server_mod.event_bus = original_bus

    def test_replay_endpoint_incomplete(self, setup_env):
        """Overflow buffer, replay old range. complete=false, status 206."""
        from tools.dashboard.event_bus import EventBus
        from starlette.testclient import TestClient

        server_mod = self._get_server()
        fresh_bus = EventBus()
        original_bus = server_mod.event_bus
        server_mod.event_bus = fresh_bus

        try:
            asyncio.run(_broadcast_large(fresh_bus, 3000))

            with TestClient(server_mod.app) as client:
                resp = client.get("/api/events/replay?from=1&to=5")
                assert resp.status_code == 206
                body = resp.json()
                assert body["complete"] is False
        finally:
            server_mod.event_bus = original_bus


# ── TestSeqMonotonicity ──────────────────────────────────────────────


class TestSeqMonotonicity:
    """Sequence numbers are strictly monotonically increasing."""

    def test_seq_increases(self, bus):
        """Broadcast 5 events. Each seq strictly greater than the previous."""
        asyncio.run(_broadcast_n(bus, 5))

        events, _ = bus.replay(1, 5)
        seqs = [e["seq"] for e in events]
        assert seqs == [1, 2, 3, 4, 5]
        for i in range(1, len(seqs)):
            assert seqs[i] > seqs[i - 1]

    def test_seq_survives_across_topics(self, bus):
        """Broadcast to different topics. Global seq still monotonic."""
        async def _multi_topic():
            await bus.broadcast("topic_a", {"v": 1}, dedup=False)
            await bus.broadcast("topic_b", {"v": 2}, dedup=False)
            await bus.broadcast("topic_a", {"v": 3}, dedup=False)
            await bus.broadcast("topic_c", {"v": 4}, dedup=False)
            await bus.broadcast("topic_b", {"v": 5}, dedup=False)

        asyncio.run(_multi_topic())

        events, complete = bus.replay(1, 5)
        assert complete is True
        seqs = [e["seq"] for e in events]
        assert seqs == [1, 2, 3, 4, 5]
        topics = [e["topic"] for e in events]
        assert topics == ["topic_a", "topic_b", "topic_a", "topic_c", "topic_b"]
