"""``GraphDB()`` default path routes through ``resolve_caller_db_path``.

Regression for auto-5fc7x: direct ``GraphDB()`` calls used to hardcode
``DEFAULT_DB = data/graph.db``, which re-created the legacy file even after
the per-org migration (auto-9iq2s) split content into ``data/orgs/*.db``.
After this bead the default is ``None``, which resolves via
``resolve_caller_db_path`` — identical routing to the ``ops.*`` layer.

Default slug shifted from ``autonomy`` → ``personal`` in auto-txg5.3
(scopeless write convergence, absorbing auto-s45z9). Tests below pin
the expected slug explicitly.
"""

from __future__ import annotations

import pytest

from tools.graph import db as graph_db
from tools.graph.db import GraphDB


@pytest.fixture
def orgs_root(tmp_path, monkeypatch):
    """Pin ``AUTONOMY_ORGS_DIR`` + ``DEFAULT_DB`` to tmp, unset ``GRAPH_DB``.

    ``GRAPH_DB`` has highest priority inside ``resolve_caller_db_path``, so
    tests that want to exercise per-org / legacy routing must clear it.
    We also redirect ``DEFAULT_DB`` so the legacy-fallback case never
    touches the real ``data/graph.db`` — the whole point of this bead is
    that callers stop writing there.
    """
    root = tmp_path / "orgs"
    legacy = tmp_path / "legacy.db"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(root))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.setattr(graph_db, "DEFAULT_DB", legacy)
    return root


def test_no_args_routes_to_personal_when_per_org_db_present(orgs_root):
    # Scopeless default is ``personal`` post-txg5.3 — caller_org=None with
    # no env must land on personal.db when that file exists.
    GraphDB.create_org_db("personal", type_="personal").close()

    db = GraphDB()
    try:
        assert db.db_path == orgs_root / "personal.db"
    finally:
        db.close()


def test_no_args_falls_back_to_legacy_when_per_org_absent(orgs_root):
    # No autonomy.db materialised — resolver falls back to DEFAULT_DB
    # (redirected to tmp by the fixture so we don't touch the real file).
    db = GraphDB()
    try:
        assert db.db_path == graph_db.DEFAULT_DB
    finally:
        db.close()


def test_caller_org_routes_to_that_org_db(orgs_root):
    GraphDB.create_org_db("anchore").close()

    db = GraphDB(caller_org="anchore")
    try:
        assert db.db_path == orgs_root / "anchore.db"
    finally:
        db.close()


def test_explicit_db_path_wins_over_caller_org(orgs_root, tmp_path):
    """An explicit positional ``db_path`` bypasses routing — required so
    test overrides and the migration script keep working unchanged."""
    GraphDB.create_org_db("autonomy").close()
    explicit = tmp_path / "explicit.db"

    db = GraphDB(explicit)
    try:
        assert db.db_path == explicit
    finally:
        db.close()


def test_graph_db_env_overrides_default_routing(orgs_root, tmp_path, monkeypatch):
    """``GRAPH_DB`` env wins even when a per-org DB exists — preserves the
    test-pinning pattern used across ``test_ops.py``."""
    GraphDB.create_org_db("personal", type_="personal").close()
    pinned = tmp_path / "pinned.db"
    monkeypatch.setenv("GRAPH_DB", str(pinned))

    db = GraphDB()
    try:
        assert db.db_path == pinned
    finally:
        db.close()
