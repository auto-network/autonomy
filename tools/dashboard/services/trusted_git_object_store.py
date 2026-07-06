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
import tempfile
from pathlib import Path


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
