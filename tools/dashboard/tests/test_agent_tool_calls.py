"""Tests for Agent tool_calls enrichment via subagent JSONL reading.

Tests count_tool_uses and _enrich_entries description-matching against
synthetic fixture data that mirrors real Claude Code session structure.
"""

import json
import tempfile
from pathlib import Path

from tools.dashboard.session_monitor import SessionState, count_tool_uses
from tools.dashboard.server import _enrich_entries


# ── Fixture helpers ──────────────────────────────────────────────


def _write_subagent_jsonl(path: Path, tool_use_counts: list[int]) -> None:
    """Write a subagent JSONL file with the given number of tool_use blocks per assistant turn."""
    with open(path, "w") as f:
        for count in tool_use_counts:
            content = []
            for i in range(count):
                content.append({"type": "tool_use", "id": f"tu_{i}", "name": "Bash", "input": {}})
            entry = {
                "type": "assistant",
                "message": {
                    "content": content,
                    "usage": {"input_tokens": 100},
                },
            }
            f.write(json.dumps(entry) + "\n")
        # Also write some non-assistant entries (should be ignored)
        f.write(json.dumps({"type": "user", "message": {"content": "hello"}}) + "\n")
        f.write(json.dumps({"type": "tool_result", "content": "ok"}) + "\n")


def _write_meta_json(path: Path, description: str) -> None:
    """Write a subagent meta.json file."""
    path.write_text(json.dumps({"description": description}))


def _make_tool_use_entry(tool_id: str, description: str) -> dict:
    """Create a parsed tool_use entry for the Agent tool."""
    return {
        "type": "tool_use",
        "role": "assistant",
        "tool_name": "Agent",
        "tool_id": tool_id,
        "input": {"description": description, "prompt": "do something"},
        "timestamp": 1000.0,
    }


def _make_tool_result_entry(tool_id: str, content: str = "Agent completed.") -> dict:
    """Create a parsed tool_result entry."""
    return {
        "type": "tool_result",
        "role": "tool",
        "tool_id": tool_id,
        "content": content,
        "is_error": False,
        "timestamp": 1001.0,
    }


# ── Tests ────────────────────────────────────────────────────────


def test_count_tool_uses_basic():
    """count_tool_uses counts tool_use blocks in assistant messages."""
    with tempfile.TemporaryDirectory() as tmp:
        jsonl = Path(tmp) / "subagent.jsonl"
        # 3 assistant turns with 4, 5, 3 tool_use blocks = 12 total
        _write_subagent_jsonl(jsonl, [4, 5, 3])
        assert count_tool_uses(jsonl) == 12


def test_count_tool_uses_empty():
    """count_tool_uses returns 0 for empty file."""
    with tempfile.TemporaryDirectory() as tmp:
        jsonl = Path(tmp) / "subagent.jsonl"
        jsonl.write_text("")
        assert count_tool_uses(jsonl) == 0


def test_count_tool_uses_no_tool_use():
    """count_tool_uses returns 0 when assistant messages have no tool_use blocks."""
    with tempfile.TemporaryDirectory() as tmp:
        jsonl = Path(tmp) / "subagent.jsonl"
        with open(jsonl, "w") as f:
            f.write(json.dumps({
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "hello"}]},
            }) + "\n")
        assert count_tool_uses(jsonl) == 0


def test_count_tool_uses_missing_file():
    """count_tool_uses returns 0 for nonexistent file."""
    assert count_tool_uses(Path("/nonexistent/path.jsonl")) == 0


def test_enrich_single_agent():
    """_enrich_entries matches a single Agent tool_use to its subagent JSONL."""
    with tempfile.TemporaryDirectory() as tmp:
        session_dir = Path(tmp) / "session-abc"
        subagents = session_dir / "subagents"
        subagents.mkdir(parents=True)

        # Write subagent files
        _write_meta_json(subagents / "agent-001.meta.json", "Search codebase")
        _write_subagent_jsonl(subagents / "agent-001.jsonl", [4, 5, 3])  # 12 total

        entries = [
            _make_tool_use_entry("tid_1", "Search codebase"),
            _make_tool_result_entry("tid_1"),
        ]

        _enrich_entries(entries, session_dir=session_dir)

        result_entry = entries[1]
        assert result_entry.get("tool_calls") == 12


def test_enrich_multiple_agents():
    """_enrich_entries correctly matches multiple Agent calls by description."""
    with tempfile.TemporaryDirectory() as tmp:
        session_dir = Path(tmp) / "session-abc"
        subagents = session_dir / "subagents"
        subagents.mkdir(parents=True)

        # Write 3 subagent file pairs
        _write_meta_json(subagents / "agent-001.meta.json", "Search codebase")
        _write_subagent_jsonl(subagents / "agent-001.jsonl", [10, 10, 11])  # 31

        _write_meta_json(subagents / "agent-002.meta.json", "Run tests")
        _write_subagent_jsonl(subagents / "agent-002.jsonl", [20, 21])  # 41

        _write_meta_json(subagents / "agent-003.meta.json", "Fix bug")
        _write_subagent_jsonl(subagents / "agent-003.jsonl", [8, 8, 8])  # 24

        entries = [
            _make_tool_use_entry("tid_1", "Search codebase"),
            _make_tool_result_entry("tid_1"),
            _make_tool_use_entry("tid_2", "Run tests"),
            _make_tool_result_entry("tid_2"),
            _make_tool_use_entry("tid_3", "Fix bug"),
            _make_tool_result_entry("tid_3"),
        ]

        _enrich_entries(entries, session_dir=session_dir)

        assert entries[1].get("tool_calls") == 31
        assert entries[3].get("tool_calls") == 41
        assert entries[5].get("tool_calls") == 24


def test_enrich_no_session_dir():
    """_enrich_entries is a no-op when session_dir is None."""
    entries = [
        _make_tool_use_entry("tid_1", "Search codebase"),
        _make_tool_result_entry("tid_1"),
    ]
    _enrich_entries(entries, session_dir=None)
    assert "tool_calls" not in entries[1]


def test_enrich_no_subagents_dir():
    """_enrich_entries doesn't crash when subagents/ doesn't exist."""
    with tempfile.TemporaryDirectory() as tmp:
        session_dir = Path(tmp) / "session-abc"
        session_dir.mkdir()
        # No subagents/ directory

        entries = [
            _make_tool_use_entry("tid_1", "Search codebase"),
            _make_tool_result_entry("tid_1"),
        ]
        _enrich_entries(entries, session_dir=session_dir)
        assert "tool_calls" not in entries[1]


def test_enrich_non_agent_tool_ignored():
    """_enrich_entries ignores non-Agent tool_use entries."""
    with tempfile.TemporaryDirectory() as tmp:
        session_dir = Path(tmp) / "session-abc"
        subagents = session_dir / "subagents"
        subagents.mkdir(parents=True)

        entries = [
            {
                "type": "tool_use",
                "role": "assistant",
                "tool_name": "Bash",
                "tool_id": "tid_1",
                "input": {"command": "ls"},
                "timestamp": 1000.0,
            },
            _make_tool_result_entry("tid_1"),
        ]
        _enrich_entries(entries, session_dir=session_dir)
        assert "tool_calls" not in entries[1]


def test_enrich_description_mismatch():
    """_enrich_entries skips subagents with non-matching descriptions."""
    with tempfile.TemporaryDirectory() as tmp:
        session_dir = Path(tmp) / "session-abc"
        subagents = session_dir / "subagents"
        subagents.mkdir(parents=True)

        _write_meta_json(subagents / "agent-001.meta.json", "Wrong description")
        _write_subagent_jsonl(subagents / "agent-001.jsonl", [5, 5])

        entries = [
            _make_tool_use_entry("tid_1", "Search codebase"),
            _make_tool_result_entry("tid_1"),
        ]
        _enrich_entries(entries, session_dir=session_dir)
        assert "tool_calls" not in entries[1]


def test_enrich_claimed_prevents_double_match():
    """When two Agent calls have the same description, each claims a different subagent."""
    with tempfile.TemporaryDirectory() as tmp:
        session_dir = Path(tmp) / "session-abc"
        subagents = session_dir / "subagents"
        subagents.mkdir(parents=True)

        # Two subagents with the same description (e.g. retried agent)
        _write_meta_json(subagents / "agent-001.meta.json", "Search codebase")
        _write_subagent_jsonl(subagents / "agent-001.jsonl", [5])  # 5

        _write_meta_json(subagents / "agent-002.meta.json", "Search codebase")
        _write_subagent_jsonl(subagents / "agent-002.jsonl", [8])  # 8

        entries = [
            _make_tool_use_entry("tid_1", "Search codebase"),
            _make_tool_result_entry("tid_1"),
            _make_tool_use_entry("tid_2", "Search codebase"),
            _make_tool_result_entry("tid_2"),
        ]
        _enrich_entries(entries, session_dir=session_dir)

        # First match gets agent-001 (sorted), second gets agent-002
        assert entries[1].get("tool_calls") == 5
        assert entries[3].get("tool_calls") == 8


def test_session_state_enrichment():
    """SessionMonitor._enrich_agent_entries tracks state across batches."""
    with tempfile.TemporaryDirectory() as tmp:
        # Simulate a session with JSONL at session_dir/project/uuid.jsonl
        project_dir = Path(tmp) / "project"
        project_dir.mkdir()
        jsonl_file = project_dir / "abc123.jsonl"
        jsonl_file.write_text("")

        subagents = project_dir / "abc123" / "subagents"
        subagents.mkdir(parents=True)
        _write_meta_json(subagents / "agent-001.meta.json", "Explore code")
        _write_subagent_jsonl(subagents / "agent-001.jsonl", [3, 4])  # 7

        state = SessionState(
            session_id="abc123",
            tmux_name="auto-t1",
            session_type="terminal",
            project="project",
            jsonl_path=jsonl_file,
        )

        from tools.dashboard.session_monitor import SessionMonitor

        # Batch 1: tool_use arrives
        batch1 = [_make_tool_use_entry("tid_1", "Explore code")]
        SessionMonitor._enrich_agent_entries(state, batch1)

        # State should have recorded the description
        assert state.agent_descriptions.get("tid_1") == "Explore code"

        # Batch 2: tool_result arrives later
        batch2 = [_make_tool_result_entry("tid_1")]
        SessionMonitor._enrich_agent_entries(state, batch2)

        assert batch2[0].get("tool_calls") == 7
        assert len(state.claimed_subagents) == 1


if __name__ == "__main__":
    import sys

    tests = [
        test_count_tool_uses_basic,
        test_count_tool_uses_empty,
        test_count_tool_uses_no_tool_use,
        test_count_tool_uses_missing_file,
        test_enrich_single_agent,
        test_enrich_multiple_agents,
        test_enrich_no_session_dir,
        test_enrich_no_subagents_dir,
        test_enrich_non_agent_tool_ignored,
        test_enrich_description_mismatch,
        test_enrich_claimed_prevents_double_match,
        test_session_state_enrichment,
    ]

    failed = 0
    for test in tests:
        try:
            test()
            print(f"  PASS  {test.__name__}")
        except Exception as e:
            print(f"  FAIL  {test.__name__}: {e}")
            failed += 1

    if failed:
        print(f"\n{failed}/{len(tests)} tests failed")
        sys.exit(1)
    else:
        print(f"\nAll {len(tests)} tests passed")
