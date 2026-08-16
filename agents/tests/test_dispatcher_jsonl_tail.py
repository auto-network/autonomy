"""Tests for the JSONL tail check that extends stale threshold for running tools.

Covers `_has_running_tool` and the latched `_extended` flag flow inside
`poll_and_collect`. The flow replaces the prior dashboard_db.get_session
lookup that never matched dispatch agents.
"""

from __future__ import annotations

import json
import time
from unittest.mock import patch

import pytest

from agents.dispatcher import (
    RunningAgent,
    STALE_THRESHOLD_SECS,
    STALE_THRESHOLD_TOOL_SECS,
    _has_running_tool,
    poll_and_collect,
)


def _write_jsonl(path, entries) -> None:
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))


def _make_running_agent(**overrides):
    defaults = dict(
        bead_id="auto-test",
        container_name="agent-auto-test-1234",
        container_id="abc123def456",
        output_dir="/tmp/test-output",
        worktree_path="/tmp/test-worktree",
        branch="agent/auto-test",
        branch_base="aaa111",
        image="autonomy-agent",
        started_at=time.time(),
    )
    defaults.update(overrides)
    return RunningAgent(**defaults)


# ── _has_running_tool ───────────────────────────────────────────


class TestHasRunningTool:
    def test_with_tool_use(self, tmp_path):
        """Last entry = assistant containing a tool_use block → True."""
        f = tmp_path / "session.jsonl"
        _write_jsonl(f, [
            {"type": "user", "message": {"content": "go"}},
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "running pytest"},
                {"type": "tool_use", "name": "Bash", "input": {"cmd": "pytest"}},
            ]}},
        ])
        assert _has_running_tool(f) is True

    def test_with_text_only(self, tmp_path):
        """Last entry = assistant text without tool_use → False."""
        f = tmp_path / "session.jsonl"
        _write_jsonl(f, [
            {"type": "user", "message": {"content": "hi"}},
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "all done"},
            ]}},
        ])
        assert _has_running_tool(f) is False

    def test_with_user_last(self, tmp_path):
        """Last entry = user (waiting on agent) → False."""
        f = tmp_path / "session.jsonl"
        _write_jsonl(f, [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {}},
            ]}},
            {"type": "user", "message": {"content": "stop"}},
        ])
        assert _has_running_tool(f) is False

    def test_empty_file(self, tmp_path):
        """Empty JSONL file → False."""
        f = tmp_path / "session.jsonl"
        f.write_text("")
        assert _has_running_tool(f) is False

    def test_malformed_last_line(self, tmp_path):
        """Malformed JSON in last line → False (no crash)."""
        f = tmp_path / "session.jsonl"
        f.write_text(
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash"}]}}) + "\n"
            + "{not json{{\n"
        )
        assert _has_running_tool(f) is False

    def test_string_content_no_tool(self, tmp_path):
        """Assistant with string content (no list) → False."""
        f = tmp_path / "session.jsonl"
        _write_jsonl(f, [
            {"type": "assistant", "message": {"content": "plain text reply"}},
        ])
        assert _has_running_tool(f) is False

    def test_missing_file(self, tmp_path):
        """Missing file → False (no crash)."""
        assert _has_running_tool(tmp_path / "does-not-exist.jsonl") is False

    def test_only_blank_lines(self, tmp_path):
        """File with only whitespace → False."""
        f = tmp_path / "session.jsonl"
        f.write_text("\n\n   \n")
        assert _has_running_tool(f) is False

    def test_reads_only_tail(self, tmp_path):
        """Older tool_use buried > 8KB back doesn't make a stuck agent extend."""
        f = tmp_path / "session.jsonl"
        old = {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {}}]}}
        # Pad with large user turn so the old assistant is well past 8KB tail.
        big_text = "x" * 16384
        recent = {"type": "user", "message": {"content": big_text}}
        f.write_text(json.dumps(old) + "\n" + json.dumps(recent) + "\n")
        assert _has_running_tool(f) is False


# ── poll_and_collect: _extended latching ────────────────────────


def _setup_session(tmp_path, entries):
    """Create an output dir with a session JSONL and stale mtime."""
    output_dir = tmp_path / "output"
    sessions = output_dir / "sessions"
    sessions.mkdir(parents=True)
    f = sessions / "abc.jsonl"
    _write_jsonl(f, entries)
    return output_dir, f


class TestExtendedFlagLatching:
    @patch("agents.dispatcher._collect_live_stats")
    @patch("agents.dispatcher.poll_container")
    def test_extended_latches_and_skips_kill(self, mock_poll, mock_stats, tmp_path):
        """JSONL stale > 300s with running tool → extended set, not killed."""
        output_dir, jsonl = _setup_session(tmp_path, [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {}}]}},
        ])
        # Comfortably past the plain thinking budget, far under the tool
        # budget. Derived, not restated: a hardcoded number here silently
        # stops testing the boundary the moment the budget moves.
        old = time.time() - (STALE_THRESHOLD_SECS + 100)
        import os
        os.utime(jsonl, (old, old))

        agent = _make_running_agent(
            output_dir=str(output_dir),
            started_at=time.time() - 9999,  # past boot grace
        )
        running = [agent]
        mock_poll.return_value = (False, -1)

        with patch("agents.dispatcher._has_running_tool",
                   wraps=__import__("agents.dispatcher",
                                    fromlist=["_has_running_tool"])._has_running_tool
                   ) as spy:
            poll_and_collect(running)
            assert spy.call_count == 1

            # Still running, latched.
            assert len(running) == 1
            assert agent._extended is True

            # Next tick: still stale, but extended skips the file read.
            poll_and_collect(running)
            assert spy.call_count == 1
            assert len(running) == 1

    @patch("agents.dispatcher.cleanup_worktree")
    @patch("agents.dispatcher.release_bead")
    @patch("agents.dispatcher._notify_dispatch_nag")
    @patch("agents.dispatcher._record_run")
    @patch("agents.dispatcher.collect_results")
    @patch("agents.dispatcher.kill_container")
    @patch("agents.dispatcher.poll_container")
    def test_no_tool_killed_past_thinking_budget(
            self, mock_poll, mock_kill, mock_collect,
            mock_record, mock_notify, mock_release,
            mock_cleanup, tmp_path):
        """Past the thinking budget with NO tool running → killed.

        The budget exists to reap a genuinely dead run. What it must not do is
        reap a live one that is merely thinking hard, which is why the number
        is a tunable and why this test reads it rather than repeating it.
        """
        output_dir, jsonl = _setup_session(tmp_path, [
            {"type": "user", "message": {"content": "stuck"}},
        ])
        old = time.time() - (STALE_THRESHOLD_SECS + 100)
        import os
        os.utime(jsonl, (old, old))

        agent = _make_running_agent(
            output_dir=str(output_dir),
            started_at=time.time() - 9999,
        )
        running = [agent]
        mock_poll.return_value = (False, -1)
        from agents.dispatcher import DispatchResult
        mock_collect.return_value = DispatchResult(
            bead_id=agent.bead_id, exit_code=-1)

        poll_and_collect(running)

        assert agent._extended is False
        mock_kill.assert_called_once_with(agent.container_name)
        assert len(running) == 0

    @patch("agents.dispatcher.cleanup_worktree")
    @patch("agents.dispatcher.release_bead")
    @patch("agents.dispatcher._notify_dispatch_nag")
    @patch("agents.dispatcher._record_run")
    @patch("agents.dispatcher.collect_results")
    @patch("agents.dispatcher.kill_container")
    @patch("agents.dispatcher.poll_container")
    def test_extended_killed_past_tool_budget(self, mock_poll, mock_kill, mock_collect,
                                         mock_record, mock_notify, mock_release,
                                         mock_cleanup, tmp_path):
        """A tool that never returns is still reaped, at the tool budget."""
        output_dir, jsonl = _setup_session(tmp_path, [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {}}]}},
        ])
        old = time.time() - (STALE_THRESHOLD_TOOL_SECS + 200)
        import os
        os.utime(jsonl, (old, old))

        agent = _make_running_agent(
            output_dir=str(output_dir),
            started_at=time.time() - 9999,
        )
        running = [agent]
        mock_poll.return_value = (False, -1)
        from agents.dispatcher import DispatchResult
        mock_collect.return_value = DispatchResult(
            bead_id=agent.bead_id, exit_code=-1)

        poll_and_collect(running)

        assert agent._extended is True
        mock_kill.assert_called_once_with(agent.container_name)
        assert len(running) == 0

    @patch("agents.dispatcher._collect_live_stats")
    @patch("agents.dispatcher.poll_container")
    def test_fresh_jsonl_not_killed(self, mock_poll, mock_stats, tmp_path):
        """Fresh mtime → never stale, no helper call."""
        output_dir, jsonl = _setup_session(tmp_path, [
            {"type": "user", "message": {"content": "starting"}},
        ])
        # Default mtime = now — well inside the thinking budget.
        agent = _make_running_agent(
            output_dir=str(output_dir),
            started_at=time.time() - 9999,
        )
        running = [agent]
        mock_poll.return_value = (False, -1)

        with patch("agents.dispatcher._has_running_tool") as spy:
            poll_and_collect(running)
            spy.assert_not_called()

        assert agent._extended is False
        assert len(running) == 1
