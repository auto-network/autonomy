"""Shared isolation for every dashboard plugin test suite.

Plugin suites do not load ``tools/dashboard/tests/conftest.py``, so nothing
redirected the stores they touch: run on their own they created personal,
machine, dashboard, experiments and mission-control stores plus the
lifespan's state files in the checkout's ``data/``. In a full sweep one of
them left an enrolled identity in ``data/personal.db`` and every later
test that read the personal store saw it (auto-fus3y).

Module-scoped because the browser suites start their mock server once per
module and it must inherit the redirected environment; auto-restored so the
redirect never leaks to other suites sharing the xdist worker.
"""
from __future__ import annotations

import sys

import pytest

_SERVER_STATE = (
    ("DASHBOARD_EVENT_BUS_STATE", "event_bus.state", "EVENT_BUS_STATE_PATH"),
    ("DASHBOARD_TAIL_STATE", "tail_state.snapshot", "TAIL_STATE_PATH"),
    ("DASHBOARD_RESTART_NOTICE_STATE", "restart_notice.state", "RESTART_NOTICE_STATE_PATH"),
    ("DASHBOARD_RESOURCE_MONITOR_STATE", "resource_monitor.state", "RESOURCE_MONITOR_STATE_PATH"),
    ("DASHBOARD_WORKTREE_ROW_CACHE_STATE", "worktree_row_cache.state", "WORKTREE_ROW_CACHE_PATH"),
)


@pytest.fixture(scope="module", autouse=True)
def _plugin_suite_data_isolation(tmp_path_factory):
    from tools.data_paths import STORE_MANIFEST

    root = tmp_path_factory.mktemp("plugin-data")
    with pytest.MonkeyPatch.context() as mp:
        # The personal and machine stores root beside the orgs directory.
        (root / "orgs").mkdir()
        mp.setenv("AUTONOMY_ORGS_DIR", str(root / "orgs"))
        for store in STORE_MANIFEST:
            # The main graph store stays unpinned, as in the dashboard
            # suite: pinning it collapses org resolution to one database.
            if store.env is None or store.key == "graph":
                continue
            target = root / store.relative
            (target if store.kind == "dir" else target.parent).mkdir(parents=True, exist_ok=True)
            mp.setenv(store.env, str(target))
        server = sys.modules.get("tools.dashboard.server")
        for env, name, attr in _SERVER_STATE:
            mp.setenv(env, str(root / name))
            # Paths are fixed when the server module is imported; repoint a
            # module that is already loaded.
            if server is not None and hasattr(server, attr):
                mp.setattr(server, attr, root / name)
        if server is not None and hasattr(server, "DISPATCHER_TOKEN_FILE"):
            mp.setattr(server, "DISPATCHER_TOKEN_FILE", root / ".dispatch_token")
        # Never route graph calls to the live dashboard.
        mp.delenv("GRAPH_API", raising=False)
        mp.delenv("GRAPH_ORG", raising=False)
        yield root
