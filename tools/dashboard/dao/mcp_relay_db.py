"""MCP-relay peer store — per-`openai/session` org bindings + crosstalk grants.

Operational instance data, not graph Settings (high write churn, short-lived,
keyed by an opaque OpenAI-stamped session id). Mirrors mission_control_db's
schema-on-connect DAO shape. See design note graph://eeb23208-257.

Identity is `openai/session` (per-chat, turn-stable, mTLS-stamped by OpenAI's
tunnel). The relay resolves a session here; approval binds it to exactly one
Autonomy org at a level (read | readwrite) for a TTL. CrossTalk to a specific
target session is a separate, per-(session, target) sticky grant.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from pathlib import Path

from tools.data_paths import resolve_store

DB_PATH = resolve_store("mcp_relay")

# status values shared by both tables
PENDING, APPROVED, DENIED, REVOKED = "pending", "approved", "denied", "revoked"

CREATE_TABLES = """\
CREATE TABLE IF NOT EXISTS mcp_sessions (
    openai_session   TEXT PRIMARY KEY,
    openai_subject   TEXT NOT NULL DEFAULT '',
    openai_org       TEXT NOT NULL DEFAULT '',
    intent           TEXT NOT NULL DEFAULT '',
    requested_org    TEXT NOT NULL DEFAULT '',
    autonomy_org     TEXT,
    level            TEXT,
    status           TEXT NOT NULL DEFAULT 'pending',
    approval_id      TEXT,
    expires_at       REAL,
    approved_by      TEXT,
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mcp_sessions_status
    ON mcp_sessions(status, expires_at);

CREATE TABLE IF NOT EXISTS mcp_crosstalk_grants (
    grant_id         TEXT PRIMARY KEY,
    openai_session   TEXT NOT NULL,
    target_session   TEXT NOT NULL,
    target_org       TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'pending',
    approval_id      TEXT,
    expires_at       REAL,
    approved_by      TEXT,
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL,
    UNIQUE (openai_session, target_session)
);

CREATE INDEX IF NOT EXISTS idx_mcp_crosstalk_session
    ON mcp_crosstalk_grants(openai_session);
"""


def _db_path(db_path: Path | str | None = None) -> Path:
    return Path(db_path) if db_path is not None else DB_PATH


def _get_conn(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open a connection with the schema guaranteed present (a fresh deployment's
    first request may be a read; a bare 'no such table' would surface as a 500)."""
    path = _db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(CREATE_TABLES)
    return conn


def init_db(db_path: Path | str | None = None) -> None:
    _get_conn(db_path).close()


def _live(row: dict | None, now: float | None = None) -> bool:
    """True iff the row is approved and unexpired."""
    if not row or row.get("status") != APPROVED:
        return False
    exp = row.get("expires_at")
    return exp is None or exp > (now if now is not None else time.time())


# ── Sessions ─────────────────────────────────────────────────────

def get_session(openai_session: str, *, db_path: Path | str | None = None) -> dict | None:
    conn = _get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM mcp_sessions WHERE openai_session = ?", (openai_session,)
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def upsert_pending_session(
    openai_session: str,
    *,
    openai_subject: str = "",
    openai_org: str = "",
    intent: str = "",
    requested_org: str = "",
    approval_id: str | None = None,
    db_path: Path | str | None = None,
) -> dict:
    """Ensure a pending record exists for a session that just said hello.

    If no row exists, insert a pending one. If a row exists and is NOT currently
    live (pending/denied/revoked/expired), reset it to pending with the new
    intent/requested_org/approval_id (a re-hello asking for a fresh grant, e.g.
    a different org). A live (approved+unexpired) row is returned unchanged —
    the caller decides whether to re-pop for a different org.
    """
    now = time.time()
    conn = _get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM mcp_sessions WHERE openai_session = ?", (openai_session,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO mcp_sessions (openai_session, openai_subject, openai_org,"
                " intent, requested_org, status, approval_id, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (openai_session, openai_subject, openai_org, intent, requested_org,
                 PENDING, approval_id, now, now),
            )
            conn.commit()
        elif not _live(dict(row), now):
            conn.execute(
                "UPDATE mcp_sessions SET openai_subject=?, openai_org=?, intent=?,"
                " requested_org=?, status=?, approval_id=?, autonomy_org=NULL,"
                " level=NULL, expires_at=NULL, approved_by=NULL, updated_at=?"
                " WHERE openai_session=?",
                (openai_subject, openai_org, intent, requested_org, PENDING,
                 approval_id, now, openai_session),
            )
            conn.commit()
    finally:
        conn.close()
    return get_session(openai_session, db_path=db_path)


def approve_session(
    openai_session: str,
    *,
    autonomy_org: str,
    level: str,
    expires_at: float | None,
    approved_by: str = "",
    db_path: Path | str | None = None,
) -> dict | None:
    """Bind a session to one org at a level, for a TTL. level ∈ {read, readwrite}."""
    if level not in ("read", "readwrite"):
        raise ValueError("level must be 'read' or 'readwrite'")
    now = time.time()
    conn = _get_conn(db_path)
    try:
        cur = conn.execute(
            "UPDATE mcp_sessions SET status=?, autonomy_org=?, level=?, expires_at=?,"
            " approved_by=?, updated_at=? WHERE openai_session=?",
            (APPROVED, autonomy_org, level, expires_at, approved_by, now, openai_session),
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
    finally:
        conn.close()
    return get_session(openai_session, db_path=db_path)


def set_session_status(
    openai_session: str, status: str, *, db_path: Path | str | None = None
) -> bool:
    """deny/revoke a session (kills any live binding)."""
    now = time.time()
    conn = _get_conn(db_path)
    try:
        cur = conn.execute(
            "UPDATE mcp_sessions SET status=?, autonomy_org=NULL, level=NULL,"
            " expires_at=NULL, updated_at=? WHERE openai_session=?",
            (status, now, openai_session),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def resolve_session(openai_session: str, *, db_path: Path | str | None = None) -> dict:
    """The relay's per-request check: returns the effective authorization for a
    session — {status, autonomy_org, level, expires_at}. `status` is 'approved'
    only when a live (unexpired) binding exists; an expired binding reports
    'expired', an unknown session 'unknown'."""
    row = get_session(openai_session, db_path=db_path)
    if row is None:
        return {"status": "unknown"}
    if _live(row):
        return {"status": APPROVED, "autonomy_org": row["autonomy_org"],
                "level": row["level"], "expires_at": row["expires_at"]}
    if row["status"] == APPROVED:  # was approved but expired
        return {"status": "expired"}
    return {"status": row["status"]}


# ── CrossTalk grants ─────────────────────────────────────────────

def get_crosstalk_grant(
    openai_session: str, target_session: str, *, db_path: Path | str | None = None
) -> dict | None:
    conn = _get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM mcp_crosstalk_grants WHERE openai_session=? AND target_session=?",
            (openai_session, target_session),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def upsert_pending_crosstalk(
    openai_session: str,
    target_session: str,
    *,
    target_org: str = "",
    approval_id: str | None = None,
    db_path: Path | str | None = None,
) -> dict:
    """Ensure a pending grant exists for a (session, target) pair. A live grant
    is returned unchanged; otherwise (re)set to pending."""
    now = time.time()
    conn = _get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM mcp_crosstalk_grants WHERE openai_session=? AND target_session=?",
            (openai_session, target_session),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO mcp_crosstalk_grants (grant_id, openai_session, target_session,"
                " target_org, status, approval_id, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), openai_session, target_session, target_org,
                 PENDING, approval_id, now, now),
            )
            conn.commit()
        elif not _live(dict(row), now):
            conn.execute(
                "UPDATE mcp_crosstalk_grants SET target_org=?, status=?, approval_id=?,"
                " expires_at=NULL, approved_by=NULL, updated_at=?"
                " WHERE openai_session=? AND target_session=?",
                (target_org, PENDING, approval_id, now, openai_session, target_session),
            )
            conn.commit()
    finally:
        conn.close()
    return get_crosstalk_grant(openai_session, target_session, db_path=db_path)


def approve_crosstalk(
    openai_session: str,
    target_session: str,
    *,
    expires_at: float | None,
    approved_by: str = "",
    db_path: Path | str | None = None,
) -> dict | None:
    now = time.time()
    conn = _get_conn(db_path)
    try:
        cur = conn.execute(
            "UPDATE mcp_crosstalk_grants SET status=?, expires_at=?, approved_by=?,"
            " updated_at=? WHERE openai_session=? AND target_session=?",
            (APPROVED, expires_at, approved_by, now, openai_session, target_session),
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
    finally:
        conn.close()
    return get_crosstalk_grant(openai_session, target_session, db_path=db_path)


def crosstalk_allowed(
    openai_session: str, target_session: str, *, db_path: Path | str | None = None
) -> bool:
    return _live(get_crosstalk_grant(openai_session, target_session, db_path=db_path))


def list_sessions(*, db_path: Path | str | None = None) -> list[dict]:
    conn = _get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM mcp_sessions ORDER BY created_at DESC"
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]
