"""Tests for compaction recovery — JSONL inode change detection.

When a session compacts (or any other in-place rewrite that unlinks the
old file and creates a new one at the same path), the kernel auto-removes
the IN_MODIFY watch on the old inode and delivers IN_IGNORED. Without
re-registering, the dashboard is permanently blind to the new file.

These tests cover both recovery paths:
  - IN_CREATE on the parent directory triggers _rewatch_replaced_jsonl
  - The reconciliation loop's per-session inode check triggers it as a
    backstop when IN_CREATE was missed
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# Skip module if inotify_simple / pytest-asyncio aren't installed (container deps).
pytest.importorskip("inotify_simple")
pytest.importorskip("pytest_asyncio")

from tools.dashboard.session_harness import parse_claude_log_line as _parse_jsonl_entry


# ── Helpers (mirrors test_inotify_tailer.py) ───────────────────────────

def _init_test_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute("""CREATE TABLE IF NOT EXISTS tmux_sessions (
        tmux_name TEXT PRIMARY KEY, session_uuid TEXT, graph_source_id TEXT,
        type TEXT NOT NULL, project TEXT NOT NULL, jsonl_path TEXT,
        bead_id TEXT, created_at REAL NOT NULL, is_live INTEGER DEFAULT 1,
        file_offset INTEGER DEFAULT 0, last_activity REAL,
        last_message TEXT DEFAULT '', entry_count INTEGER DEFAULT 0,
        context_tokens INTEGER DEFAULT 0, label TEXT DEFAULT '',
        topics TEXT DEFAULT '[]', role TEXT DEFAULT '',
        nag_enabled INTEGER DEFAULT 0, nag_interval INTEGER DEFAULT 15,
        nag_message TEXT DEFAULT '', nag_last_sent REAL DEFAULT 0,
        dispatch_nag INTEGER DEFAULT 0,
        resolution_dir TEXT, session_uuids TEXT DEFAULT '[]',
        curr_jsonl_file TEXT
    )""")
    conn.commit()
    conn.close()


def _insert_session(
    db_path: Path,
    tmux_name: str,
    jsonl_path: str,
    *,
    session_type: str = "host",
    resolution_dir: str | None = None,
) -> None:
    """Insert a test session.

    Default to ``host`` so the IN_CREATE rewatch path can be exercised
    without _handle_container_create racing to re-link via _link_session_file
    (which fires `graph ingest-session` as a subprocess).
    """
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO tmux_sessions"
        " (tmux_name, type, project, jsonl_path, created_at, is_live,"
        "  resolution_dir, session_uuids, curr_jsonl_file)"
        " VALUES (?, ?, 'test', ?, ?, 1, ?, '[]', ?)",
        (
            tmux_name,
            session_type,
            jsonl_path,
            time.time(),
            resolution_dir or str(Path(jsonl_path).parent),
            jsonl_path,
        ),
    )
    conn.commit()
    conn.close()


def _make_assistant_entry(text: str = "Hello") -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
        "timestamp": "2026-04-29T00:00:00Z",
    }


def _write_jsonl_entry(path: Path, entry: dict) -> None:
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")


@pytest.fixture
def setup_env(tmp_path):
    db_path = tmp_path / "dashboard.db"
    _init_test_db(db_path)
    os.environ["DASHBOARD_DB"] = str(db_path)
    import importlib
    from tools.dashboard.dao import dashboard_db as db_mod
    importlib.reload(db_mod)
    yield tmp_path, db_path
    os.environ.pop("DASHBOARD_DB", None)


def _replace_inode(path: Path, fresh_content: str) -> int:
    """Unlink + recreate ``path`` so it has a new inode. Returns the inode."""
    old_inode = path.stat().st_ino
    path.unlink()
    path.write_text(fresh_content)
    new_inode = path.stat().st_ino
    # Filesystems occasionally recycle inodes; the test only makes sense
    # when the inode actually changed. Keep retrying with extra writes
    # until it differs or we've tried several times.
    attempt = 0
    while new_inode == old_inode and attempt < 5:
        path.unlink()
        # Touch a sibling to perturb inode allocation, then recreate.
        sibling = path.parent / f"_inode-perturb-{attempt}"
        sibling.write_text("x")
        path.write_text(fresh_content)
        sibling.unlink()
        new_inode = path.stat().st_ino
        attempt += 1
    assert new_inode != old_inode, "could not coerce a new inode for the test"
    return new_inode


# ── Tests ──────────────────────────────────────────────────────────────


class TestAddFileWatchRecordsInode:
    """_add_file_watch must record last_known_inode for the safety net."""

    def test_inode_recorded(self, setup_env):
        tmp_path, _ = setup_env
        jsonl = tmp_path / "test.jsonl"
        jsonl.write_text("")

        from tools.dashboard.session_monitor import SessionMonitor, _TailState
        mon = SessionMonitor()
        mon._init_inotify()
        mon._tail_states["auto-i"] = _TailState()
        mon._add_file_watch("auto-i", str(jsonl))

        ts = mon._tail_states["auto-i"]
        assert ts.last_known_inode == jsonl.stat().st_ino


class TestInCreateRewatch:
    """IN_CREATE on the dir-watch should re-register the file watch when
    a tracked path is replaced with a new inode."""

    @pytest.mark.asyncio
    async def test_compaction_resumes_broadcasts(self, setup_env):
        tmp_path, db_path = setup_env

        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir()
        jsonl = sess_dir / "session.jsonl"
        _write_jsonl_entry(jsonl, _make_assistant_entry("pre-compaction"))
        _insert_session(
            db_path, "auto-comp-1", str(jsonl),
            session_type="host",  # avoid _link_session_file subprocess
            resolution_dir=str(sess_dir),
        )

        from tools.dashboard.session_monitor import SessionMonitor
        from tools.dashboard.event_bus import EventBus

        bus = EventBus()
        mon = SessionMonitor()

        with patch.object(SessionMonitor, "_check_tmux", staticmethod(lambda name: True)):
            await mon.start(event_bus=bus, entry_parser=_parse_jsonl_entry)
            assert mon._use_inotify

            ts = mon._tail_states.get("auto-comp-1")
            assert ts is not None
            old_wd = ts.watch_descriptor
            old_inode = ts.last_known_inode
            assert old_wd is not None
            assert old_inode != 0

            # Drain the initial registry events.
            q = bus.subscribe()
            await asyncio.sleep(0.1)
            while not q.empty():
                q.get_nowait()

            # Compaction: unlink + recreate at the same path. Write a
            # post-compaction entry into the new file.
            new_inode = _replace_inode(
                jsonl,
                json.dumps(_make_assistant_entry("post-compaction")) + "\n",
            )

            # Wait for the inotify tailer to process IN_IGNORED + IN_CREATE.
            # The settle predicate must cover watch identity AND the
            # persisted cursor reaching the new file's EOF — stopping at
            # reattachment races the async catch-up drain, which can then
            # leak into executor shutdown at test teardown (round-2 review
            # polish item).
            from tools.dashboard.dao.dashboard_db import get_session
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                ts = mon._tail_states.get("auto-comp-1")
                row = get_session("auto-comp-1")
                if (
                    ts is not None
                    and ts.watch_descriptor is not None
                    and ts.last_known_inode == new_inode
                    and row is not None
                    and row["file_offset"] == jsonl.stat().st_size
                ):
                    break
                await asyncio.sleep(0.05)

            ts = mon._tail_states.get("auto-comp-1")
            assert ts is not None, "TailState should still exist after rewatch"
            assert ts.watch_descriptor is not None, (
                "watch_descriptor must be re-registered after compaction"
            )
            assert ts.last_known_inode == new_inode, (
                f"last_known_inode {ts.last_known_inode} != new {new_inode}"
            )
            assert ts.watch_descriptor in mon._wd_to_inode
            inode_key = mon._wd_to_inode[ts.watch_descriptor]
            assert ("session", "auto-comp-1") in mon._inode_watches[inode_key]["subscribers"]

            # File offset must be reset (new file is a fresh stream).
            row = get_session("auto-comp-1")
            # Offset may have already advanced past byte 0 because the
            # rewatch path tails immediately. The contract is that it was
            # reset before the post-compaction read; it should now equal
            # the post-compaction file size.
            assert row["file_offset"] == jsonl.stat().st_size

            # A session:messages broadcast for the new content should have
            # been emitted.
            saw_messages = False
            for _ in range(50):
                if q.empty():
                    await asyncio.sleep(0.02)
                    continue
                topic, data, _seq = q.get_nowait()
                if topic == "session:messages" and data["session_id"] == "auto-comp-1":
                    saw_messages = True
                    break
            assert saw_messages, (
                "expected session:messages broadcast for post-compaction entry"
            )

            mon._tailer_task.cancel()
            mon._liveness_task.cancel()
            if mon._reconciliation_task is not None:
                mon._reconciliation_task.cancel()
            for task in (
                mon._tailer_task,
                mon._liveness_task,
                mon._reconciliation_task,
            ):
                if task is None:
                    continue
                try:
                    await task
                except asyncio.CancelledError:
                    pass


class TestReconciliationInodeBackstop:
    """The reconciliation loop must rewatch when last_known_inode disagrees
    with the on-disk inode, even if IN_CREATE was never delivered."""

    @pytest.mark.asyncio
    async def test_reconciliation_detects_inode_change(self, setup_env):
        tmp_path, db_path = setup_env

        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir()
        jsonl = sess_dir / "session.jsonl"
        _write_jsonl_entry(jsonl, _make_assistant_entry())
        _insert_session(
            db_path, "auto-recon-1", str(jsonl),
            session_type="host",
            resolution_dir=str(sess_dir),
        )

        from tools.dashboard.session_monitor import SessionMonitor, _TailState
        mon = SessionMonitor()
        mon._init_inotify()

        # Seed a tail state with an inode that won't match the on-disk one.
        # This simulates the kernel skipping IN_IGNORED + IN_CREATE delivery
        # entirely (queue overflow, dropped events).
        ts = _TailState()
        ts.last_known_inode = jsonl.stat().st_ino
        ts.watch_descriptor = None  # as if IN_IGNORED already cleared it
        mon._tail_states["auto-recon-1"] = ts

        # Replace inode without going through inotify.
        new_inode = _replace_inode(
            jsonl,
            json.dumps(_make_assistant_entry("via-reconciliation")) + "\n",
        )
        assert new_inode != ts.last_known_inode

        # Drive a single reconciliation tick — no SSE bus needed for the
        # rewatch verification; tail entries will be processed without a
        # broadcast (event_bus is None) but the watch state should update.
        await mon.reconciliation_tick()

        ts = mon._tail_states.get("auto-recon-1")
        assert ts is not None
        assert ts.watch_descriptor is not None, (
            "reconciliation must re-register the watch when inode changed"
        )
        assert ts.last_known_inode == new_inode, (
            f"last_known_inode {ts.last_known_inode} != new {new_inode}"
        )
        assert ts.watch_descriptor in mon._wd_to_inode

    @pytest.mark.asyncio
    async def test_reconciliation_skips_when_inode_unchanged(self, setup_env):
        tmp_path, db_path = setup_env

        sess_dir = tmp_path / "sessions"
        sess_dir.mkdir()
        jsonl = sess_dir / "session.jsonl"
        _write_jsonl_entry(jsonl, _make_assistant_entry())
        _insert_session(
            db_path, "auto-recon-2", str(jsonl),
            session_type="host",
            resolution_dir=str(sess_dir),
        )

        from tools.dashboard.session_monitor import SessionMonitor, _TailState
        mon = SessionMonitor()
        mon._init_inotify()

        ts = _TailState()
        ts.last_known_inode = jsonl.stat().st_ino
        ts.watch_descriptor = 9999  # placeholder — must remain untouched
        mon._tail_states["auto-recon-2"] = ts

        await mon.reconciliation_tick()

        ts = mon._tail_states.get("auto-recon-2")
        assert ts is not None
        # Watch descriptor should be unchanged because the inode did not
        # change. The reconciliation backstop should not touch a healthy
        # watch.
        assert ts.watch_descriptor == 9999
