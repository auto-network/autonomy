"""Tests for subagent file exclusion — Boundary A (filesystem → monitor).

Subagent JSONL files (in uuid/subagents/) must NEVER interfere with primary
session file management. The _find_primary_jsonls fix (auto-uq0b) filters them.

Two code paths that use _find_primary_jsonls:
  1. IN_CREATE handler — file discovery in resolution_dir
  2. Recovery — _recover_unresolved_sessions

Uses tmp_path with real filesystem. No real sessions.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.dashboard.session_monitor import SessionMonitor, _TailState, _find_primary_jsonls


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
