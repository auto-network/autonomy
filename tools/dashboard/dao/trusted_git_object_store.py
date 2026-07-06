"""Trusted git object store metadata DAO.

This stores snapshot lifecycle metadata for the host-owned trusted object
store used by commit creation, signing, attach, and governed rewrite flows.
The actual git object bytes live in the host-owned filesystem store; this
module only manages lookup, integrity, and retention metadata.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Iterable, Mapping
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


# ── snapshot + entry DAO helpers ─────────────────────────────────────
#
# These manage only the lifecycle metadata. The object bytes themselves live
# in the host filesystem store (services layer); a row here binds a snapshot to
# its workflow, its captured object set, and its integrity/retention state.

_VALID_SNAPSHOT_STATUS = {"captured", "verified", "gc_pending", "released", "gc_deleted"}


def insert_snapshot(
    conn: sqlite3.Connection,
    *,
    snapshot_ref: str,
    workflow_id: str,
    repo_slug: str,
    commit_sha: str,
    tree_sha: str,
    parent_shas: Iterable[str],
    manifest_sha256: str,
    canonical_preview_sha256: str,
    snapshot_type: str,
    store_root: str,
    retention_class: str,
    status: str = "captured",
    captured_by: str = "dashboard",
    captured_at: float | None = None,
    retention_expires_at: float | None = None,
) -> None:
    """Insert one snapshot lifecycle row.

    Enum-valued columns (snapshot_type, status, captured_by, retention_class)
    are enforced by the table CHECK constraints — a bad value raises
    ``sqlite3.IntegrityError`` rather than being silently stored.
    """
    conn.execute(
        """
        INSERT INTO trusted_git_object_snapshots (
            snapshot_ref, workflow_id, repo_slug, commit_sha, tree_sha,
            parent_shas_json, manifest_sha256, canonical_preview_sha256,
            snapshot_type, status, captured_at, captured_by, store_root,
            retention_class, retention_expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot_ref, workflow_id, repo_slug, commit_sha, tree_sha,
            json.dumps([str(p) for p in parent_shas]),
            manifest_sha256, canonical_preview_sha256,
            snapshot_type, status,
            float(captured_at if captured_at is not None else time.time()),
            captured_by, store_root, retention_class, retention_expires_at,
        ),
    )


def add_entries(conn: sqlite3.Connection, snapshot_ref: str, entries: Iterable[Mapping]) -> None:
    """Insert object-membership rows for a snapshot.

    Each entry needs object_oid, object_type, object_size, object_path,
    object_sha256, position. The FK to trusted_git_object_snapshots is enforced
    (PRAGMA foreign_keys=ON), so entries for a missing snapshot are rejected.
    """
    conn.executemany(
        """
        INSERT INTO trusted_git_object_entries (
            snapshot_ref, object_oid, object_type, object_size,
            object_path, object_sha256, position
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                snapshot_ref, e["object_oid"], e["object_type"], int(e["object_size"]),
                e["object_path"], e["object_sha256"], int(e["position"]),
            )
            for e in entries
        ],
    )


def get_snapshot(conn: sqlite3.Connection, snapshot_ref: str) -> dict | None:
    """Return the snapshot row as a dict (with parent_shas decoded), or None."""
    row = conn.execute(
        "SELECT * FROM trusted_git_object_snapshots WHERE snapshot_ref = ?",
        (snapshot_ref,),
    ).fetchone()
    if row is None:
        return None
    record = dict(row)
    record["parent_shas"] = json.loads(record.get("parent_shas_json") or "[]")
    return record


def list_entries(conn: sqlite3.Connection, snapshot_ref: str) -> list[dict]:
    """Return the snapshot's object entries ordered by position."""
    rows = conn.execute(
        "SELECT * FROM trusted_git_object_entries WHERE snapshot_ref = ? ORDER BY position",
        (snapshot_ref,),
    ).fetchall()
    return [dict(r) for r in rows]


def update_snapshot_status(conn: sqlite3.Connection, snapshot_ref: str, status: str) -> None:
    """Transition a snapshot's lifecycle status (validated against the enum)."""
    if status not in _VALID_SNAPSHOT_STATUS:
        raise ValueError(f"invalid snapshot status: {status!r}")
    conn.execute(
        "UPDATE trusted_git_object_snapshots SET status = ? WHERE snapshot_ref = ?",
        (status, snapshot_ref),
    )


def record_integrity(
    conn: sqlite3.Connection, snapshot_ref: str, *, status: str, at: float | None = None
) -> None:
    """Record the outcome of an integrity check on a snapshot."""
    conn.execute(
        "UPDATE trusted_git_object_snapshots "
        "SET latest_integrity_at = ?, latest_integrity_status = ? WHERE snapshot_ref = ?",
        (float(at if at is not None else time.time()), status, snapshot_ref),
    )
