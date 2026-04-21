"""``cli._get_db_path`` routes through ``resolve_caller_db_path``.

Regression for auto-rw1aq (auto-9iq2s.4): before this fix ``graph note``,
``graph sessions``, and every other host CLI write landed in the legacy
``data/graph.db`` because ``_get_db_path()`` hardcoded ``cwd()/data/graph.db``
(or ``DEFAULT_DB``) rather than delegating to the per-org resolver. The
auto-5fc7x ``GraphDB()`` default fix was bypassed because argparse set
``args.db = _get_db_path()`` and every ``cmd_*`` passed that explicit path
into ``GraphDB(args.db)``.

After this bead, ``_get_db_path()`` delegates to ``resolve_caller_db_path``
— identical routing to the ``ops.*`` layer and ``GraphDB()`` default.
"""

from __future__ import annotations

import pytest

from tools.graph import db as graph_db
from tools.graph.cli import _get_db_path


@pytest.fixture
def orgs_root(tmp_path, monkeypatch):
    """Pin ``AUTONOMY_ORGS_DIR`` + ``DEFAULT_DB`` to tmp, unset ``GRAPH_DB``.

    The dispatch container exports ``GRAPH_DB`` so tests that want to
    exercise per-org / legacy routing must explicitly clear it
    (pitfall auto-9iq2s). We also redirect ``DEFAULT_DB`` so the
    legacy-fallback branch never touches the real ``data/graph.db``.
    """
    root = tmp_path / "orgs"
    legacy = tmp_path / "legacy.db"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(root))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.setattr(graph_db, "DEFAULT_DB", legacy)
    return root


def test_no_env_per_org_present_returns_personal_db(orgs_root):
    # Scopeless default shifted to ``personal`` in auto-txg5.3.
    graph_db.GraphDB.create_org_db("personal", type_="personal").close()

    assert _get_db_path() == orgs_root / "personal.db"


def test_no_env_per_org_absent_falls_back_to_legacy(orgs_root):
    # No personal.db materialised — resolver falls back to DEFAULT_DB
    # (redirected to tmp by the fixture so we don't touch the real file).
    assert _get_db_path() == graph_db.DEFAULT_DB


def test_graph_db_env_wins(orgs_root, tmp_path, monkeypatch):
    graph_db.GraphDB.create_org_db("personal", type_="personal").close()
    pinned = tmp_path / "pinned.db"
    monkeypatch.setenv("GRAPH_DB", str(pinned))

    assert _get_db_path() == pinned


def test_non_default_caller_org_routes_to_that_org_db(orgs_root):
    graph_db.GraphDB.create_org_db("anchore").close()

    assert _get_db_path("anchore") == orgs_root / "anchore.db"


def test_graph_org_env_sets_caller_org(orgs_root, monkeypatch):
    """``GRAPH_ORG`` env var drives routing when no explicit caller_org."""
    graph_db.GraphDB.create_org_db("anchore").close()
    monkeypatch.setenv("GRAPH_ORG", "anchore")

    assert _get_db_path() == orgs_root / "anchore.db"


def test_explicit_caller_org_beats_graph_org_env(orgs_root, monkeypatch):
    """Explicit kwarg wins over ``GRAPH_ORG`` env — tests/API handlers
    with a concrete caller pin the destination regardless of env."""
    graph_db.GraphDB.create_org_db("anchore").close()
    graph_db.GraphDB.create_org_db("autonomy").close()
    monkeypatch.setenv("GRAPH_ORG", "anchore")

    assert _get_db_path("autonomy") == orgs_root / "autonomy.db"
