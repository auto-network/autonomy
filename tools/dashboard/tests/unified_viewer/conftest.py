"""
Mock-based fixtures for unified_viewer browser tests.

Overrides the parent conftest's test_client with one backed by DASHBOARD_MOCK,
so tests never touch real databases (no pymysql, no experiments.db).
Follows the same pattern as session_picker/conftest.py.
"""
import importlib
import json
import os

import pytest

from tools.dashboard.tests.fixtures import (
    MOCK_SESSION_ENTRIES,
    STANDARD_SESSIONS,
    TEST_EXPERIMENT_ID,
    make_experiment,
    make_linked_session,
    make_unresolved_session,
    make_dead_session,
)


@pytest.fixture
def mock_fixture(tmp_path):
    """Write a fixture file and set DASHBOARD_MOCK before server import."""
    fixture = {
        "beads": [],
        "active_sessions": STANDARD_SESSIONS,
        "session_entries": {
            s["session_id"]: MOCK_SESSION_ENTRIES for s in STANDARD_SESSIONS
        },
        "experiments": [make_experiment(TEST_EXPERIMENT_ID)],
    }
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
