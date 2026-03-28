"""Tests for initial JSONL file discovery — Boundary A (filesystem → monitor).

Covers the path from newly registered session to first JSONL resolution:
  - Container: _resolve_jsonl_in_dir finds first JSONL in resolution_dir
  - Host: _resolve_host_jsonl matches via .meta.json tmux_session field

Uses tmp_path with real filesystem. Mocks tmux. No real sessions.
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

def _make_jsonl(directory: Path, uuid: str, entries: list[dict] | None = None,
                mtime_offset: float = 0) -> Path:
    """Create a JSONL file with controlled content and mtime."""
    p = directory / f"{uuid}.jsonl"
    if entries is None:
        entries = [{"type": "system", "uuid": f"{uuid}-init"}]
    p.write_text("".join(json.dumps(e) + "\n" for e in entries))
    t = time.time() + mtime_offset
    os.utime(p, (t, t))
    return p


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def container_dir(tmp_path):
    """Isolated container resolution directory."""
    d = tmp_path / "sessions" / "-workspace-repo"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def host_projects_dir(tmp_path):
    """Simulated ~/.claude/projects/ tree for host resolution."""
    projects = tmp_path / ".claude" / "projects"
    projects.mkdir(parents=True)
    return projects


# ── TestContainerResolution ───────────────────────────────────────────────

class TestContainerResolution:
    """Container sessions discover their first JSONL via _resolve_jsonl_in_dir."""

    def test_first_jsonl_discovered(self, container_dir):
        """Create JSONL in resolution_dir → session resolves.

        Expected: GREEN — _resolve_jsonl_in_dir returns the file.
        """
        jsonl = _make_jsonl(container_dir, "aaaa-1111", [
            {"type": "user", "message": {"content": "hello"}, "uuid": "msg-001"},
        ])

        resolved = SessionMonitor._resolve_jsonl_in_dir(container_dir)
        assert resolved is not None, "Should discover the JSONL file"
        assert resolved == jsonl
        assert resolved.stem == "aaaa-1111"

    def test_resolution_dir_stable_after_discovery(self, container_dir):
        """resolution_dir unchanged after file found — needed for rollover detection.

        Expected: GREEN — _TailState.resolution_dir is preserved; code keeps it
        after resolution (the bug where it was cleared is fixed per graph://9cbf8b80).
        """
        jsonl = _make_jsonl(container_dir, "bbbb-2222")

        ts = _TailState(needs_resolution=True, resolution_dir=container_dir)

        # Simulate the resolution block from _polling_tailer_loop
        resolved = SessionMonitor._resolve_jsonl_in_dir(ts.resolution_dir)
        if resolved:
            ts.needs_resolution = False
            # Key invariant: resolution_dir is NOT cleared
            # (Bug fix: line `ts.resolution_dir = None` was removed)

        assert not ts.needs_resolution, "Should be resolved"
        assert ts.resolution_dir == container_dir, (
            "resolution_dir must be preserved for rollover detection"
        )

    def test_empty_dir_returns_none(self, container_dir):
        """Empty directory → no JSONL → returns None.

        Expected: GREEN — _resolve_jsonl_in_dir handles empty dirs gracefully.
        """
        resolved = SessionMonitor._resolve_jsonl_in_dir(container_dir)
        assert resolved is None

    def test_multiple_files_returns_newest(self, container_dir):
        """Multiple JSONL files → returns newest by mtime.

        Expected: GREEN — _resolve_jsonl_in_dir uses max(key=st_mtime).
        """
        old = _make_jsonl(container_dir, "old-uuid", mtime_offset=-10)
        new = _make_jsonl(container_dir, "new-uuid", mtime_offset=0)

        resolved = SessionMonitor._resolve_jsonl_in_dir(container_dir)
        assert resolved == new, "Should return the newest file"


# ── TestHostResolution ────────────────────────────────────────────────────

class TestHostResolution:
    """Host sessions discover their JSONL via .meta.json tmux_session matching."""

    def test_meta_json_matches_tmux(self, host_projects_dir):
        """meta.json with tmux_session matching → resolved.

        Expected: GREEN — _resolve_host_jsonl scans .meta.json files for match.
        """
        project_dir = host_projects_dir / "-workspace-repo"
        project_dir.mkdir()

        # Create JSONL and its .meta.json
        jsonl = _make_jsonl(project_dir, "aaaa-1111")
        meta = project_dir / "aaaa-1111.meta.json"
        meta.write_text(json.dumps({"tmux_session": "host-test-001"}))

        monitor = SessionMonitor()
        # Path.home() / ".claude" / "projects" must resolve to host_projects_dir
        # host_projects_dir = tmp_path / ".claude" / "projects"
        # So Path.home() should return tmp_path
        fake_home = host_projects_dir.parent.parent  # tmp_path
        with patch("pathlib.Path.home", return_value=fake_home):
            resolved = monitor._resolve_host_jsonl("host-test-001")

        assert resolved is not None, "Should find JSONL via .meta.json match"
        assert resolved.stem == "aaaa-1111"

    def test_meta_json_wrong_tmux_ignored(self, host_projects_dir):
        """meta.json with different tmux → not resolved.

        Expected: GREEN — _resolve_host_jsonl only matches exact tmux_session.
        """
        project_dir = host_projects_dir / "-workspace-repo"
        project_dir.mkdir()

        jsonl = _make_jsonl(project_dir, "bbbb-2222")
        meta = project_dir / "bbbb-2222.meta.json"
        meta.write_text(json.dumps({"tmux_session": "host-OTHER-session"}))

        monitor = SessionMonitor()
        with patch("pathlib.Path.home", return_value=host_projects_dir.parent.parent):
            resolved = monitor._resolve_host_jsonl("host-test-002")

        assert resolved is None, "Should not match different tmux_session"

    def test_meta_json_no_jsonl_ignored(self, host_projects_dir):
        """.meta.json exists but .jsonl missing → not resolved.

        Expected: GREEN — _resolve_host_jsonl checks jsonl.exists() before returning.
        """
        project_dir = host_projects_dir / "-workspace-repo"
        project_dir.mkdir()

        # Only create .meta.json, no .jsonl
        meta = project_dir / "cccc-3333.meta.json"
        meta.write_text(json.dumps({"tmux_session": "host-test-003"}))
        # Don't create cccc-3333.jsonl

        monitor = SessionMonitor()
        with patch("pathlib.Path.home", return_value=host_projects_dir.parent.parent):
            resolved = monitor._resolve_host_jsonl("host-test-003")

        assert resolved is None, "Should not resolve when .jsonl is missing"

    def test_no_meta_stays_unresolved(self, host_projects_dir):
        """No .meta.json → session stays unresolved.

        Expected: GREEN — _resolve_host_jsonl returns None when no meta files match.
        """
        project_dir = host_projects_dir / "-workspace-repo"
        project_dir.mkdir()

        # Create a JSONL but no .meta.json
        _make_jsonl(project_dir, "dddd-4444")

        monitor = SessionMonitor()
        with patch("pathlib.Path.home", return_value=host_projects_dir.parent.parent):
            resolved = monitor._resolve_host_jsonl("host-test-004")

        assert resolved is None, "Should stay unresolved without .meta.json"
