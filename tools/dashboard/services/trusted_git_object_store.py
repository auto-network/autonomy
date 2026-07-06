"""Trusted git object store — capture / verification / GC service (DN2).

Host-owned. The agent container never gets a direct filesystem path into this
store; the ``snapshot_ref`` / ``store_root`` that appear in workflow rows are
opaque, host-resolved tokens, not paths the agent can browse.

Object bytes are content-addressed by SHA-256. Identical content dedupes to one
file, and every read re-hashes the bytes and rejects any that don't match the
requested digest — so on-disk tampering can't silently change what later gets
assembled and signed. This is the physical half of the N3 anti-tamper boundary;
the metadata half lives in ``dao.trusted_git_object_store``.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import uuid
from pathlib import Path

from tools.dashboard.dao import trusted_git_object_store as snapshot_dao


class ObjectIntegrityError(Exception):
    """Raised when stored bytes do not hash to the digest they were filed under."""


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ContentAddressedStore:
    """A SHA-256 content-addressed byte store rooted at a host directory.

    Layout: ``<root>/<digest[:2]>/<digest[2:]>``. Writes are atomic (temp file +
    rename) so a crashed capture never leaves a partial object readable under a
    valid-looking name.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def _path_for(self, digest: str) -> Path:
        return self.root / digest[:2] / digest[2:]

    def put(self, data: bytes) -> str:
        """Store ``data``, returning its SHA-256 digest. Idempotent: storing the
        same bytes again is a no-op and returns the same digest."""
        digest = sha256_hex(data)
        path = self._path_for(digest)
        if path.exists():
            return digest
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(tmp_name, path)  # atomic within the same directory
        except BaseException:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise
        return digest

    def get(self, digest: str) -> bytes:
        """Return the bytes stored under ``digest``, re-verifying their hash.

        Raises ``KeyError`` if absent, ``ObjectIntegrityError`` if the on-disk
        bytes have been tampered with (hash no longer matches the digest)."""
        path = self._path_for(digest)
        if not path.exists():
            raise KeyError(f"object {digest} not in trusted store")
        data = path.read_bytes()
        actual = sha256_hex(data)
        if actual != digest:
            raise ObjectIntegrityError(
                f"trusted-store tamper: object filed under {digest} now hashes to {actual}"
            )
        return data

    def exists(self, digest: str) -> bool:
        return self._path_for(digest).exists()

    def verify(self, digest: str) -> bool:
        """True iff the object is present and its bytes still hash to ``digest``."""
        try:
            self.get(digest)
        except (KeyError, ObjectIntegrityError):
            return False
        return True


# ── snapshot capture (the N3 freeze) ─────────────────────────────────


def _git_bytes(git_dir: Path | str, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(git_dir), *args], capture_output=True, check=True
    ).stdout


def _git_text(git_dir: Path | str, *args: str) -> str:
    return _git_bytes(git_dir, *args).decode()


def _enumerate_tree_objects(git_dir: Path | str, tree_sha: str) -> list[tuple[str, str, int | None, str]]:
    """Return (oid, type, size|None, path) for the root tree plus every reachable
    subtree and blob. The root tree isn't listed by ls-tree (which lists entries
    *under* it), so it's prepended explicitly."""
    objects: list[tuple[str, str, int | None, str]] = [(tree_sha, "tree", None, "")]
    out = _git_text(git_dir, "ls-tree", "-r", "-t", "-l", tree_sha)
    for line in out.splitlines():
        meta, path = line.split("\t", 1)
        _mode, otype, oid, size = meta.split()
        objects.append((oid, otype, None if size == "-" else int(size), path))
    return objects


def capture_snapshot(
    *,
    workflow_id: str,
    repo_slug: str,
    commit_sha: str,
    tree_sha: str,
    parent_shas,
    git_dir: Path | str,
    store: ContentAddressedStore,
    dao_conn,
    snapshot_type: str,
    retention_class: str = "active",
    store_root: str = "default",
    canonical_payload: bytes = b"",
    snapshot_ref: str | None = None,
    captured_by: str = "dashboard",
    retention_expires_at: float | None = None,
) -> str:
    """Freeze the objects reachable from ``tree_sha`` into the trusted store and
    record the snapshot metadata. Returns the ``snapshot_ref``.

    This is the N3 anti-tamper freeze: object bytes are read as they exist *now*
    and copied into the content store. ``tree_sha`` pins the content — any later
    worktree or index change produces a different tree and cannot alter what this
    snapshot captured.
    """
    snapshot_ref = snapshot_ref or f"snap-{uuid.uuid4().hex}"
    objects = _enumerate_tree_objects(git_dir, tree_sha)
    entries = []
    manifest_lines = []
    for position, (oid, otype, size, path) in enumerate(objects):
        content = _git_bytes(git_dir, "cat-file", otype, oid)
        object_sha256 = store.put(content)
        entries.append(
            {
                "object_oid": oid,
                "object_type": otype,
                "object_size": len(content) if size is None else size,
                "object_path": path,
                "object_sha256": object_sha256,
                "position": position,
            }
        )
        manifest_lines.append(f"{position} {oid} {otype} {object_sha256}")
    manifest_sha256 = sha256_hex("\n".join(manifest_lines).encode())
    snapshot_dao.insert_snapshot(
        dao_conn,
        snapshot_ref=snapshot_ref,
        workflow_id=workflow_id,
        repo_slug=repo_slug,
        commit_sha=commit_sha,
        tree_sha=tree_sha,
        parent_shas=parent_shas,
        manifest_sha256=manifest_sha256,
        canonical_preview_sha256=sha256_hex(canonical_payload),
        snapshot_type=snapshot_type,
        store_root=store_root,
        retention_class=retention_class,
        captured_by=captured_by,
        retention_expires_at=retention_expires_at,
    )
    snapshot_dao.add_entries(dao_conn, snapshot_ref, entries)
    dao_conn.commit()
    return snapshot_ref


def verify_snapshot(*, snapshot_ref: str, store: ContentAddressedStore, dao_conn) -> bool:
    """True iff every captured object is present in the store and un-tampered."""
    entries = snapshot_dao.list_entries(dao_conn, snapshot_ref)
    if not entries:
        return False
    return all(store.verify(entry["object_sha256"]) for entry in entries)
