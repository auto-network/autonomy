"""Tests for subagent file exclusion — Boundary A (filesystem → monitor).

Subagent JSONL files (in uuid/subagents/) must NEVER interfere with primary
session file management. The _find_primary_jsonls fix (auto-uq0b) filters them.

Two code paths that use _find_primary_jsonls:
  1. IN_CREATE handler — file discovery in resolution_dir
  2. Recovery — _recover_unresolved_sessions

Uses tmp_path with real filesystem. No real sessions.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tools.dashboard.session_monitor import (
    SessionMonitor,
    _TailState,
    _classify_codex_rollout,
    _find_primary_jsonls,
    _is_codex_subagent_rollout,
)


# ── Helpers ───────────────────────────────────────────────────────────────

def _make_jsonl(directory: Path, name: str, entries: list[dict] | None = None,
                mtime_offset: float = 0) -> Path:
    """Create a JSONL file with controlled content and mtime."""
    p = directory / f"{name}.jsonl"
    if entries is None:
        entries = [{"type": "system"}]
    p.write_text("".join(json.dumps(e) + "\n" for e in entries))
    t = time.time() + mtime_offset
    os.utime(p, (t, t))
    return p


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def container_session(tmp_path):
    """Isolated container session directory with one JSONL."""
    resolution_dir = tmp_path / "sessions" / "-workspace-repo"
    resolution_dir.mkdir(parents=True)
    uuid = "aaaa-1111"
    jsonl = _make_jsonl(resolution_dir, uuid, [
        {"type": "user", "message": {"content": "hello"}, "uuid": "msg-001", "parentUuid": None},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}, "uuid": "msg-002"},
    ])
    return {"resolution_dir": resolution_dir, "uuid": uuid, "jsonl": jsonl}


@pytest.fixture
def session_with_subagents(container_session):
    """Primary + subagent files, subagent artificially newer."""
    resolution_dir = container_session["resolution_dir"]
    uuid = container_session["uuid"]
    subagent_dir = resolution_dir / uuid / "subagents"
    subagent_dir.mkdir(parents=True)
    sub_jsonl = subagent_dir / "agent-abc123.jsonl"
    sub_jsonl.write_text('{"type":"assistant","message":{"content":[{"type":"text","text":"subagent"}]}}\n')
    # Make subagent file 10 seconds newer than primary
    os.utime(sub_jsonl, (time.time() + 10, time.time() + 10))
    return {**container_session, "subagent_jsonl": sub_jsonl, "subagent_dir": subagent_dir}


# ── TestSubagentExclusion ─────────────────────────────────────────────────

class TestSubagentExclusion:
    """Subagent files must never be selected as primary session JSONL."""

    def test_subagent_newer_than_primary_ignored(self, session_with_subagents):
        """Subagent file has newer mtime → _find_primary_jsonls excludes it.

        Expected: GREEN — _find_primary_jsonls excludes paths containing "subagents".
        """
        rd = session_with_subagents["resolution_dir"]
        primary = session_with_subagents["jsonl"]
        sub = session_with_subagents["subagent_jsonl"]

        # Verify precondition: subagent IS newer
        assert sub.stat().st_mtime > primary.stat().st_mtime, \
            "Precondition: subagent file should be newer than primary"

        primaries = _find_primary_jsonls(rd)
        assert len(primaries) == 1
        assert primaries[0] == primary, (
            f"Should find primary {primary.name}, not subagent {sub.name}"
        )

    def test_find_primary_jsonls_excludes_subagents(self, session_with_subagents):
        """_find_primary_jsonls() returns only files without 'subagents' in path.

        Expected: GREEN — the function filters by 'subagents' not in f.parts.
        """
        rd = session_with_subagents["resolution_dir"]
        primary = session_with_subagents["jsonl"]
        sub = session_with_subagents["subagent_jsonl"]

        # rglob would find both files
        all_jsonls = list(rd.rglob("*.jsonl"))
        assert len(all_jsonls) >= 2, "Precondition: both primary and subagent exist"

        # _find_primary_jsonls should exclude the subagent
        primaries = _find_primary_jsonls(rd)
        assert len(primaries) == 1, f"Should find exactly 1 primary, found {len(primaries)}"
        assert primaries[0] == primary
        assert sub not in primaries

    def test_directory_watch_ignores_subdirs(self, session_with_subagents):
        """IN_CREATE on resolution_dir doesn't fire for files in uuid/subagents/.

        Expected: RED — inotify IN_CREATE watches on resolution_dir are not recursive;
        they only fire for files created directly in the watched directory. But the test
        documents the invariant that subagent file creation should not trigger rollover.

        Note: inotify IN_CREATE on a directory does NOT fire for files in subdirectories.
        This is actually the desired behavior — we only want to detect new primary JSONLs
        created directly in resolution_dir.
        """
        rd = session_with_subagents["resolution_dir"]
        sub_dir = session_with_subagents["subagent_dir"]

        # The key invariant: even if we somehow got notified about a subagent file,
        # _find_primary_jsonls would still exclude it from rollover candidates.
        # Create yet another subagent file
        new_sub = sub_dir / "agent-def456.jsonl"
        new_sub.write_text('{"type":"assistant","message":{"content":[{"type":"text","text":"sub2"}]}}\n')
        os.utime(new_sub, (time.time() + 20, time.time() + 20))

        primaries = _find_primary_jsonls(rd)
        assert len(primaries) == 1, "New subagent should not appear in primary list"
        assert all("subagents" not in str(p) for p in primaries)

    def test_deeply_nested_subagent_excluded(self, container_session):
        """Subagent in deeper nesting (uuid/subagents/nested/) still excluded.

        Expected: GREEN — _find_primary_jsonls checks 'subagents' in f.parts.
        """
        rd = container_session["resolution_dir"]
        uuid = container_session["uuid"]

        # Create deeply nested subagent
        deep_dir = rd / uuid / "subagents" / "nested" / "deep"
        deep_dir.mkdir(parents=True)
        deep_sub = _make_jsonl(deep_dir, "agent-deep", mtime_offset=20)

        primaries = _find_primary_jsonls(rd)
        assert deep_sub not in primaries
        assert len(primaries) == 1, "Only the original primary should be found"

    def test_multiple_primaries_no_subagents(self, container_session):
        """Multiple primary files (no subagents) all returned.

        Expected: GREEN — _find_primary_jsonls returns all non-subagent JSONLs.
        """
        rd = container_session["resolution_dir"]

        # Add a second primary JSONL (rollover)
        second = _make_jsonl(rd, "bbbb-2222", mtime_offset=5)

        primaries = _find_primary_jsonls(rd)
        assert len(primaries) == 2
        names = {p.name for p in primaries}
        assert "aaaa-1111.jsonl" in names
        assert "bbbb-2222.jsonl" in names

    def test_codex_parent_rollout_not_classified_as_subagent(self, tmp_path):
        """Parent rollout lacks the fork markers and must not be skipped."""
        path = tmp_path / "rollout-parent.jsonl"
        path.write_text(json.dumps({
            "type": "session_meta",
            "payload": {
                "originator": "codex-tui",
                "agent_nickname": "Parent",
            },
        }) + "\n")

        assert _is_codex_subagent_rollout(path) is False

    def test_codex_forked_rollout_classified_as_subagent(self, tmp_path):
        """Forked Codex rollouts carry explicit parent/subagent markers."""
        path = tmp_path / "rollout-forked.jsonl"
        path.write_text(json.dumps({
            "type": "session_meta",
            "payload": {
                "originator": "codex-tui",
                "forked_from_id": "019dc805-parent",
                "agent_nickname": "Aristotle",
                "source": {
                    "subagent": {
                        "thread_spawn": {
                            "parent_thread_id": "019dc805-parent",
                            "depth": 1,
                        },
                    },
                },
            },
        }) + "\n")

        assert _is_codex_subagent_rollout(path) is True

    @pytest.mark.parametrize(
        ("contents", "reason"),
        [
            ("", "empty"),
            ('{"type":"session_meta","payload":', "partial_json"),
            ('{"type":"event_msg","payload":{}}\n', "missing_session_meta"),
        ],
    )
    def test_incomplete_codex_rollout_is_unknown(self, tmp_path, contents, reason):
        """Create-time content is UNKNOWN, never equivalent to a main rollout."""
        path = tmp_path / "rollout-incomplete.jsonl"
        path.write_text(contents)

        classification = _classify_codex_rollout(path)

        assert classification.kind == "unknown"
        assert classification.reason == reason
        assert path not in _find_primary_jsonls(tmp_path)

    def test_partial_utf8_codex_header_is_unknown(self, tmp_path):
        """A read racing a multibyte write must not escape the classifier."""
        path = tmp_path / "rollout-partial-utf8.jsonl"
        path.write_bytes(b'{"type":"session_meta","payload":{"text":"\xf0\x9f')

        classification = _classify_codex_rollout(path)

        assert classification.kind == "unknown"
        assert classification.reason == "decode_failed"

    @pytest.mark.asyncio
    async def test_in_create_defers_until_child_header_is_complete(
        self, tmp_path, caplog,
    ):
        """IN_CREATE on an empty child must not repoint the parent session."""
        path = tmp_path / "rollout-racy-child.jsonl"
        path.touch()
        row = {
            "jsonl_path": str(tmp_path / "rollout-parent.jsonl"),
            "session_uuids": json.dumps(["rollout-parent"]),
            "harness": "codex",
        }
        child_header = {
            "type": "session_meta",
            "payload": {
                "forked_from_id": "parent",
                "source": {"subagent": {"thread_spawn": {"depth": 1}}},
                # Real Codex session_meta records are large enough for the
                # create/write visibility race to be observable.
                "base_instructions": {"text": "x" * 20_000},
            },
        }

        async def finish_header():
            await asyncio.sleep(0)
            path.write_text(json.dumps(child_header) + "\n")

        writer = asyncio.create_task(finish_header())
        monitor = SessionMonitor()
        with (
            patch(
                "tools.dashboard.session_monitor._CODEX_HEADER_RETRY_DELAYS_SECONDS",
                (0.01,),
            ),
            patch(
                "tools.dashboard.session_monitor.resolve_harness_for_session_row"
            ) as resolve_harness,
            caplog.at_level("INFO"),
        ):
            await monitor._handle_container_create("auto-race", row, path)
        await writer

        resolve_harness.assert_not_called()
        assert "classification=unknown" in caplog.text
        assert "action=defer" in caplog.text
        assert "classification=subagent" in caplog.text
        assert "action=skip" in caplog.text

    @pytest.mark.asyncio
    async def test_in_create_links_after_delayed_main_header(self, tmp_path):
        """A genuine main rollover links once its complete header is visible."""
        path = tmp_path / "rollout-racy-main.jsonl"
        path.touch()
        old_path = tmp_path / "rollout-parent.jsonl"
        row = {
            "jsonl_path": str(old_path),
            "session_uuids": json.dumps(["rollout-parent"]),
            "harness": "codex",
        }
        main_header = {
            "type": "session_meta",
            "payload": {"source": "cli", "id": "new-main"},
        }

        async def finish_header():
            await asyncio.sleep(0)
            path.write_text(json.dumps(main_header) + "\n")

        writer = asyncio.create_task(finish_header())
        monitor = SessionMonitor()
        harness = MagicMock()
        harness.resolve_session.return_value = {
            "jsonl_path": path,
            "resolution_dir": tmp_path,
        }
        with (
            patch(
                "tools.dashboard.session_monitor._CODEX_HEADER_RETRY_DELAYS_SECONDS",
                (0.01,),
            ),
            patch(
                "tools.dashboard.session_monitor.resolve_harness_for_session_row",
                return_value=harness,
            ),
        ):
            await monitor._handle_container_create("auto-race", row, path)
        await writer

        harness.resolve_session.assert_called_once_with(
            tmux_name="auto-race",
            row=row,
            jsonl_path=path,
        )
        harness.attach_live_monitoring.assert_called_once()

    @pytest.mark.asyncio
    async def test_in_create_never_links_when_header_stays_incomplete(
        self, tmp_path, caplog,
    ):
        """Retry exhaustion must abandon an unknown file instead of failing open."""
        path = tmp_path / "rollout-still-empty.jsonl"
        path.touch()
        row = {
            "jsonl_path": str(tmp_path / "rollout-parent.jsonl"),
            "session_uuids": json.dumps(["rollout-parent"]),
            "harness": "codex",
        }
        monitor = SessionMonitor()

        with (
            patch(
                "tools.dashboard.session_monitor._CODEX_HEADER_RETRY_DELAYS_SECONDS",
                (0,),
            ),
            patch(
                "tools.dashboard.session_monitor.resolve_harness_for_session_row"
            ) as resolve_harness,
            caplog.at_level("INFO"),
        ):
            await monitor._handle_container_create("auto-race", row, path)

        resolve_harness.assert_not_called()
        assert "classification=unknown" in caplog.text
        assert "action=defer" in caplog.text
        assert "action=abandon" in caplog.text

    def test_watch_scan_logs_skipped_sibling_subagent(self, tmp_path, caplog):
        """A scan-discovered sibling child leaves evidence with the old path."""
        parent = tmp_path / "rollout-parent.jsonl"
        parent.write_text(json.dumps({
            "type": "session_meta",
            "payload": {"source": "cli"},
        }) + "\n")
        child = tmp_path / "rollout-child.jsonl"
        child.write_text(json.dumps({
            "type": "session_meta",
            "payload": {
                "forked_from_id": "parent",
                "source": {"subagent": {}},
            },
        }) + "\n")
        monitor = SessionMonitor()

        with (
            patch(
                "tools.dashboard.session_monitor.get_session",
                return_value={"jsonl_path": str(parent)},
            ),
            caplog.at_level("INFO"),
        ):
            monitor._scan_dir_for_existing_jsonls("auto-race", str(tmp_path))

        assert f"old_path={parent}" in caplog.text
        assert f"candidate={child}" in caplog.text
        assert "classification=subagent" in caplog.text
        assert "action=skip" in caplog.text
        assert "source=watch_scan" in caplog.text

    def test_run_directory_fallback_excludes_sibling_subagent(
        self, tmp_path, monkeypatch,
    ):
        """Fallback resolution for a tmux name returns the main Codex rollout."""
        agent_runs = tmp_path / "agent-runs"
        sessions = agent_runs / "auto-race-20260807" / "sessions" / "2026" / "08" / "07"
        sessions.mkdir(parents=True)
        child = sessions / "rollout-child.jsonl"
        child.write_text(json.dumps({
            "type": "session_meta",
            "payload": {
                "forked_from_id": "parent",
                "source": {"subagent": {}},
            },
        }) + "\n")
        main = sessions / "rollout-main.jsonl"
        main.write_text(json.dumps({
            "type": "session_meta",
            "payload": {"source": "cli"},
        }) + "\n")
        monkeypatch.setenv("DASHBOARD_AGENT_RUNS_DIR", str(agent_runs))

        assert SessionMonitor().resolve_session_file("auto-race") == main
