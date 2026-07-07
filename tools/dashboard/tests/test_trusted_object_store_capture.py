from __future__ import annotations

import sqlite3
import subprocess

import pytest

from tools.dashboard.dao import trusted_git_object_store as dao
from tools.dashboard.services import trusted_git_object_store as snapshot_service
from tools.dashboard.services.trusted_git_object_store import (
    ContentAddressedStore,
    capture_snapshot,
    verify_snapshot,
)


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)


def _git_out(repo, *args) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, check=True
    ).stdout.decode().strip()


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    return repo


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    dao.init_schema_on_connection(conn)
    return conn


def test_capture_records_snapshot_and_every_reachable_object(tmp_path):
    repo = _repo(tmp_path)
    (repo / "a.txt").write_text("hello\n")
    (repo / "sub").mkdir()
    (repo / "sub" / "b.txt").write_text("world\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "c1")
    tree = _git_out(repo, "rev-parse", "HEAD^{tree}")
    commit = _git_out(repo, "rev-parse", "HEAD")

    store = ContentAddressedStore(tmp_path / "store")
    conn = _conn()
    ref = capture_snapshot(
        workflow_id="wf", repo_slug="r", commit_sha=commit, tree_sha=tree,
        parent_shas=[], git_dir=repo, store=store, dao_conn=conn,
        snapshot_type="rewrite_source",
    )

    snap = dao.get_snapshot(conn, ref)
    assert snap["tree_sha"] == tree
    assert snap["status"] == "verified"
    assert snap["latest_integrity_status"] == "verified"
    assert snap["latest_integrity_at"] is not None
    entries = dao.list_entries(conn, ref)
    types = {e["object_type"] for e in entries}
    assert "tree" in types and "blob" in types  # root tree + subtree + blobs
    blobs = {e["object_path"]: store.get(e["object_sha256"]) for e in entries if e["object_type"] == "blob"}
    assert blobs["a.txt"] == b"hello\n"
    assert blobs["sub/b.txt"] == b"world\n"
    assert verify_snapshot(snapshot_ref=ref, store=store, dao_conn=conn)


def test_n3_snapshot_survives_index_advance(tmp_path):
    """Load-bearing N3 proof: after capture, STAGING a different content (a real
    index advance to a new tree — not merely a dirty worktree) must leave the
    snapshot's objects and their bytes unchanged."""
    repo = _repo(tmp_path)
    (repo / "a.txt").write_text("original\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "c1")
    tree = _git_out(repo, "rev-parse", "HEAD^{tree}")
    commit = _git_out(repo, "rev-parse", "HEAD")

    store = ContentAddressedStore(tmp_path / "store")
    conn = _conn()
    ref = capture_snapshot(
        workflow_id="wf", repo_slug="r", commit_sha=commit, tree_sha=tree,
        parent_shas=[], git_dir=repo, store=store, dao_conn=conn,
        snapshot_type="commit_create",
    )
    before = {(e["object_oid"], e["object_sha256"]) for e in dao.list_entries(conn, ref)}
    blob_digest = [e["object_sha256"] for e in dao.list_entries(conn, ref) if e["object_type"] == "blob"][0]

    # advance the index to a new tree
    (repo / "a.txt").write_text("TAMPERED\n")
    _git(repo, "add", "a.txt")
    assert _git_out(repo, "write-tree") != tree  # the index genuinely advanced

    after = {(e["object_oid"], e["object_sha256"]) for e in dao.list_entries(conn, ref)}
    assert after == before
    assert verify_snapshot(snapshot_ref=ref, store=store, dao_conn=conn)
    assert store.get(blob_digest) == b"original\n"  # the captured bytes, not the staged tamper


def test_capture_rolls_back_when_integrity_check_fails(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    (repo / "a.txt").write_text("data\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "c1")
    tree = _git_out(repo, "rev-parse", "HEAD^{tree}")

    store = ContentAddressedStore(tmp_path / "store")
    conn = _conn()
    monkeypatch.setattr(snapshot_service, "verify_snapshot", lambda **_kwargs: False)

    with pytest.raises(RuntimeError, match="integrity verification"):
        capture_snapshot(
            workflow_id="wf",
            repo_slug="r",
            commit_sha=_git_out(repo, "rev-parse", "HEAD"),
            tree_sha=tree,
            parent_shas=[],
            git_dir=repo,
            store=store,
            dao_conn=conn,
            snapshot_type="commit_create",
        )

    row = conn.execute("SELECT COUNT(*) AS n FROM trusted_git_object_snapshots").fetchone()
    assert row["n"] == 0


def test_verify_fails_when_a_captured_object_is_tampered_in_the_store(tmp_path):
    repo = _repo(tmp_path)
    (repo / "a.txt").write_text("data\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "c1")
    tree = _git_out(repo, "rev-parse", "HEAD^{tree}")

    store = ContentAddressedStore(tmp_path / "store")
    conn = _conn()
    ref = capture_snapshot(
        workflow_id="wf", repo_slug="r", commit_sha=_git_out(repo, "rev-parse", "HEAD"),
        tree_sha=tree, parent_shas=[], git_dir=repo, store=store, dao_conn=conn,
        snapshot_type="commit_create",
    )
    entry = dao.list_entries(conn, ref)[0]
    store._path_for(entry["object_sha256"]).write_bytes(b"swapped bytes")
    assert verify_snapshot(snapshot_ref=ref, store=store, dao_conn=conn) is False


def test_verify_fails_on_poisoned_canonical_preview_hash(tmp_path):
    """Codex's reproduction: swapping canonical_preview_sha256 in the metadata
    row must fail verification, not pass."""
    repo = _repo(tmp_path)
    (repo / "a.txt").write_text("x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "c1")
    tree = _git_out(repo, "rev-parse", "HEAD^{tree}")
    store = ContentAddressedStore(tmp_path / "store")
    conn = _conn()
    ref = capture_snapshot(
        workflow_id="wf", repo_slug="r", commit_sha=_git_out(repo, "rev-parse", "HEAD"),
        tree_sha=tree, parent_shas=[], git_dir=repo, store=store, dao_conn=conn,
        snapshot_type="commit_create", canonical_payload=b"preview bytes",
    )
    assert verify_snapshot(snapshot_ref=ref, store=store, dao_conn=conn) is True
    conn.execute(
        "UPDATE trusted_git_object_snapshots SET canonical_preview_sha256 = ? WHERE snapshot_ref = ?",
        ("0" * 64, ref),
    )
    conn.commit()
    assert verify_snapshot(snapshot_ref=ref, store=store, dao_conn=conn) is False


def test_verify_fails_on_tampered_entry_even_when_repointed_object_exists(tmp_path):
    """Repointing an entry's object hash to a different but present object must
    still fail verify via the manifest recompute — object presence alone isn't trust."""
    repo = _repo(tmp_path)
    (repo / "a.txt").write_text("y\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "c1")
    tree = _git_out(repo, "rev-parse", "HEAD^{tree}")
    store = ContentAddressedStore(tmp_path / "store")
    conn = _conn()
    ref = capture_snapshot(
        workflow_id="wf", repo_slug="r", commit_sha=_git_out(repo, "rev-parse", "HEAD"),
        tree_sha=tree, parent_shas=[], git_dir=repo, store=store, dao_conn=conn,
        snapshot_type="commit_create",
    )
    other = store.put(b"unrelated but present in the store")  # a valid, present object
    entry = dao.list_entries(conn, ref)[0]
    conn.execute(
        "UPDATE trusted_git_object_entries SET object_sha256 = ? WHERE snapshot_ref = ? AND object_oid = ?",
        (other, ref, entry["object_oid"]),
    )
    conn.commit()
    assert verify_snapshot(snapshot_ref=ref, store=store, dao_conn=conn) is False
