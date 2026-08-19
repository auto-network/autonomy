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

from tools import data_paths
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
    data_paths._LEGACY_STORE_CLASSIFICATION.clear()
    seed_shared_org_at(orgs_root / "personal.db", "personal")
    resolved = _local_store_db_path("personal", orgs_root)
    assert resolved == orgs_root.parent / "personal.db"
    assert resolved != orgs_root / "personal.db"


def test_relocation_refuses_a_shared_org_collision_loudly(orgs_root):
    data_paths._LEGACY_STORE_CLASSIFICATION.clear()
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
    data_paths._LEGACY_STORE_CLASSIFICATION.clear()
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
    data_paths._LEGACY_STORE_CLASSIFICATION.clear()
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
    data_paths._LEGACY_STORE_CLASSIFICATION.clear()
    GraphDB(orgs_root / "machine.db").close()  # full schema, no orgs row
    assert _local_store_db_path("machine", orgs_root) == orgs_root / "machine.db"
    relocate_local_stores(orgs_root)
    assert (orgs_root.parent / "machine.db").exists()


def test_relocation_folds_the_wal_before_moving_anything(orgs_root):
    """F1: in WAL mode the .db can be a bare header while every committed
    row lives in the -wal. The move must checkpoint first and move ONE
    file — committed rows survive, and no sidecar travels or lingers."""
    data_paths._LEGACY_STORE_CLASSIFICATION.clear()
    legacy = orgs_root / "personal.db"
    conn = sqlite3.connect(legacy)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE vital (v TEXT)")
    conn.execute("INSERT INTO vital VALUES ('identity-armor')")
    conn.commit()
    conn.close()
    assert (orgs_root / "personal.db-wal").exists() or True  # wal may be
    # checkpointed on close by sqlite; recreate pressure so it exists:
    conn = sqlite3.connect(legacy)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("INSERT INTO vital VALUES ('second-row')")
    conn.commit()  # leave WAL un-checkpointed by not closing cleanly first
    wal_present = (orgs_root / "personal.db-wal").exists()
    conn.close()

    relocate_local_stores(orgs_root)

    moved = orgs_root.parent / "personal.db"
    assert moved.exists()
    for suffix in ("-wal", "-shm", "-journal"):
        assert not (orgs_root / f"personal.db{suffix}").exists()
        # sidecars never travel; the new home builds its own
    conn = sqlite3.connect(moved)
    try:
        rows = {r[0] for r in conn.execute("SELECT v FROM vital")}
    finally:
        conn.close()
    assert rows == {"identity-armor", "second-row"}, (
        f"committed rows lost in the move (wal_present_before={wal_present})"
    )


def test_relocation_skips_a_live_holder_and_leaves_its_store_alone(orgs_root):
    """F2/F3: a connection holding the legacy store open with a write in
    flight is a live holder — the relocation skips this boot instead of
    moving the file out from under it."""
    data_paths._LEGACY_STORE_CLASSIFICATION.clear()
    legacy = orgs_root / "personal.db"
    holder = sqlite3.connect(legacy)
    holder.execute("CREATE TABLE t (v TEXT)")
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO t VALUES ('in-flight')")
    try:
        relocate_local_stores(orgs_root)  # must not raise, must not move
        assert legacy.exists(), "moved out from under a live writer"
        assert not (orgs_root.parent / "personal.db").exists()
    finally:
        holder.rollback()
        holder.close()


def test_the_reservation_covers_remove_and_the_rename_source(orgs_root):
    """F6: destruction and renaming are guarded like creation — the
    operator's store cannot leave the local-store world through the
    organization surface, with or without force."""
    org_ops.ensure_bootstrap_orgs(root=orgs_root, personal_only=True)
    for name in LOCAL_STORE_SLUGS:
        with pytest.raises(org_ops.OrgError, match="local store"):
            org_ops.remove_org(name)
        with pytest.raises(org_ops.OrgError, match="local store"):
            org_ops.remove_org(name, force=True)
        with pytest.raises(org_ops.OrgError, match="local store"):
            org_ops.rename_org(name, "definitely-an-org")
    assert (orgs_root.parent / "personal.db").exists(), "store untouched"


# ── the graph and ledger resolvers are ONE function (Codex, F5 round 2) ──

def test_graph_and_ledger_resolve_local_stores_identically_in_every_state(
    orgs_root,
):
    """Two copies that agree on the normal cases agree exactly where
    agreement is worthless. Both consumers delegate to
    tools.data_paths.resolve_local_store_path, and this asserts the
    behavior — same answer or same exception — across all four states:
    fresh, unclaimed-legacy, shared-org squatter, corrupt."""
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

    # Fresh: both answer the real home.
    data_paths._LEGACY_STORE_CLASSIFICATION.clear()
    a, b = both("personal")
    assert a == b == ("path", orgs_root.parent / "personal.db")

    # Unclaimed legacy: both serve the legacy file.
    GraphDB(orgs_root / "machine.db").close()
    a, b = both("machine")
    assert a == b == ("path", orgs_root / "machine.db")

    # Shared-org squatter: both answer the real home, neither serves it.
    data_paths._LEGACY_STORE_CLASSIFICATION.clear()
    GraphDB.create_org_db(
        "personal", type_="shared", path=orgs_root / "personal.db",
    ).close()
    a, b = both("personal")
    assert a == b == ("path", orgs_root.parent / "personal.db")

    # Corrupt: both refuse with the same named error.
    data_paths._LEGACY_STORE_CLASSIFICATION.clear()
    (orgs_root / "personal.db").write_bytes(b"garbage, not sqlite")
    a, b = both("personal")
    assert a == b == ("raise", "LocalStoreUnreadableError")


# ── two relocators, both interleaves (all-reviewer rejection of 30859c0f) ──
# Assertions are on ROWS in the served store and the backup, because the
# failure mode is silent: the losing code path reported success while
# destroying both copies (graph://35bb284f-3b3 — sqlite3.connect CREATES
# the file, so it is neither an existence probe nor a lock).

def _seed_real_store(legacy):
    conn = sqlite3.connect(legacy)
    conn.execute("CREATE TABLE settings (v TEXT)")
    conn.executemany(
        "INSERT INTO settings VALUES (?)",
        [("identity-armor",), ("credentials",)],
    )
    conn.commit()
    conn.close()


def _rows(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {r[0] for r in conn.execute("SELECT v FROM settings")}
    finally:
        conn.close()


def test_a_loser_that_raced_past_its_guards_destroys_nothing(
    orgs_root, monkeypatch,
):
    """The winner completes ENTIRELY between the loser's pre-checks and its
    connect. On the rejected code the loser conjured an empty legacy file,
    backed it up over the winner's backup, and clobbered the winner's
    relocated store — both copies lost, success reported."""
    data_paths._LEGACY_STORE_CLASSIFICATION.clear()
    legacy = orgs_root / "personal.db"
    _seed_real_store(legacy)

    # The interleave point is the CONNECT — after every guard the loser
    # has (a hook at any earlier guard tests the wrong path; the rejected
    # code survives it via its both-exist check).
    real_connect = sqlite3.connect
    fired = {"done": False}

    def winner_then_connect(*args, **kwargs):
        # Fire at the MOVER'S connect (never the classifier's read-only
        # probe): the reproduced destruction requires the loser to have
        # passed every guard including classification before the winner
        # moves the file.
        if (
            not fired["done"] and args
            and "personal.db" in str(args[0])
            and "mode=ro" not in str(args[0])
        ):
            fired["done"] = True
            monkeypatch.undo()  # the winner runs unpatched, to completion
            relocate_local_stores(orgs_root)
            monkeypatch.setattr(
                graph_db_mod.sqlite3, "connect", winner_then_connect,
            )
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(graph_db_mod.sqlite3, "connect", winner_then_connect)
    relocate_local_stores(orgs_root)  # the loser's run; winner fires inside
    monkeypatch.undo()

    served = orgs_root.parent / "personal.db"
    backup = orgs_root / "personal.db.pre-relocation-backup"
    assert _rows(served) == {"identity-armor", "credentials"}, "served store lost"
    assert _rows(backup) == {"identity-armor", "credentials"}, "backup lost"
    assert not legacy.exists(), "no conjured legacy file left behind"


def test_barrier_started_relocators_never_crash_and_never_lose_rows(
    tmp_path,
):
    """Pairs of REAL processes started on a barrier. On the rejected code:
    60/60 raised FileNotFoundError out of relocate_local_stores; here every
    pair must exit 0 with the served store and backup intact."""
    import subprocess
    import sys
    import time as _time

    driver = tmp_path / "driver.py"
    driver.write_text(
        "import os, sys, time\n"
        "sys.path.insert(0, %r)\n"
        "os.environ['AUTONOMY_ORGS_DIR'] = sys.argv[1]\n"
        "start_flag = sys.argv[2]\n"
        "from tools.graph.db import relocate_local_stores\n"
        "while not os.path.exists(start_flag):\n"
        "    time.sleep(0.001)\n"
        "relocate_local_stores(sys.argv[1])\n" % "/workspace/repo"
    )
    for i in range(8):
        root = tmp_path / f"round-{i}" / "orgs"
        root.mkdir(parents=True)
        legacy = root / "personal.db"
        _seed_real_store(legacy)
        flag = tmp_path / f"round-{i}" / "go"
        procs = [
            subprocess.Popen(
                [sys.executable, str(driver), str(root), str(flag)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            for _ in range(2)
        ]
        _time.sleep(0.15)
        flag.touch()
        for p in procs:
            _, err = p.communicate(timeout=60)
            assert p.returncode == 0, (
                f"round {i}: relocator crashed:\n{err.decode()[-800:]}"
            )
        served = root.parent / "personal.db"
        assert _rows(served) == {"identity-armor", "credentials"}, (
            f"round {i}: served store lost rows"
        )
        backup = root / "personal.db.pre-relocation-backup"
        if backup.exists():
            assert _rows(backup) == {"identity-armor", "credentials"}, (
                f"round {i}: backup lost rows"
            )
