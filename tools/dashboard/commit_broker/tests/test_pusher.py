"""Tests for the real push step — against actual local git repositories.

Proves the two things that matter: objects materialized from frozen bytes land
with the exact git identity, and the ref update is an atomic compare-and-swap
that refuses to overwrite a branch that moved since the pre-flight.
"""

from __future__ import annotations

import subprocess

import pytest

from tools.dashboard.commit_broker.pusher import (
    materialize_objects,
    push_signed_commit,
    read_remote_tip,
)

REF = "refs/heads/main"


def _git(repo, *args, stdin=None):
    return subprocess.run(["git", "-C", str(repo), *args], input=stdin, capture_output=True)


def _init(path, bare=False):
    args = ["git", "init", "-q"] + (["--bare"] if bare else []) + [str(path)]
    subprocess.run(args, capture_output=True, check=True)
    if not bare:
        _git(path, "config", "user.email", "t@example.com")
        _git(path, "config", "user.name", "Tester")
    return path


def _commit(repo, filename, content, message):
    (repo / filename).write_text(content)
    _git(repo, "add", filename)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.decode().strip()


def _tip(staging, remote):
    return read_remote_tip(repo_dir=str(staging), remote=str(remote), target_ref=REF)


def test_materialize_from_frozen_bytes_preserves_git_identity(tmp_path):
    # Make a commit in a source repo, extract its objects as raw bytes (what the
    # trusted store holds), materialize them into a FRESH repo, and confirm the
    # commit SHA git computes matches — the byte-exact property end to end.
    src = _init(tmp_path / "src")
    commit_sha = _commit(src, "a.txt", "hello\n", "first")
    tree_sha = _git(src, "rev-parse", "HEAD^{tree}").stdout.decode().strip()
    blob_sha = _git(src, "rev-parse", "HEAD:a.txt").stdout.decode().strip()

    objects = []
    for sha, gtype in [(blob_sha, "blob"), (tree_sha, "tree"), (commit_sha, "commit")]:
        objects.append((gtype, _git(src, "cat-file", gtype, sha).stdout))

    dst = _init(tmp_path / "dst")
    written = materialize_objects(repo_dir=str(dst), objects=objects)
    assert written[-1] == commit_sha  # git recomputed the same identity
    assert _git(dst, "cat-file", "-e", commit_sha).returncode == 0  # object present


def test_new_ref_push_lands_then_update_with_correct_lease_lands(tmp_path):
    remote = _init(tmp_path / "remote.git", bare=True)
    staging = _init(tmp_path / "staging")

    c1 = _commit(staging, "a.txt", "one\n", "c1")
    out = push_signed_commit(
        repo_dir=str(staging), remote=str(remote), signed_commit_sha=c1,
        target_ref=REF, expected_ref_sha=None, is_new_ref=True,
    )
    assert out.ok, out.reason
    assert _tip(staging, remote) == c1

    c2 = _commit(staging, "a.txt", "two\n", "c2")
    out = push_signed_commit(
        repo_dir=str(staging), remote=str(remote), signed_commit_sha=c2,
        target_ref=REF, expected_ref_sha=c1, is_new_ref=False,
    )
    assert out.ok, out.reason
    assert _tip(staging, remote) == c2


def test_stale_lease_is_rejected_and_remote_is_unchanged(tmp_path):
    remote = _init(tmp_path / "remote.git", bare=True)
    staging = _init(tmp_path / "staging")
    c1 = _commit(staging, "a.txt", "one\n", "c1")
    push_signed_commit(repo_dir=str(staging), remote=str(remote), signed_commit_sha=c1,
                       target_ref=REF, expected_ref_sha=None, is_new_ref=True)
    c2 = _commit(staging, "a.txt", "two\n", "c2")
    push_signed_commit(repo_dir=str(staging), remote=str(remote), signed_commit_sha=c2,
                       target_ref=REF, expected_ref_sha=c1, is_new_ref=False)
    # remote is now at c2. Try to update with a STALE expected value (c1).
    out = push_signed_commit(
        repo_dir=str(staging), remote=str(remote), signed_commit_sha=c1,
        target_ref=REF, expected_ref_sha=c1, is_new_ref=False,
    )
    assert not out.ok
    assert _tip(staging, remote) == c2  # the moved branch was NOT clobbered


def test_new_ref_collision_is_rejected(tmp_path):
    remote = _init(tmp_path / "remote.git", bare=True)
    staging = _init(tmp_path / "staging")
    c1 = _commit(staging, "a.txt", "one\n", "c1")
    push_signed_commit(repo_dir=str(staging), remote=str(remote), signed_commit_sha=c1,
                       target_ref=REF, expected_ref_sha=None, is_new_ref=True)
    # Ref now exists. A "new ref" push must be refused, not overwrite it.
    c2 = _commit(staging, "a.txt", "two\n", "c2")
    out = push_signed_commit(
        repo_dir=str(staging), remote=str(remote), signed_commit_sha=c2,
        target_ref=REF, expected_ref_sha=None, is_new_ref=True,
    )
    assert not out.ok
    assert _tip(staging, remote) == c1


def test_read_remote_tip_absent_is_none(tmp_path):
    remote = _init(tmp_path / "remote.git", bare=True)
    staging = _init(tmp_path / "staging")
    assert _tip(staging, remote) is None


def test_connector_reads_frozen_bytes_from_the_trusted_store_and_pushes(tmp_path):
    # The connector the publish step calls: it must source every byte from the
    # trusted store, not from any live repo. Prove a commit pushes when its
    # tree/blob and its (stand-in signed) bytes come entirely from the store.
    import sqlite3

    from tools.dashboard.commit_broker.pusher import build_trusted_store_pusher
    from tools.dashboard.dao import trusted_git_object_store as snapshot_dao
    from tools.dashboard.services.trusted_git_object_store import ContentAddressedStore

    src = _init(tmp_path / "src")
    commit_sha = _commit(src, "a.txt", "hello\n", "msg")
    tree_sha = _git(src, "rev-parse", "HEAD^{tree}").stdout.decode().strip()
    blob_sha = _git(src, "rev-parse", "HEAD:a.txt").stdout.decode().strip()
    commit_bytes = _git(src, "cat-file", "commit", commit_sha).stdout
    tree_bytes = _git(src, "cat-file", "tree", tree_sha).stdout
    blob_bytes = _git(src, "cat-file", "blob", blob_sha).stdout

    store = ContentAddressedStore(tmp_path / "store")
    tree_digest = store.put(tree_bytes)
    blob_digest = store.put(blob_bytes)
    signed_digest = store.put(commit_bytes)  # stand-in for the assembled signed bytes

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    snapshot_dao.init_schema_on_connection(conn)
    snapshot_dao.insert_snapshot(
        conn, snapshot_ref="snap1", workflow_id="w", repo_slug="r", commit_sha=commit_sha,
        tree_sha=tree_sha, parent_shas=[], manifest_sha256="m" * 64,
        canonical_preview_sha256=signed_digest, snapshot_type="commit_create",
        store_root="root", retention_class="active",
    )
    snapshot_dao.add_entries(conn, "snap1", [
        {"object_oid": tree_sha, "object_type": "tree", "object_size": len(tree_bytes),
         "object_path": "", "object_sha256": tree_digest, "position": 0},
        {"object_oid": blob_sha, "object_type": "blob", "object_size": len(blob_bytes),
         "object_path": "a.txt", "object_sha256": blob_digest, "position": 1},
    ])
    conn.commit()

    remote = _init(tmp_path / "remote.git", bare=True)
    push_objects = build_trusted_store_pusher(
        store=store, snapshot_dao=snapshot_dao, dao_conn=conn, snapshot_ref="snap1",
        signed_object_sha256=signed_digest, remote=str(remote),
        staging_dir=str(tmp_path / "staging"),
    )
    out = push_objects(
        signed_commit_sha=commit_sha, target_ref=REF, expected_ref_sha=None,
        is_new_ref=True, credential=None,
    )
    assert out.ok, out.reason
    assert read_remote_tip(repo_dir=str(src), remote=str(remote), target_ref=REF) == commit_sha


def test_connector_seeds_existing_ref_before_pushing_a_rewrite_commit(tmp_path):
    # A rewrite commit has a parent that already exists on the remote. The
    # staging repo starts empty, so the connector must seed it from the remote
    # before materializing the frozen bytes; otherwise git cannot traverse the
    # commit's ancestry during push.
    import sqlite3

    from tools.dashboard.commit_broker.pusher import build_trusted_store_pusher
    from tools.dashboard.dao import trusted_git_object_store as snapshot_dao
    from tools.dashboard.services.trusted_git_object_store import ContentAddressedStore

    remote = _init(tmp_path / "remote.git", bare=True)
    src = _init(tmp_path / "src")
    base_sha = _commit(src, "base.txt", "base\n", "base")
    push_signed_commit(
        repo_dir=str(src), remote=str(remote), signed_commit_sha=base_sha,
        target_ref=REF, expected_ref_sha=None, is_new_ref=True,
    )

    rewrite_sha = _commit(src, "rewrite.txt", "rewrite\n", "rewrite")
    tree_sha = _git(src, "rev-parse", "HEAD^{tree}").stdout.decode().strip()
    blob_sha = _git(src, "rev-parse", "HEAD:rewrite.txt").stdout.decode().strip()
    commit_bytes = _git(src, "cat-file", "commit", rewrite_sha).stdout
    tree_bytes = _git(src, "cat-file", "tree", tree_sha).stdout
    blob_bytes = _git(src, "cat-file", "blob", blob_sha).stdout

    store = ContentAddressedStore(tmp_path / "store")
    tree_digest = store.put(tree_bytes)
    blob_digest = store.put(blob_bytes)
    signed_digest = store.put(commit_bytes)

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    snapshot_dao.init_schema_on_connection(conn)
    snapshot_dao.insert_snapshot(
        conn, snapshot_ref="snap1", workflow_id="w", repo_slug="r", commit_sha=rewrite_sha,
        tree_sha=tree_sha, parent_shas=[base_sha], manifest_sha256="m" * 64,
        canonical_preview_sha256=signed_digest, snapshot_type="rewrite_result",
        store_root="root", retention_class="active",
    )
    snapshot_dao.add_entries(conn, "snap1", [
        {"object_oid": tree_sha, "object_type": "tree", "object_size": len(tree_bytes),
         "object_path": "", "object_sha256": tree_digest, "position": 0},
        {"object_oid": blob_sha, "object_type": "blob", "object_size": len(blob_bytes),
         "object_path": "rewrite.txt", "object_sha256": blob_digest, "position": 1},
    ])
    conn.commit()

    push_objects = build_trusted_store_pusher(
        store=store, snapshot_dao=snapshot_dao, dao_conn=conn, snapshot_ref="snap1",
        signed_object_sha256=signed_digest, remote=str(remote),
        staging_dir=str(tmp_path / "staging"),
    )
    out = push_objects(
        signed_commit_sha=rewrite_sha, target_ref=REF, expected_ref_sha=base_sha,
        is_new_ref=False, credential=None,
    )
    assert out.ok, out.reason
    assert read_remote_tip(repo_dir=str(src), remote=str(remote), target_ref=REF) == rewrite_sha


def test_chain_pusher_lands_tip_with_full_ancestry_and_single_pusher_cannot(tmp_path):
    # A 2-link rewrite chain (base -> link1 -> link2). The chain pusher must
    # materialize BOTH links so the tip pushes with its full ancestry; the
    # single-commit pusher, given only the tip, must FAIL for lack of the parent.
    import sqlite3

    from tools.dashboard.commit_broker.pusher import build_chain_pusher, build_trusted_store_pusher
    from tools.dashboard.dao import trusted_git_object_store as snapshot_dao
    from tools.dashboard.services.trusted_git_object_store import ContentAddressedStore

    src = _init(tmp_path / "src")
    base_sha = _commit(src, "a.txt", "base\n", "base")
    link1_sha = _commit(src, "a.txt", "one\n", "link1")
    link2_sha = _commit(src, "a.txt", "two\n", "link2")

    store = ContentAddressedStore(tmp_path / "store")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    snapshot_dao.init_schema_on_connection(conn)

    def _stash_link(idx, sha):
        tree = _git(src, "rev-parse", f"{sha}^{{tree}}").stdout.decode().strip()
        blob = _git(src, "rev-parse", f"{sha}:a.txt").stdout.decode().strip()
        tb, bb, cb = (_git(src, "cat-file", t, o).stdout for t, o in (("tree", tree), ("blob", blob), ("commit", sha)))
        td, bd, sd = store.put(tb), store.put(bb), store.put(cb)
        ref = f"snap-{idx}"
        snapshot_dao.insert_snapshot(
            conn, snapshot_ref=ref, workflow_id="w", repo_slug="r", commit_sha=sha, tree_sha=tree,
            parent_shas=[], manifest_sha256="m" * 64, canonical_preview_sha256=sd,
            snapshot_type="rewrite_result", store_root="root", retention_class="active",
        )
        snapshot_dao.add_entries(conn, ref, [
            {"object_oid": tree, "object_type": "tree", "object_size": len(tb), "object_path": "", "object_sha256": td, "position": 0},
            {"object_oid": blob, "object_type": "blob", "object_size": len(bb), "object_path": "a.txt", "object_sha256": bd, "position": 1},
        ])
        return (ref, sd)

    links = [_stash_link(1, link1_sha), _stash_link(2, link2_sha)]
    conn.commit()

    # remote starts at the base commit on main
    remote = _init(tmp_path / "remote.git", bare=True)
    push_signed_commit(repo_dir=str(src), remote=str(remote), signed_commit_sha=base_sha, target_ref=REF, expected_ref_sha=None, is_new_ref=True)

    # single-commit pusher with only the tip -> FAILS (missing link1 ancestry)
    single = build_trusted_store_pusher(store=store, snapshot_dao=snapshot_dao, dao_conn=conn,
                                        snapshot_ref=links[1][0], signed_object_sha256=links[1][1],
                                        remote=str(remote), staging_dir=str(tmp_path / "stage-single"))
    out_single = single(signed_commit_sha=link2_sha, target_ref=REF, expected_ref_sha=base_sha, is_new_ref=False, credential=None)
    assert not out_single.ok
    assert read_remote_tip(repo_dir=str(src), remote=str(remote), target_ref=REF) == base_sha  # unchanged

    # chain pusher with both links -> tip lands with full ancestry
    chain = build_chain_pusher(store=store, snapshot_dao=snapshot_dao, dao_conn=conn, links=links,
                               remote=str(remote), staging_dir=str(tmp_path / "stage-chain"))
    out_chain = chain(signed_commit_sha=link2_sha, target_ref=REF, expected_ref_sha=base_sha, is_new_ref=False, credential=None)
    assert out_chain.ok, out_chain.reason
    assert read_remote_tip(repo_dir=str(src), remote=str(remote), target_ref=REF) == link2_sha
