"""Phase 2: todos snapshot persistence — tmux_sessions.todos column is written
by the tailer and warm-up paths, read back through the session registry.

Covers:
- Schema migration adds the ``todos`` column with DEFAULT '[]', idempotently.
- ``TaskStateTracker.snapshot(tmux_name)`` returns the per-session todo list
  in insertion order, reflecting subject renames + status transitions.
- ``update_todos()`` round-trips JSON and overwrites prior snapshots.
- Warm-up replay populates ``tmux_sessions.todos`` so post-restart reads
  see the full history WITHOUT waiting for a fresh TaskUpdate event
  (this is the load-bearing test for the restart-repopulation acceptance
  criterion; it fails cleanly when the warm-up persistence hook is absent).
- Live ``enrich()`` debounces: a no-op TaskUpdate that doesn't mutate the
  snapshot does NOT fire ``update_todos()`` again.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path

import pytest


# ── Schema migration ─────────────────────────────────────────────────────


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "dashboard.db"
    monkeypatch.setenv("DASHBOARD_DB", str(db_path))
    from tools.dashboard.dao import dashboard_db as db
    db._conn = None
    db._DB_PATH = db_path
    db.init_db(db_path)
    yield db
    if db._conn:
        db._conn.close()
        db._conn = None


class TestTodosMigration:
    def test_todos_column_exists(self, fresh_db):
        row = fresh_db.get_conn().execute(
            "SELECT todos FROM tmux_sessions LIMIT 0"
        ).description
        assert row is not None

    def test_todos_default_is_empty_list(self, fresh_db):
        fresh_db.upsert_session("sess-1", "container", "autonomy")
        row = fresh_db.get_session("sess-1")
        assert row["todos"] == "[]"

    def test_double_init_is_idempotent(self, tmp_path):
        from tools.dashboard.dao import dashboard_db as db
        db_path = tmp_path / "idempotent.db"
        db._conn = None
        db._DB_PATH = db_path
        db.init_db(db_path)
        db._conn.close()
        db._conn = None
        # Re-init against existing DB must not raise
        db.init_db(db_path)
        db.get_conn().execute("SELECT todos FROM tmux_sessions LIMIT 0")
        db._conn.close()
        db._conn = None


# ── TaskStateTracker.snapshot() ──────────────────────────────────────────


def _create(subject: str, **extra) -> dict:
    return {
        "type": "tool_use",
        "tool_name": "TaskCreate",
        "tool_id": f"tu-c-{subject[:8]}",
        "input": {"subject": subject, **extra},
    }


def _update(task_id: str, **fields) -> dict:
    return {
        "type": "tool_use",
        "tool_name": "TaskUpdate",
        "tool_id": f"tu-u-{task_id}-{fields.get('status','')}",
        "input": {"taskId": task_id, **fields},
    }


class TestSnapshot:
    def test_unknown_session_returns_empty_list(self):
        from tools.dashboard.session_monitor import TaskStateTracker
        tracker = TaskStateTracker()
        assert tracker.snapshot("never-seen") == []

    def test_reflects_create_and_update_in_insertion_order(self):
        from tools.dashboard.session_monitor import TaskStateTracker
        tracker = TaskStateTracker()
        tracker.enrich("sess-a", [
            _create("First"),
            _create("Second"),
            _create("Third"),
            _update("2", status="in_progress"),
            _update("1", status="completed"),
        ])
        snap = tracker.snapshot("sess-a")
        assert [t["task_id"] for t in snap] == ["1", "2", "3"]
        assert [t["subject"] for t in snap] == ["First", "Second", "Third"]
        assert [t["status"] for t in snap] == ["completed", "in_progress", "pending"]

    def test_reflects_subject_rename(self):
        from tools.dashboard.session_monitor import TaskStateTracker
        tracker = TaskStateTracker()
        tracker.enrich("s", [
            _create("Old name"),
            _update("1", subject="New name"),
        ])
        snap = tracker.snapshot("s")
        assert snap[0]["subject"] == "New name"

    def test_returns_fresh_list_each_call(self):
        """Caller mutating the returned list must not affect tracker state."""
        from tools.dashboard.session_monitor import TaskStateTracker
        tracker = TaskStateTracker()
        tracker.enrich("s", [_create("T1"), _create("T2")])
        a = tracker.snapshot("s")
        a.clear()
        a.append({"hacked": True})
        b = tracker.snapshot("s")
        assert len(b) == 2
        assert b[0]["subject"] == "T1"

    def test_sessions_are_isolated(self):
        from tools.dashboard.session_monitor import TaskStateTracker
        tracker = TaskStateTracker()
        tracker.enrich("a", [_create("A only")])
        tracker.enrich("b", [_create("B only")])
        assert tracker.snapshot("a")[0]["subject"] == "A only"
        assert tracker.snapshot("b")[0]["subject"] == "B only"
        assert len(tracker.snapshot("a")) == 1


# ── update_todos() persistence ───────────────────────────────────────────


class TestUpdateTodosPersistence:
    def test_round_trip(self, fresh_db):
        fresh_db.upsert_session("s1", "container", "autonomy")
        todos = [
            {"task_id": "1", "subject": "Do a thing",
             "status": "in_progress", "description": "", "activeForm": "Doing a thing"},
        ]
        fresh_db.update_todos("s1", todos)
        row = fresh_db.get_session("s1")
        assert json.loads(row["todos"]) == todos

    def test_overwrites_prior_snapshot(self, fresh_db):
        fresh_db.upsert_session("s1", "container", "autonomy")
        fresh_db.update_todos("s1", [{"task_id": "1", "subject": "old",
                                      "status": "pending", "description": "", "activeForm": ""}])
        fresh_db.update_todos("s1", [{"task_id": "2", "subject": "new",
                                      "status": "completed", "description": "", "activeForm": ""}])
        row = fresh_db.get_session("s1")
        stored = json.loads(row["todos"])
        assert len(stored) == 1
        assert stored[0]["task_id"] == "2"

    def test_empty_list_persists_as_empty_string(self, fresh_db):
        """An empty snapshot must store ``"[]"`` (never NULL) so readers can
        unconditionally ``json.loads`` without a None check."""
        fresh_db.upsert_session("s1", "container", "autonomy")
        fresh_db.update_todos("s1", [{"task_id": "1", "subject": "x",
                                      "status": "pending", "description": "", "activeForm": ""}])
        fresh_db.update_todos("s1", [])
        row = fresh_db.get_session("s1")
        assert row["todos"] == "[]"
        assert json.loads(row["todos"]) == []


# ── Warm-up repopulates the DB (restart path) ────────────────────────────


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    with open(path, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def _task_entry(tool_id: str, name: str, inp: dict, ts: str) -> dict:
    """Emit a Claude-format assistant entry wrapping a single Task* tool_use.
    ``_parse_jsonl_entry`` unwraps message.content and produces the parsed
    dicts the tracker consumes.
    """
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": inp}],
        },
        "timestamp": ts,
    }


class TestWarmupPopulatesDb:
    """The critical restart-repopulation test.

    A fresh SessionMonitor + TaskStateTracker simulates a cold dashboard
    process. Warm-up must persist the reconstructed snapshot to
    ``tmux_sessions.todos`` on its own — no live TaskUpdate event needed.
    """

    def test_warmup_writes_todos_snapshot(self, fresh_db, tmp_path, monkeypatch):
        # Seed JSONL with a history of Task* events.
        jsonl = tmp_path / "session-abc.jsonl"
        entries = [
            _task_entry("t1", "TaskCreate",
                        {"subject": "Investigate bug", "description": "find root cause",
                         "activeForm": "Investigating bug", "status": "pending"},
                        "2026-04-20T10:00:00Z"),
            _task_entry("t2", "TaskCreate",
                        {"subject": "Write fix", "description": "implement fix",
                         "activeForm": "Writing fix", "status": "pending"},
                        "2026-04-20T10:01:00Z"),
            _task_entry("t3", "TaskUpdate",
                        {"taskId": "1", "status": "in_progress"},
                        "2026-04-20T10:02:00Z"),
            _task_entry("t4", "TaskUpdate",
                        {"taskId": "1", "status": "completed"},
                        "2026-04-20T10:03:00Z"),
            _task_entry("t5", "TaskUpdate",
                        {"taskId": "2", "status": "in_progress"},
                        "2026-04-20T10:04:00Z"),
        ]
        _write_jsonl(jsonl, entries)

        # Seed DB: session exists, file_offset set past end of file so the
        # warm-up replay covers the full history.
        fresh_db.upsert_session("auto-warm", "container", "autonomy",
                                jsonl_path=str(jsonl))
        file_size = jsonl.stat().st_size
        fresh_db.update_tail_state("auto-warm", file_offset=file_size)

        # Build a fresh monitor + tracker (simulating a new process).
        from tools.dashboard.session_monitor import SessionMonitor, TaskStateTracker, _TailState
        from tools.dashboard.server import _parse_jsonl_entry
        monitor = SessionMonitor()
        tracker = TaskStateTracker()
        monitor._entry_parser = _parse_jsonl_entry
        monitor._entry_enricher = tracker.enrich
        monitor._todo_snapshot = tracker.snapshot

        # Warm-up for the session — simulates the first post-restart read.
        ts = _TailState()
        row = fresh_db.get_session("auto-warm")
        asyncio.run(monitor._warm_task_tracker_if_needed("auto-warm", dict(row), ts))

        # DB now has the reconstructed snapshot — no fresh TaskUpdate fired.
        stored = json.loads(fresh_db.get_session("auto-warm")["todos"])
        assert [t["task_id"] for t in stored] == ["1", "2"]
        assert stored[0]["subject"] == "Investigate bug"
        assert stored[0]["status"] == "completed"
        assert stored[1]["subject"] == "Write fix"
        assert stored[1]["status"] == "in_progress"

        # Tracker state matches what was persisted (same representation).
        assert tracker.snapshot("auto-warm") == stored

    def test_cold_start_api_returns_todos(self, fresh_db, tmp_path, monkeypatch):
        """End-to-end cold-start flow: after warm-up, the session registry
        surfaces todos so /api/dao/active_sessions reflects them.
        """
        jsonl = tmp_path / "session-xyz.jsonl"
        entries = [
            _task_entry("a", "TaskCreate",
                        {"subject": "First", "description": "",
                         "activeForm": "Doing first", "status": "pending"},
                        "2026-04-20T10:00:00Z"),
            _task_entry("b", "TaskUpdate",
                        {"taskId": "1", "status": "in_progress"},
                        "2026-04-20T10:01:00Z"),
        ]
        _write_jsonl(jsonl, entries)

        fresh_db.upsert_session("auto-cold", "container", "autonomy",
                                jsonl_path=str(jsonl))
        fresh_db.update_tail_state("auto-cold", file_offset=jsonl.stat().st_size)

        from tools.dashboard.session_monitor import SessionMonitor, TaskStateTracker, _TailState
        from tools.dashboard.server import _parse_jsonl_entry
        monitor = SessionMonitor()
        tracker = TaskStateTracker()
        monitor._entry_parser = _parse_jsonl_entry
        monitor._entry_enricher = tracker.enrich
        monitor._todo_snapshot = tracker.snapshot

        ts = _TailState()
        row = fresh_db.get_session("auto-cold")
        asyncio.run(monitor._warm_task_tracker_if_needed("auto-cold", dict(row), ts))

        # Registry payload is what /api/dao/active_sessions returns.
        registry = monitor.get_registry()
        entry = next(e for e in registry if e["session_id"] == "auto-cold")
        assert "todos" in entry
        assert entry["todos"][0]["task_id"] == "1"
        assert entry["todos"][0]["status"] == "in_progress"
        assert entry["todos"][0]["subject"] == "First"


# ── Live-update + debounce ───────────────────────────────────────────────


class TestLiveUpdateDebounce:
    """After warm-up, live TaskUpdates flowing through ``enrich()`` trigger
    one ``update_todos()`` per actual snapshot change. A no-op update does
    NOT write to the DB.
    """

    def test_enrich_persists_new_snapshot_and_debounces_noop(self, fresh_db):
        fresh_db.upsert_session("live", "container", "autonomy")
        from tools.dashboard.session_monitor import SessionMonitor, TaskStateTracker, _TailState
        monitor = SessionMonitor()
        tracker = TaskStateTracker()
        monitor._entry_enricher = tracker.enrich
        monitor._todo_snapshot = tracker.snapshot
        ts = _TailState()

        # First enrichment: seeds two tasks; persistence writes the snapshot.
        tracker.enrich("live", [_create("A"), _create("B")])
        asyncio.run(monitor._persist_todos_if_changed("live", ts))
        first = json.loads(fresh_db.get_session("live")["todos"])
        assert [t["subject"] for t in first] == ["A", "B"]

        # Corrupt the DB row to detect whether the debounce blocks a no-op.
        # If the helper tries to write, the value reverts to the real snapshot.
        conn = fresh_db.get_conn()
        conn.execute("UPDATE tmux_sessions SET todos=? WHERE tmux_name=?",
                     (json.dumps([{"sentinel": True}]), "live"))
        conn.commit()

        # Apply an update that DOES change the snapshot → DB should be
        # overwritten from the sentinel.
        tracker.enrich("live", [_update("1", status="in_progress")])
        asyncio.run(monitor._persist_todos_if_changed("live", ts))
        after = json.loads(fresh_db.get_session("live")["todos"])
        assert [t["status"] for t in after] == ["in_progress", "pending"]

        # Re-corrupt, then apply an identity "update" that yields the same
        # snapshot. The debounce must block the write → sentinel survives.
        conn.execute("UPDATE tmux_sessions SET todos=? WHERE tmux_name=?",
                     (json.dumps([{"sentinel": True}]), "live"))
        conn.commit()
        # TaskUpdate with same status — snapshot unchanged (debounce hit).
        tracker.enrich("live", [_update("1", status="in_progress")])
        asyncio.run(monitor._persist_todos_if_changed("live", ts))
        sentinel_check = json.loads(fresh_db.get_session("live")["todos"])
        assert sentinel_check == [{"sentinel": True}], \
            "debounce should have prevented a redundant update_todos() write"
