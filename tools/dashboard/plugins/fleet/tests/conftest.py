"""Dedicated L2.B browser harness for the Fleet plugin."""
from __future__ import annotations

import os
import time

import pytest

from tools.dashboard.test_lib.l2b_harness import (
    close_browser,
    open_browser,
    start_mock_server,
    stop_mock_server,
)
from tools.dashboard.tests._xdist import worker_index, worker_test_port


@pytest.fixture(scope="module")
def sweep_server(tmp_path_factory):
    tmpdir = tmp_path_factory.mktemp("fleet_sweep")
    state = start_mock_server(
        {
            "active_sessions": [],
            "session_entries": {},
            "recent_sessions": [],
            "worktrees": [],
            "beads": [],
            "runs": [],
            "experiments": [],
            "settings": {
                "dashboard.plugin": {
                    "_all": [{"key": "fleet", "payload": {"enabled": True}}],
                },
            },
        },
        tmpdir,
        port=worker_test_port(8117),
    )
    try:
        yield state
    finally:
        stop_mock_server(state)


@pytest.fixture(scope="module")
def browser(sweep_server):
    previous = os.environ.get("AGENT_BROWSER_SESSION")
    os.environ["AGENT_BROWSER_SESSION"] = (
        f"fleet-l2b-{worker_index()}-{os.getpid()}"
    )
    open_browser(sweep_server["url"] + "/sessions")
    time.sleep(1)
    yield sweep_server
    close_browser()
    if previous is None:
        os.environ.pop("AGENT_BROWSER_SESSION", None)
    else:
        os.environ["AGENT_BROWSER_SESSION"] = previous
