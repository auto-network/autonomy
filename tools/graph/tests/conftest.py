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
def _evict_graph_pool():
    from tools.graph.db import GraphDB
    GraphDB.close_all_pooled()
    yield
    GraphDB.close_all_pooled()
