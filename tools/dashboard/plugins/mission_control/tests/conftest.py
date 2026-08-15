"""Shared fixtures for the Mission Control plugin test suite.

Every test here gets its own organization directory, whatever the process it
lands in was left holding.

The dashboard's shared ``test_app`` fixture sets ``DASHBOARD_DB``,
``DASHBOARD_EVENT_BUS_STATE`` and ``AUTONOMY_ORGS_DIR`` straight onto the
environment and never puts them back, so any test that runs after one of
those in the same worker inherits a temporary directory that has since been
removed. A test here then resolves an organization against nothing and fails
for a reason that has no connection to what it was checking.

That never happens in a configuration anybody runs -- the plugin suites are
not in ``testpaths``, so they are never collected alongside the dashboard's
own tests. The protection is an accident of how the suites are invoked
rather than anything this code arranges, and it stops the moment somebody
names both paths in one command. Restoring the environment in the fixture
that dirties it is the real repair, and is tracked separately: it is used
across the dashboard suite, so proving nothing depended on the leak needs a
full run on a machine that can survive one.

This makes the plugin suite indifferent to the question. ``monkeypatch``
overrides whatever was inherited and restores after every test, so the suite
also never becomes the thing it is defending against.
"""

from __future__ import annotations

import os

import pytest

from tools.graph.db import GraphDB


@pytest.fixture(autouse=True)
def _hermetic_orgs_dir(tmp_path, monkeypatch):
    orgs = tmp_path / "orgs"
    orgs.mkdir(exist_ok=True)
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)

    # Pooled handles outlive the environment variable that decided where they
    # point, so a connection opened against the previous directory would be
    # handed back here and quietly used.
    GraphDB.close_all_pooled()
    for slug, kind in (("autonomy", "shared"), ("personal", "personal")):
        GraphDB.create_org_db(
            slug, type_=kind, path=os.path.join(str(orgs), f"{slug}.db"),
        ).close()
    GraphDB.close_all_pooled()
    try:
        yield
    finally:
        GraphDB.close_all_pooled()
