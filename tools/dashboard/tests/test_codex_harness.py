"""Codex session harness contract tests.

These tests pin the provider-specific extraction boundary: Codex rollout
records become the same normalized entries consumed by the shared monitor and
viewer, without promoting cumulative usage into the context-token stat.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from tools.dashboard.session_harness import (
    CODEX_HARNESS,
    extract_codex_context_tokens,
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


# ── Tests from local scaffolding (complementary coverage) ────────────


def test_codex_register_session_uses_run_sessions_dir(tmp_path):
    run_dir = tmp_path / "run"
    sessions_dir = run_dir / "sessions"
    sessions_dir.mkdir(parents=True)

    monitor = AsyncMock()
    asyncio.run(
        CODEX_HARNESS.register_session(
            monitor=monitor,
            tmux_name="auto-codex-1",
            session_type="container",
            project="autonomy-codex",
            run_dir=run_dir,
            seed_message="Starting...",
        ),
    )

    monitor.register.assert_awaited_once()
    kwargs = monitor.register.await_args.kwargs
    assert kwargs["jsonl_path"] == sessions_dir
    assert kwargs["resolution_dir"] == sessions_dir
    assert kwargs["harness"] == "codex"


def test_resolve_harness_for_path_reads_session_meta(tmp_path):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / ".session_meta.json").write_text(json.dumps({"harness": "codex"}))
    nested = sessions_dir / "2026" / "04" / "22"
    nested.mkdir(parents=True)
    rollout = nested / "rollout-2026-04-22T22-40-19-uuid.jsonl"
    rollout.write_text("")

    harness = resolve_harness_for_path(rollout)
    assert harness.name == "codex"



def test_parse_codex_log_line_parses_update_plan_and_function_call_output():
    parsed = parse_codex_log_line(json.dumps({
        "timestamp": "2026-04-22T22:48:14.033Z",
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": "update_plan",
            "arguments": json.dumps({
                "plan": [
                    {"step": "Trace current code", "status": "in_progress"},
                    {"step": "Add parser", "status": "pending"},
                ],
            }),
            "call_id": "call_plan_1",
        },
    }))
    assert isinstance(parsed, list)
    assert parsed[0]["type"] == "tool_use"
    assert parsed[0]["tool_name"] == "update_plan"
    assert parsed[0]["tool_id"] == "call_plan_1"
    assert parsed[1]["type"] == "todo_plan"
    assert parsed[1]["todos"] == [
        {"subject": "Trace current code", "status": "in_progress"},
        {"subject": "Add parser", "status": "pending"},
    ]

    result = parse_codex_log_line(json.dumps({
        "timestamp": "2026-04-22T22:54:03.009Z",
        "type": "response_item",
        "payload": {
            "type": "function_call_output",
            "call_id": "call_exec_1",
            "output": "Chunk ID: 123\nOutput:\n/workspace/repo\n",
        },
    }))
    assert result is not None
    assert result["type"] == "tool_result"
    assert result["tool_id"] == "call_exec_1"


