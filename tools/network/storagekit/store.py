"""Content-at-rest object store — the per-organization persistence layer.

Stores each encrypted content object as its immutable body ciphertext,
addressed by the ciphertext hash under a hash-fanned blob directory
(the attachment path model), paired with its signed object-key header
in SQLite. Bodies are encrypted once and never rewritten (contract §1,
Invariant 7): a committed ``(object_id, revision_id)`` admits only a
byte-identical replay; anything else is a refused overwrite.

The store guarantees structural header validity, content-hash integrity
of the persisted pair, and counter exactness — the per-state object
counter is incremented in the SAME transaction as the object-row insert
(register CONTINUITY-SURFACE INDEX REQUIREMENTS), so warning-time
continuity reads one counter row and never scans object rows, and drift
between counter and rows is structurally impossible. Fold-based
authority, loss coverage, and secret recovery belong to the acceptance
and create/read layers.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path

from . import object_header
from .errors import (
    BodyNotFoundError,
    ContentHashMismatchError,
    ObjectNotFoundError,
    RevisionExistsError,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS content_objects (
    object_id        TEXT NOT NULL,
    revision_id      TEXT NOT NULL,
    genesis_id       TEXT NOT NULL,
    domain_id        TEXT NOT NULL,
    storage_state_id TEXT NOT NULL,
    ciphertext_hash  TEXT NOT NULL,
    header_json      BLOB NOT NULL,
    created_at       INTEGER NOT NULL,
    PRIMARY KEY (object_id, revision_id)
);
CREATE INDEX IF NOT EXISTS idx_content_objects_hash
    ON content_objects(ciphertext_hash);
CREATE INDEX IF NOT EXISTS idx_content_objects_state
    ON content_objects(storage_state_id);
CREATE TABLE IF NOT EXISTS content_bodies (
    ciphertext_hash TEXT PRIMARY KEY,
    size_bytes      INTEGER NOT NULL,
    file_path       TEXT NOT NULL UNIQUE,
    created_at      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS state_object_counts (
    storage_state_id TEXT PRIMARY KEY,
    object_count     INTEGER NOT NULL
);
"""


class ContentStore:
    """SQLite at ``root/content.db``; blobs under ``root/bodies``."""

    def __init__(self, root):
        self.root = Path(root)
        self.bodies_dir = self.root / "bodies"
        self.bodies_dir.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.root / "content.db")
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "ContentStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- write ---------------------------------------------------------------------

    def _blob_path(self, ciphertext_hash: str) -> Path:
        return self.bodies_dir / ciphertext_hash[:2] / ciphertext_hash

    def put_object(self, header, body_ciphertext: bytes) -> str:
        """Persist the pair; returns the ciphertext hash.

        Structural header verification first (nothing persisted on a
        malformed, mis-suited, or forged header), then the content
        address, then immutability: a byte-identical replay is
        idempotent, any other overwrite is refused. Body file and body
        row land before the object row, so a committed header always
        references a present body; object insert and counter increment
        share one transaction.
        """
        header = object_header.verify_structure(header)
        if not isinstance(body_ciphertext, (bytes, bytearray)):
            raise ContentHashMismatchError("body ciphertext must be bytes")
        body = bytes(body_ciphertext)
        if hashlib.sha256(body).hexdigest() != header.ciphertext_hash:
            raise ContentHashMismatchError(
                "body does not hash to the header's content address"
            )
        wire = header.to_json()
        row = self._db.execute(
            "SELECT header_json FROM content_objects WHERE object_id=? AND revision_id=?",
            (header.object_id, header.revision_id),
        ).fetchone()
        if row is not None:
            if bytes(row[0]) == wire:
                return header.ciphertext_hash  # idempotent replay
            raise RevisionExistsError(
                f"revision {header.object_id}/{header.revision_id} is committed "
                "with different bytes"
            )
        now = int(time.time())
        path = self._blob_path(header.ciphertext_hash)
        with self._db:  # one transaction: body row, object row, counter
            body_row = self._db.execute(
                "SELECT file_path FROM content_bodies WHERE ciphertext_hash=?",
                (header.ciphertext_hash,),
            ).fetchone()
            if body_row is None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(body)
                self._db.execute(
                    "INSERT INTO content_bodies VALUES (?, ?, ?, ?)",
                    (header.ciphertext_hash, len(body), str(path), now),
                )
            self._db.execute(
                "INSERT INTO content_objects VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    header.object_id,
                    header.revision_id,
                    header.genesis_id,
                    header.domain_id,
                    header.storage_state_id,
                    header.ciphertext_hash,
                    wire,
                    now,
                ),
            )
            self._db.execute(
                "INSERT INTO state_object_counts VALUES (?, 1) "
                "ON CONFLICT(storage_state_id) "
                "DO UPDATE SET object_count = object_count + 1",
                (header.storage_state_id,),
            )
        return header.ciphertext_hash

    # -- read ----------------------------------------------------------------------

    def get_object(self, object_id: str, revision_id: str) -> tuple:
        """The (header, body) pair, content-hash-verified on the way out."""
        row = self._db.execute(
            "SELECT header_json FROM content_objects WHERE object_id=? AND revision_id=?",
            (object_id, revision_id),
        ).fetchone()
        if row is None:
            raise ObjectNotFoundError(f"no object {object_id}/{revision_id}")
        header = object_header.ObjectKeyHeader.from_json(bytes(row[0]))
        body_row = self._db.execute(
            "SELECT file_path FROM content_bodies WHERE ciphertext_hash=?",
            (header.ciphertext_hash,),
        ).fetchone()
        if body_row is None:
            raise BodyNotFoundError(f"no body row for {header.ciphertext_hash}")
        path = Path(body_row[0])
        if not path.is_file():
            raise BodyNotFoundError(f"body blob missing at {path}")
        blob = path.read_bytes()
        if hashlib.sha256(blob).hexdigest() != header.ciphertext_hash:
            raise ContentHashMismatchError(
                "stored body no longer hashes to its content address"
            )
        return header, blob

    def object_count(self, storage_state_id: str) -> int:
        """One counter-row lookup — never an object-row scan."""
        row = self._db.execute(
            "SELECT object_count FROM state_object_counts WHERE storage_state_id=?",
            (storage_state_id,),
        ).fetchone()
        return int(row[0]) if row is not None else 0
