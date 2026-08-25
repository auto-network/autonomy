"""Content-at-rest, kept inside the database the secret is scoped to.

A ``@vaulted`` secret is sealed by the SAME code an organization uses
(``storage_object.seal_revision`` / ``open_revision``): the plaintext becomes
an immutable content object and the settings row keeps a locator. The only
thing that differs from the org path is WHERE the sealed body lands. The
original :class:`storagekit.store.ContentStore` puts it in its own
``content.db`` plus a ``bodies/`` directory — a second database beside the one
the row lives in. That is wrong for a secret: a personal secret must ride
``personal.db`` to every machine on the fleet, and a locator that points at a
body in a separate file arrives at a fresh machine pointing at nothing.

:class:`DbContentStore` is a drop-in for the injected content store that keeps
the sealed body in the SAME SQLite database as the settings row — personal
secrets in ``personal.db``, an organization's in that organization's database.
Nothing new is created on disk. It implements exactly the three methods the
seal and open paths call — ``put_object``, ``get_object``, ``object_count`` —
with the same immutability, content-hash and counter guarantees as the
original, so ``seal_revision`` and ``open_revision`` are reused byte for byte
and never learn what backs them.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path

from tools.network.fleet_sync_connection import FleetSyncConnection
from tools.network.storagekit import object_header
from tools.network.storagekit.errors import (
    BodyNotFoundError,
    ContentHashMismatchError,
    ObjectNotFoundError,
    RevisionExistsError,
)

#: Tables live in the scoped database itself, prefixed so they never collide
#: with the settings schema already there. The body is a BLOB column, not a
#: file path — there is no bodies directory, because there is no second place.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS vault_content_objects (
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
CREATE INDEX IF NOT EXISTS idx_vault_content_objects_hash
    ON vault_content_objects(ciphertext_hash);
CREATE INDEX IF NOT EXISTS idx_vault_content_objects_state
    ON vault_content_objects(storage_state_id);
CREATE TABLE IF NOT EXISTS vault_content_bodies (
    ciphertext_hash TEXT PRIMARY KEY,
    size_bytes      INTEGER NOT NULL,
    body            BLOB NOT NULL,
    created_at      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS vault_state_object_counts (
    storage_state_id TEXT PRIMARY KEY,
    object_count     INTEGER NOT NULL
);
"""


def vault_db_path_for(org: "str | None", *, root=None):
    """The database file a vault set scoped to *org* uses.

    This is the ROUTING invariant. A vault set's ciphertext and key-control
    records must land in the SAME database file that ``settings_ops`` writes
    the setting row to — never anywhere else. Personal (``org is None``)
    resolves to the personal store; an organization resolves to that
    organization's database. By deriving the path from the exact resolver the
    row uses (``resolve_caller_db_path``) rather than a path chosen here, an
    org-rooted secret CANNOT be written into ``personal.db`` and a personal
    secret cannot be written into an org database: one resolver, one answer,
    for the row and its sealed content alike.
    """
    from tools.graph.db import resolve_caller_db_path

    return resolve_caller_db_path(org, root=root)


class DbContentStore:
    """The injected content store, backed by the scoped database.

    Constructed from a path to the scoped SQLite database (``personal.db`` or
    an organization's) — the same file the settings row is written to. The
    content tables are created on demand in that database; the sealed body is a
    BLOB inside it. Opening the same database again on a second machine (fleet
    sync copies the file) finds the body already present.
    """

    def __init__(self, db_path: "str | Path"):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(
            self.db_path, factory=FleetSyncConnection
        )
        self._db.executescript(_SCHEMA)
        from tools.network.fleet_sync.catalog import (
            attach_active_production_catalog,
        )
        self._fleet_catalog = attach_active_production_catalog(self._db)

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "DbContentStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- write ---------------------------------------------------------------

    def put_object(self, header, body_ciphertext: bytes) -> str:
        """Persist the (header, body) pair; return the ciphertext hash.

        Structural header verification first, then the content address, then
        immutability: a byte-identical replay is idempotent, any other
        overwrite of a committed revision is refused. Body row, object row and
        counter increment share one transaction, so a committed header always
        references a present body.
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
            "SELECT header_json FROM vault_content_objects "
            "WHERE object_id=? AND revision_id=?",
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
        with self._db:  # one transaction: body row, object row, counter
            body_row = self._db.execute(
                "SELECT size_bytes FROM vault_content_bodies WHERE ciphertext_hash=?",
                (header.ciphertext_hash,),
            ).fetchone()
            if body_row is None:
                self._db.execute(
                    "INSERT INTO vault_content_bodies VALUES (?, ?, ?, ?)",
                    (header.ciphertext_hash, len(body), body, now),
                )
            self._db.execute(
                "INSERT INTO vault_content_objects VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
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
                "INSERT INTO vault_state_object_counts VALUES (?, 1) "
                "ON CONFLICT(storage_state_id) "
                "DO UPDATE SET object_count = object_count + 1",
                (header.storage_state_id,),
            )
        return header.ciphertext_hash

    # -- read ----------------------------------------------------------------

    def get_object(self, object_id: str, revision_id: str) -> tuple:
        """The (header, body) pair, content-hash-verified on the way out."""
        row = self._db.execute(
            "SELECT header_json FROM vault_content_objects "
            "WHERE object_id=? AND revision_id=?",
            (object_id, revision_id),
        ).fetchone()
        if row is None:
            raise ObjectNotFoundError(f"no object {object_id}/{revision_id}")
        header = object_header.ObjectKeyHeader.from_json(bytes(row[0]))
        body_row = self._db.execute(
            "SELECT body FROM vault_content_bodies WHERE ciphertext_hash=?",
            (header.ciphertext_hash,),
        ).fetchone()
        if body_row is None:
            raise BodyNotFoundError(f"no body row for {header.ciphertext_hash}")
        blob = bytes(body_row[0])
        if hashlib.sha256(blob).hexdigest() != header.ciphertext_hash:
            raise ContentHashMismatchError(
                "stored body no longer hashes to its content address"
            )
        return header, blob

    def object_count(self, storage_state_id: str) -> int:
        """One counter-row lookup — never an object-row scan."""
        row = self._db.execute(
            "SELECT object_count FROM vault_state_object_counts "
            "WHERE storage_state_id=?",
            (storage_state_id,),
        ).fetchone()
        return int(row[0]) if row is not None else 0
