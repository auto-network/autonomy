from __future__ import annotations

import sqlite3

import pytest

from tools.dashboard.dao import trusted_git_object_store as store


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    store.init_schema_on_connection(conn)
    return conn


def _snapshot(conn, ref="snap-1", **over):
    kwargs = dict(
        snapshot_ref=ref,
        workflow_id="wf-1",
        repo_slug="autonomy",
        commit_sha="c" * 40,
        tree_sha="t" * 40,
        parent_shas=["p1" * 20, "p2" * 20],
        manifest_sha256="m" * 64,
        canonical_preview_sha256="cp" * 32,
        snapshot_type="commit_create",
        store_root="store-root-token",
        retention_class="active",
        captured_at=1700000000.0,
    )
    kwargs.update(over)
    store.insert_snapshot(conn, **kwargs)


def test_insert_and_get_round_trips_with_decoded_parents():
    conn = _conn()
    _snapshot(conn)
    got = store.get_snapshot(conn, "snap-1")
    assert got is not None
    assert got["workflow_id"] == "wf-1"
    assert got["tree_sha"] == "t" * 40
    assert got["status"] == "captured"           # default
    assert got["captured_by"] == "dashboard"     # default
    assert got["parent_shas"] == ["p1" * 20, "p2" * 20]


def test_get_missing_returns_none():
    assert store.get_snapshot(_conn(), "nope") is None


def test_entries_insert_and_list_in_position_order():
    conn = _conn()
    _snapshot(conn)
    store.add_entries(conn, "snap-1", [
        {"object_oid": "b2", "object_type": "blob", "object_size": 10, "object_path": "a/b", "object_sha256": "s2", "position": 2},
        {"object_oid": "t0", "object_type": "tree", "object_size": 40, "object_path": "", "object_sha256": "s0", "position": 0},
        {"object_oid": "b1", "object_type": "blob", "object_size": 5, "object_path": "a", "object_sha256": "s1", "position": 1},
    ])
    entries = store.list_entries(conn, "snap-1")
    assert [e["position"] for e in entries] == [0, 1, 2]
    assert [e["object_oid"] for e in entries] == ["t0", "b1", "b2"]


def test_entries_for_missing_snapshot_are_rejected_by_fk():
    conn = _conn()
    with pytest.raises(sqlite3.IntegrityError):
        store.add_entries(conn, "ghost", [
            {"object_oid": "x", "object_type": "blob", "object_size": 1, "object_path": "x", "object_sha256": "s", "position": 0},
        ])


@pytest.mark.parametrize("field,bad", [
    ("snapshot_type", "not_a_type"),
    ("status", "not_a_status"),
    ("captured_by", "attacker"),
    ("retention_class", "forever"),
])
def test_enum_check_constraints_reject_bad_values(field, bad):
    conn = _conn()
    with pytest.raises(sqlite3.IntegrityError):
        _snapshot(conn, **{field: bad})


def test_update_status_transitions_and_rejects_bad_status():
    conn = _conn()
    _snapshot(conn)
    store.update_snapshot_status(conn, "snap-1", "verified")
    assert store.get_snapshot(conn, "snap-1")["status"] == "verified"
    with pytest.raises(ValueError):
        store.update_snapshot_status(conn, "snap-1", "bogus")


def test_record_integrity_sets_latest_fields():
    conn = _conn()
    _snapshot(conn)
    store.record_integrity(conn, "snap-1", status="ok", at=1700000123.0)
    got = store.get_snapshot(conn, "snap-1")
    assert got["latest_integrity_at"] == 1700000123.0
    assert got["latest_integrity_status"] == "ok"
