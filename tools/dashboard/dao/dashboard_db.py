"""Dashboard DB — SQLite-backed persistent state owned by the dashboard process.

Single source of truth for tmux session identity. Replaces in-memory dicts,
scattered meta files, and file-scanning recovery.

Database: data/dashboard.db
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DB_PATH = Path(__file__).parents[3] / "data" / "dashboard.db"
_conn: sqlite3.Connection | None = None

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS tmux_sessions (
    tmux_name       TEXT PRIMARY KEY,
    session_uuid    TEXT,
    graph_source_id TEXT,
    type            TEXT NOT NULL,
    project         TEXT NOT NULL,
    jsonl_path      TEXT,
    bead_id         TEXT,
    created_at      REAL NOT NULL,
    is_live         INTEGER DEFAULT 1,
    file_offset     INTEGER DEFAULT 0,
    last_activity   REAL,
    last_message    TEXT DEFAULT '',
    entry_count     INTEGER DEFAULT 0,
    context_tokens  INTEGER DEFAULT 0,
    label           TEXT DEFAULT '',
    topics          TEXT DEFAULT '[]'
);
"""


def init_db(db_path: Path | None = None) -> None:
    """Initialise dashboard.db and create schema. Idempotent."""
    global _conn
    path = db_path or _DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(str(path), check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA busy_timeout=5000")
    _conn.executescript(_SCHEMA)
    _conn.commit()
    # Migrate: add label column if missing (for existing databases)
    try:
        _conn.execute("SELECT label FROM tmux_sessions LIMIT 0")
    except sqlite3.OperationalError:
        _conn.execute("ALTER TABLE tmux_sessions ADD COLUMN label TEXT DEFAULT ''")
        _conn.commit()
    # Migrate: add topics column if missing (for existing databases)
    try:
        _conn.execute("SELECT topics FROM tmux_sessions LIMIT 0")
    except sqlite3.OperationalError:
        _conn.execute("ALTER TABLE tmux_sessions ADD COLUMN topics TEXT DEFAULT '[]'")
        _conn.commit()
    logger.info("dashboard_db: initialised at %s", path)


def get_conn() -> sqlite3.Connection:
    """Return the module-level connection, initialising if needed."""
    if _conn is None:
        init_db()
    assert _conn is not None
    return _conn


# ── INSERT / UPDATE helpers ─────────────────────────────────────


def insert_session(
    tmux_name: str,
    session_type: str,
    project: str,
    *,
    bead_id: str | None = None,
    jsonl_path: str | None = None,
    session_uuid: str | None = None,
) -> None:
    """INSERT a new session row. Raises sqlite3.IntegrityError on duplicate name."""
    conn = get_conn()
    conn.execute(
        "INSERT INTO tmux_sessions"
        " (tmux_name, type, project, bead_id, jsonl_path, session_uuid, created_at, is_live)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
        (tmux_name, session_type, project, bead_id, jsonl_path, session_uuid, time.time()),
    )
    conn.commit()


def update_jsonl_link(tmux_name: str, session_uuid: str, jsonl_path: str, project: str | None = None) -> None:
    """LINK step: set session_uuid and jsonl_path after watcher discovers the file."""
    conn = get_conn()
    if project:
        conn.execute(
            "UPDATE tmux_sessions SET session_uuid=?, jsonl_path=?, project=? WHERE tmux_name=?",
            (session_uuid, jsonl_path, project, tmux_name),
        )
    else:
        conn.execute(
            "UPDATE tmux_sessions SET session_uuid=?, jsonl_path=? WHERE tmux_name=?",
            (session_uuid, jsonl_path, tmux_name),
        )
    conn.commit()


def link_and_enrich(tmux_name: str, session_uuid: str, jsonl_path: str, project: str | None = None) -> None:
    """LINK + ENRICH in one shot: set session_uuid, jsonl_path, and graph_source_id.

    1. Updates dashboard.db with session_uuid and jsonl_path
    2. Runs `graph ingest-session` to ingest the JSONL and get the graph source ID
    3. Updates dashboard.db with graph_source_id

    This is the ONLY function that should be called when a JSONL is discovered
    (by the watcher or the Link Terminal handshake).
    """
    import subprocess

    # LINK: write session_uuid and jsonl_path
    update_jsonl_link(tmux_name, session_uuid, jsonl_path, project)
    logger.info("dashboard_db: LINK  %s → uuid=%s  path=%s", tmux_name, session_uuid[:12], jsonl_path)

    # ENRICH: ingest into graph and capture source ID
    try:
        result = subprocess.run(
            ["graph", "ingest-session", jsonl_path],
            capture_output=True, text=True, timeout=30,
        )
        graph_source_id = result.stdout.strip()
        if result.returncode == 0 and graph_source_id:
            update_graph_source(tmux_name, graph_source_id)
            logger.info("dashboard_db: ENRICH  %s → graph=%s", tmux_name, graph_source_id[:11])
        else:
            logger.warning("dashboard_db: ENRICH failed for %s: %s", tmux_name, result.stderr.strip())
    except Exception:
        logger.warning("dashboard_db: ENRICH error for %s", tmux_name, exc_info=True)


def update_graph_source(tmux_name: str, graph_source_id: str) -> None:
    """ENRICH step: set graph_source_id after graph ingestion."""
    conn = get_conn()
    conn.execute(
        "UPDATE tmux_sessions SET graph_source_id=? WHERE tmux_name=?",
        (graph_source_id, tmux_name),
    )
    conn.commit()


def update_tail_state(
    tmux_name: str,
    *,
    file_offset: int | None = None,
    last_activity: float | None = None,
    last_message: str | None = None,
    entry_count: int | None = None,
    context_tokens: int | None = None,
) -> None:
    """TAIL step: update read position and latest content."""
    conn = get_conn()
    parts = []
    vals: list[Any] = []
    if file_offset is not None:
        parts.append("file_offset=?")
        vals.append(file_offset)
    if last_activity is not None:
        parts.append("last_activity=?")
        vals.append(last_activity)
    if last_message is not None:
        parts.append("last_message=?")
        vals.append(last_message)
    if entry_count is not None:
        parts.append("entry_count=?")
        vals.append(entry_count)
    if context_tokens is not None:
        parts.append("context_tokens=?")
        vals.append(context_tokens)
    if not parts:
        return
    vals.append(tmux_name)
    conn.execute(f"UPDATE tmux_sessions SET {', '.join(parts)} WHERE tmux_name=?", vals)
    conn.commit()


def update_label(tmux_name: str, label: str) -> None:
    """Set or clear the user-facing label for a session."""
    conn = get_conn()
    conn.execute("UPDATE tmux_sessions SET label=? WHERE tmux_name=?", (label, tmux_name))
    conn.commit()


def update_topics(tmux_name: str, topics: list[str]) -> None:
    """Set the sub-topic status lines for a session (1-4 items, max 80 chars each)."""
    import json
    conn = get_conn()
    conn.execute("UPDATE tmux_sessions SET topics=? WHERE tmux_name=?",
                 (json.dumps(topics), tmux_name))
    conn.commit()


def mark_dead(tmux_name: str) -> None:
    """CLOSE step: mark session as no longer live."""
    conn = get_conn()
    conn.execute("UPDATE tmux_sessions SET is_live=0 WHERE tmux_name=?", (tmux_name,))
    conn.commit()


def delete_session(tmux_name: str) -> None:
    """Hard-delete a session row (used for cleanup of expired dead sessions)."""
    conn = get_conn()
    conn.execute("DELETE FROM tmux_sessions WHERE tmux_name=?", (tmux_name,))
    conn.commit()


# ── Queries ─────────────────────────────────────────────────────


def get_live_sessions() -> list[dict]:
    """Return all sessions with is_live=1."""
    conn = get_conn()
    rows = conn.execute("SELECT * FROM tmux_sessions WHERE is_live=1").fetchall()
    return [dict(r) for r in rows]


def get_all_sessions() -> list[dict]:
    """Return all sessions (live and dead)."""
    conn = get_conn()
    rows = conn.execute("SELECT * FROM tmux_sessions ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def get_session(tmux_name: str) -> dict | None:
    """Return a single session by tmux_name, or None."""
    conn = get_conn()
    row = conn.execute("SELECT * FROM tmux_sessions WHERE tmux_name=?", (tmux_name,)).fetchone()
    return dict(row) if row else None


def get_tailable_sessions() -> list[dict]:
    """Return live sessions that have a jsonl_path set (ready for tailing)."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM tmux_sessions WHERE is_live=1 AND jsonl_path IS NOT NULL"
    ).fetchall()
    return [dict(r) for r in rows]


def get_sessions_needing_resolution() -> list[dict]:
    """Return live sessions with no jsonl_path yet (need directory resolution or watcher link)."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM tmux_sessions WHERE is_live=1 AND jsonl_path IS NULL"
    ).fetchall()
    return [dict(r) for r in rows]


def session_exists(tmux_name: str) -> bool:
    """Check if a session with this name exists in the DB."""
    conn = get_conn()
    row = conn.execute("SELECT 1 FROM tmux_sessions WHERE tmux_name=?", (tmux_name,)).fetchone()
    return row is not None


def count_live() -> int:
    """Count live sessions."""
    conn = get_conn()
    row = conn.execute("SELECT COUNT(*) FROM tmux_sessions WHERE is_live=1").fetchone()
    return row[0] if row else 0


def upsert_session(
    tmux_name: str,
    session_type: str,
    project: str,
    *,
    bead_id: str | None = None,
    jsonl_path: str | None = None,
    session_uuid: str | None = None,
    created_at: float | None = None,
    file_offset: int = 0,
    last_message: str = "",
    is_live: bool = True,
    label: str = "",
) -> None:
    """INSERT OR REPLACE — used for seeding on first run."""
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO tmux_sessions"
        " (tmux_name, type, project, bead_id, jsonl_path, session_uuid,"
        "  created_at, is_live, file_offset, last_message, label)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            tmux_name, session_type, project, bead_id, jsonl_path, session_uuid,
            created_at or time.time(), 1 if is_live else 0, file_offset, last_message,
            label,
        ),
    )
    conn.commit()
