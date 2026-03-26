"""Tests for Phase 0 schema migration: resolution_dir, session_uuids, curr_jsonl_file.

Covers:
- New columns exist after init_db()
- upsert_session() preserves label/topics/role/nag on conflict
- insert_session() stores resolution_dir and derives session_uuids
- update_jsonl_link() appends to session_uuids array
- Backfill logic for existing rows
"""
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    """Each test gets a fresh dashboard.db via init_db()."""
    db_path = tmp_path / "dashboard.db"
    monkeypatch.setenv("DASHBOARD_DB", str(db_path))
    # Reset module-level connection so init_db() creates a fresh one
    from tools.dashboard.dao import dashboard_db as db
    db._conn = None
    db._DB_PATH = db_path
    db.init_db(db_path)
    yield db
    if db._conn:
        db._conn.close()
        db._conn = None


class TestNewColumnsExist:
    """New columns must exist after init_db()."""

    def test_resolution_dir_column(self, _isolate_db):
        db = _isolate_db
        row = db.get_conn().execute("SELECT resolution_dir FROM tmux_sessions LIMIT 0").description
        assert row is not None

    def test_session_uuids_column(self, _isolate_db):
        db = _isolate_db
        row = db.get_conn().execute("SELECT session_uuids FROM tmux_sessions LIMIT 0").description
        assert row is not None

    def test_curr_jsonl_file_column(self, _isolate_db):
        db = _isolate_db
        row = db.get_conn().execute("SELECT curr_jsonl_file FROM tmux_sessions LIMIT 0").description
        assert row is not None


class TestUpsertPreservesMetadata:
    """upsert_session() must NOT overwrite label, topics, role, nag settings."""

    def test_preserves_label_on_conflict(self, _isolate_db):
        db = _isolate_db
        # First insert with label
        db.upsert_session("test-1", "container", "autonomy", label="My Label")
        # Re-upsert with empty label (simulates seed_from_filesystem)
        db.upsert_session("test-1", "container", "autonomy", label="")
        row = db.get_session("test-1")
        assert row["label"] == "My Label"

    def test_preserves_topics_on_conflict(self, _isolate_db):
        db = _isolate_db
        db.upsert_session("test-1", "container", "autonomy")
        db.update_topics("test-1", ["Topic A", "Topic B"])
        # Re-upsert
        db.upsert_session("test-1", "container", "autonomy")
        row = db.get_session("test-1")
        assert json.loads(row["topics"]) == ["Topic A", "Topic B"]

    def test_preserves_role_on_conflict(self, _isolate_db):
        db = _isolate_db
        db.upsert_session("test-1", "container", "autonomy")
        db.update_role("test-1", "coordinator")
        # Re-upsert
        db.upsert_session("test-1", "container", "autonomy")
        row = db.get_session("test-1")
        assert row["role"] == "coordinator"

    def test_preserves_nag_on_conflict(self, _isolate_db):
        db = _isolate_db
        db.upsert_session("test-1", "container", "autonomy")
        db.update_nag_config("test-1", enabled=True, interval=10, message="Check in")
        # Re-upsert
        db.upsert_session("test-1", "container", "autonomy")
        nag = db.get_nag_config("test-1")
        assert nag["enabled"] is True
        assert nag["interval"] == 10
        assert nag["message"] == "Check in"

    def test_updates_file_fields_on_conflict(self, _isolate_db):
        db = _isolate_db
        db.upsert_session("test-1", "container", "autonomy",
                          jsonl_path="/old/path.jsonl", session_uuid="old-uuid")
        db.upsert_session("test-1", "container", "autonomy",
                          jsonl_path="/new/path.jsonl", session_uuid="new-uuid",
                          resolution_dir="/new", session_uuids=json.dumps(["new-uuid"]),
                          curr_jsonl_file="/new/path.jsonl")
        row = db.get_session("test-1")
        assert row["jsonl_path"] == "/new/path.jsonl"
        assert row["session_uuid"] == "new-uuid"
        assert row["resolution_dir"] == "/new"
        assert json.loads(row["session_uuids"]) == ["new-uuid"]
        assert row["curr_jsonl_file"] == "/new/path.jsonl"

    def test_coalesce_resolution_dir_not_overwritten_with_null(self, _isolate_db):
        db = _isolate_db
        db.upsert_session("test-1", "container", "autonomy",
                          resolution_dir="/existing/dir")
        # Re-upsert without resolution_dir
        db.upsert_session("test-1", "container", "autonomy")
        row = db.get_session("test-1")
        assert row["resolution_dir"] == "/existing/dir"

    def test_empty_session_uuids_does_not_overwrite(self, _isolate_db):
        db = _isolate_db
        db.upsert_session("test-1", "container", "autonomy",
                          session_uuids=json.dumps(["uuid-1", "uuid-2"]))
        # Re-upsert with default empty session_uuids
        db.upsert_session("test-1", "container", "autonomy")
        row = db.get_session("test-1")
        assert json.loads(row["session_uuids"]) == ["uuid-1", "uuid-2"]

    def test_updates_last_message_only_if_nonempty(self, _isolate_db):
        db = _isolate_db
        db.upsert_session("test-1", "container", "autonomy", last_message="Hello")
        db.upsert_session("test-1", "container", "autonomy", last_message="")
        row = db.get_session("test-1")
        assert row["last_message"] == "Hello"


class TestInsertSession:
    """insert_session() stores new columns correctly."""

    def test_stores_resolution_dir(self, _isolate_db):
        db = _isolate_db
        db.insert_session("test-1", "container", "autonomy", resolution_dir="/some/dir")
        row = db.get_session("test-1")
        assert row["resolution_dir"] == "/some/dir"

    def test_derives_session_uuids_from_uuid(self, _isolate_db):
        db = _isolate_db
        db.insert_session("test-1", "container", "autonomy",
                          session_uuid="abc-123", jsonl_path="/dir/abc-123.jsonl")
        row = db.get_session("test-1")
        assert json.loads(row["session_uuids"]) == ["abc-123"]
        assert row["curr_jsonl_file"] == "/dir/abc-123.jsonl"

    def test_empty_uuids_when_no_uuid(self, _isolate_db):
        db = _isolate_db
        db.insert_session("test-1", "container", "autonomy")
        row = db.get_session("test-1")
        assert json.loads(row["session_uuids"]) == []
        assert row["curr_jsonl_file"] is None


class TestUpdateJsonlLink:
    """update_jsonl_link() appends to session_uuids and sets curr_jsonl_file."""

    def test_appends_uuid(self, _isolate_db):
        db = _isolate_db
        db.insert_session("test-1", "container", "autonomy",
                          session_uuid="uuid-1", jsonl_path="/dir/uuid-1.jsonl")
        # Link a second file (rollover)
        db.update_jsonl_link("test-1", "uuid-2", "/dir/uuid-2.jsonl")
        row = db.get_session("test-1")
        assert json.loads(row["session_uuids"]) == ["uuid-1", "uuid-2"]
        assert row["curr_jsonl_file"] == "/dir/uuid-2.jsonl"
        assert row["session_uuid"] == "uuid-2"

    def test_does_not_duplicate_uuid(self, _isolate_db):
        db = _isolate_db
        db.insert_session("test-1", "container", "autonomy",
                          session_uuid="uuid-1", jsonl_path="/dir/uuid-1.jsonl")
        # Re-link same file
        db.update_jsonl_link("test-1", "uuid-1", "/dir/uuid-1.jsonl")
        row = db.get_session("test-1")
        assert json.loads(row["session_uuids"]) == ["uuid-1"]

    def test_sets_resolution_dir_on_first_link(self, _isolate_db):
        db = _isolate_db
        db.insert_session("test-1", "container", "autonomy")
        db.update_jsonl_link("test-1", "uuid-1", "/dir/proj/uuid-1.jsonl")
        row = db.get_session("test-1")
        assert row["resolution_dir"] == "/dir/proj"

    def test_does_not_overwrite_existing_resolution_dir(self, _isolate_db):
        db = _isolate_db
        db.insert_session("test-1", "container", "autonomy",
                          resolution_dir="/original/dir")
        db.update_jsonl_link("test-1", "uuid-1", "/other/uuid-1.jsonl")
        row = db.get_session("test-1")
        assert row["resolution_dir"] == "/original/dir"


class TestBackfill:
    """Backfill logic clears subagent paths and populates new columns."""

    def test_clears_subagent_path(self, tmp_path, monkeypatch):
        """Sessions with 'subagents' in jsonl_path get cleared."""
        db_path = tmp_path / "backfill.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        # Create the old schema (without new columns)
        conn.executescript("""
            CREATE TABLE tmux_sessions (
                tmux_name TEXT PRIMARY KEY, session_uuid TEXT, graph_source_id TEXT,
                type TEXT NOT NULL, project TEXT NOT NULL, jsonl_path TEXT,
                bead_id TEXT, created_at REAL NOT NULL, is_live INTEGER DEFAULT 1,
                file_offset INTEGER DEFAULT 0, last_activity REAL,
                last_message TEXT DEFAULT '', entry_count INTEGER DEFAULT 0,
                context_tokens INTEGER DEFAULT 0, label TEXT DEFAULT '',
                topics TEXT DEFAULT '[]', role TEXT DEFAULT '',
                nag_enabled INTEGER DEFAULT 0, nag_interval INTEGER DEFAULT 15,
                nag_message TEXT DEFAULT '', nag_last_sent REAL DEFAULT 0,
                dispatch_nag INTEGER DEFAULT 0
            );
        """)
        conn.execute(
            "INSERT INTO tmux_sessions (tmux_name, type, project, created_at, jsonl_path, session_uuid)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("auto-bad", "container", "autonomy", time.time(),
             "/data/agent-runs/auto-bad-123/sessions/autonomy/uuid-1/subagents/agent-sub.jsonl",
             "agent-sub"),
        )
        conn.commit()
        conn.close()

        from tools.dashboard.dao import dashboard_db as db
        db._conn = None
        db._DB_PATH = db_path
        db.init_db(db_path)

        row = db.get_session("auto-bad")
        assert row["jsonl_path"] is None
        assert row["session_uuid"] is None
        db._conn.close()
        db._conn = None

    def test_backfills_from_jsonl_path(self, tmp_path, monkeypatch):
        """Sessions with a valid jsonl_path get resolution_dir and session_uuids."""
        db_path = tmp_path / "backfill.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE tmux_sessions (
                tmux_name TEXT PRIMARY KEY, session_uuid TEXT, graph_source_id TEXT,
                type TEXT NOT NULL, project TEXT NOT NULL, jsonl_path TEXT,
                bead_id TEXT, created_at REAL NOT NULL, is_live INTEGER DEFAULT 1,
                file_offset INTEGER DEFAULT 0, last_activity REAL,
                last_message TEXT DEFAULT '', entry_count INTEGER DEFAULT 0,
                context_tokens INTEGER DEFAULT 0, label TEXT DEFAULT '',
                topics TEXT DEFAULT '[]', role TEXT DEFAULT '',
                nag_enabled INTEGER DEFAULT 0, nag_interval INTEGER DEFAULT 15,
                nag_message TEXT DEFAULT '', nag_last_sent REAL DEFAULT 0,
                dispatch_nag INTEGER DEFAULT 0
            );
        """)
        conn.execute(
            "INSERT INTO tmux_sessions (tmux_name, type, project, created_at, jsonl_path, session_uuid)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("auto-good", "container", "autonomy", time.time(),
             "/data/agent-runs/auto-good-123/sessions/autonomy/abc-def.jsonl",
             "abc-def"),
        )
        conn.commit()
        conn.close()

        from tools.dashboard.dao import dashboard_db as db
        db._conn = None
        db._DB_PATH = db_path
        db.init_db(db_path)

        row = db.get_session("auto-good")
        assert row["resolution_dir"] == "/data/agent-runs/auto-good-123/sessions/autonomy"
        assert json.loads(row["session_uuids"]) == ["abc-def"]
        assert row["curr_jsonl_file"] == "/data/agent-runs/auto-good-123/sessions/autonomy/abc-def.jsonl"
        db._conn.close()
        db._conn = None


class TestMigrationIdempotent:
    """Running init_db() twice must not fail."""

    def test_double_init(self, tmp_path):
        from tools.dashboard.dao import dashboard_db as db
        db_path = tmp_path / "idempotent.db"
        db._conn = None
        db._DB_PATH = db_path
        db.init_db(db_path)
        # Close and re-init
        db._conn.close()
        db._conn = None
        db.init_db(db_path)  # Should not raise
        # Verify columns still exist
        row = db.get_conn().execute("SELECT resolution_dir, session_uuids, curr_jsonl_file FROM tmux_sessions LIMIT 0").description
        assert len(row) == 3
        db._conn.close()
        db._conn = None
