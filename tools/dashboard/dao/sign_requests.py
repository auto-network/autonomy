"""Commit-signing rendezvous — one table.

An agent's ``gpg.program`` shim posts the exact commit bytes here (blocked mid
``git commit``); the operator's browser reads them, signs in-page with the org
key, and posts the signature back; the shim polls until it appears. A request is
``pending`` (signature NULL), ``signed`` (armored signature), or ``declined``
(empty string). Nothing here signs or assembles — it only carries bytes.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DB_PATH = Path(os.environ.get("SIGN_REQUESTS_DB", str(REPO_ROOT / "data" / "sign_requests.db")))

SCHEMA = """
CREATE TABLE IF NOT EXISTS sign_requests (
    id          TEXT PRIMARY KEY,
    session     TEXT NOT NULL,
    repo        TEXT NOT NULL,
    payload     BLOB NOT NULL,   -- the exact commit bytes to sign
    signature   TEXT,            -- NULL = pending, '' = declined, armored = signed
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sign_requests_pending
    ON sign_requests(session, created_at) WHERE signature IS NULL;
"""


def _conn(db_path: Path | str | None = None) -> sqlite3.Connection:
    p = Path(db_path) if db_path is not None else DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(p))
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)  # cheap CREATE IF NOT EXISTS; keeps callers simple
    return c


def init_db(db_path: Path | str | None = None) -> None:
    _conn(db_path).close()


def create(*, session: str, repo: str, payload: bytes, created_at: float,
           db_path: Path | str | None = None) -> str:
    """Insert a new pending request; return its id."""
    rid = uuid.uuid4().hex[:12]
    c = _conn(db_path)
    try:
        c.execute(
            "INSERT INTO sign_requests (id, session, repo, payload, signature, created_at) "
            "VALUES (?, ?, ?, ?, NULL, ?)",
            (rid, session, repo, payload, created_at),
        )
        c.commit()
    finally:
        c.close()
    return rid


def get(request_id: str, db_path: Path | str | None = None) -> dict | None:
    c = _conn(db_path)
    try:
        r = c.execute("SELECT * FROM sign_requests WHERE id = ?", (request_id,)).fetchone()
        return dict(r) if r else None
    finally:
        c.close()


def set_signature(request_id: str, signature: str, db_path: Path | str | None = None) -> bool:
    """Attach the operator's result. ``signature`` is the armored signature, or
    '' to decline. Only writes a still-pending row (first writer wins); returns
    True iff it updated one."""
    c = _conn(db_path)
    try:
        cur = c.execute(
            "UPDATE sign_requests SET signature = ? WHERE id = ? AND signature IS NULL",
            (signature, request_id),
        )
        c.commit()
        return cur.rowcount > 0
    finally:
        c.close()


def pending_id_for_session(session: str, db_path: Path | str | None = None) -> str | None:
    """The oldest pending request for a session, or None — backs the session
    viewer's ``commit_sign_pending`` field."""
    c = _conn(db_path)
    try:
        r = c.execute(
            "SELECT id FROM sign_requests WHERE session = ? AND signature IS NULL "
            "ORDER BY created_at LIMIT 1",
            (session,),
        ).fetchone()
        return r["id"] if r else None
    finally:
        c.close()
