from __future__ import annotations

import sqlite3

from tools.dashboard.dao import trusted_git_object_store as dao
from tools.dashboard.services.trusted_git_object_store import (
    ContentAddressedStore,
    collect_garbage,
)


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    dao.init_schema_on_connection(conn)
    return conn


def _snap(conn, store, ref, blobs, *, expires_at, status="captured"):
    """Create a snapshot referencing each of ``blobs`` (bytes). Returns the
    stored digests."""
    entries = []
    for i, data in enumerate(blobs):
        digest = store.put(data)
        entries.append({
            "object_oid": f"{ref}-oid-{i}", "object_type": "blob",
            "object_size": len(data), "object_path": f"f{i}",
            "object_sha256": digest, "position": i,
        })
    dao.insert_snapshot(
        conn, snapshot_ref=ref, workflow_id="wf", repo_slug="r",
        commit_sha="c" * 40, tree_sha="t" * 40, parent_shas=[],
        manifest_sha256="m" * 64, canonical_preview_sha256="p" * 64,
        snapshot_type="commit_create", store_root="root",
        retention_class="active", status=status, retention_expires_at=expires_at,
    )
    dao.add_entries(conn, ref, entries)
    conn.commit()
    return [e["object_sha256"] for e in entries]


def test_gc_collects_expired_snapshot_and_deletes_its_objects(tmp_path):
    conn, store = _conn(), ContentAddressedStore(tmp_path)
    digests = _snap(conn, store, "s1", [b"a", b"b"], expires_at=100.0)
    summary = collect_garbage(dao_conn=conn, store=store, now=200.0)
    assert summary == {"snapshots_collected": 1, "objects_deleted": 2}
    assert dao.get_snapshot(conn, "s1")["status"] == "gc_deleted"
    assert all(not store.exists(d) for d in digests)


def test_gc_ignores_unexpired_and_null_expiry(tmp_path):
    conn, store = _conn(), ContentAddressedStore(tmp_path)
    future = _snap(conn, store, "future", [b"x"], expires_at=1e12)
    never = _snap(conn, store, "never", [b"y"], expires_at=None)
    summary = collect_garbage(dao_conn=conn, store=store, now=200.0)
    assert summary == {"snapshots_collected": 0, "objects_deleted": 0}
    assert store.exists(future[0]) and store.exists(never[0])
    assert dao.get_snapshot(conn, "future")["status"] == "captured"


def test_gc_keeps_object_still_shared_with_a_live_snapshot(tmp_path):
    conn, store = _conn(), ContentAddressedStore(tmp_path)
    # shared content b"shared" in both; s_expired also has a unique object
    shared = store.put(b"shared")
    _snap(conn, store, "s_expired", [b"shared", b"unique"], expires_at=100.0)
    _snap(conn, store, "s_live", [b"shared"], expires_at=1e12)  # not expired
    summary = collect_garbage(dao_conn=conn, store=store, now=200.0)
    assert summary["snapshots_collected"] == 1
    # the shared object survives (live snapshot references it); the unique one is gone
    assert store.exists(shared)
    assert summary["objects_deleted"] == 1
    assert dao.get_snapshot(conn, "s_live")["status"] == "captured"


def test_gc_deletes_shared_object_only_once_all_referencers_are_collected(tmp_path):
    conn, store = _conn(), ContentAddressedStore(tmp_path)
    shared = store.put(b"shared")
    _snap(conn, store, "s1", [b"shared"], expires_at=100.0)
    _snap(conn, store, "s2", [b"shared"], expires_at=100.0)  # both expired
    summary = collect_garbage(dao_conn=conn, store=store, now=200.0)
    assert summary["snapshots_collected"] == 2
    assert not store.exists(shared)  # no live referencer left -> deleted
    assert summary["objects_deleted"] == 1  # counted once, not per-snapshot


def test_gc_resumes_an_interrupted_sweep_and_frees_stranded_bytes(tmp_path):
    """A crash between phase 1 (mark gc_pending, commit) and phase 2 (delete
    bytes) must be recovered by the next run — not leave bytes stranded forever.
    Regression for the storage leak Sonnet found: gc_deleted rows were excluded
    from every future sweep, so an interrupted collection never finished."""
    conn, store = _conn(), ContentAddressedStore(tmp_path)
    digests = _snap(conn, store, "s1", [b"a", b"b"], expires_at=100.0)
    # Simulate a crash: phase 1 ran (snapshot committed gc_pending) but phase 2
    # never removed the bytes.
    dao.update_snapshot_status(conn, "s1", "gc_pending")
    conn.commit()
    assert all(store.exists(d) for d in digests)  # bytes still on disk after the crash
    # A later sweep (process restart) must resume and finish it.
    summary = collect_garbage(dao_conn=conn, store=store, now=200.0)
    assert summary["snapshots_collected"] == 1
    assert dao.get_snapshot(conn, "s1")["status"] == "gc_deleted"
    assert all(not store.exists(d) for d in digests)  # bytes now freed, not stranded
