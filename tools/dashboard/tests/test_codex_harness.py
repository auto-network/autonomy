"""Codex session harness contract tests.

These tests pin the provider-specific extraction boundary: Codex rollout
records become the same normalized entries consumed by the shared monitor and
viewer, without promoting cumulative usage into the context-token stat.
"""

from __future__ import annotations

import json

from tools.dashboard.session_harness import (
    CODEX_HARNESS,
    parse_codex_log_line,
    resolve_harness_for_path,
    resolve_harness_for_session_row,
)


TS = "2026-04-23T00:51:42.056Z"


def _line(obj: dict) -> str:
    return json.dumps(obj)


def test_resolve_codex_harness_from_rollout_path():
    harness = resolve_harness_for_path(
        "/home/agent/.codex/sessions/2026/04/23/"
        "rollout-2026-04-23T00-44-19-thread.jsonl"
    )
    assert harness.name == "codex"


def test_resolve_codex_harness_from_rollout_session_uuid():
    harness = resolve_harness_for_session_row({
        "session_uuid": "rollout-2026-04-23T00-44-19-thread",
    })
    assert harness.name == "codex"


def test_context_tokens_use_last_input_not_cumulative_or_cached():
    raw = {
        "timestamp": TS,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "last_token_usage": {
                    "input_tokens": 140_593,
                    "cached_input_tokens": 139_648,
                    "output_tokens": 619,
                    "total_tokens": 141_212,
                },
                "total_token_usage": {
                    "input_tokens": 3_308_209,
                    "cached_input_tokens": 3_047_424,
                    "output_tokens": 13_227,
                    "total_tokens": 3_321_436,
                },
                "model_context_window": 258_400,
            },
        },
    }

    assert CODEX_HARNESS.extract_context_tokens(raw, 0) == 140_593


def test_parse_codex_exec_command_call_as_tool_use():
    entry = parse_codex_log_line(_line({
        "timestamp": TS,
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": "exec_command",
            "arguments": json.dumps({
                "cmd": "git status --short",
                "workdir": "/workspace/repo",
                "yield_time_ms": 1000,
            }),
            "call_id": "call_abc",
        },
    }))

    assert entry["type"] == "tool_use"
    assert entry["tool_name"] == "exec_command"
    assert entry["tool_id"] == "call_abc"
    assert entry["input"]["cmd"] == "git status --short"
    assert entry["input"]["command"] == "git status --short"
    assert entry["input"]["cwd"] == "/workspace/repo"


def test_parse_codex_exec_command_end_as_rich_tool_result():
    entry = parse_codex_log_line(_line({
        "timestamp": TS,
        "type": "event_msg",
        "payload": {
            "type": "exec_command_end",
            "call_id": "call_abc",
            "process_id": "123",
            "command": ["/bin/bash", "-lc", "git status --short"],
            "cwd": "/workspace/repo",
            "aggregated_output": " M tools/dashboard/session_harness.py\n",
            "exit_code": 0,
            "duration": {"secs": 1, "nanos": 500_000_000},
            "status": "completed",
        },
    }))

    assert entry["type"] == "tool_result"
    assert entry["tool_id"] == "call_abc"
    assert entry["result_kind"] == "exec_command"
    assert entry["content"] == " M tools/dashboard/session_harness.py\n"
    assert entry["command"] == "git status --short"
    assert entry["cwd"] == "/workspace/repo"
    assert entry["exit_code"] == 0
    assert entry["is_error"] is False
    assert entry["duration_seconds"] == 1.5


def test_parse_codex_user_and_agent_event_messages():
    user = parse_codex_log_line(_line({
        "timestamp": TS,
        "type": "event_msg",
        "payload": {"type": "user_message", "message": "Hello"},
    }))
    assistant = parse_codex_log_line(_line({
        "timestamp": TS,
        "type": "event_msg",
        "payload": {"type": "agent_message", "message": "Hello. How can I help?"},
    }))

    assert user == {
        "type": "user",
        "role": "user",
        "content": "Hello",
        "timestamp": TS,
    }
    assert assistant == {
        "type": "assistant_text",
        "role": "assistant",
        "content": "Hello. How can I help?",
        "timestamp": TS,
    }
