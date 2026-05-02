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


def test_parse_codex_apply_patch_custom_tool_call_as_patch_tile():
    entry = parse_codex_log_line(_line({
        "timestamp": TS,
        "type": "response_item",
        "payload": {
            "type": "custom_tool_call",
            "status": "completed",
            "call_id": "call_patch_1",
            "name": "apply_patch",
            "input": (
                "*** Begin Patch\n"
                "*** Update File: tools/dashboard/session_harness.py\n"
                "@@\n"
                "-old\n"
                "+new\n"
                "*** End Patch\n"
            ),
        },
    }))

    assert entry == {
        "type": "tool_use",
        "role": "assistant",
        "tool_name": "Patch",
        "tool_id": "call_patch_1",
        "input": {
            "description": "tools/dashboard/session_harness.py",
            "file_path": "tools/dashboard/session_harness.py",
            "files": ["tools/dashboard/session_harness.py"],
            "patch": (
                "*** Begin Patch\n"
                "*** Update File: tools/dashboard/session_harness.py\n"
                "@@\n"
                "-old\n"
                "+new\n"
                "*** End Patch\n"
            ),
        },
        "timestamp": TS,
    }


def test_parse_codex_patch_apply_end_as_tool_result():
    entry = parse_codex_log_line(_line({
        "timestamp": TS,
        "type": "event_msg",
        "payload": {
            "type": "patch_apply_end",
            "call_id": "call_patch_1",
            "stdout": "Success. Updated the following files:\nM tools/dashboard/session_harness.py\n",
            "stderr": "",
            "success": True,
            "status": "completed",
            "changes": {
                "/workspace/repo/tools/dashboard/session_harness.py": {
                    "type": "update",
                    "unified_diff": "@@ -1 +1 @@\n-old\n+new\n",
                    "move_path": None,
                },
            },
        },
    }))

    assert entry["type"] == "tool_result"
    assert entry["tool_id"] == "call_patch_1"
    assert entry["result_kind"] == "patch_apply_end"
    assert entry["content"] == "Success. Updated the following files:\nM tools/dashboard/session_harness.py\n"
    assert entry["changed_files"] == ["tools/dashboard/session_harness.py"]
    assert entry["status"] == "completed"
    assert entry["is_error"] is False


def test_postprocess_codex_exec_into_read_tiles():
    entries = CODEX_HARNESS.postprocess_entries([
        {
            "type": "tool_use",
            "role": "assistant",
            "tool_name": "exec_command",
            "tool_id": "call_read",
            "input": {
                "cmd": "sed -n '1,2p' tools/dashboard/server.py",
                "command": "sed -n '1,2p' tools/dashboard/server.py",
                "cwd": "/workspace/repo",
            },
            "timestamp": TS,
        },
        {
            "type": "tool_result",
            "role": "tool",
            "tool_id": "call_read",
            "content": "line1\nline2\n",
            "is_error": False,
            "timestamp": TS,
            "result_kind": "exec_command",
            "exit_code": 0,
            "status": "completed",
            "cwd": "/workspace/repo",
            "command": "sed -n '1,2p' tools/dashboard/server.py",
            "parsed_cmd": [{
                "type": "read",
                "cmd": "sed -n '1,2p' tools/dashboard/server.py",
                "name": "server.py",
                "path": "tools/dashboard/server.py",
            }],
            "duration_seconds": 1.0,
        },
    ])

    assert entries[0]["tool_name"] == "Read"
    assert entries[0]["input"] == {"file_path": "tools/dashboard/server.py"}
    assert entries[1]["tool_id"] == "call_read"
    assert entries[1]["line_count"] == 2
    assert entries[1]["content"] == "line1\nline2\n"


def test_postprocess_codex_exec_tool_use_defaults_to_bash_tile():
    entries = CODEX_HARNESS.postprocess_entries([
        {
            "type": "tool_use",
            "role": "assistant",
            "tool_name": "exec_command",
            "tool_id": "call_bash",
            "input": {
                "cmd": "git status --short",
                "command": "git status --short",
                "cwd": "/workspace/repo",
            },
            "timestamp": TS,
        },
    ])

    assert entries == [{
        "type": "tool_use",
        "role": "assistant",
        "tool_name": "Bash",
        "tool_id": "call_bash",
        "input": {
            "cmd": "git status --short",
            "command": "git status --short",
            "cwd": "/workspace/repo",
        },
        "timestamp": TS,
    }]


def test_postprocess_codex_exec_chunk_can_update_prior_tool_use_and_expand_stacked_reads():
    entries = CODEX_HARNESS.postprocess_entries([
        {
            "type": "tool_result",
            "role": "tool",
            "tool_id": "call_multi",
            "content": "a1\na2\nb1\nb2\nb3\n",
            "is_error": False,
            "timestamp": TS,
            "result_kind": "exec_command",
            "exit_code": 0,
            "status": "completed",
            "cwd": "/workspace/repo",
            "command": "sed -n '1,2p' tools/a.py && sed -n '5,7p' tools/b.py",
            "parsed_cmd": [
                {"type": "read", "cmd": "sed -n '1,2p' tools/a.py", "name": "a.py", "path": "tools/a.py"},
                {"type": "read", "cmd": "sed -n '5,7p' tools/b.py", "name": "b.py", "path": "tools/b.py"},
            ],
            "duration_seconds": 1.0,
        },
    ])

    assert [entry["type"] for entry in entries] == ["tool_use", "tool_use", "tool_result", "tool_result"]
    assert [entry["tool_name"] for entry in entries[:2]] == ["Read", "Read"]
    assert entries[0]["tool_id"] == "call_multi"
    assert entries[1]["tool_id"] == "call_multi#2"
    assert entries[2]["content"] == "a1\na2\n"
    assert entries[2]["line_count"] == 2
    assert entries[3]["content"] == "b1\nb2\nb3\n"
    assert entries[3]["line_count"] == 3


def test_postprocess_codex_exec_into_grep_tile():
    entries = CODEX_HARNESS.postprocess_entries([
        {
            "type": "tool_result",
            "role": "tool",
            "tool_id": "call_rg",
            "content": "tools/dashboard/server.py:1:context_tokens\n",
            "is_error": False,
            "timestamp": TS,
            "result_kind": "exec_command",
            "exit_code": 0,
            "status": "completed",
            "cwd": "/workspace/repo",
            "command": "rg -n 'context_tokens' tools/dashboard -S",
            "parsed_cmd": [{
                "type": "search",
                "cmd": "rg -n 'context_tokens' tools/dashboard -S",
                "query": "context_tokens",
                "path": "tools/dashboard",
            }],
            "duration_seconds": 1.0,
        },
    ])

    assert entries[0]["type"] == "tool_use"
    assert entries[0]["tool_name"] == "Grep"
    assert entries[0]["input"] == {
        "pattern": "context_tokens",
        "path": "tools/dashboard",
    }
    assert entries[1]["type"] == "tool_result"
    assert entries[1]["tool_id"] == "call_rg"


def test_postprocess_codex_prefers_patch_apply_end_over_custom_tool_output(tmp_path):
    session_dir = tmp_path / "patch-session"
    session_dir.mkdir()
    parsed = []
    for raw in (
        {
            "timestamp": TS,
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "status": "completed",
                "call_id": "call_patch_1",
                "name": "apply_patch",
                "input": (
                    "*** Begin Patch\n"
                    "*** Update File: tools/dashboard/session_harness.py\n"
                    "*** End Patch\n"
                ),
            },
        },
        {
            "timestamp": TS,
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "call_patch_1",
                "output": json.dumps({
                    "output": "fallback result",
                    "metadata": {"exit_code": 0, "duration_seconds": 0.0},
                }),
            },
        },
        {
            "timestamp": TS,
            "type": "event_msg",
            "payload": {
                "type": "patch_apply_end",
                "call_id": "call_patch_1",
                "stdout": "Success. Updated the following files:\nM tools/dashboard/session_harness.py\n",
                "stderr": "",
                "success": True,
                "status": "completed",
                "changes": {
                    "/workspace/repo/tools/dashboard/session_harness.py": {
                        "type": "update",
                        "unified_diff": "@@ -1 +1 @@\n-old\n+new\n",
                        "move_path": None,
                    },
                },
            },
        },
    ):
        parsed_entry = parse_codex_log_line(_line(raw))
        if isinstance(parsed_entry, list):
            parsed.extend(parsed_entry)
        elif parsed_entry:
            parsed.append(parsed_entry)

    entries = CODEX_HARNESS.postprocess_entries(parsed, session_dir=session_dir)

    assert [entry["type"] for entry in entries] == ["tool_use", "tool_result"]
    assert entries[0]["tool_name"] == "Patch"
    assert entries[1]["result_kind"] == "patch_apply_end"
    assert entries[1]["content"] == "Success. Updated the following files:\nM tools/dashboard/session_harness.py\n"


def test_postprocess_codex_folds_write_stdin_progress_into_parent_exec_tile(tmp_path):
    session_dir = tmp_path / "progress-session"
    session_dir.mkdir()
    parsed = []
    for raw in (
        {
            "timestamp": "2026-04-24T01:16:23.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "arguments": json.dumps({
                    "cmd": "pytest tools/dashboard/tests/test_worktrees.py -q",
                    "workdir": "/workspace/repo",
                    "yield_time_ms": 1000,
                }),
                "call_id": "call_exec_1",
            },
        },
        {
            "timestamp": "2026-04-24T01:16:24.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_exec_1",
                "output": (
                    "Chunk ID: start\n"
                    "Wall time: 1.0017 seconds\n"
                    "Process running with session ID 27299\n"
                    "Original token count: 12\n"
                    "Output:\n"
                    "bringing up nodes...\n"
                ),
            },
        },
        {
            "timestamp": "2026-04-24T01:16:27.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "write_stdin",
                "arguments": json.dumps({
                    "session_id": 27299,
                    "chars": "",
                    "yield_time_ms": 1000,
                    "max_output_tokens": 3000,
                }),
                "call_id": "call_write_1",
            },
        },
        {
            "timestamp": "2026-04-24T01:16:32.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_write_1",
                "output": (
                    "Chunk ID: poll\n"
                    "Wall time: 5.0007 seconds\n"
                    "Process running with session ID 27299\n"
                    "Original token count: 2\n"
                    "Output:\n"
                    "......."
                ),
            },
        },
    ):
        parsed_entry = parse_codex_log_line(_line(raw))
        if isinstance(parsed_entry, list):
            parsed.extend(parsed_entry)
        elif parsed_entry:
            parsed.append(parsed_entry)

    entries = CODEX_HARNESS.postprocess_entries(parsed, session_dir=session_dir)

    tool_uses = [entry for entry in entries if entry["type"] == "tool_use"]
    tool_results = [entry for entry in entries if entry["type"] == "tool_result"]

    assert len(tool_uses) == 1
    assert tool_uses[0]["tool_name"] == "Bash"
    assert tool_uses[0]["tool_id"] == "call_exec_1"
    assert all(entry["tool_id"] == "call_exec_1" for entry in tool_results)
    assert all(entry["status"] == "running" for entry in tool_results)
    assert tool_results[-1]["content"] == "......."


def test_postprocess_codex_drops_late_write_stdin_after_exec_completion(tmp_path):
    session_dir = tmp_path / "complete-session"
    session_dir.mkdir()
    parsed = []
    for raw in (
        {
            "timestamp": "2026-04-24T01:16:23.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "arguments": json.dumps({
                    "cmd": "pytest tools/dashboard/tests/test_worktrees.py -q",
                    "workdir": "/workspace/repo",
                }),
                "call_id": "call_exec_1",
            },
        },
        {
            "timestamp": "2026-04-24T01:16:24.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_exec_1",
                "output": (
                    "Chunk ID: start\n"
                    "Wall time: 1.0017 seconds\n"
                    "Process running with session ID 27299\n"
                    "Original token count: 12\n"
                    "Output:\n"
                    "bringing up nodes...\n"
                ),
            },
        },
        {
            "timestamp": "2026-04-24T01:16:41.000Z",
            "type": "event_msg",
            "payload": {
                "type": "exec_command_end",
                "call_id": "call_exec_1",
                "process_id": "27299",
                "command": ["/bin/bash", "-lc", "pytest tools/dashboard/tests/test_worktrees.py -q"],
                "cwd": "/workspace/repo",
                "parsed_cmd": [{"type": "unknown", "cmd": "pytest tools/dashboard/tests/test_worktrees.py -q"}],
                "aggregated_output": "....................\n20 passed in 18.06s\n",
                "exit_code": 0,
                "duration": {"secs": 18, "nanos": 0},
                "status": "completed",
            },
        },
        {
            "timestamp": "2026-04-24T01:16:43.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "write_stdin",
                "arguments": json.dumps({
                    "session_id": 27299,
                    "chars": "",
                    "yield_time_ms": 1000,
                    "max_output_tokens": 3000,
                }),
                "call_id": "call_write_1",
            },
        },
        {
            "timestamp": "2026-04-24T01:16:44.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_write_1",
                "output": (
                    "Chunk ID: done\n"
                    "Wall time: 0.0000 seconds\n"
                    "Process exited with code 0\n"
                    "Original token count: 21\n"
                    "Output:\n"
                    "... [100%]\n"
                    "20 passed in 18.06s\n"
                ),
            },
        },
    ):
        parsed_entry = parse_codex_log_line(_line(raw))
        if isinstance(parsed_entry, list):
            parsed.extend(parsed_entry)
        elif parsed_entry:
            parsed.append(parsed_entry)

    entries = CODEX_HARNESS.postprocess_entries(parsed, session_dir=session_dir)
    tool_results = [entry for entry in entries if entry["type"] == "tool_result" and entry["tool_id"] == "call_exec_1"]

    assert tool_results[-1]["status"] == "completed"
    assert tool_results[-1]["exit_code"] == 0
    assert tool_results[-1]["content"] == "....................\n20 passed in 18.06s\n"


def test_postprocess_codex_drops_late_exec_function_call_output_after_completion(tmp_path):
    session_dir = tmp_path / "late-exec-output"
    session_dir.mkdir()
    parsed = []
    for raw in (
        {
            "timestamp": "2026-04-24T01:16:23.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "arguments": json.dumps({
                    "cmd": "git status --short",
                    "workdir": "/workspace/repo",
                }),
                "call_id": "call_exec_1",
            },
        },
        {
            "timestamp": "2026-04-24T01:16:24.000Z",
            "type": "event_msg",
            "payload": {
                "type": "exec_command_end",
                "call_id": "call_exec_1",
                "process_id": "27299",
                "command": ["/bin/bash", "-lc", "git status --short"],
                "cwd": "/workspace/repo",
                "parsed_cmd": [{"type": "unknown", "cmd": "git status --short"}],
                "aggregated_output": "M tools/dashboard/session_harness.py\n",
                "exit_code": 0,
                "duration": {"secs": 1, "nanos": 0},
                "status": "completed",
            },
        },
        {
            "timestamp": "2026-04-24T01:16:25.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_exec_1",
                "output": (
                    "Chunk ID: done\n"
                    "Wall time: 0.0000 seconds\n"
                    "Process exited with code 0\n"
                    "Original token count: 4\n"
                    "Output:\n"
                    "M tools/dashboard/session_harness.py\n"
                ),
            },
        },
    ):
        parsed_entry = parse_codex_log_line(_line(raw))
        if isinstance(parsed_entry, list):
            parsed.extend(parsed_entry)
        elif parsed_entry:
            parsed.append(parsed_entry)

    entries = CODEX_HARNESS.postprocess_entries(parsed, session_dir=session_dir)

    tool_results = [entry for entry in entries if entry["type"] == "tool_result"]
    assert len(tool_results) == 1
    assert tool_results[0]["result_kind"] == "exec_command"
    assert tool_results[0]["status"] == "completed"
    assert tool_results[0]["exit_code"] == 0
    assert tool_results[0]["content"] == "M tools/dashboard/session_harness.py\n"


def test_postprocess_codex_exec_completion_from_function_call_output_is_completed(tmp_path):
    session_dir = tmp_path / "exec-output-complete"
    session_dir.mkdir()
    parsed = []
    for raw in (
        {
            "timestamp": "2026-04-24T01:16:23.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "arguments": json.dumps({
                    "cmd": "git status --short",
                    "workdir": "/workspace/repo",
                }),
                "call_id": "call_exec_1",
            },
        },
        {
            "timestamp": "2026-04-24T01:16:24.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_exec_1",
                "output": (
                    "Chunk ID: done\n"
                    "Wall time: 0.0000 seconds\n"
                    "Process exited with code 0\n"
                    "Original token count: 4\n"
                    "Output:\n"
                    "M tools/dashboard/session_harness.py\n"
                ),
            },
        },
    ):
        parsed_entry = parse_codex_log_line(_line(raw))
        if isinstance(parsed_entry, list):
            parsed.extend(parsed_entry)
        elif parsed_entry:
            parsed.append(parsed_entry)

    entries = CODEX_HARNESS.postprocess_entries(parsed, session_dir=session_dir)

    tool_results = [entry for entry in entries if entry["type"] == "tool_result"]
    assert len(tool_results) == 1
    assert tool_results[0]["result_kind"] == "exec_command"
    assert tool_results[0]["status"] == "completed"
    assert tool_results[0]["exit_code"] == 0
    assert tool_results[0]["is_error"] is False


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


def test_parse_codex_inbound_crosstalk_user_message():
    entry = parse_codex_log_line(_line({
        "timestamp": TS,
        "type": "event_msg",
        "payload": {
            "type": "user_message",
            "message": (
                '<crosstalk from="host-0422-201533" label="Dashboard UI" '
                'source="5706c4cc-6570-4acd-a457-a8907bdb54f5" turn="1774" '
                'timestamp="2026-04-23T21:41:16Z">\n'
                'Rebase required before your commit can be merged.\n'
                '</crosstalk>'
            ),
        },
    }))

    assert entry == {
        "type": "crosstalk",
        "role": "crosstalk",
        "content": "Rebase required before your commit can be merged.",
        "sender": "host-0422-201533",
        "sender_label": "Dashboard UI",
        "source_id": "5706c4cc-6570-4acd-a457-a8907bdb54f5",
        "turn": "1774",
        "timestamp": TS,
    }


def test_parse_codex_inbound_crosstalk_allows_angle_bracket_code():
    entry = parse_codex_log_line(_line({
        "timestamp": TS,
        "type": "event_msg",
        "payload": {
            "type": "user_message",
            "message": (
                '<crosstalk from="host-0422-201533" label="Dashboard UI" '
                'source="5706c4cc-6570-4acd-a457-a8907bdb54f5" turn="1774" '
                'timestamp="2026-04-23T21:41:16Z">\n'
                'if (left < right && total > 0) return items[i];\n'
                '</crosstalk>'
            ),
        },
    }))

    assert entry == {
        "type": "crosstalk",
        "role": "crosstalk",
        "content": "if (left < right && total > 0) return items[i];",
        "sender": "host-0422-201533",
        "sender_label": "Dashboard UI",
        "source_id": "5706c4cc-6570-4acd-a457-a8907bdb54f5",
        "turn": "1774",
        "timestamp": TS,
    }


def test_parse_codex_compacted_history_as_compact_summary():
    entry = parse_codex_log_line(_line({
        "timestamp": TS,
        "type": "compacted",
        "payload": {
            "message": "",
            "replacement_history": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Hello"}],
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": [{
                        "type": "input_text",
                        "text": (
                            '<crosstalk from="dashboard-ui" label="Dashboard UI" source="" turn="0" '
                            'timestamp="2026-04-23T21:43:41Z">\n'
                            'Rebase required.\n'
                            '</crosstalk>'
                        ),
                    }],
                },
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Rebased onto master."}],
                },
                {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "hidden"}],
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": [{
                        "type": "input_text",
                        "text": (
                            "<environment_context>\n"
                            "  <cwd>/workspace/repo</cwd>\n"
                            "</environment_context>"
                        ),
                    }],
                },
            ],
        },
    }))

    assert entry["type"] == "compact_summary"
    assert entry["role"] == "compact_summary"
    assert entry["timestamp"] == TS
    assert "User: Hello" in entry["content"]
    assert "Crosstalk from Dashboard UI: Rebase required." in entry["content"]
    assert "Assistant: Rebased onto master." in entry["content"]
    assert "hidden" not in entry["content"]
    assert "<environment_context>" not in entry["content"]


def test_parse_codex_task_lifecycle_events_are_suppressed():
    started = parse_codex_log_line(_line({
        "timestamp": TS,
        "type": "event_msg",
        "payload": {
            "type": "task_started",
            "turn_id": "turn_123",
            "started_at": 1776905065,
            "model_context_window": 258400,
            "collaboration_mode_kind": "default",
        },
    }))
    complete = parse_codex_log_line(_line({
        "timestamp": TS,
        "type": "event_msg",
        "payload": {
            "type": "task_complete",
            "turn_id": "turn_123",
            "completed_at": 1776905067,
            "duration_ms": 2390,
            "last_agent_message": "Hello. How can I help?",
        },
    }))

    assert started is None
    assert complete is None


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
