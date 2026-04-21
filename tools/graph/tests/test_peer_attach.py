"""Peer-DB attach lifecycle tests (auto-txg5.4).

Verifies the ``cross_org`` helpers that resolve peers, open their DBs
read-only via the connection pool, and the scan-order + isolation
contract. SQLite ATTACH is deliberately avoided (we merge in Python) —
so "attach" here means "open a second GraphDB handle to a peer file".

Covers:

- :func:`cross_org.list_org_slugs` — filesystem-as-truth, alphabetical.
- :func:`cross_org.resolve_peers` — default / Setting-pinned / explicit /
  ``GRAPH_DB``-pinned modes.
- :func:`cross_org.open_peer_db` — returns pooled ro instance, ``None``
  on missing slug, evicts cleanly via ``close_all_pooled``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.graph import cross_org
from tools.graph import db as graph_db_mod
from tools.graph.db import GraphDB


@pytest.fixture
def orgs_root(tmp_path, monkeypatch):
    """Isolate ``data/orgs/`` in tmp and clear GRAPH_{DB,ORG} env."""
    root = tmp_path / "orgs"
    legacy = tmp_path / "legacy.db"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(root))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    monkeypatch.setattr(graph_db_mod, "DEFAULT_DB", legacy)
    return root


@pytest.fixture(autouse=True)
def _evict_pool():
    GraphDB.close_all_pooled()
    try:
        yield
    finally:
        GraphDB.close_all_pooled()


def _seed_org(slug: str) -> None:
    GraphDB.create_org_db(slug).close()


# ── list_org_slugs ───────────────────────────────────────────


def test_list_org_slugs_empty_when_no_dir(orgs_root):
    assert cross_org.list_org_slugs() == []


def test_list_org_slugs_returns_alphabetical(orgs_root):
    _seed_org("zeta")
    _seed_org("autonomy")
    _seed_org("middle")
    assert cross_org.list_org_slugs() == ["autonomy", "middle", "zeta"]


# ── resolve_peers ────────────────────────────────────────────


def test_resolve_peers_default_is_every_sibling_db(orgs_root):
    _seed_org("autonomy")
    _seed_org("anchore")
    _seed_org("personal")

    peers = cross_org.resolve_peers("anchore", None)
    assert sorted(peers) == ["autonomy", "personal"]


def test_resolve_peers_excludes_caller(orgs_root):
    _seed_org("anchore")
    _seed_org("autonomy")

    peers = cross_org.resolve_peers("anchore", None)
    assert "anchore" not in peers
    assert peers == ["autonomy"]


def test_resolve_peers_explicit_list_wins(orgs_root):
    _seed_org("autonomy")
    _seed_org("anchore")
    _seed_org("third")

    # Explicit list trumps the default, but missing slugs are dropped
    peers = cross_org.resolve_peers("anchore", ["autonomy", "ghost"])
    assert peers == ["autonomy"]


def test_resolve_peers_empty_list_means_isolated(orgs_root):
    _seed_org("anchore")
    _seed_org("autonomy")

    # Explicit [] is distinct from None — operator opted into isolation.
    assert cross_org.resolve_peers("anchore", []) == []


def test_resolve_peers_graph_db_pinned_returns_empty(orgs_root, tmp_path, monkeypatch):
    _seed_org("autonomy")
    _seed_org("anchore")
    monkeypatch.setenv("GRAPH_DB", str(tmp_path / "pin.db"))

    assert cross_org.resolve_peers("anchore", None) == []


def test_resolve_peers_honours_subscription_setting(orgs_root):
    """``autonomy.org.peer-subscription#1`` in personal.db pins the set."""
    _seed_org("autonomy")
    _seed_org("anchore")
    _seed_org("third")
    _seed_org("personal")

    # Seed a subscription keyed on "anchore" → peers: ["autonomy"]
    from tools.graph import settings_ops
    settings_ops.add_setting(
        "autonomy.org.peer-subscription", 1,
        key="anchore",
        payload={"peers": ["autonomy"]},
        caller_org="personal",
        state="canonical",
    )

    peers = cross_org.resolve_peers("anchore", None)
    assert peers == ["autonomy"]


def test_resolve_peers_empty_subscription_means_isolated(orgs_root):
    """``peers=[]`` in the Setting = fully isolated (distinct from absence)."""
    _seed_org("autonomy")
    _seed_org("anchore")
    _seed_org("personal")

    from tools.graph import settings_ops
    settings_ops.add_setting(
        "autonomy.org.peer-subscription", 1,
        key="anchore",
        payload={"peers": []},
        caller_org="personal",
        state="canonical",
    )

    assert cross_org.resolve_peers("anchore", None) == []


def test_resolve_peers_absent_subscription_is_default(orgs_root):
    """When personal.db exists but has no subscription Setting → default."""
    _seed_org("autonomy")
    _seed_org("anchore")
    _seed_org("personal")

    peers = cross_org.resolve_peers("anchore", None)
    assert sorted(peers) == ["autonomy", "personal"]


# ── open_peer_db ─────────────────────────────────────────────


def test_open_peer_db_returns_ro_pooled(orgs_root):
    _seed_org("autonomy")
    db = cross_org.open_peer_db("autonomy")
    assert db is not None
    assert db.read_only is True
    assert ("autonomy", "ro") in GraphDB.pooled_slots()


def test_open_peer_db_missing_slug_returns_none(orgs_root):
    _seed_org("autonomy")
    assert cross_org.open_peer_db("ghost") is None


def test_open_peer_db_same_slug_returns_same_instance(orgs_root):
    _seed_org("autonomy")
    a = cross_org.open_peer_db("autonomy")
    b = cross_org.open_peer_db("autonomy")
    assert a is b


def test_close_all_pooled_evicts_peer_connections(orgs_root):
    _seed_org("autonomy")
    cross_org.open_peer_db("autonomy")
    assert ("autonomy", "ro") in GraphDB.pooled_slots()
    GraphDB.close_all_pooled()
    assert GraphDB.pooled_slots() == []
