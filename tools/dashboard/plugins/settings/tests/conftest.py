"""Pytest fixtures for the Settings UI plugin's L2.B sweep.

Boots a dedicated DASHBOARD_MOCK uvicorn instance via the shared
``l2b_harness`` (``tools/dashboard/test_lib/l2b_harness.py``) so this
plugin's tests stay collocated under ``tools/dashboard/plugins/settings/``
without importing from ``tools/dashboard/tests/test_behavioral_sweep.py``.
"""
from __future__ import annotations

import json
import time

import pytest

from tools.dashboard.test_lib.l2b_harness import (
    close_browser,
    open_browser,
    start_mock_server,
    stop_mock_server,
)
from tools.dashboard.tests._xdist import worker_test_port


def _build_settings_fixture() -> dict:
    """Bare-bones DASHBOARD_MOCK fixture for the Settings plugin sweep.

    The plugin tests stub ``window.fetch`` in the browser to deliver
    canned JSON for ``/api/orgs``, ``/api/graph/sets``, and the
    ``/api/graph/settings/...`` endpoints, so the server-side fixture
    is intentionally minimal — just enough to render the page shell
    and load the plugin's static assets. The dashboard.plugin#1 row
    enables the Settings plugin so navigating to ``/settings``
    succeeds.
    """
    return {
        "active_sessions": [],
        "session_entries": {},
        "recent_sessions": [],
        "worktrees": [],
        "beads": [],
        "runs": [],
        "experiments": [],
        "settings": {
            "dashboard.plugin": {
                "_all": [
                    # Settings plugin: enabled out of the box.
                    {"key": "settings", "payload": {"enabled": True}},
                    # Coordinator-board ships dormant; included so the
                    # toggle test has a row to flip.
                    {
                        "key": "coordinator-board",
                        "payload": {"enabled": False},
                    },
                ],
            },
        },
    }


@pytest.fixture(scope="module")
def sweep_server(tmp_path_factory):
    """Boot a DASHBOARD_MOCK uvicorn for the Settings plugin sweep."""
    tmpdir = tmp_path_factory.mktemp("settings_sweep")
    state = start_mock_server(
        _build_settings_fixture(),
        tmpdir,
        # Distinct base port so this sweep can run in parallel with
        # the main behavioral sweep on the same xdist worker.
        port=worker_test_port(8095),
    )
    try:
        yield state
    finally:
        stop_mock_server(state)


@pytest.fixture(scope="module")
def browser(sweep_server):
    """Open one agent-browser session for the Settings sweep."""
    open_browser(sweep_server["url"] + "/sessions")
    # Give Alpine + plugin-script load time to settle so the first
    # navigateTo('/settings') in a test hits a fully-bootstrapped SPA.
    time.sleep(1.0)
    yield sweep_server
    close_browser()
