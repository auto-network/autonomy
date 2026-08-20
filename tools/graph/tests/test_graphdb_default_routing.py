"""``GraphDB()`` default path routes through ``resolve_caller_db_path``.

The resolver has exactly one behavior: an org (default ``personal``,
auto-txg5.3) resolves to its own database path, whether or not the file
exists yet. There is no other destination.
"""

from __future__ import annotations

import pytest

from tools.graph import db as graph_db
from tools.graph.db import GraphDB


@pytest.fixture
def orgs_root(tmp_path, monkeypatch):
    """Pin ``AUTONOMY_ORGS_DIR`` to tmp, unset ``GRAPH_DB``.

    ``GRAPH_DB`` has highest priority inside ``resolve_caller_db_path``, so
    tests that want to exercise per-org routing must clear it.
    """
    root = tmp_path / "orgs"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(root))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    return root


def test_no_args_routes_to_personal_when_per_org_db_present(orgs_root):
    # Scopeless default is ``personal`` post-txg5.3 — org=None with
    # no env must land on personal.db when that file exists.
    GraphDB.create_org_db("personal", type_="personal").close()

    db = GraphDB()
    try:
        assert db.db_path == orgs_root.parent / "personal.db"
    finally:
        db.close()


def test_no_args_resolves_to_personal_even_before_the_file_exists(orgs_root):
    # The org's own path is the answer whether or not the file exists —
    # personal materializes on demand; nothing ever resolves elsewhere.
    db = GraphDB()
    try:
        assert db.db_path == orgs_root.parent / "personal.db"
    finally:
        db.close()


def test_org_routes_to_that_org_db(orgs_root):
    GraphDB.create_org_db("anchore").close()

    db = GraphDB(org="anchore")
    try:
        assert db.db_path == orgs_root / "anchore.db"
    finally:
        db.close()


def test_explicit_db_path_wins_over_org(orgs_root, tmp_path):
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
    test-pinning pattern used across ``test_ops.py``. (org is None here, so
    the auto-23d9m conflict check never fires.)"""
    GraphDB.create_org_db("personal", type_="personal").close()
    pinned = tmp_path / "pinned.db"
    monkeypatch.setenv("GRAPH_DB", str(pinned))

    db = GraphDB()
    try:
        assert db.db_path == pinned
    finally:
        db.close()


# ── auto-23d9m: a GRAPH_DB pin must not silently discard an explicit org ──


def test_graph_db_pin_conflicting_with_explicit_org_refuses(
    orgs_root, tmp_path, monkeypatch
):
    """An ambient ``GRAPH_DB`` pin that names a DIFFERENT file than an explicit
    org's own DB must refuse loudly, not discard the org.

    This is the mechanism behind the auto-c46me double-mint: the create-org
    overwrite guard read ``org='autonomy'`` while ``GRAPH_DB`` pointed at the
    scopeless store, so it truthfully reported "no key here" — in the wrong DB.

    RED-first: before the fix, ``resolve_caller_db_path`` returned the pin and
    discarded the org, so this ``pytest.raises`` would fail.
    """
    pinned = tmp_path / "pinned.db"
    monkeypatch.setenv("GRAPH_DB", str(pinned))
    with pytest.raises(graph_db.OrgResolutionConflict):
        graph_db.resolve_caller_db_path("autonomy")


def test_graph_db_pin_matching_explicit_org_is_honored(orgs_root, monkeypatch):
    """No conflict when the pin already points at the org's own DB — the pin is
    returned, no refusal."""
    expected = orgs_root / "autonomy.db"
    monkeypatch.setenv("GRAPH_DB", str(expected))
    assert graph_db.resolve_caller_db_path("autonomy") == expected


def test_graph_db_pin_with_org_none_is_honored(orgs_root, tmp_path, monkeypatch):
    """The legitimate pin callers (tests, ``graph --db`` CLI, harness) pass
    ``org=None`` and are unaffected by the conflict check."""
    pinned = tmp_path / "pinned.db"
    monkeypatch.setenv("GRAPH_DB", str(pinned))
    assert graph_db.resolve_caller_db_path(None) == pinned
    assert graph_db.resolve_caller_db_path() == pinned
