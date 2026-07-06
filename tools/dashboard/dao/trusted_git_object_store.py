"""Trusted git object store metadata DAO.

This stores snapshot lifecycle metadata for the host-owned trusted object
store used by commit creation, signing, attach, and governed rewrite flows.
The actual git object bytes live in the host-owned filesystem store; this
module only manages lookup, integrity, and retention metadata.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
DB_PATH = Path(
    os.environ.get("TRUSTED_GIT_OBJECT_STORE_DB", str(REPO_ROOT / "data" / "trusted_git_object_store.db"))
)

CREATE_TABLES = """\
CREATE TABLE IF NOT EXISTS trusted_git_object_snapshots (
    snapshot_ref              TEXT PRIMARY KEY,
    workflow_id               TEXT NOT NULL,
    repo_slug                 TEXT NOT NULL,
    commit_sha                TEXT NOT NULL,
    tree_sha                  TEXT NOT NULL,
    parent_shas_json          TEXT NOT NULL DEFAULT '[]',
    manifest_sha256           TEXT NOT NULL,
    canonical_preview_sha256  TEXT NOT NULL,
    snapshot_type             TEXT NOT NULL CHECK (
        snapshot_type IN ('commit_create', 'rewrite_source', 'rewrite_result')
    ),
    status                    TEXT NOT NULL CHECK (
        status IN ('captured', 'verified', 'gc_pending', 'released', 'gc_deleted')
    ),
    captured_at               REAL NOT NULL,
    captured_by               TEXT NOT NULL CHECK (
        captured_by IN ('dashboard', 'reconciler', 'broker')
    ),
    store_root                TEXT NOT NULL,
    latest_integrity_at       REAL,
    latest_integrity_status   TEXT,
    retention_class           TEXT NOT NULL CHECK (
        retention_class IN ('active', 'signing_pending', 'published', 'terminal')
    ),
    retention_expires_at      REAL
);

CREATE TABLE IF NOT EXISTS trusted_git_object_entries (
    snapshot_ref              TEXT NOT NULL,
    object_oid                TEXT NOT NULL,
    object_type               TEXT NOT NULL CHECK (
        object_type IN ('blob', 'tree', 'commit', 'tag')
    ),
    object_size               INTEGER NOT NULL,
    object_path               TEXT NOT NULL,
    object_sha256             TEXT NOT NULL,
    position                  INTEGER NOT NULL,
    PRIMARY KEY (snapshot_ref, object_oid),
    FOREIGN KEY(snapshot_ref) REFERENCES trusted_git_object_snapshots(snapshot_ref)
);
"""


def _db_path(db_path: Path | str | None = None) -> Path:
    return Path(db_path) if db_path is not None else DB_PATH


def _get_conn(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = _db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema_on_connection(conn: sqlite3.Connection) -> None:
    conn.executescript(CREATE_TABLES)


def init_db(db_path: Path | str | None = None) -> None:
    conn = _get_conn(db_path)
    try:
        init_schema_on_connection(conn)
        conn.commit()
    finally:
        conn.close()
