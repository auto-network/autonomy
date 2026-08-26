from __future__ import annotations

import json

import pytest

from tools.dashboard import server
from tools.dashboard.session_harness import parse_claude_log_line, parse_codex_log_line
from tools.dashboard.session_monitor import _OPERATOR_INPUT_TYPES


class _Request:
    def __init__(self, body: dict):
        self._body = body

    async def json(self) -> dict:
        return self._body


@pytest.mark.asyncio
async def test_typed_notification_is_idempotent_and_not_operator_input(monkeypatch):
    delivered: list[tuple[str, str]] = []

    async def fake_send(session: str, text: str) -> None:
        delivered.append((session, text))

    monkeypatch.setattr(server, "_tmux_session_exists", lambda _session: True)
    monkeypatch.setattr(server, "tmux_send", fake_send)
    server._SESSION_NOTIFICATION_IDS.clear()
    payload = {
        "tmux_session": "auto-test",
        "notification_id": "agent-test:at-1",
        "kind": "agent-test",
        "status": "failed",
        "summary": "Agent Test failed: 2 < 3 & evidence retained",
        "body": "Use the retained failure query.",
    }

    first = await server.api_session_notify(_Request(payload))
    second = await server.api_session_notify(_Request(payload))

    assert json.loads(first.body)["status"] == "accepted"
    assert json.loads(second.body)["status"] == "duplicate"
    assert len(delivered) == 1
    envelope = delivered[0][1]
    claude = parse_claude_log_line(json.dumps({
        "type": "user", "timestamp": "2026-08-20T00:00:00Z",
        "message": {"role": "user", "content": envelope},
    }))
    assert claude["type"] == "system"
    assert claude["role"] == "system"
    assert claude["type"] not in _OPERATOR_INPUT_TYPES
    assert claude["body"] == "Use the retained failure query."

    codex = parse_codex_log_line(json.dumps({
        "timestamp": "2026-08-20T00:00:00Z",
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": envelope}],
        },
    }))
    assert codex["type"] == "system"
    assert codex["role"] == "system"
