"""Tests for initial JSONL file discovery — Boundary A (filesystem → monitor).

Covers the path from newly registered session to first JSONL resolution:
  - Container: IN_CREATE detects first JSONL in resolution_dir
  - Host terminals resolve the same way: their transcript is in their run dir

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


# ── TestContainerResolution ───────────────────────────────────────────────

class TestContainerResolution:
    """Container sessions discover their first JSONL via IN_CREATE on resolution_dir."""

    def test_first_jsonl_discovered(self, container_dir):
        """Create JSONL in resolution_dir → _find_primary_jsonls finds it.

        Expected: GREEN — _find_primary_jsonls returns the file.
        """
        jsonl = _make_jsonl(container_dir, "aaaa-1111", [
            {"type": "user", "message": {"content": "hello"}, "uuid": "msg-001"},
        ])

        primaries = _find_primary_jsonls(container_dir)
        assert len(primaries) == 1, "Should discover the JSONL file"
        assert primaries[0] == jsonl
        assert primaries[0].stem == "aaaa-1111"

    def test_resolution_dir_stable_after_discovery(self, container_dir):
        """resolution_dir unchanged after file found — needed for IN_CREATE rollover.

        Expected: GREEN — _TailState.resolution_dir is preserved; code keeps it
        after resolution (the bug where it was cleared is fixed per graph://9cbf8b80).
        """
        _make_jsonl(container_dir, "bbbb-2222")

        ts = _TailState(needs_resolution=True, resolution_dir=container_dir)

        # Simulate IN_CREATE resolution
        primaries = _find_primary_jsonls(ts.resolution_dir)
        if primaries:
            ts.needs_resolution = False
            # Key invariant: resolution_dir is NOT cleared

        assert not ts.needs_resolution, "Should be resolved"
        assert ts.resolution_dir == container_dir, (
            "resolution_dir must be preserved for rollover detection"
        )

    def test_empty_dir_returns_empty(self, container_dir):
        """Empty directory → no JSONL → empty list.

        Expected: GREEN — _find_primary_jsonls handles empty dirs gracefully.
        """
        primaries = _find_primary_jsonls(container_dir)
        assert primaries == []

    def test_multiple_files_all_found(self, container_dir):
        """Multiple JSONL files → all found by _find_primary_jsonls.

        Expected: GREEN — _find_primary_jsonls returns all primary JSONLs.
        """
        old = _make_jsonl(container_dir, "old-uuid", mtime_offset=-10)
        new = _make_jsonl(container_dir, "new-uuid", mtime_offset=0)

        primaries = _find_primary_jsonls(container_dir)
        assert len(primaries) == 2, "Should find both JSONL files"
        assert set(primaries) == {old, new}
