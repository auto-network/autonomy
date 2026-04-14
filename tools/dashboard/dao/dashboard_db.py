"""Dashboard DB — SQLite-backed persistent state owned by the dashboard process.

Single source of truth for tmux session identity. Replaces in-memory dicts,
scattered meta files, and file-scanning recovery.

Database: data/dashboard.db
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DB_PATH = Path(os.environ.get("DASHBOARD_DB", str(Path(__file__).parents[3] / "data" / "dashboard.db")))
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
    topics          TEXT DEFAULT '[]',
    role            TEXT DEFAULT ''
);
"""


def _backfill_new_columns(conn: sqlite3.Connection) -> None:
    """Backfill resolution_dir, session_uuids, curr_jsonl_file for existing rows.

    Called once when the new columns are first added.
    """
    import json as _json

    rows = conn.execute(
        "SELECT tmux_name, type, bead_id, jsonl_path FROM tmux_sessions"
    ).fetchall()
    if not rows:
        return

    agent_runs = Path(__file__).resolve().parents[3] / "data" / "agent-runs"
    updated = 0
    for row in rows:
        tmux_name = row[0]
        stype = row[1]
        bead_id = row[2]
        jsonl_path = row[3]

        resolution_dir: str | None = None
        session_uuids: list[str] = []
        curr_jsonl_file: str | None = None

        if jsonl_path:
            jp = Path(jsonl_path)

            # Validate: clear subagent paths (graph://301b0811-0f1 bug)
            if "subagents" in str(jp):
                logger.warning("dashboard_db: backfill clearing subagent path for %s: %s", tmux_name, jsonl_path)
                conn.execute(
                    "UPDATE tmux_sessions SET jsonl_path=NULL, session_uuid=NULL WHERE tmux_name=?",
                    (tmux_name,),
                )
                continue

            # Derive resolution_dir from jsonl_path parent
            resolution_dir = str(jp.parent)
            session_uuids = [jp.stem]
            curr_jsonl_file = jsonl_path
        elif stype == "container" and bead_id:
            # Container without jsonl_path: derive from bead_id
            if agent_runs.exists():
                matches = sorted(
                    agent_runs.glob(f"{bead_id}-*"),
                    key=lambda p: p.stat().st_mtime, reverse=True,
                )
                for m in matches:
                    sess_dir = m / "sessions"
                    if sess_dir.exists():
                        # Find project subdirs
                        subdirs = [d for d in sess_dir.iterdir() if d.is_dir() and not d.name.startswith(".")]
                        if subdirs:
                            resolution_dir = str(subdirs[0])
                        break

        if resolution_dir or session_uuids or curr_jsonl_file:
            conn.execute(
                "UPDATE tmux_sessions SET resolution_dir=?, session_uuids=?, curr_jsonl_file=? WHERE tmux_name=?",
                (resolution_dir, _json.dumps(session_uuids), curr_jsonl_file, tmux_name),
            )
            updated += 1

    conn.commit()
    if updated:
        logger.info("dashboard_db: backfilled %d rows with resolution_dir/session_uuids/curr_jsonl_file", updated)


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
    # Migrate: add nag columns if missing
    try:
        _conn.execute("SELECT nag_enabled FROM tmux_sessions LIMIT 0")
    except sqlite3.OperationalError:
        _conn.execute("ALTER TABLE tmux_sessions ADD COLUMN nag_enabled INTEGER DEFAULT 0")
        _conn.execute("ALTER TABLE tmux_sessions ADD COLUMN nag_interval INTEGER DEFAULT 15")
        _conn.execute("ALTER TABLE tmux_sessions ADD COLUMN nag_message TEXT DEFAULT ''")
        _conn.execute("ALTER TABLE tmux_sessions ADD COLUMN nag_last_sent REAL DEFAULT 0")
        _conn.commit()
    # Migrate: add dispatch_nag column if missing
    try:
        _conn.execute("SELECT dispatch_nag FROM tmux_sessions LIMIT 0")
    except sqlite3.OperationalError:
        _conn.execute("ALTER TABLE tmux_sessions ADD COLUMN dispatch_nag INTEGER DEFAULT 0")
        _conn.commit()
    # Migrate: add role column if missing
    try:
        _conn.execute("SELECT role FROM tmux_sessions LIMIT 0")
    except sqlite3.OperationalError:
        _conn.execute("ALTER TABLE tmux_sessions ADD COLUMN role TEXT DEFAULT ''")
        _conn.commit()
    # Migrate: add resolution_dir, session_uuids, curr_jsonl_file columns (Phase 0)
    try:
        _conn.execute("SELECT resolution_dir FROM tmux_sessions LIMIT 0")
    except sqlite3.OperationalError:
        _conn.execute("ALTER TABLE tmux_sessions ADD COLUMN resolution_dir TEXT")
        _conn.execute("ALTER TABLE tmux_sessions ADD COLUMN session_uuids TEXT DEFAULT '[]'")
        _conn.execute("ALTER TABLE tmux_sessions ADD COLUMN curr_jsonl_file TEXT")
        _conn.commit()
        _backfill_new_columns(_conn)
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
    resolution_dir: str | None = None,
) -> None:
    """INSERT a new session row. Raises sqlite3.IntegrityError on duplicate name."""
    import json as _json

    conn = get_conn()
    # Build session_uuids and curr_jsonl_file from initial values
    session_uuids = _json.dumps([session_uuid]) if session_uuid else "[]"
    curr_jsonl_file = jsonl_path  # initially same as jsonl_path
    conn.execute(
        "INSERT INTO tmux_sessions"
        " (tmux_name, type, project, bead_id, jsonl_path, session_uuid,"
        "  resolution_dir, session_uuids, curr_jsonl_file, created_at, is_live)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
        (tmux_name, session_type, project, bead_id, jsonl_path, session_uuid,
         resolution_dir, session_uuids, curr_jsonl_file, time.time()),
    )
    conn.commit()


def update_jsonl_link(tmux_name: str, session_uuid: str, jsonl_path: str, project: str | None = None) -> None:
    """LINK step: set session_uuid, jsonl_path, and append to session_uuids.

    Also updates curr_jsonl_file and derives resolution_dir from the file path.
    """
    import json as _json

    conn = get_conn()
    # Read current session_uuids and append new UUID if not already present
    row = conn.execute(
        "SELECT session_uuids FROM tmux_sessions WHERE tmux_name=?", (tmux_name,)
    ).fetchone()
    uuids: list[str] = _json.loads(row[0] or "[]") if row and row[0] else []
    if session_uuid not in uuids:
        uuids.append(session_uuid)

    resolution_dir = str(Path(jsonl_path).parent)

    if project:
        conn.execute(
            "UPDATE tmux_sessions SET session_uuid=?, jsonl_path=?, project=?,"
            " resolution_dir=COALESCE(resolution_dir, ?), session_uuids=?,"
            " curr_jsonl_file=? WHERE tmux_name=?",
            (session_uuid, jsonl_path, project, resolution_dir, _json.dumps(uuids),
             jsonl_path, tmux_name),
        )
    else:
        conn.execute(
            "UPDATE tmux_sessions SET session_uuid=?, jsonl_path=?,"
            " resolution_dir=COALESCE(resolution_dir, ?), session_uuids=?,"
            " curr_jsonl_file=? WHERE tmux_name=?",
            (session_uuid, jsonl_path, resolution_dir, _json.dumps(uuids),
             jsonl_path, tmux_name),
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


def update_role(tmux_name: str, role: str) -> None:
    """Set or clear the explicit role for a session."""
    conn = get_conn()
    conn.execute("UPDATE tmux_sessions SET role=? WHERE tmux_name=?", (role, tmux_name))
    conn.commit()


def get_nag_config(tmux_name: str) -> dict | None:
    """Return nag configuration for a session."""
    conn = get_conn()
    row = conn.execute(
        "SELECT nag_enabled, nag_interval, nag_message, nag_last_sent"
        " FROM tmux_sessions WHERE tmux_name=?",
        (tmux_name,),
    ).fetchone()
    if not row:
        return None
    return {
        "enabled": bool(row["nag_enabled"]),
        "interval": row["nag_interval"] or 15,
        "message": row["nag_message"] or "",
        "last_sent": row["nag_last_sent"] or 0,
    }


def update_nag_config(
    tmux_name: str,
    *,
    enabled: bool | None = None,
    interval: int | None = None,
    message: str | None = None,
) -> None:
    """Update nag configuration for a session."""
    conn = get_conn()
    parts, vals = [], []
    if enabled is not None:
        parts.append("nag_enabled=?")
        vals.append(1 if enabled else 0)
    if interval is not None:
        parts.append("nag_interval=?")
        vals.append(interval)
    if message is not None:
        parts.append("nag_message=?")
        vals.append(message)
    if not parts:
        return
    vals.append(tmux_name)
    conn.execute(f"UPDATE tmux_sessions SET {', '.join(parts)} WHERE tmux_name=?", vals)
    conn.commit()


def update_nag_last_sent(tmux_name: str, ts: float) -> None:
    """Record when the last nag was sent."""
    conn = get_conn()
    conn.execute("UPDATE tmux_sessions SET nag_last_sent=? WHERE tmux_name=?", (ts, tmux_name))
    conn.commit()


def update_dispatch_nag(tmux_name: str, enabled: bool) -> None:
    """Enable or disable dispatch completion nag for a session."""
    conn = get_conn()
    conn.execute("UPDATE tmux_sessions SET dispatch_nag=? WHERE tmux_name=?",
                 (1 if enabled else 0, tmux_name))
    conn.commit()


def get_dispatch_nag_sessions() -> list[str]:
    """Return tmux_names of live sessions with dispatch_nag enabled."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT tmux_name FROM tmux_sessions WHERE dispatch_nag=1 AND is_live=1"
    ).fetchall()
    return [r["tmux_name"] for r in rows]


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


def find_dead_session(session_uuid: str | None = None, file_path: str | None = None) -> dict | None:
    """Find a dead (is_live=0) session by session_uuid or jsonl_path.

    Checks session_uuid first, then falls back to jsonl_path.
    Returns the full row as a dict, or None.
    """
    conn = get_conn()
    if session_uuid:
        row = conn.execute(
            "SELECT * FROM tmux_sessions WHERE session_uuid=? AND is_live=0"
            " ORDER BY created_at DESC LIMIT 1",
            (session_uuid,),
        ).fetchone()
        if row:
            return dict(row)
    if file_path:
        row = conn.execute(
            "SELECT * FROM tmux_sessions WHERE jsonl_path=? AND is_live=0"
            " ORDER BY created_at DESC LIMIT 1",
            (file_path,),
        ).fetchone()
        if row:
            return dict(row)
    return None


def find_live_session(session_uuid: str | None = None, file_path: str | None = None) -> dict | None:
    """Find a live (is_live=1) session by session_uuid or jsonl_path.

    Checks session_uuid first, then falls back to jsonl_path.
    Returns the full row as a dict, or None.
    """
    conn = get_conn()
    if session_uuid:
        row = conn.execute(
            "SELECT * FROM tmux_sessions WHERE session_uuid=? AND is_live=1"
            " ORDER BY created_at DESC LIMIT 1",
            (session_uuid,),
        ).fetchone()
        if row:
            return dict(row)
    if file_path:
        row = conn.execute(
            "SELECT * FROM tmux_sessions WHERE jsonl_path=? AND is_live=1"
            " ORDER BY created_at DESC LIMIT 1",
            (file_path,),
        ).fetchone()
        if row:
            return dict(row)
    return None


def revive_session(tmux_name: str, *, file_offset: int = 0) -> None:
    """Re-activate a dead session: set is_live=1 and reset file_offset for backfill."""
    conn = get_conn()
    conn.execute(
        "UPDATE tmux_sessions SET is_live=1, file_offset=? WHERE tmux_name=?",
        (file_offset, tmux_name),
    )
    conn.commit()


def upsert_session(
    tmux_name: str,
    session_type: str,
    project: str,
    *,
    bead_id: str | None = None,
    jsonl_path: str | None = None,
    session_uuid: str | None = None,
    resolution_dir: str | None = None,
    session_uuids: str = "[]",
    curr_jsonl_file: str | None = None,
    created_at: float | None = None,
    file_offset: int = 0,
    last_message: str = "",
    is_live: bool = True,
    label: str = "",
) -> None:
    """INSERT ... ON CONFLICT — used for seeding on first run.

    Preserves existing label, topics, role, and nag settings on re-registration.
    Only file resolution fields and liveness are updated on conflict.
    """
    conn = get_conn()
    conn.execute(
        "INSERT INTO tmux_sessions"
        " (tmux_name, type, project, bead_id, jsonl_path, session_uuid,"
        "  resolution_dir, session_uuids, curr_jsonl_file,"
        "  created_at, is_live, file_offset, last_message, label)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(tmux_name) DO UPDATE SET"
        "  jsonl_path = excluded.jsonl_path,"
        "  session_uuid = excluded.session_uuid,"
        "  resolution_dir = COALESCE(excluded.resolution_dir, resolution_dir),"
        "  session_uuids = CASE WHEN excluded.session_uuids != '[]'"
        "    THEN excluded.session_uuids ELSE session_uuids END,"
        "  curr_jsonl_file = COALESCE(excluded.curr_jsonl_file, curr_jsonl_file),"
        "  file_offset = excluded.file_offset,"
        "  last_message = CASE WHEN excluded.last_message != ''"
        "    THEN excluded.last_message ELSE last_message END,"
        "  is_live = excluded.is_live",
        (
            tmux_name, session_type, project, bead_id, jsonl_path, session_uuid,
            resolution_dir, session_uuids, curr_jsonl_file,
            created_at or time.time(), 1 if is_live else 0, file_offset, last_message,
            label,
        ),
    )
    conn.commit()
