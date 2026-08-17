"""auto-rsvzk: pending_outbound ring + api_session_send client_id contract.

Covers the design-independent backend half of the optimistic-outbound
flow. The frontend half (rendering "sending" → "confirmed" state
transitions in the viewer) layers on when Codex's design revision
lands. These tests verify the round-trip without a browser.
"""
from __future__ import annotations

import time

import pytest

from tools.dashboard import pending_outbound


@pytest.fixture(autouse=True)
def _clear_state():
    """Reset the module-global ring between tests."""
    pending_outbound._buffers.clear()
    yield
    pending_outbound._buffers.clear()


# ── pending_outbound module ────────────────────────────────────────


def test_is_in_flight_returns_false_for_unknown_client_id():
    assert pending_outbound.is_in_flight("auto-x", "cid-1") is False


def test_is_in_flight_returns_false_for_empty_client_id():
    pending_outbound.record_send("auto-x", "cid-1", "hi")
    assert pending_outbound.is_in_flight("auto-x", "") is False


def test_record_then_is_in_flight():
    pending_outbound.record_send("auto-x", "cid-1", "hello")
    assert pending_outbound.is_in_flight("auto-x", "cid-1") is True


def test_record_with_empty_client_id_is_noop():
    pending_outbound.record_send("auto-x", "", "hello")
    assert pending_outbound.pending_count("auto-x") == 0


def test_match_and_consume_returns_client_id():
    pending_outbound.record_send("auto-x", "cid-1", "hello")
    assert pending_outbound.match_and_consume("auto-x", "hello") == "cid-1"
    # Consumed → no longer in flight.
    assert pending_outbound.is_in_flight("auto-x", "cid-1") is False


def test_match_and_consume_returns_none_when_no_match():
    pending_outbound.record_send("auto-x", "cid-1", "hello")
    assert pending_outbound.match_and_consume("auto-x", "different") is None
    # Original entry survives the no-match attempt.
    assert pending_outbound.is_in_flight("auto-x", "cid-1") is True


def test_match_and_consume_returns_none_for_empty_text():
    pending_outbound.record_send("auto-x", "cid-1", "hello")
    assert pending_outbound.match_and_consume("auto-x", "") is None


def test_fifo_match_for_identical_text():
    """Two identical messages match in send order — the first echo
    consumes the first pending entry, the second echo consumes the
    second."""
    pending_outbound.record_send("auto-x", "cid-1", "ok")
    pending_outbound.record_send("auto-x", "cid-2", "ok")
    first = pending_outbound.match_and_consume("auto-x", "ok")
    second = pending_outbound.match_and_consume("auto-x", "ok")
    assert first == "cid-1"
    assert second == "cid-2"


def test_ring_evicts_oldest_at_max():
    """Beyond MAX_PENDING_PER_SESSION, the oldest entry is dropped."""
    max_n = pending_outbound.MAX_PENDING_PER_SESSION
    for i in range(max_n + 5):
        pending_outbound.record_send("auto-x", f"cid-{i}", f"msg-{i}")
    # First five should have been evicted.
    for i in range(5):
        assert pending_outbound.is_in_flight("auto-x", f"cid-{i}") is False
    # Last max_n should remain.
    for i in range(5, max_n + 5):
        assert pending_outbound.is_in_flight("auto-x", f"cid-{i}") is True
    assert pending_outbound.pending_count("auto-x") == max_n


def test_clear_session_drops_all_entries():
    pending_outbound.record_send("auto-x", "cid-1", "a")
    pending_outbound.record_send("auto-x", "cid-2", "b")
    pending_outbound.clear_session("auto-x")
    assert pending_outbound.pending_count("auto-x") == 0
    assert pending_outbound.is_in_flight("auto-x", "cid-1") is False


def test_clear_session_is_idempotent():
    """Clearing a session with no buffer is a no-op."""
    pending_outbound.clear_session("auto-never-existed")  # should not raise


def test_per_session_isolation():
    """Sessions don't share their pending rings."""
    pending_outbound.record_send("auto-a", "cid-1", "hello")
    pending_outbound.record_send("auto-b", "cid-1", "hello")
    # Same client_id in two sessions — both independently in flight.
    assert pending_outbound.is_in_flight("auto-a", "cid-1") is True
    assert pending_outbound.is_in_flight("auto-b", "cid-1") is True
    # Consuming from one leaves the other intact.
    assert pending_outbound.match_and_consume("auto-a", "hello") == "cid-1"
    assert pending_outbound.is_in_flight("auto-b", "cid-1") is True


def test_snapshot_returns_pending_entries():
    pending_outbound.record_send("auto-x", "cid-1", "hi")
    pending_outbound.record_send("auto-x", "cid-2", "bye")
    snap = pending_outbound.snapshot("auto-x")
    assert len(snap) == 2
    assert {e["client_id"] for e in snap} == {"cid-1", "cid-2"}
    assert {e["text"] for e in snap} == {"hi", "bye"}
    for entry in snap:
        assert isinstance(entry["ts"], float)
        assert entry["ts"] > 0


def test_repeated_record_updates_timestamp_and_moves_to_end():
    pending_outbound.record_send("auto-x", "cid-1", "first")
    first_ts = pending_outbound.snapshot("auto-x")[0]["ts"]
    time.sleep(0.01)
    pending_outbound.record_send("auto-x", "cid-1", "updated")
    snap = pending_outbound.snapshot("auto-x")
    assert len(snap) == 1
    assert snap[0]["text"] == "updated"
    assert snap[0]["ts"] >= first_ts


# ── api_session_send integration ──────────────────────────────────


def test_api_session_send_records_client_id(tmp_path, monkeypatch):
    """POST /api/session/send with a client_id stashes it in the ring."""
    monkeypatch.setenv("DASHBOARD_DB", str(tmp_path / "d.db"))
    import importlib
    from tools.dashboard.dao import dashboard_db
    importlib.reload(dashboard_db)
    from tools.dashboard import server as _server
    # Re-import pending_outbound — server holds a reference to the
    # module, not the buffer dict, so the autouse clear fixture above
    # still resets state correctly.
    from tools.dashboard import pending_outbound as po
    po._buffers.clear()

    import asyncio
    from unittest.mock import patch, AsyncMock

    async def _go():
        with patch.object(_server, "_tmux_session_exists", return_value=True), \
             patch.object(_server, "tmux_send", new=AsyncMock()) as mock_send:
            req = _FakeRequest({
                "tmux_session": "auto-send",
                "message": "hello",
                "client_id": "cid-abc",
            })
            resp = await _server.api_session_send(req)
            mock_send.assert_awaited_once_with("auto-send", "hello")
            return resp

    resp = asyncio.run(_go())
    body = resp.body.decode()
    assert "client_id" in body
    assert "cid-abc" in body
    assert po.is_in_flight("auto-send", "cid-abc") is True


def test_api_session_send_idempotent_for_in_flight_client_id(tmp_path, monkeypatch):
    """Second POST with same client_id returns in_flight and does NOT
    re-paste into the harness."""
    monkeypatch.setenv("DASHBOARD_DB", str(tmp_path / "d.db"))
    import importlib
    from tools.dashboard.dao import dashboard_db
    importlib.reload(dashboard_db)
    from tools.dashboard import server as _server
    from tools.dashboard import pending_outbound as po
    po._buffers.clear()

    import asyncio
    from unittest.mock import patch, AsyncMock

    async def _go():
        with patch.object(_server, "_tmux_session_exists", return_value=True), \
             patch.object(_server, "tmux_send", new=AsyncMock()) as mock_send:
            # First send.
            await _server.api_session_send(_FakeRequest({
                "tmux_session": "auto-x",
                "message": "ping",
                "client_id": "cid-1",
            }))
            # Second send with the same client_id while the first is
            # still in flight (no echo received yet).
            resp = await _server.api_session_send(_FakeRequest({
                "tmux_session": "auto-x",
                "message": "ping",
                "client_id": "cid-1",
            }))
            return resp, mock_send

    resp, mock_send = asyncio.run(_go())
    # tmux_send was called exactly ONCE — the second POST short-circuits.
    assert mock_send.await_count == 1, (
        f"tmux_send called {mock_send.await_count}× — expected exactly 1 "
        "(retry must not double-paste while in-flight)"
    )
    body = resp.body.decode()
    assert "in_flight" in body


def test_api_session_send_without_client_id_skips_ring(tmp_path, monkeypatch):
    """Sends without a client_id work as before — no ring entry created."""
    monkeypatch.setenv("DASHBOARD_DB", str(tmp_path / "d.db"))
    import importlib
    from tools.dashboard.dao import dashboard_db
    importlib.reload(dashboard_db)
    from tools.dashboard import server as _server
    from tools.dashboard import pending_outbound as po
    po._buffers.clear()

    import asyncio
    from unittest.mock import patch, AsyncMock

    async def _go():
        with patch.object(_server, "_tmux_session_exists", return_value=True), \
             patch.object(_server, "tmux_send", new=AsyncMock()):
            return await _server.api_session_send(_FakeRequest({
                "tmux_session": "auto-noid",
                "message": "no-id message",
            }))

    asyncio.run(_go())
    assert po.pending_count("auto-noid") == 0


# ── Test helpers ───────────────────────────────────────────────────


class _FakeRequest:
    """Minimum starlette.Request shape for api_session_send."""
    def __init__(self, body: dict):
        self._body = body
    async def json(self):
        return self._body
