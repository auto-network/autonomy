"""The personal and machine stores live outside the organization namespace.

Acceptance for auto-35kmy (design of record graph://21a0da9e-1c2, D4): an
organization slug and a local store are no longer the same kind of string.
Enumeration cannot produce a local store, the two names are refused as org
slugs, the one-time relocation moves the files (never copies), and a local
store enters a peer set only through auto-9uj7i's explicit rule.
"""

from __future__ import annotations

import sqlite3

import pytest

from tools.graph import cross_org, org_ops, settings_ops
from tools.graph import db as graph_db_mod
from tools.graph.db import (
    GraphDB,
    LOCAL_STORE_SLUGS,
    _local_store_db_path,
    _org_db_path,
    relocate_local_stores,
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
    yield root
    GraphDB.close_all_pooled()


def test_enumeration_returns_only_organizations(orgs_root):
    GraphDB.create_org_db("acme").close()
    GraphDB.create_org_db("personal", type_="personal").close()
    assert _local_store_db_path("personal", orgs_root).exists()
    # … and enumeration still yields organizations only, even with a
    # legacy-layout file present.
    (orgs_root / "machine.db").touch()
    assert cross_org.list_org_slugs() == ["acme"]


def test_the_two_names_are_refused_as_organization_slugs(orgs_root):
    for name in LOCAL_STORE_SLUGS:
        with pytest.raises(org_ops.OrgError, match="reserved"):
            org_ops.create_org(name)
        with pytest.raises(org_ops.OrgError, match="reserved"):
            org_ops.create_org_shell(name)
    # The one legitimate reserved-name creation: provisioning the
    # operator's own store.
    ref = org_ops.create_org("personal", type_="personal")
    assert ref.slug == "personal"
    assert _org_db_path("personal", orgs_root).exists()


def test_relocation_moves_the_files_and_leaves_nothing_behind(orgs_root):
    for name in LOCAL_STORE_SLUGS:
        conn = sqlite3.connect(orgs_root / f"{name}.db")
        conn.execute("CREATE TABLE marker (v TEXT)")
        conn.execute("INSERT INTO marker VALUES (?)", (name,))
        conn.commit()
        conn.close()

    relocate_local_stores(orgs_root)

    for name in LOCAL_STORE_SLUGS:
        assert not (orgs_root / f"{name}.db").exists(), "moved, not copied"
        moved = orgs_root.parent / f"{name}.db"
        assert moved.exists()
        conn = sqlite3.connect(moved)
        try:
            assert conn.execute("SELECT v FROM marker").fetchone()[0] == name
        finally:
            conn.close()
    # Idempotent: a second run has nothing to do.
    relocate_local_stores(orgs_root)


def test_an_unrelocated_store_keeps_resolving_at_the_legacy_path(orgs_root):
    legacy = orgs_root / "personal.db"
    legacy.touch()
    assert _local_store_db_path("personal", orgs_root) == legacy
    relocate_local_stores(orgs_root)
    assert _local_store_db_path("personal", orgs_root) == (
        orgs_root.parent / "personal.db"
    )


def test_a_both_locations_conflict_is_left_alone_and_serves_the_new(orgs_root):
    (orgs_root / "personal.db").touch()
    (orgs_root.parent / "personal.db").touch()
    relocate_local_stores(orgs_root)
    assert (orgs_root / "personal.db").exists(), "conflict: nothing touched"
    assert _local_store_db_path("personal", orgs_root) == (
        orgs_root.parent / "personal.db"
    ), "resolution serves the relocated copy"


def test_local_stores_enter_a_peer_set_only_via_the_explicit_rule(
    orgs_root, monkeypatch,
):
    """Default and pinned subscriptions both resolve the local stores —
    and removing the explicit addition removes them ENTIRELY, proving the
    glob no longer smuggles them in."""
    GraphDB.create_org_db("acme").close()
    GraphDB.create_org_db("other").close()
    GraphDB.create_org_db("personal", type_="personal").close()
    GraphDB(_local_store_db_path("machine", orgs_root)).close()

    for peers in (
        cross_org.resolve_peers("acme", None),  # absent subscription
    ):
        assert "personal" in peers and "machine" in peers
        assert "other" in peers

    settings_ops.add_setting(
        cross_org.PEER_SUBSCRIPTION_SET_ID, 1, key="acme",
        payload={"peers": []}, org="personal", state="canonical",
    )
    pinned = cross_org.resolve_peers("acme", None)
    assert "personal" in pinned and "machine" in pinned
    assert "other" not in pinned

    # Remove the explicit rule: nothing else re-adds them.
    monkeypatch.setattr(
        cross_org, "_with_local_stores", lambda peers, org, root=None: peers,
    )
    assert cross_org.resolve_peers("acme", None) == []


def test_bootstrap_provisions_personal_at_the_new_home(orgs_root):
    org_ops.ensure_bootstrap_orgs(root=orgs_root, personal_only=True)
    assert (orgs_root.parent / "personal.db").exists()
    assert not (orgs_root / "personal.db").exists()


def test_bootstrap_relocates_a_legacy_layout(orgs_root):
    GraphDB.create_org_db(
        "personal", type_="personal", path=orgs_root / "personal.db",
    ).close()
    org_ops.ensure_bootstrap_orgs(root=orgs_root, personal_only=True)
    assert (orgs_root.parent / "personal.db").exists()
    assert not (orgs_root / "personal.db").exists()
    # And the relocated store still reads/writes by its literal name.
    settings_ops._open("personal", None).close()


# ── a pre-reservation SHARED org under a reserved name (Codex P1a) ───

def seed_shared_org_at(path, slug):
    GraphDB.create_org_db(slug, type_="shared", path=path).close()


def test_a_shared_org_named_personal_is_never_served_as_the_local_store(
    orgs_root,
):
    """The file was valid when created; classification is by BOOTSTRAP ROW,
    never by filename. Resolution answers with the real (fresh) home so no
    personal credential can land in the shared organization."""
    graph_db_mod._LEGACY_STORE_CLASSIFICATION.clear()
    seed_shared_org_at(orgs_root / "personal.db", "personal")
    resolved = _local_store_db_path("personal", orgs_root)
    assert resolved == orgs_root.parent / "personal.db"
    assert resolved != orgs_root / "personal.db"


def test_relocation_refuses_a_shared_org_collision_loudly(orgs_root):
    graph_db_mod._LEGACY_STORE_CLASSIFICATION.clear()
    seed_shared_org_at(orgs_root / "machine.db", "machine")
    with pytest.raises(graph_db_mod.LocalStoreCollisionError) as caught:
        relocate_local_stores(orgs_root)
    message = str(caught.value)
    assert "SHARED organization" in message
    assert "UPDATE orgs SET slug" in message, "the remedy is in the error"
    # Not migrated, not deleted: the operator renames, nothing else moves it.
    assert (orgs_root / "machine.db").exists()
    assert not (orgs_root.parent / "machine.db").exists()


def test_a_personal_typed_legacy_row_still_migrates(orgs_root):
    graph_db_mod._LEGACY_STORE_CLASSIFICATION.clear()
    GraphDB.create_org_db(
        "personal", type_="personal", path=orgs_root / "personal.db",
    ).close()
    relocate_local_stores(orgs_root)
    assert (orgs_root.parent / "personal.db").exists()
    assert not (orgs_root / "personal.db").exists()


# ── damage is not absence (Codex P1 round three) ─────────────

def test_a_corrupt_legacy_file_is_never_served_or_migrated(orgs_root):
    """'There is no data' and 'I cannot read this' are different states;
    merging them fails toward adopting the broken thing as the live store."""
    graph_db_mod._LEGACY_STORE_CLASSIFICATION.clear()
    corrupt = orgs_root / "personal.db"
    corrupt.write_bytes(b"this is not a sqlite database at all")

    with pytest.raises(graph_db_mod.LocalStoreUnreadableError):
        _local_store_db_path("personal", orgs_root)
    with pytest.raises(graph_db_mod.LocalStoreUnreadableError) as caught:
        relocate_local_stores(orgs_root)
    assert "restore" in str(caught.value).lower()
    # Untouched: not moved, not deleted, not adopted.
    assert corrupt.exists()
    assert corrupt.read_bytes().startswith(b"this is not")
    assert not (orgs_root.parent / "personal.db").exists()


def test_a_readable_rowless_file_is_still_the_unclaimed_middle_state(
    orgs_root,
):
    """The on-demand machine store is a readable database with no orgs row —
    tri-state classification must keep it valid, not lump it with damage."""
    graph_db_mod._LEGACY_STORE_CLASSIFICATION.clear()
    GraphDB(orgs_root / "machine.db").close()  # full schema, no orgs row
    assert _local_store_db_path("machine", orgs_root) == orgs_root / "machine.db"
    relocate_local_stores(orgs_root)
    assert (orgs_root.parent / "machine.db").exists()
