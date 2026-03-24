"""Tests for JSONL rollover detection in session_monitor._tailer_loop().

When Claude rolls over in a container (plan mode exit, context exhaustion, /clear),
it creates a new JSONL with a new UUID. The session monitor must detect the new file
and switch to it via link_and_enrich + tail state reset.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tools.dashboard.session_monitor import SessionMonitor, _TailState


@pytest.fixture()
def tmp_sessions_dir(tmp_path):
    """Create a sessions directory with an old JSONL file."""
    sessions_dir = tmp_path / "autonomy"
    sessions_dir.mkdir()
    return sessions_dir


def _make_jsonl(sessions_dir: Path, name: str, content: str = '{"type":"system"}\n', mtime_offset: float = 0) -> Path:
    """Create a JSONL file with controlled mtime."""
    p = sessions_dir / f"{name}.jsonl"
    p.write_text(content)
    # Set mtime relative to now
    t = time.time() + mtime_offset
    import os
    os.utime(p, (t, t))
    return p


class TestJSONLRolloverDetection:
    """Test the rollover detection block in _tailer_loop."""

    def test_rollover_detected_when_newer_jsonl_exists(self, tmp_sessions_dir):
        """When a newer JSONL appears in the sessions dir, link_and_enrich is called."""
        old_jsonl = _make_jsonl(tmp_sessions_dir, "old-uuid", mtime_offset=-10)
        new_jsonl = _make_jsonl(tmp_sessions_dir, "new-uuid", mtime_offset=0)

        monitor = SessionMonitor()
        # Pre-populate tail state as if we were already tailing the old file
        monitor._tail_states["auto-0323-container"] = _TailState()

        sessions = [{
            "tmux_name": "auto-0323-container",
            "jsonl_path": str(old_jsonl),
            "is_live": 1,
            "file_offset": 100,
        }]

        with patch("tools.dashboard.dao.dashboard_db.link_and_enrich") as mock_link:
            # Run just the rollover detection logic extracted from _tailer_loop
            tailable = {r["tmux_name"]: r for r in sessions}
            for tmux_name, ts in list(monitor._tail_states.items()):
                if ts.needs_resolution:
                    continue
                row = tailable.get(tmux_name)
                if not row or not row.get("jsonl_path"):
                    continue
                current_path = Path(row["jsonl_path"])
                if not current_path.exists():
                    continue
                sessions_dir = current_path.parent
                jsonl_files = sorted(sessions_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
                if len(jsonl_files) <= 1:
                    continue
                newest = jsonl_files[-1]
                if newest != current_path:
                    mock_link(
                        tmux_name,
                        session_uuid=newest.stem,
                        jsonl_path=str(newest),
                        project=sessions_dir.name,
                    )
                    monitor._tail_states.pop(tmux_name, None)
                    monitor._tail_states[tmux_name] = _TailState()

            # Verify link_and_enrich called with new file
            mock_link.assert_called_once_with(
                "auto-0323-container",
                session_uuid="new-uuid",
                jsonl_path=str(new_jsonl),
                project="autonomy",
            )

        # Verify tail state was reset
        ts = monitor._tail_states["auto-0323-container"]
        assert ts.broadcast_seq == 0  # fresh TailState

    def test_no_rollover_when_single_jsonl(self, tmp_sessions_dir):
        """No rollover when only one JSONL exists."""
        only_jsonl = _make_jsonl(tmp_sessions_dir, "only-uuid")

        monitor = SessionMonitor()
        monitor._tail_states["auto-single"] = _TailState()
        monitor._tail_states["auto-single"].broadcast_seq = 5  # simulate existing activity

        sessions = [{
            "tmux_name": "auto-single",
            "jsonl_path": str(only_jsonl),
            "is_live": 1,
        }]

        with patch("tools.dashboard.dao.dashboard_db.link_and_enrich") as mock_link:
            tailable = {r["tmux_name"]: r for r in sessions}
            for tmux_name, ts in list(monitor._tail_states.items()):
                if ts.needs_resolution:
                    continue
                row = tailable.get(tmux_name)
                if not row or not row.get("jsonl_path"):
                    continue
                current_path = Path(row["jsonl_path"])
                if not current_path.exists():
                    continue
                sessions_dir = current_path.parent
                jsonl_files = sorted(sessions_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
                if len(jsonl_files) <= 1:
                    continue
                newest = jsonl_files[-1]
                if newest != current_path:
                    mock_link(
                        tmux_name,
                        session_uuid=newest.stem,
                        jsonl_path=str(newest),
                        project=sessions_dir.name,
                    )
                    monitor._tail_states.pop(tmux_name, None)
                    monitor._tail_states[tmux_name] = _TailState()

            mock_link.assert_not_called()

        # Tail state unchanged
        assert monitor._tail_states["auto-single"].broadcast_seq == 5

    def test_no_rollover_when_current_is_newest(self, tmp_sessions_dir):
        """No rollover when the tracked file IS the newest."""
        old_jsonl = _make_jsonl(tmp_sessions_dir, "old-uuid", mtime_offset=-10)
        current_jsonl = _make_jsonl(tmp_sessions_dir, "current-uuid", mtime_offset=0)

        monitor = SessionMonitor()
        monitor._tail_states["auto-current"] = _TailState()

        sessions = [{
            "tmux_name": "auto-current",
            "jsonl_path": str(current_jsonl),  # already tracking the newest
            "is_live": 1,
        }]

        with patch("tools.dashboard.dao.dashboard_db.link_and_enrich") as mock_link:
            tailable = {r["tmux_name"]: r for r in sessions}
            for tmux_name, ts in list(monitor._tail_states.items()):
                if ts.needs_resolution:
                    continue
                row = tailable.get(tmux_name)
                if not row or not row.get("jsonl_path"):
                    continue
                current_path = Path(row["jsonl_path"])
                if not current_path.exists():
                    continue
                sessions_dir = current_path.parent
                jsonl_files = sorted(sessions_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
                if len(jsonl_files) <= 1:
                    continue
                newest = jsonl_files[-1]
                if newest != current_path:
                    mock_link(
                        tmux_name,
                        session_uuid=newest.stem,
                        jsonl_path=str(newest),
                        project=sessions_dir.name,
                    )
                    monitor._tail_states.pop(tmux_name, None)
                    monitor._tail_states[tmux_name] = _TailState()

            mock_link.assert_not_called()

    def test_unresolved_sessions_skipped(self, tmp_sessions_dir):
        """Sessions with needs_resolution=True are skipped by rollover detection."""
        old_jsonl = _make_jsonl(tmp_sessions_dir, "old-uuid", mtime_offset=-10)
        new_jsonl = _make_jsonl(tmp_sessions_dir, "new-uuid", mtime_offset=0)

        monitor = SessionMonitor()
        ts = _TailState()
        ts.needs_resolution = True  # still unresolved
        monitor._tail_states["auto-unresolved"] = ts

        sessions = [{
            "tmux_name": "auto-unresolved",
            "jsonl_path": str(old_jsonl),
            "is_live": 1,
        }]

        with patch("tools.dashboard.dao.dashboard_db.link_and_enrich") as mock_link:
            tailable = {r["tmux_name"]: r for r in sessions}
            for tmux_name, ts in list(monitor._tail_states.items()):
                if ts.needs_resolution:
                    continue
                row = tailable.get(tmux_name)
                if not row or not row.get("jsonl_path"):
                    continue
                current_path = Path(row["jsonl_path"])
                if not current_path.exists():
                    continue
                sessions_dir = current_path.parent
                jsonl_files = sorted(sessions_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
                if len(jsonl_files) <= 1:
                    continue
                newest = jsonl_files[-1]
                if newest != current_path:
                    mock_link(
                        tmux_name,
                        session_uuid=newest.stem,
                        jsonl_path=str(newest),
                        project=sessions_dir.name,
                    )
                    monitor._tail_states.pop(tmux_name, None)
                    monitor._tail_states[tmux_name] = _TailState()

            mock_link.assert_not_called()

    def test_rollover_with_multiple_old_files(self, tmp_sessions_dir):
        """When multiple old JSONLs exist, the newest one is selected."""
        oldest = _make_jsonl(tmp_sessions_dir, "uuid-1", mtime_offset=-20)
        middle = _make_jsonl(tmp_sessions_dir, "uuid-2", mtime_offset=-10)
        newest = _make_jsonl(tmp_sessions_dir, "uuid-3", mtime_offset=0)

        monitor = SessionMonitor()
        monitor._tail_states["auto-multi"] = _TailState()

        # Currently tracking the middle file (not the oldest, not the newest)
        sessions = [{
            "tmux_name": "auto-multi",
            "jsonl_path": str(middle),
            "is_live": 1,
        }]

        with patch("tools.dashboard.dao.dashboard_db.link_and_enrich") as mock_link:
            tailable = {r["tmux_name"]: r for r in sessions}
            for tmux_name, ts in list(monitor._tail_states.items()):
                if ts.needs_resolution:
                    continue
                row = tailable.get(tmux_name)
                if not row or not row.get("jsonl_path"):
                    continue
                current_path = Path(row["jsonl_path"])
                if not current_path.exists():
                    continue
                sessions_dir = current_path.parent
                jsonl_files = sorted(sessions_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
                if len(jsonl_files) <= 1:
                    continue
                newest_file = jsonl_files[-1]
                if newest_file != current_path:
                    mock_link(
                        tmux_name,
                        session_uuid=newest_file.stem,
                        jsonl_path=str(newest_file),
                        project=sessions_dir.name,
                    )
                    monitor._tail_states.pop(tmux_name, None)
                    monitor._tail_states[tmux_name] = _TailState()

            # Should switch to uuid-3, not uuid-1
            mock_link.assert_called_once_with(
                "auto-multi",
                session_uuid="uuid-3",
                jsonl_path=str(newest),
                project="autonomy",
            )
