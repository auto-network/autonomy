from __future__ import annotations

import sqlite3
import subprocess

from tools.dashboard.dao import trusted_git_object_store as dao
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
