"""Pytest fixtures for the Primers UI plugin's L2.B sweep (auto-9fyy0).

Boots a dedicated DASHBOARD_MOCK uvicorn instance via the shared
``l2b_harness`` (``tools/dashboard/test_lib/l2b_harness.py``) so this
plugin's tests stay collocated under ``tools/dashboard/plugins/primers/``
without importing from ``tools/dashboard/tests/test_behavioral_sweep.py``.
"""
from __future__ import annotations

import time

import pytest

from tools.dashboard.test_lib.l2b_harness import (
    close_browser,
    open_browser,
    start_mock_server,
    stop_mock_server,
)
from tools.dashboard.tests._xdist import worker_test_port


def _build_primers_fixture() -> dict:
    """Bare-bones DASHBOARD_MOCK fixture for the Primers plugin sweep.

    The plugin tests stub ``window.fetch`` in the browser to deliver
    canned JSON for ``/api/primers/workspaces`` and
    ``/api/primers/workspace/<id>``, so the server-side fixture is
    intentionally minimal — just enough for the dashboard shell to
    render and the plugin loader to register the route. The
    ``dashboard.plugin#1`` row keeps the plugin enabled.
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
                    {"key": "primers", "payload": {"enabled": True}},
                ],
            },
        },
    }


@pytest.fixture(scope="module")
def sweep_server(tmp_path_factory):
    """Boot a DASHBOARD_MOCK uvicorn for the Primers plugin sweep."""
    tmpdir = tmp_path_factory.mktemp("primers_sweep")
    state = start_mock_server(
        _build_primers_fixture(),
        tmpdir,
        # Distinct base port so this sweep can run in parallel with the
        # main behavioral sweep + Settings sweep on the same xdist worker.
        port=worker_test_port(8105),
    )
    try:
        yield state
    finally:
        stop_mock_server(state)


@pytest.fixture(scope="module")
def browser(sweep_server):
    """Open one agent-browser session for the Primers sweep."""
    open_browser(sweep_server["url"] + "/sessions")
    # Give Alpine + plugin-script load time to settle so the first
    # navigateTo('/primers') in a test hits a fully-bootstrapped SPA.
    time.sleep(1.0)
    yield sweep_server
    close_browser()
