"""The personal and machine stores live outside the organization namespace.

Acceptance for auto-35kmy (design of record graph://21a0da9e-1c2, D4): an
organization slug and a local store are no longer the same kind of string.
Enumeration cannot produce a local store, the two names are refused as org
slugs on every surface (create, remove, rename), and a local store enters a
peer set only through auto-9uj7i's explicit rule.

There is NO in-process migration: moving an existing legacy file is a
one-time operator action (see DEPLOY.md, "Relocating the local stores"),
per the design's own rule that migration code is throwaway — the automated
mover shipped, declined to run on the first real boot (live store, busy
skip), and was deleted. THE LOAD-BEARING PERMANENT SURFACE IS THE RESOLVER
FALLBACK: resolution serves whichever location holds the store, so a
legacy layout keeps working today, next month, or forever — it is the only
thing standing between an unmoved file and a store that reads empty.
"""

from __future__ import annotations

import sqlite3

import pytest

from tools import data_paths
from tools.graph import cross_org, org_ops, settings_ops
from tools.graph import db as graph_db_mod
from tools.graph.db import (
    GraphDB,
    LOCAL_STORE_SLUGS,
    _local_store_db_path,
    _org_db_path,
)


@pytest.fixture
def orgs_root(tmp_path, monkeypatch):
    root = tmp_path / "orgs"
    root.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(root))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    monkeypatch.setattr(graph_db_mod, "DEFAULT_DB", tmp_path / "legacy.db")
    GraphDB.close_all_pooled()
    data_paths._LEGACY_STORE_CLASSIFICATION.clear()
    yield root
    GraphDB.close_all_pooled()


def test_enumeration_returns_only_organizations(orgs_root):
    GraphDB.create_org_db("acme").close()
    GraphDB.create_org_db("personal", type_="personal").close()
    assert _local_store_db_path("personal", orgs_root).exists()
    # Enumeration yields organizations only, even with a legacy-layout
    # file present.
    (orgs_root / "machine.db").touch()
    assert cross_org.list_org_slugs() == ["acme"]


def test_the_two_names_are_refused_on_every_organization_surface(orgs_root):
    """Create, shell-create, remove, and the rename SOURCE — the operator's
    store cannot be created as, deleted through, or renamed into the
    organization namespace, force included."""
    for name in LOCAL_STORE_SLUGS:
        with pytest.raises(org_ops.OrgError, match="reserved"):
            org_ops.create_org(name)
        with pytest.raises(org_ops.OrgError, match="reserved"):
            org_ops.create_org_shell(name)
    ref = org_ops.create_org("personal", type_="personal")
    assert ref.slug == "personal"
    assert _org_db_path("personal", orgs_root).exists()
    for name in LOCAL_STORE_SLUGS:
        with pytest.raises(org_ops.OrgError, match="local store"):
            org_ops.remove_org(name)
        with pytest.raises(org_ops.OrgError, match="local store"):
            org_ops.remove_org(name, force=True)
        with pytest.raises(org_ops.OrgError, match="local store"):
            org_ops.rename_org(name, "definitely-an-org")
    assert (orgs_root.parent / "personal.db").exists(), "store untouched"


def test_the_resolver_fallback_serves_a_legacy_layout_indefinitely(orgs_root):
    """THE permanent load-bearing surface after the mover's deletion: an
    unmoved store keeps being served from the legacy path — today, next
    month, or forever — and the new location wins the moment the operator
    performs the one-time mv. Live proof this matters: post-merge
    containers run with personal.db still at the legacy path."""
    legacy = orgs_root / "personal.db"
    GraphDB(legacy).close()  # readable, unclaimed — the legitimate shape
    assert _local_store_db_path("personal", orgs_root) == legacy
    # Peer resolution and content scans find it there too.
    assert "personal" in cross_org.all_store_slugs()
    GraphDB.create_org_db("acme").close()
    assert "personal" in cross_org.resolve_peers("acme", None)
    # The operator's one-time manual move (the DEPLOY.md runbook):
    legacy.rename(orgs_root.parent / "personal.db")
    assert _local_store_db_path("personal", orgs_root) == (
        orgs_root.parent / "personal.db"
    )
    assert "personal" in cross_org.all_store_slugs()


def test_a_shared_org_squatting_on_a_reserved_name_is_never_served(orgs_root):
    """Classification is by BOOTSTRAP ROW, never filename: a pre-reservation
    shared organization at the legacy path is not the operator's store, and
    resolution answers with the real home instead."""
    GraphDB.create_org_db(
        "personal", type_="shared", path=orgs_root / "personal.db",
    ).close()
    resolved = _local_store_db_path("personal", orgs_root)
    assert resolved == orgs_root.parent / "personal.db"


def test_a_corrupt_legacy_file_refuses_rather_than_serving(orgs_root):
    """Damage is not absence: an unreadable file is never adopted as the
    live store — resolution refuses loudly with the remedy."""
    (orgs_root / "personal.db").write_bytes(b"not a sqlite database")
    with pytest.raises(graph_db_mod.LocalStoreUnreadableError, match="restore"):
        _local_store_db_path("personal", orgs_root)


def test_graph_and_ledger_resolve_local_stores_identically_in_every_state(
    orgs_root,
):
    """Two copies that agree on the normal cases agree exactly where
    agreement is worthless. Both consumers delegate to
    tools.data_paths.resolve_local_store_path; asserted across all four
    states: fresh, unclaimed legacy, shared-org squatter, corrupt."""
    from tools.network.ledger.store import org_ledger_db_path

    def both(name):
        outcomes = []
        for resolver in (
            lambda: _local_store_db_path(name, orgs_root),
            lambda: org_ledger_db_path(name, orgs_root),
        ):
            try:
                outcomes.append(("path", resolver()))
            except Exception as exc:
                outcomes.append(("raise", type(exc).__name__))
        return outcomes

    a, b = both("personal")  # fresh
    assert a == b == ("path", orgs_root.parent / "personal.db")

    GraphDB(orgs_root / "machine.db").close()  # unclaimed legacy
    a, b = both("machine")
    assert a == b == ("path", orgs_root / "machine.db")

    data_paths._LEGACY_STORE_CLASSIFICATION.clear()
    GraphDB.create_org_db(  # shared-org squatter
        "personal", type_="shared", path=orgs_root / "personal.db",
    ).close()
    a, b = both("personal")
    assert a == b == ("path", orgs_root.parent / "personal.db")

    data_paths._LEGACY_STORE_CLASSIFICATION.clear()
    (orgs_root / "personal.db").write_bytes(b"garbage, not sqlite")  # corrupt
    a, b = both("personal")
    assert a == b == ("raise", "LocalStoreUnreadableError")


def test_local_stores_enter_a_peer_set_only_via_the_explicit_rule(
    orgs_root, monkeypatch,
):
    GraphDB.create_org_db("acme").close()
    GraphDB.create_org_db("other").close()
    GraphDB.create_org_db("personal", type_="personal").close()
    GraphDB(_local_store_db_path("machine", orgs_root)).close()

    peers = cross_org.resolve_peers("acme", None)  # absent subscription
    assert "personal" in peers and "machine" in peers and "other" in peers

    settings_ops.add_setting(
        cross_org.PEER_SUBSCRIPTION_SET_ID, 1, key="acme",
        payload={"peers": []}, org="personal", state="canonical",
    )
    pinned = cross_org.resolve_peers("acme", None)
    assert "personal" in pinned and "machine" in pinned
    assert "other" not in pinned

    monkeypatch.setattr(
        cross_org, "_with_local_stores", lambda peers, org, root=None: peers,
    )
    assert cross_org.resolve_peers("acme", None) == []


def test_bootstrap_provisions_personal_at_the_new_home(orgs_root):
    org_ops.ensure_bootstrap_orgs(root=orgs_root, personal_only=True)
    assert (orgs_root.parent / "personal.db").exists()
    assert not (orgs_root / "personal.db").exists()
