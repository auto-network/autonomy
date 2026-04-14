"""
Fixtures for resume API tests.

Sets up:
- A temporary graph.db with session sources (for source_id resolution)
- JSONL files on disk (for resumable checks)
- Mock tmux + launch_session to avoid real container/tmux calls
- TestClient backed by DASHBOARD_MOCK
"""
import importlib
import json
import sqlite3
import time

import pytest

from tools.dashboard.tests.fixtures import (
    MOCK_SESSION_ENTRIES,
    TEST_EXPERIMENT_ID,
    make_experiment,
    make_session,
)


RESUME_SESSIONS = [
    make_session("auto-test-resume", label="Resumable session", entry_count=50),
]


def _make_graph_db(db_path, sources):
    """Create a minimal graph.db with sources table."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("""CREATE TABLE IF NOT EXISTS sources (
        id TEXT PRIMARY KEY,
        type TEXT NOT NULL,
        platform TEXT,
        project TEXT,
        title TEXT,
        url TEXT,
        file_path TEXT UNIQUE,
        metadata TEXT DEFAULT '{}',
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        ingested_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    )""")
    for src in sources:
        conn.execute(
            "INSERT INTO sources (id, type, project, title, file_path, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (src["id"], src["type"], src.get("project", ""),
             src.get("title", ""), src.get("file_path", ""),
             json.dumps(src.get("metadata", {}))),
        )
    conn.commit()
    conn.close()
    return str(db_path)


@pytest.fixture
def resume_env(tmp_path):
    """Set up graph.db + JSONL files for resume tests.

    Returns a dict with paths and IDs needed by tests.
    """
    # Create a JSONL file that "exists on disk"
    jsonl_dir = tmp_path / "data" / "agent-runs" / "test-run-20260414" / "sessions" / "test-uuid"
    jsonl_dir.mkdir(parents=True)
    jsonl_file = jsonl_dir / "abc123-def456.jsonl"
    jsonl_file.write_text(json.dumps({"type": "human", "message": {"role": "user"}}) + "\n")

    # A host session JSONL (not under agent-runs)
    host_jsonl_dir = tmp_path / "claude" / "projects" / "test-proj"
    host_jsonl_dir.mkdir(parents=True)
    host_jsonl = host_jsonl_dir / "host-uuid-999.jsonl"
    host_jsonl.write_text(json.dumps({"type": "human", "message": {"role": "user"}}) + "\n")

    # Graph sources
    sources = [
        {
            "id": "src-container-session",
            "type": "session",
            "project": "autonomy",
            "title": "Container session alpha",
            "file_path": str(jsonl_file),
            "metadata": {"session_uuid": "abc123-def456"},
        },
        {
            "id": "src-host-session",
            "type": "session",
            "project": "autonomy",
            "title": "Host session beta",
            "file_path": str(host_jsonl),
            "metadata": {"session_uuid": "host-uuid-999"},
        },
        {
            "id": "src-missing-jsonl",
            "type": "session",
            "project": "autonomy",
            "title": "Session with missing JSONL",
            "file_path": "/tmp/nonexistent/gone.jsonl",
            "metadata": {"session_uuid": "missing-uuid-000"},
        },
        {
            "id": "src-not-a-session",
            "type": "conversation",
            "project": "autonomy",
            "title": "A ChatGPT conversation",
            "file_path": "",
            "metadata": {},
        },
    ]

    graph_db_path = _make_graph_db(tmp_path / "graph.db", sources)

    return {
        "graph_db": graph_db_path,
        "jsonl_file": str(jsonl_file),
        "host_jsonl": str(host_jsonl),
        "container_source_id": "src-container-session",
        "host_source_id": "src-host-session",
        "missing_source_id": "src-missing-jsonl",
        "non_session_source_id": "src-not-a-session",
        "tmp_path": tmp_path,
    }


@pytest.fixture
def mock_fixture(tmp_path, resume_env):
    """Write mock fixture and configure DASHBOARD_MOCK."""
    # Include resume-specific recent sessions with the new fields
    recent = [
        {
            "id": "src-container-session",
            "type": "session",
            "date": "2026-04-14",
            "title": "Container session alpha",
            "project": "autonomy",
            "session_uuid": "abc123-def456",
            "file_path": resume_env["jsonl_file"],
            "resumable": True,
        },
        {
            "id": "src-missing-jsonl",
            "type": "session",
            "date": "2026-04-13",
            "title": "Session with missing JSONL",
            "project": "autonomy",
            "session_uuid": "missing-uuid-000",
            "file_path": "/tmp/nonexistent/gone.jsonl",
            "resumable": False,
        },
    ]
    fixture = {
        "beads": [],
        "active_sessions": RESUME_SESSIONS,
        "session_entries": {},
        "recent_sessions": recent,
        "experiments": [make_experiment(TEST_EXPERIMENT_ID)],
    }
    path = tmp_path / "fixtures.json"
    path.write_text(json.dumps(fixture, indent=2))
    return str(path)


@pytest.fixture
def test_client(mock_fixture, resume_env, monkeypatch):
    """TestClient with mocked tmux, launch_session, and graph.db."""
    monkeypatch.setenv("DASHBOARD_MOCK", mock_fixture)

    # Reload mock DAO
    from tools.dashboard.dao import mock as mock_mod
    importlib.reload(mock_mod)

    # Patch graph.db path for source resolution in the resume endpoint
    from tools.graph import db as graph_db_mod
    original_init = graph_db_mod.GraphDB.__init__

    def patched_init(self, db_path=None, **kwargs):
        original_init(self, db_path=resume_env["graph_db"], **kwargs)

    monkeypatch.setattr(graph_db_mod.GraphDB, "__init__", patched_init)

    # Patch tmux to always succeed
    def fake_subprocess_run(cmd, **kwargs):
        class FakeResult:
            returncode = 0
            stdout = b""
            stderr = b""
        return FakeResult()

    import subprocess as sp
    original_run = sp.run

    def selective_subprocess_run(cmd, **kwargs):
        if isinstance(cmd, list) and cmd[0] == "tmux":
            return fake_subprocess_run(cmd, **kwargs)
        return original_run(cmd, **kwargs)

    monkeypatch.setattr(sp, "run", selective_subprocess_run)

    # Patch launch_session to return a mock docker command
    from agents import session_launcher

    def mock_launch_session(**kwargs):
        resume = kwargs.get("resume_uuid", "")
        resume_flag = f" --resume {resume}" if resume else ""
        return f"docker run --rm -it test-image claude --dangerously-skip-permissions{resume_flag}"

    monkeypatch.setattr(session_launcher, "launch_session", mock_launch_session)

    # Patch session_monitor.register to be a no-op
    from tools.dashboard import session_monitor as sm_mod
    async def fake_register(**kwargs):
        pass
    monkeypatch.setattr(sm_mod.session_monitor, "register", fake_register)

    # Reload server
    from tools.dashboard import server
    importlib.reload(server)

    from starlette.testclient import TestClient
    with TestClient(server.app) as client:
        yield client
