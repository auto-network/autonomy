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
    """Clear live shell routing env and pin host-direct dispatch so tests
    opt into HTTP routing explicitly.

    The working Autonomy shell exports ``GRAPH_API`` / ``GRAPH_ORG`` /
    ``GRAPH_SCOPE`` for interactive use. Most graph tests are host-mode
    unit tests and expect a clean environment; inheriting those vars
    silently routes them through the live dashboard or a scoped org.

    After the API-first dispatch flip (auto-lq20j), ``get_client()``
    defaults to HttpClient even with no ``GRAPH_API`` set — so unit tests
    that drive ``cmd_*`` handlers would suddenly try to reach a real
    dashboard. Pin ``_FORCE_HOST_DIRECT = True`` here so existing host-mode
    tests behave as they always have; tests that need API routing (e.g.
    ``test_cli_api_smoke``) override the flag explicitly via the
    ``api_client`` fixture.
    """
    from tools.graph import client as _client_mod
    for name in (
        "GRAPH_API",
        "GRAPH_DB",
        "GRAPH_ORG",
        "GRAPH_SCOPE",
        "AUTONOMY_ORGS_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(_client_mod, "_FORCE_HOST_DIRECT", True)


@pytest.fixture(autouse=True)
def _evict_graph_pool():
    from tools.graph.db import GraphDB
    GraphDB.close_all_pooled()
    yield
    GraphDB.close_all_pooled()


@pytest.fixture(autouse=True)
def _isolate_schema_registry_global():
    """Snapshot + restore the process-global schema registry around every
    graph test.

    ``tools.graph.schemas.registry.SCHEMAS`` / ``UPCONVERTERS`` are mutable
    module-level dicts. Tests that register stub/variant schemas mutate them
    in place; without cleanup the extra registrations leak to later tests on
    the same xdist worker (graph or dashboard), which then resolve the wrong
    validator and fail in a shifting, hard-to-reproduce way. Restore here so
    no graph test can leave the registry dirty for the next one.
    """
    from tools.graph.schemas import registry as _reg
    schemas_snap = dict(_reg.SCHEMAS)
    upcon_snap = dict(_reg.UPCONVERTERS)
    try:
        yield
    finally:
        _reg.SCHEMAS.clear()
        _reg.SCHEMAS.update(schemas_snap)
        _reg.UPCONVERTERS.clear()
        _reg.UPCONVERTERS.update(upcon_snap)
