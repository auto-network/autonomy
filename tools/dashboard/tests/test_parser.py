"""Comprehensive tests for _parse_jsonl_entry() — the JSONL parsing pipeline.

Every test uses realistic JSONL lines matching actual Claude Code session output.
Covers: classification, semantic bash, upconversion, content extraction, noise
filtering, crosstalk parsing, system messages, and edge cases.
"""

import json

import pytest

from tools.dashboard.server import _parse_jsonl_entry


# ── Helpers ───────────────────────────────────────────────────────────

def _line(obj: dict) -> str:
    """Serialize a dict to a JSONL line string."""
    return json.dumps(obj)


TS = "2026-03-24T12:00:00Z"


# ── Realistic JSONL fixtures ─────────────────────────────────────────
# Modeled on real Claude Code JSONL output observed in data/agent-runs/.

FIXTURE_USER_STRING = {
    "parentUuid": "aaa", "isSidechain": False, "type": "user",
    "message": {"role": "user", "content": "Hello, can you help me with the session cards?"},
    "timestamp": TS,
}

FIXTURE_USER_LIST_TEXT = {
    "parentUuid": "bbb", "isSidechain": False, "type": "user",
    "message": {"role": "user", "content": [
        {"type": "text", "text": "Fix this bug in the dashboard"},
    ]},
    "timestamp": TS,
}

FIXTURE_USER_LIST_TOOL_RESULT = {
    "parentUuid": "ccc", "isSidechain": False, "type": "user",
    "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_01ABC", "content": "file1.py\nfile2.py\n"},
    ]},
    "timestamp": TS,
}

FIXTURE_USER_LIST_TOOL_RESULT_BLOCKS = {
    "parentUuid": "ccc2", "isSidechain": False, "type": "user",
    "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_01DEF",
         "content": [{"type": "text", "text": "line1\n"}, {"type": "text", "text": "line2\n"}]},
    ]},
    "timestamp": TS,
}

FIXTURE_ASSISTANT_TEXT = {
    "parentUuid": "ddd", "isSidechain": False, "type": "assistant",
    "message": {"role": "assistant", "content": [
        {"type": "text", "text": "I can see the issue. Let me fix it."},
    ]},
    "timestamp": TS,
}

FIXTURE_ASSISTANT_TOOL_USE_READ = {
    "parentUuid": "eee", "isSidechain": False, "type": "assistant",
    "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_01ABC", "name": "Read",
         "input": {"file_path": "/workspace/repo/tools/dashboard/server.py"}},
    ]},
    "timestamp": TS,
}

FIXTURE_ASSISTANT_THINKING = {
    "parentUuid": "fff", "isSidechain": False, "type": "assistant",
    "message": {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "Let me analyze this problem step by step..."},
    ]},
    "timestamp": TS,
}

FIXTURE_ASSISTANT_MIXED = {
    "parentUuid": "ggg", "isSidechain": False, "type": "assistant",
    "message": {"role": "assistant", "content": [
        {"type": "text", "text": "Searching for the issue..."},
        {"type": "tool_use", "id": "toolu_01XYZ", "name": "Grep",
         "input": {"pattern": "def main", "path": "/workspace/repo"}},
    ]},
    "timestamp": TS,
}

FIXTURE_PROGRESS = {
    "parentUuid": "hhh", "isSidechain": True, "type": "progress",
    "data": {"type": "hook_progress", "hookEvent": "PostToolUse"},
    "timestamp": TS,
}

FIXTURE_SYSTEM = {
    "type": "system", "timestamp": TS,
}

FIXTURE_SIDECHAIN = {
    "parentUuid": "iii", "isSidechain": True, "type": "user",
    "message": {"role": "user", "content": "Find the function definition"},
    "timestamp": TS,
}

FIXTURE_QUEUE_ENQUEUE = {
    "type": "queue-operation", "operation": "enqueue",
    "content": "Stop working on that and do this instead",
    "timestamp": TS,
}

FIXTURE_QUEUE_DEQUEUE = {
    "type": "queue-operation", "operation": "dequeue",
    "content": "Stop working on that and do this instead",
    "timestamp": TS,
}

FIXTURE_BASH_CROSSTALK_SEND = {
    "parentUuid": "jjj", "isSidechain": False, "type": "assistant",
    "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_01CT", "name": "Bash",
         "input": {"command": 'curl -sk -X POST https://localhost:8765/crosstalk/send '
                              '-d \'{"target":"auto-peer-session","message":"Analysis complete"}\''}}
    ]},
    "timestamp": TS,
}

FIXTURE_BASH_GRAPH_COMMENT = {
    "parentUuid": "kkk", "isSidechain": False, "type": "assistant",
    "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_01GC", "name": "Bash",
         "input": {"command": "graph comment dc4c73ee 'Fixed the logic in step 3'"}}
    ]},
    "timestamp": TS,
}

FIXTURE_BASH_GRAPH_COMMENT_INTEGRATE = {
    "parentUuid": "lll", "isSidechain": False, "type": "assistant",
    "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_01GCI", "name": "Bash",
         "input": {"command": "graph comment integrate df5f1546"}}
    ]},
    "timestamp": TS,
}

FIXTURE_BASH_DISPATCH_APPROVE = {
    "parentUuid": "mmm", "isSidechain": False, "type": "assistant",
    "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_01DA", "name": "Bash",
         "input": {"command": "graph dispatch approve auto-5kj"}}
    ]},
    "timestamp": TS,
}

FIXTURE_BASH_BD_SETSTATE = {
    "parentUuid": "nnn", "isSidechain": False, "type": "assistant",
    "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_01BS", "name": "Bash",
         "input": {"command": "bd set-state auto-5kj status=done --reason 'all tests pass'"}}
    ]},
    "timestamp": TS,
}

FIXTURE_TOOL_RESULT_STRING = {
    "type": "tool_result", "toolUseId": "toolu_01ABC",
    "message": {"role": "user", "content": "file1.py\nfile2.py\nfile3.py\n"},
    "is_error": False,
    "timestamp": TS,
}

FIXTURE_TOOL_RESULT_LIST = {
    "type": "tool_result", "toolUseId": "toolu_01DEF",
    "message": {"role": "user", "content": [
        {"type": "text", "text": "total 24\ndrwxr-xr-x 4 user user 4096\n"},
    ]},
    "timestamp": TS,
}

FIXTURE_TOOL_RESULT_NOTE_SAVED = {
    "type": "tool_result", "toolUseId": "toolu_01NS",
    "message": {"role": "user", "content": "\u2713 Note saved (src:f6c6c43e-24a1-4b9f-8c3d-1e2f3a4b5c6d)\nTags: pitfall, dashboard"},
    "timestamp": TS,
}

FIXTURE_TOOL_RESULT_THOUGHT_CAPTURED = {
    "type": "tool_result", "toolUseId": "toolu_01TC",
    "message": {"role": "user", "content": "\u2713 Captured: a1b2c3d4-5e6f-7890-abcd-ef1234567890 (thought about auth passkeys)"},
    "timestamp": TS,
}

FIXTURE_TOOL_RESULT_COMMENT_ADDED = {
    "type": "tool_result", "toolUseId": "toolu_01CA",
    "message": {"role": "user", "content": "\u2713 Comment added (id:df5f1546-9a8b-4c7d-e6f5-1234abcd5678) on f6c6c43e"},
    "timestamp": TS,
}

FIXTURE_TOOL_RESULT_ERROR = {
    "type": "tool_result", "toolUseId": "toolu_01ERR",
    "message": {"role": "user", "content": "Error: file not found /workspace/missing.py"},
    "is_error": True,
    "timestamp": TS,
}

FIXTURE_USER_TASK_NOTIFICATION = {
    "parentUuid": "ooo", "isSidechain": False, "type": "user",
    "message": {"role": "user", "content": (
        "<task-notification>\n"
        "<summary>Building package</summary>\n"
        "<status>success</status>\n"
        "</task-notification>"
    )},
    "timestamp": TS,
}

FIXTURE_USER_SYSTEM_REMINDER = {
    "parentUuid": "ppp", "isSidechain": False, "type": "user",
    "message": {"role": "user", "content": (
        "<system-reminder>\n"
        "The following deferred tools are available: Read, Edit, Write\n"
        "</system-reminder>"
    )},
    "timestamp": TS,
}

FIXTURE_USER_COMMAND_NAME = {
    "parentUuid": "qqq", "isSidechain": False, "type": "user",
    "message": {"role": "user", "content": "<command-name>commit</command-name>"},
    "timestamp": TS,
}

FIXTURE_CROSSTALK_INBOUND = {
    "parentUuid": "rrr", "isSidechain": False, "type": "user",
    "message": {"role": "user", "content": (
        '<crosstalk from="auto-0323-022132" label="Librarian" '
        'source="bc23e1ff" turn="286" timestamp="2026-03-24T12:00:30Z">\n'
        'Please check that function\n'
        '</crosstalk>'
    )},
    "timestamp": TS,
}

FIXTURE_CROSSTALK_ANGLE_BRACKET_BODY = {
    "parentUuid": "sss", "isSidechain": False, "type": "user",
    "message": {"role": "user", "content": (
        '<crosstalk from="auto-evil" label="Attacker" '
        'source="deadbeef" turn="1" timestamp="2026-03-24T12:00:00Z">\n'
        '<script>alert("xss")</script>\n'
        '</crosstalk>'
    )},
    "timestamp": TS,
}

FIXTURE_QUEUE_TASK_NOTIFICATION = {
    "type": "queue-operation", "operation": "enqueue",
    "content": "<task-notification><summary>Build done</summary></task-notification>",
    "timestamp": TS,
}

FIXTURE_QUEUE_CROSSTALK = {
    "type": "queue-operation", "operation": "enqueue",
    "content": (
        '<crosstalk from="auto-peer" label="Peer" '
        'source="aabbccdd" turn="10" timestamp="2026-03-24T13:00:00Z">\n'
        'Hey, are you done yet?\n'
        '</crosstalk>'
    ),
    "timestamp": TS,
}


# ── TestEntryClassification ──────────────────────────────────────────

class TestEntryClassification:
    """Core entry type classification — the first routing decision."""

    def test_user_string_content(self):
        result = _parse_jsonl_entry(_line(FIXTURE_USER_STRING))
        assert result["type"] == "user"
        assert result["content"] == "Hello, can you help me with the session cards?"
        assert result["timestamp"] == TS

    def test_user_list_content_with_text(self):
        result = _parse_jsonl_entry(_line(FIXTURE_USER_LIST_TEXT))
        assert result["type"] == "user"
        assert result["content"] == "Fix this bug in the dashboard"

    def test_user_list_content_tool_result(self):
        result = _parse_jsonl_entry(_line(FIXTURE_USER_LIST_TOOL_RESULT))
        assert result["type"] == "tool_result"
        assert result["tool_id"] == "toolu_01ABC"
        assert "file1.py" in result["content"]

    def test_user_list_content_tool_result_with_list_content(self):
        """tool_result blocks where content is a list of text blocks."""
        result = _parse_jsonl_entry(_line(FIXTURE_USER_LIST_TOOL_RESULT_BLOCKS))
        assert result["type"] == "tool_result"
        assert result["content"] == "line1\nline2\n"

    def test_assistant_text_block(self):
        result = _parse_jsonl_entry(_line(FIXTURE_ASSISTANT_TEXT))
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["type"] == "assistant_text"
        assert result[0]["content"] == "I can see the issue. Let me fix it."

    def test_assistant_tool_use(self):
        result = _parse_jsonl_entry(_line(FIXTURE_ASSISTANT_TOOL_USE_READ))
        assert isinstance(result, list)
        entry = result[0]
        assert entry["type"] == "tool_use"
        assert entry["tool_name"] == "Read"
        assert entry["tool_id"] == "toolu_01ABC"
        assert entry["input"] == {"file_path": "/workspace/repo/tools/dashboard/server.py"}

    def test_assistant_thinking(self):
        result = _parse_jsonl_entry(_line(FIXTURE_ASSISTANT_THINKING))
        assert isinstance(result, list)
        assert result[0]["type"] == "thinking"
        assert "analyze this problem" in result[0]["content"]

    def test_assistant_mixed_blocks(self):
        """Text + tool_use in same message → multiple entries returned."""
        result = _parse_jsonl_entry(_line(FIXTURE_ASSISTANT_MIXED))
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["type"] == "assistant_text"
        assert result[1]["type"] == "tool_use"
        assert result[1]["tool_name"] == "Grep"

    def test_progress_discarded(self):
        result = _parse_jsonl_entry(_line(FIXTURE_PROGRESS))
        assert result is None

    def test_system_discarded(self):
        result = _parse_jsonl_entry(_line(FIXTURE_SYSTEM))
        assert result is None

    def test_compact_summary_flagged(self):
        """isCompactSummary=true → compact_summary entry, not user bubble."""
        summary_body = (
            "This session is being continued from a previous conversation "
            "that ran out of context. The prior session did X, Y, Z."
        )
        raw = {
            "parentUuid": "cs1", "isSidechain": False, "type": "user",
            "isCompactSummary": True, "isVisibleInTranscriptOnly": True,
            "message": {"role": "user", "content": summary_body},
            "timestamp": TS,
        }
        result = _parse_jsonl_entry(_line(raw))
        assert result["type"] == "compact_summary"
        assert result["role"] == "compact_summary"
        assert result["content"] == summary_body
        assert result["timestamp"] == TS

    def test_compact_summary_list_content(self):
        """isCompactSummary with list-of-text-blocks content also classified."""
        raw = {
            "parentUuid": "cs2", "isSidechain": False, "type": "user",
            "isCompactSummary": True,
            "message": {"role": "user", "content": [
                {"type": "text", "text": "This session is being continued..."}
            ]},
            "timestamp": TS,
        }
        result = _parse_jsonl_entry(_line(raw))
        assert result["type"] == "compact_summary"
        assert "continued" in result["content"]

    def test_sidechain_discarded(self):
        result = _parse_jsonl_entry(_line(FIXTURE_SIDECHAIN))
        assert result is None

    def test_queue_operation_enqueue(self):
        result = _parse_jsonl_entry(_line(FIXTURE_QUEUE_ENQUEUE))
        assert result["type"] == "user"
        assert result["content"] == "Stop working on that and do this instead"
        assert result["queued"] is True

    def test_queue_operation_dequeue_skipped(self):
        result = _parse_jsonl_entry(_line(FIXTURE_QUEUE_DEQUEUE))
        assert result is None

    def test_tool_result_string_content(self):
        result = _parse_jsonl_entry(_line(FIXTURE_TOOL_RESULT_STRING))
        assert result["type"] == "tool_result"
        assert result["tool_id"] == "toolu_01ABC"
        assert "file1.py" in result["content"]

    def test_tool_result_list_content(self):
        result = _parse_jsonl_entry(_line(FIXTURE_TOOL_RESULT_LIST))
        assert result["type"] == "tool_result"
        assert result["tool_id"] == "toolu_01DEF"
        assert "drwxr-xr-x" in result["content"]


# ── TestSemanticBashClassification ───────────────────────────────────

class TestSemanticBashClassification:
    """Semantic Bash: special tool_use entries that get reclassified."""

    def test_crosstalk_send_detected(self):
        result = _parse_jsonl_entry(_line(FIXTURE_BASH_CROSSTALK_SEND))
        assert isinstance(result, list)
        entry = result[0]
        assert entry["type"] == "crosstalk"
        assert entry["direction"] == "sent"
        assert entry["target"] == "auto-peer-session"
        assert entry["content"] == "Analysis complete"

    def test_graph_comment_detected(self):
        result = _parse_jsonl_entry(_line(FIXTURE_BASH_GRAPH_COMMENT))
        assert isinstance(result, list)
        entry = result[0]
        assert entry["type"] == "semantic_bash"
        assert entry["semantic_type"] == "comment-added"
        assert entry["source_id"] == "dc4c73ee"

    def test_graph_comment_integrate_skipped(self):
        """'graph comment integrate' should NOT be semantic — regular tool_use."""
        result = _parse_jsonl_entry(_line(FIXTURE_BASH_GRAPH_COMMENT_INTEGRATE))
        assert isinstance(result, list)
        entry = result[0]
        assert entry["type"] == "tool_use"
        assert entry["tool_name"] == "Bash"

    def test_dispatch_approve_detected(self):
        result = _parse_jsonl_entry(_line(FIXTURE_BASH_DISPATCH_APPROVE))
        assert isinstance(result, list)
        entry = result[0]
        assert entry["type"] == "semantic_bash"
        assert entry["semantic_type"] == "dispatch-approved"
        assert entry["bead_id"] == "auto-5kj"

    def test_bd_setstate_detected(self):
        result = _parse_jsonl_entry(_line(FIXTURE_BASH_BD_SETSTATE))
        assert isinstance(result, list)
        entry = result[0]
        assert entry["type"] == "semantic_bash"
        assert entry["semantic_type"] == "state-changed"
        assert entry["bead_id"] == "auto-5kj"
        assert "status=done" in entry["state"]

    def test_non_bash_tool_not_classified(self):
        """Read/Write/Edit/Grep → regular tool_use, no semantic classification."""
        for name in ("Read", "Write", "Edit", "Grep"):
            fixture = {
                "type": "assistant", "timestamp": TS,
                "message": {"role": "assistant", "content": [
                    {"type": "tool_use", "id": f"toolu_{name}", "name": name,
                     "input": {"file_path": "/tmp/test.py"}},
                ]},
            }
            result = _parse_jsonl_entry(_line(fixture))
            assert isinstance(result, list)
            assert result[0]["type"] == "tool_use"
            assert result[0]["tool_name"] == name

    def test_bash_without_semantic_pattern(self):
        """Plain Bash commands (ls, git) → regular tool_use."""
        fixture = {
            "type": "assistant", "timestamp": TS,
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_plain", "name": "Bash",
                 "input": {"command": "git status"}},
            ]},
        }
        result = _parse_jsonl_entry(_line(fixture))
        assert isinstance(result, list)
        assert result[0]["type"] == "tool_use"
        assert result[0]["tool_name"] == "Bash"


# ── TestUpconversion ─────────────────────────────────────────────────

class TestUpconversion:
    """Graph CLI result upconversion to semantic tiles."""

    def test_note_saved_upconverted(self):
        result = _parse_jsonl_entry(_line(FIXTURE_TOOL_RESULT_NOTE_SAVED))
        entries = result if isinstance(result, list) else [result]
        sem = next((e for e in entries if e.get("type") == "semantic_bash"), None)
        assert sem is not None
        assert sem["semantic_type"] == "note-created"
        assert sem["source_id"] == "f6c6c43e-24a1-4b9f-8c3d-1e2f3a4b5c6d"

    def test_thought_captured_upconverted(self):
        result = _parse_jsonl_entry(_line(FIXTURE_TOOL_RESULT_THOUGHT_CAPTURED))
        entries = result if isinstance(result, list) else [result]
        sem = next((e for e in entries if e.get("type") == "semantic_bash"), None)
        assert sem is not None
        assert sem["semantic_type"] == "thought-captured"
        assert sem["source_id"] == "a1b2c3d4-5e6f-7890-abcd-ef1234567890"

    def test_comment_added_upconverted(self):
        result = _parse_jsonl_entry(_line(FIXTURE_TOOL_RESULT_COMMENT_ADDED))
        entries = result if isinstance(result, list) else [result]
        sem = next((e for e in entries if e.get("type") == "semantic_bash"), None)
        assert sem is not None
        assert sem["semantic_type"] == "comment-added"
        assert sem["comment_id"] == "df5f1546-9a8b-4c7d-e6f5-1234abcd5678"

    def test_normal_tool_result_not_upconverted(self):
        result = _parse_jsonl_entry(_line(FIXTURE_TOOL_RESULT_STRING))
        assert result["type"] == "tool_result"
        assert "semantic_type" not in result

    def test_note_saved_in_user_tool_result_block(self):
        """Upconversion also works for tool_result blocks inside user messages."""
        fixture = {
            "type": "user", "timestamp": TS,
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_01NS",
                 "content": "\u2713 Note saved (src:abcdef12-3456)"},
            ]},
        }
        result = _parse_jsonl_entry(_line(fixture))
        entries = result if isinstance(result, list) else [result]
        sem = next((e for e in entries if e.get("type") == "semantic_bash"), None)
        assert sem is not None
        assert sem["semantic_type"] == "note-created"
        assert sem["source_id"] == "abcdef12-3456"


# ── TestContentExtraction ────────────────────────────────────────────

class TestContentExtraction:
    """Content extraction, field preservation, and truncation."""

    def test_tool_use_input_passed_through(self):
        """input dict preserved in full (not serialized to string)."""
        result = _parse_jsonl_entry(_line(FIXTURE_ASSISTANT_TOOL_USE_READ))
        entry = result[0]
        assert isinstance(entry["input"], dict)
        assert entry["input"]["file_path"] == "/workspace/repo/tools/dashboard/server.py"

    def test_tool_use_tool_id_preserved(self):
        result = _parse_jsonl_entry(_line(FIXTURE_ASSISTANT_TOOL_USE_READ))
        assert result[0]["tool_id"] == "toolu_01ABC"

    def test_is_error_preserved(self):
        result = _parse_jsonl_entry(_line(FIXTURE_TOOL_RESULT_ERROR))
        assert result["type"] == "tool_result"
        assert result["is_error"] is True

    def test_content_preserved_in_full(self):
        """Tool result content > 2000 chars → preserved without truncation."""
        long_content = "x" * 3000
        fixture = {
            "type": "tool_result", "toolUseId": "toolu_long",
            "message": {"role": "user", "content": long_content},
            "timestamp": TS,
        }
        result = _parse_jsonl_entry(_line(fixture))
        assert result["content"] == long_content

    def test_user_content_preserved_in_full(self):
        """User text content > 2000 chars → preserved without truncation."""
        long_text = "y" * 3000
        fixture = {
            "type": "user", "timestamp": TS,
            "message": {"role": "user", "content": long_text},
        }
        result = _parse_jsonl_entry(_line(fixture))
        assert result["content"] == long_text

    def test_thinking_preserved_in_full(self):
        """Thinking > 1000 chars → preserved without truncation."""
        long_thinking = "z" * 2000
        fixture = {
            "type": "assistant", "timestamp": TS,
            "message": {"role": "assistant", "content": [
                {"type": "thinking", "thinking": long_thinking},
            ]},
        }
        result = _parse_jsonl_entry(_line(fixture))
        assert result[0]["content"] == long_thinking

    def test_tool_result_content_preserved_in_user_block(self):
        """tool_result inside user message → preserved without truncation."""
        long_result = "r" * 3000
        fixture = {
            "type": "user", "timestamp": TS,
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_trunc",
                 "content": long_result},
            ]},
        }
        result = _parse_jsonl_entry(_line(fixture))
        assert result["type"] == "tool_result"
        assert result["content"] == long_result

    def test_timestamp_preserved(self):
        result = _parse_jsonl_entry(_line(FIXTURE_ASSISTANT_TEXT))
        assert result[0]["timestamp"] == TS

    def test_role_set_correctly(self):
        # User
        r = _parse_jsonl_entry(_line(FIXTURE_USER_STRING))
        assert r["role"] == "user"
        # Assistant
        r = _parse_jsonl_entry(_line(FIXTURE_ASSISTANT_TEXT))
        assert r[0]["role"] == "assistant"
        # Tool result
        r = _parse_jsonl_entry(_line(FIXTURE_TOOL_RESULT_STRING))
        assert r["role"] == "tool"


# ── TestNoiseFiltering ───────────────────────────────────────────────

class TestNoiseFiltering:
    """Noise filtering: system-reminder, task-notification, command-name tags."""

    def test_system_reminder_classified_as_system(self):
        """<system-reminder> tags → system entry (not shown as user text)."""
        result = _parse_jsonl_entry(_line(FIXTURE_USER_SYSTEM_REMINDER))
        assert result["type"] == "system"
        assert result["tag"] == "system-reminder"
        assert result["content"] == "System reminder"

    def test_task_notification_classified_as_system(self):
        result = _parse_jsonl_entry(_line(FIXTURE_USER_TASK_NOTIFICATION))
        assert result["type"] == "system"
        assert result["tag"] == "task-notification"
        assert "Building package" in result["content"]

    def test_command_name_classified_as_system(self):
        result = _parse_jsonl_entry(_line(FIXTURE_USER_COMMAND_NAME))
        assert result["type"] == "system"
        assert result["tag"] == "command-name"
        assert "commit" in result["content"]

    def test_task_notification_in_queue_skipped(self):
        """queue-operation starting with <task-notification → skipped."""
        result = _parse_jsonl_entry(_line(FIXTURE_QUEUE_TASK_NOTIFICATION))
        assert result is None

    def test_empty_assistant_content_skipped(self):
        """Assistant with empty text blocks → None."""
        fixture = {
            "type": "assistant", "timestamp": TS,
            "message": {"role": "assistant", "content": [
                {"type": "text", "text": "   "},
            ]},
        }
        result = _parse_jsonl_entry(_line(fixture))
        assert result is None

    def test_empty_tool_result_preserved(self):
        """tool_result with empty content → preserved (not dropped).

        Empty results must populate resultMap so the viewer shows tool as completed.
        """
        fixture = {
            "type": "tool_result", "toolUseId": "toolu_empty",
            "message": {"role": "user", "content": ""},
            "timestamp": TS,
        }
        result = _parse_jsonl_entry(_line(fixture))
        assert result is not None
        assert result["type"] == "tool_result"
        assert result["tool_id"] == "toolu_empty"
        assert result["content"] == ""

    def test_user_empty_content_skipped(self):
        """User with no text and no tool_results → None."""
        fixture = {
            "type": "user", "timestamp": TS,
            "message": {"role": "user", "content": []},
        }
        result = _parse_jsonl_entry(_line(fixture))
        assert result is None

    def test_local_command_stdout_classified(self):
        fixture = {
            "type": "user", "timestamp": TS,
            "message": {"role": "user", "content": (
                "<local-command-stdout>\nBuild succeeded\n</local-command-stdout>"
            )},
        }
        result = _parse_jsonl_entry(_line(fixture))
        assert result["type"] == "system"
        assert result["tag"] == "local-command-stdout"


# ── TestCrosstalkParsing ─────────────────────────────────────────────

class TestCrosstalkParsing:
    """CrossTalk message detection — inbound and outbound."""

    def test_inbound_crosstalk_detected(self):
        result = _parse_jsonl_entry(_line(FIXTURE_CROSSTALK_INBOUND))
        assert result["type"] == "crosstalk"
        assert result["sender"] == "auto-0323-022132"
        assert result["sender_label"] == "Librarian"
        assert result["source_id"] == "bc23e1ff"
        assert result["turn"] == "286"
        assert result["content"] == "Please check that function"

    def test_outbound_crosstalk_via_curl(self):
        result = _parse_jsonl_entry(_line(FIXTURE_BASH_CROSSTALK_SEND))
        assert isinstance(result, list)
        entry = result[0]
        assert entry["type"] == "crosstalk"
        assert entry["direction"] == "sent"
        assert entry["target"] == "auto-peer-session"

    def test_crosstalk_body_angle_bracket_rejected(self):
        """Body with < or > → falls through to regular user classification."""
        result = _parse_jsonl_entry(_line(FIXTURE_CROSSTALK_ANGLE_BRACKET_BODY))
        # Should NOT be classified as crosstalk — injection prevention
        # The text still contains <crosstalk> tags, so _classify_system_message won't
        # match either. It falls through to regular user text.
        assert result["type"] == "user"

    def test_queued_crosstalk_detected(self):
        """CrossTalk envelope in queue-operation → crosstalk entry."""
        result = _parse_jsonl_entry(_line(FIXTURE_QUEUE_CROSSTALK))
        assert result["type"] == "crosstalk"
        assert result["sender"] == "auto-peer"
        assert result["queued"] is True
        assert result["content"] == "Hey, are you done yet?"


# ── TestSystemMessage ────────────────────────────────────────────────

class TestSystemMessage:
    """System message classification — harness-injected entries."""

    def test_task_notification_classified(self):
        result = _parse_jsonl_entry(_line(FIXTURE_USER_TASK_NOTIFICATION))
        assert result["type"] == "system"
        assert result["tag"] == "task-notification"
        assert result["content"] == "Building package"

    def test_system_reminder_classified(self):
        result = _parse_jsonl_entry(_line(FIXTURE_USER_SYSTEM_REMINDER))
        assert result["type"] == "system"
        assert result["tag"] == "system-reminder"

    def test_task_notification_extracts_summary(self):
        """Summary field is preferred over status for the label."""
        fixture = {
            "type": "user", "timestamp": TS,
            "message": {"role": "user", "content": (
                "<task-notification>\n"
                "<status>running</status>\n"
                "</task-notification>"
            )},
        }
        result = _parse_jsonl_entry(_line(fixture))
        assert result["content"] == "Task running"

    def test_task_notification_no_summary_no_status(self):
        fixture = {
            "type": "user", "timestamp": TS,
            "message": {"role": "user", "content": "<task-notification>\n</task-notification>"},
        }
        result = _parse_jsonl_entry(_line(fixture))
        assert result["content"] == "Task notification"


# ── TestEdgeCases ────────────────────────────────────────────────────

class TestEdgeCases:
    """Edge cases: malformed input, empty lines, unexpected structures."""

    def test_malformed_json_skipped(self):
        result = _parse_jsonl_entry("{bad json")
        assert result is None

    def test_empty_line_skipped(self):
        result = _parse_jsonl_entry("")
        assert result is None

    def test_empty_content_skipped(self):
        """Valid JSON but no meaningful content → None."""
        fixture = {
            "type": "user", "timestamp": TS,
            "message": {"role": "user", "content": ""},
        }
        result = _parse_jsonl_entry(_line(fixture))
        assert result is None

    def test_unknown_type_returns_none(self):
        fixture = {"type": "unknown_type", "timestamp": TS}
        result = _parse_jsonl_entry(_line(fixture))
        assert result is None

    def test_assistant_empty_content_list(self):
        fixture = {
            "type": "assistant", "timestamp": TS,
            "message": {"role": "assistant", "content": []},
        }
        result = _parse_jsonl_entry(_line(fixture))
        assert result is None

    def test_assistant_non_list_content(self):
        """Assistant with string content (not list) → None (falls through)."""
        fixture = {
            "type": "assistant", "timestamp": TS,
            "message": {"role": "assistant", "content": "plain string"},
        }
        result = _parse_jsonl_entry(_line(fixture))
        assert result is None

    def test_user_mixed_text_and_tool_results(self):
        """User message with both text and tool_result → list of entries."""
        fixture = {
            "type": "user", "timestamp": TS,
            "message": {"role": "user", "content": [
                {"type": "text", "text": "Here is the result:"},
                {"type": "tool_result", "tool_use_id": "toolu_mix",
                 "content": "output data"},
            ]},
        }
        result = _parse_jsonl_entry(_line(fixture))
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["type"] == "user"
        assert result[0]["content"] == "Here is the result:"
        assert result[1]["type"] == "tool_result"

    def test_tool_result_with_empty_list_content(self):
        """tool_result where content is empty list → preserved with empty content."""
        fixture = {
            "type": "tool_result", "toolUseId": "toolu_empty2",
            "message": {"role": "user", "content": []},
            "timestamp": TS,
        }
        result = _parse_jsonl_entry(_line(fixture))
        assert result is not None
        assert result["type"] == "tool_result"
        assert result["tool_id"] == "toolu_empty2"
        assert result["content"] == ""

    def test_tool_result_with_empty_string_content(self):
        """tool_result with empty string content → preserved (not dropped).

        Real case: Read tool on empty file, ToolSearch with no matches.
        resultMap must be populated so the viewer doesn't show phantom running tools.
        """
        fixture = {
            "type": "tool_result", "toolUseId": "toolu_empty_str",
            "message": {"role": "user", "content": ""},
            "timestamp": TS,
        }
        result = _parse_jsonl_entry(_line(fixture))
        assert result is not None
        assert result["type"] == "tool_result"
        assert result["tool_id"] == "toolu_empty_str"
        assert result["content"] == ""
        assert result["is_error"] is False

    def test_tool_result_empty_content_preserves_is_error(self):
        """Empty error result still has is_error=True."""
        fixture = {
            "type": "tool_result", "toolUseId": "toolu_empty_err",
            "message": {"role": "user", "content": ""},
            "is_error": True,
            "timestamp": TS,
        }
        result = _parse_jsonl_entry(_line(fixture))
        assert result is not None
        assert result["type"] == "tool_result"
        assert result["is_error"] is True

    def test_no_timestamp_defaults_to_empty(self):
        fixture = {"type": "user", "message": {"role": "user", "content": "hello"}}
        result = _parse_jsonl_entry(_line(fixture))
        assert result["timestamp"] == ""

    def test_queue_enqueue_empty_content_skipped(self):
        fixture = {
            "type": "queue-operation", "operation": "enqueue",
            "content": "", "timestamp": TS,
        }
        result = _parse_jsonl_entry(_line(fixture))
        assert result is None

    def test_user_content_non_dict_blocks_ignored(self):
        """Non-dict items in user content list are silently skipped."""
        fixture = {
            "type": "user", "timestamp": TS,
            "message": {"role": "user", "content": [
                "just a string",
                42,
                {"type": "text", "text": "real content"},
            ]},
        }
        result = _parse_jsonl_entry(_line(fixture))
        assert result["type"] == "user"
        assert result["content"] == "real content"

    def test_tool_result_content_field_precedence(self):
        """Top-level tool_result reads from message.content, not top-level content."""
        fixture = {
            "type": "tool_result", "toolUseId": "toolu_prec",
            "message": {"role": "user", "content": "from message content"},
            "content": "from top level",
            "timestamp": TS,
        }
        # _parse_jsonl_entry reads message.content for tool_result entries
        result = _parse_jsonl_entry(_line(fixture))
        assert result["type"] == "tool_result"
        assert result["content"] == "from message content"
