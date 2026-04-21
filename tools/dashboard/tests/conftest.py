collect_ignore = [
    "test_agent_tool_calls.py",  # broken import: SessionState removed from session_monitor
]

"""
Shared test fixtures for dashboard functional tests.

Provides:
- mock_tmux: patches _tmux_session_exists to return True for test sessions
- mock_jsonl: creates a JSONL file with realistic Claude session entries
- test_db: creates a dashboard.db with test sessions pointing to JSONL
- test_app: boots the dashboard app against test data
- test_client: httpx AsyncClient for API tests
- browser: agent-browser helper for UI tests
"""
import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest
import httpx


# ── Read-only workspace auto-redirect ──────────────────────────────────
# Sub-session envs that mount /workspace/repo read-only still need DB
# fixtures that init_db() can open for write. Probe for write access and,
# on failure, redirect DISPATCH_DB/DASHBOARD_DB to a per-worker tmp copy.
# Writable envs short-circuit after the probe.
import os as _os
import shutil as _shutil
import tempfile as _tempfile
from pathlib import Path as _Path


def _configure_writable_dbs_if_readonly():
    repo = _Path(__file__).resolve().parents[2]
    data_dir = repo / "data"
    probe = data_dir / f".pytest-write-probe-{_os.getpid()}"
    try:
        probe.touch()
        probe.unlink()
        return
    except (OSError, PermissionError):
        pass
    worker = _os.environ.get("PYTEST_XDIST_WORKER", "master")
    tmp = _Path(_tempfile.gettempdir()) / f"pytest-dbs-{_os.getpid()}-{worker}"
    tmp.mkdir(parents=True, exist_ok=True)
    for name, env in (("dispatch.db", "DISPATCH_DB"),
                      ("dashboard.db", "DASHBOARD_DB")):
        src = data_dir / name
        dst = tmp / name
        if src.exists() and not dst.exists():
            _shutil.copy(src, dst)
        _os.environ.setdefault(env, str(dst))


_configure_writable_dbs_if_readonly()


# ── JSONL Fixture ──────────────────────────────────────────────────────

MOCK_ENTRIES = [
    {
        "type": "human",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": "Hello, can you help me with the session cards?"}]
        },
        "timestamp": "2026-03-24T12:00:00Z"
    },
    {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Sure! Let me look at the session card code."},
                {
                    "type": "tool_use",
                    "id": "tu_001",
                    "name": "Read",
                    "input": {"file_path": "/workspace/repo/tools/dashboard/templates/pages/sessions.html"}
                }
            ]
        },
        "timestamp": "2026-03-24T12:00:05Z"
    },
    {
        "type": "human",
        "message": {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": "tu_001",
                "content": "<!-- sessions template -->\n<div x-data=\"sessionsPage()\">\n  <h2>Active Sessions</h2>\n</div>"
            }]
        },
        "timestamp": "2026-03-24T12:00:06Z"
    },
    {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "I can see the sessions template. The cards use Alpine.js with the sessionsPage() component."}]
        },
        "timestamp": "2026-03-24T12:00:10Z"
    },
]

MOCK_SESSIONS = [
    {
        "tmux_name": "auto-test-designer",
        "type": "container",
        "project": "autonomy",
        "label": "Test Designer — card redesign",
        "role": "designer",
        "last_message": "Sure! Let me look at the session card code.",
        "entry_count": 4,
        "context_tokens": 250000,
    },
    {
        "tmux_name": "auto-test-validator",
        "type": "container",
        "project": "autonomy",
        "label": "Test Validator",
        "role": "reviewer",
        "last_message": "auto-f4p4 validated PASS. All 8 gaps verified.",
        "entry_count": 200,
        "context_tokens": 100000,
    },
    {
        "tmux_name": "auto-test-coordinator",
        "type": "container",
        "project": "autonomy",
        "label": "Session Coordinator",
        "role": "coordinator",
        "last_message": "Fleet status: 4 active sessions, all working.",
        "entry_count": 1500,
        "context_tokens": 300000,
    },
    {
        "tmux_name": "host-test-host",
        "type": "host",
        "project": "autonomy",
        "label": "Host: merge recovery",
        "role": "",
        "last_message": "Dolt server restarted.",
        "entry_count": 50,
        "context_tokens": 30000,
    },
    {
        "tmux_name": "chatwith-should-be-hidden",
        "type": "container",
        "project": "autonomy",
        "label": "",
        "role": "",
        "last_message": "orphan chatwith session",
        "entry_count": 5,
        "context_tokens": 10000,
    },
]


@pytest.fixture
def mock_jsonl(tmp_path):
    """Create a JSONL file with realistic Claude session entries."""
    jsonl_path = tmp_path / "sessions" / "test-uuid" / "test.jsonl"
    jsonl_path.parent.mkdir(parents=True)
    with open(jsonl_path, "w") as f:
        for entry in MOCK_ENTRIES:
            f.write(json.dumps(entry) + "\n")
    return str(jsonl_path)


@pytest.fixture
def test_db(tmp_path, mock_jsonl):
    """Create a dashboard.db with test sessions."""
    db_path = tmp_path / "dashboard.db"
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
    now = time.time()
    for s in MOCK_SESSIONS:
        jsonl = mock_jsonl if s["tmux_name"] == "auto-test-designer" else None
        conn.execute(
            """INSERT INTO tmux_sessions
            (tmux_name, type, project, created_at, is_live, last_message,
             entry_count, context_tokens, label, role, jsonl_path, session_uuid, last_activity)
            VALUES (?,?,?,?,1,?,?,?,?,?,?,?,?)""",
            (s["tmux_name"], s["type"], s["project"], now, s["last_message"],
             s["entry_count"], s["context_tokens"], s["label"], s["role"],
             jsonl, f"uuid-{s['tmux_name']}", now)
        )
    conn.commit()
    conn.close()
    return str(db_path)


@pytest.fixture
def mock_tmux():
    """Patch tmux session existence check so test sessions stay alive."""
    test_sessions = {s["tmux_name"] for s in MOCK_SESSIONS}

    def fake_check_tmux(name):
        return name in test_sessions

    with patch("tools.dashboard.session_monitor.SessionMonitor._check_tmux", staticmethod(fake_check_tmux)):
        yield


@pytest.fixture
def shipped_settings_orgs(tmp_path_factory, monkeypatch):
    """Populate shipped-workspace Settings into a tmp orgs/ dir.

    Dashboard API handlers that render the workspace registry read from
    ``autonomy.workspace#1`` + ``autonomy.org#1`` Settings; tests need
    those populated so ``load_workspaces`` returns the same registry the
    live dashboard shows. Tests opt in by taking this fixture; ``test_app``
    alone doesn't trigger population so tests with their own ``orgs_root``
    stay isolated.
    """
    from tools.graph.db import GraphDB
    from agents.tests.conftest import (
        SHIPPED_PROJECTS_YAML,
        populate_workspaces_from_yaml,
    )

    orgs_dir = tmp_path_factory.mktemp("orgs")
    GraphDB.close_all_pooled()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    populate_workspaces_from_yaml(SHIPPED_PROJECTS_YAML, orgs_dir)
    try:
        yield orgs_dir
    finally:
        GraphDB.close_all_pooled()


@pytest.fixture
def test_app(test_db, mock_tmux):
    """Boot the dashboard app against test data with tmux mocked."""
    os.environ["DASHBOARD_DB"] = test_db
    # Reload DAO to pick up new DB path
    import importlib
    from tools.dashboard.dao import dashboard_db as db_mod
    importlib.reload(db_mod)
    # Must reload server after DAO to pick up new connection
    from tools.dashboard import server
    importlib.reload(server)
    return server.app


@pytest.fixture
def test_client(test_app):
    """Sync HTTP client for API testing."""
    from starlette.testclient import TestClient
    with TestClient(test_app) as client:
        yield client


# ── Agent Browser Helper ──────────────────────────────────────────────

class BrowserHelper:
    """Wrapper around agent-browser CLI for UI testing."""

    def __init__(self, base_url):
        self.base_url = base_url
        self._started = False

    def _run(self, *args, timeout=10):
        result = subprocess.run(
            ["agent-browser"] + list(args),
            capture_output=True, text=True, timeout=timeout
        )
        return result.stdout + result.stderr

    def open(self, path):
        url = f"{self.base_url}{path}"
        self._run("open", url, "--ignore-https-errors")
        self._started = True
        return self

    def set_viewport(self, width=390, height=844):
        self._run("set", "viewport", str(width), str(height))
        return self

    def screenshot(self, annotate=False):
        args = ["screenshot"]
        if annotate:
            args.append("--annotate")
        output = self._run(*args)
        # Extract path from output
        for line in output.split("\n"):
            if "/tmp/screenshots/" in line:
                path = line.split("/tmp/screenshots/")[1].split()[0]
                return f"/tmp/screenshots/{path}"
        return None

    def snapshot(self):
        return self._run("snapshot", "-i")

    def click(self, ref):
        return self._run("click", ref)

    def eval_js(self, js):
        return self._run("eval", js)

    def close(self):
        if self._started:
            self._run("close")
            self._started = False


@pytest.fixture
def browser():
    """Agent-browser instance. Tests must start their own server."""
    b = BrowserHelper("http://localhost:8082")
    yield b
    b.close()
