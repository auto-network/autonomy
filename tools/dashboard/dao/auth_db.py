"""Auth DB — SQLite-backed token and message storage for CrossTalk.

Database: data/auth.db
Owned by the dashboard process, never mounted into agent containers.
Stores SHA-256 hashes of session tokens (raw tokens live only in container env).
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_DB_PATH = Path(__file__).parents[3] / "data" / "auth.db"
_conn: sqlite3.Connection | None = None

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS session_tokens (
    token_hash      TEXT PRIMARY KEY,
    tmux_name       TEXT NOT NULL,
    created_at      REAL NOT NULL,
    revoked_at      REAL
);

CREATE TABLE IF NOT EXISTS crosstalk_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_session  TEXT NOT NULL,
    sender_label    TEXT NOT NULL DEFAULT '',
    target_session  TEXT NOT NULL,
    source_id       TEXT,
    turn            INTEGER,
    message         TEXT NOT NULL,
    timestamp       REAL NOT NULL,
    delivered       INTEGER DEFAULT 1
);
"""


def init_db(db_path: Path | None = None) -> None:
    """Initialise auth.db and create schema. Idempotent."""
    global _conn
    path = db_path or _DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(str(path), check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA busy_timeout=5000")
    _conn.executescript(_SCHEMA)
    _conn.commit()
    logger.info("auth_db: initialised at %s", path)


def get_conn() -> sqlite3.Connection:
    """Return the module-level connection, initialising if needed."""
    if _conn is None:
        init_db()
    assert _conn is not None
    return _conn


# -- Token operations ----------------------------------------------------------


def insert_token(token_hash: str, tmux_name: str) -> None:
    """Store a hashed session token."""
    conn = get_conn()
    conn.execute(
        "INSERT INTO session_tokens (token_hash, tmux_name, created_at) VALUES (?, ?, ?)",
        (token_hash, tmux_name, time.time()),
    )
    conn.commit()


def resolve_token(token_hash: str) -> str | None:
    """Look up tmux_name for a non-revoked token hash. Returns None if invalid."""
    conn = get_conn()
    row = conn.execute(
        "SELECT tmux_name FROM session_tokens WHERE token_hash=? AND revoked_at IS NULL",
        (token_hash,),
    ).fetchone()
    return row["tmux_name"] if row else None


def revoke_token(tmux_name: str) -> None:
    """Revoke all tokens for a given tmux session."""
    conn = get_conn()
    conn.execute(
        "UPDATE session_tokens SET revoked_at=? WHERE tmux_name=? AND revoked_at IS NULL",
        (time.time(), tmux_name),
    )
    conn.commit()


# -- Message operations --------------------------------------------------------


def insert_message(
    sender_session: str,
    sender_label: str,
    target_session: str,
    source_id: str | None,
    turn: int | None,
    message: str,
    timestamp: float,
) -> int:
    """Insert a crosstalk message and return the row id."""
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO crosstalk_messages"
        " (sender_session, sender_label, target_session, source_id, turn, message, timestamp)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (sender_session, sender_label, target_session, source_id, turn, message, timestamp),
    )
    conn.commit()
    return cur.lastrowid
