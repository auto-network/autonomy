"""Restart-notice handoff tests.

The UI receives the countdown live; this module verifies the durable half of
the contract, which is what lets the next process report an exact duration.
"""

import asyncio
import json

from tools.dashboard import server
from tools.dashboard.event_bus import EventBus


def test_restart_notice_round_trip_emits_completion(tmp_path, monkeypatch):
    notice_path = tmp_path / "restart_notice.state"
    bus = EventBus()
    monkeypatch.setattr(server, "RESTART_NOTICE_STATE_PATH", notice_path)
    monkeypatch.setattr(server, "event_bus", bus)
    monkeypatch.setattr(server, "_current_headline_context", lambda: {
        "commit_hash": "abc123", "commit_headline": "Make reloads visible",
    })

    server._write_restart_notice({"started_at_ms": 1})
    queue = bus.subscribe()
    asyncio.run(server._emit_restart_complete())

    topic, payload, seq = queue.get_nowait()
    assert topic == "server:restart"
    assert seq > 0
    assert payload["phase"] == "complete"
    assert payload["started_at_ms"] == 1
    assert payload["duration_ms"] >= 0
    assert payload["commit_headline"] == "Make reloads visible"
    assert not notice_path.exists()


def test_restart_notice_reader_rejects_invalid_state(tmp_path, monkeypatch):
    notice_path = tmp_path / "restart_notice.state"
    notice_path.write_text(json.dumps({"started_at_ms": "not-a-time"}))
    monkeypatch.setattr(server, "RESTART_NOTICE_STATE_PATH", notice_path)

    assert server._read_restart_notice() is None
