"""Shared fixtures for tools.graph tests.

The connection pool inside :class:`tools.graph.db.GraphDB` lives for the
process lifetime, keyed by ``(slug, mode)``. Tests that override
``AUTONOMY_ORGS_DIR`` per-test would otherwise share the previous test's
cached connection to a now-stale path. Evict before and after every test
so per-test orgs dirs are honoured.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_graph_env(monkeypatch):
    """Clear live shell routing env so tests opt into it explicitly.

    The working Autonomy shell exports ``GRAPH_API`` / ``GRAPH_ORG`` /
    ``GRAPH_SCOPE`` for interactive use. Most graph tests are host-mode
    unit tests and expect a clean environment; inheriting those vars
    silently routes them through the live dashboard or a scoped org.
    """
    for name in (
        "GRAPH_API",
        "GRAPH_DB",
        "GRAPH_ORG",
        "GRAPH_SCOPE",
        "AUTONOMY_ORGS_DIR",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _evict_graph_pool():
    from tools.graph.db import GraphDB
    GraphDB.close_all_pooled()
    yield
    GraphDB.close_all_pooled()
