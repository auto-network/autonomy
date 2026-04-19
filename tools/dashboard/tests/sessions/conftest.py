"""
Mock-based fixtures for sessions page tests.

Overrides the parent conftest's test_client with one backed by DASHBOARD_MOCK,
so tests never touch real databases (no pymysql, no experiments.db).

5 test sessions exercise all card features: labels, topics (3 sessions),
roles (4 types), nag (1 session), host/container types, varying entry
counts and context tokens.
"""
import importlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from tools.dashboard.tests.fixtures import (
    MOCK_SESSION_ENTRIES,
    TEST_EXPERIMENT_ID,
    make_experiment,
    make_session,
)


def _iso(minutes_ago: int) -> str:
    """Produce an ISO-8601 UTC timestamp `minutes_ago` minutes before now.

    Recent-session tests default to `since=1d`, so fixture timestamps must
    be recent relative to wall clock to survive the filter.
    """
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


# ── 5 test sessions covering all card features ──────────────────────

SESSIONS_PAGE_SESSIONS = [
    make_session(
        "auto-test-alpha",
        label="Alpha — card redesign",
        role="designer",
        entry_count=150,
        context_tokens=80000,
        last_message="Working on card CSS",
        topics=["Redesigning session cards", "CSS grid layout"],
    ),
    make_session(
        "auto-test-beta",
        label="Beta Builder",
        role="builder",
        entry_count=200,
        context_tokens=120000,
        last_message="Compiling assets",
        topics=["Asset pipeline", "Webpack config", "Tree shaking"],
    ),
    make_session(
        "auto-test-gamma",
        label="Gamma Reviewer",
        role="reviewer",
        entry_count=75,
        context_tokens=45000,
        last_message="Reviewing PR #42",
        topics=["Code review"],
    ),
    make_session(
        "host-test-delta",
        label="Host: merge recovery",
        role="coordinator",
        type="host",
        entry_count=300,
        context_tokens=250000,
        last_message="Dolt restarted",
        topics=[],
    ),
    make_session(
        "auto-test-epsilon",
        label="Epsilon Session",
        entry_count=50,
        context_tokens=30000,
        last_message="Idle session",
        topics=[],
    ),
]

# Add nag fields to gamma (the reviewer)
SESSIONS_PAGE_SESSIONS[2]["nag_enabled"] = True
SESSIONS_PAGE_SESSIONS[2]["nag_interval"] = 10
SESSIONS_PAGE_SESSIONS[2]["nag_message"] = "Check review status"

RECENT_SESSIONS = [
    {"id": "src-aaa111222333", "type": "session", "date": "2026-03-25",
     "title": "Session alpha history", "project": "autonomy",
     "last_activity_at": _iso(10), "created_at": _iso(90),
     "entry_count": 42, "context_tokens": 12000},
    {"id": "src-bbb444555666", "type": "session", "date": "2026-03-24",
     "title": "Session beta history", "project": "autonomy",
     "last_activity_at": _iso(30), "created_at": _iso(120),
     "entry_count": 17, "context_tokens": 4000},
    {"id": "src-ccc777888999", "type": "session", "date": "2026-03-23",
     "title": "Session gamma history", "project": "default",
     "last_activity_at": _iso(120), "created_at": _iso(300),
     "entry_count": 8, "context_tokens": 900},
]


def sessions_page_fixture():
    """Build the complete fixture dict for sessions page tests."""
    entries = {s["session_id"]: MOCK_SESSION_ENTRIES for s in SESSIONS_PAGE_SESSIONS}
    return {
        "beads": [],
        "active_sessions": SESSIONS_PAGE_SESSIONS,
        "session_entries": entries,
        "recent_sessions": RECENT_SESSIONS,
        "experiments": [make_experiment(TEST_EXPERIMENT_ID)],
    }


@pytest.fixture
def mock_fixture(tmp_path):
    """Write a fixture file and set DASHBOARD_MOCK before server import."""
    fixture = sessions_page_fixture()
    path = tmp_path / "fixtures.json"
    path.write_text(json.dumps(fixture, indent=2))
    return str(path)


@pytest.fixture
def test_client(mock_fixture, monkeypatch):
    """TestClient backed by DASHBOARD_MOCK — no real DBs needed."""
    monkeypatch.setenv("DASHBOARD_MOCK", mock_fixture)

    # Reload mock DAO so it picks up the new DASHBOARD_MOCK path
    from tools.dashboard.dao import mock as mock_mod
    importlib.reload(mock_mod)

    # Reload server so conditional imports re-evaluate with DASHBOARD_MOCK set
    from tools.dashboard import server
    importlib.reload(server)

    from starlette.testclient import TestClient
    with TestClient(server.app) as client:
        yield client
