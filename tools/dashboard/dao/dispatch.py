"""SQLite DAO for dispatch run data (read-only dashboard queries).

Opens data/dispatch.db in read-only WAL mode. The dispatcher is the
sole writer; this module never modifies the database.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
DB_PATH = Path(os.environ.get("DISPATCH_DB", str(_REPO_ROOT / "data" / "dispatch.db")))


def _get_conn() -> sqlite3.Connection:
    """Open a read-only WAL connection to dispatch.db.

    Uses the URI file format with mode=ro so SQLite refuses any writes.
    Creates the file if it doesn't exist (so the dashboard starts cleanly
    before the dispatcher has ever run).
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    uri = f"file:{DB_PATH}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.OperationalError:
        # DB doesn't exist yet — open read-write just to create it, then
        # immediately reopen read-only.  The dispatcher will init the schema.
        sqlite3.connect(str(DB_PATH)).close()
        conn = sqlite3.connect(uri, uri=True)
    # WAL mode is set by the dispatcher (the sole writer).  Reading it here
    # is sufficient — we don't need to set it and can't in read-only mode.
    conn.row_factory = sqlite3.Row
    return conn


_TS_FIELDS = {"started_at", "completed_at", "last_activity"}


def _coerce_ts(row: dict) -> dict:
    """Append Z to bare UTC timestamp strings returned from SQLite."""
    return {
        k: (v + "Z") if (k in _TS_FIELDS and isinstance(v, str)) else v
        for k, v in row.items()
    }


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    rows = conn.execute(sql, params).fetchall()
    return [_coerce_ts({k: row[k] for k in row.keys()}) for row in rows]


def _one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> dict | None:
    row = conn.execute(sql, params).fetchone()
    return _coerce_ts({k: row[k] for k in row.keys()}) if row else None


def get_running_with_stats() -> list[dict]:
    """Return all RUNNING dispatch runs (agents currently in-flight).

    Ordered by started_at ascending so the oldest running run appears first.
    The dispatcher updates these rows with live stats (snippet, token count,
    cpu/mem) during its poll cycle.
    """
    conn = _get_conn()
    try:
        return _rows(
            conn,
            "SELECT * FROM dispatch_runs WHERE status = ? ORDER BY started_at ASC",
            ("RUNNING",),
        )
    except sqlite3.OperationalError:
        # Table doesn't exist yet (first run before dispatcher inits schema).
        return []
    finally:
        conn.close()


def get_recent_runs(limit: int = 50) -> list[dict]:
    """Return completed runs for the timeline, most recent first.

    Excludes RUNNING rows — those are for the live panel, not the history
    timeline.  Use limit to keep the payload small.
    """
    conn = _get_conn()
    try:
        return _rows(
            conn,
            "SELECT * FROM dispatch_runs WHERE status != ? "
            "ORDER BY completed_at DESC LIMIT ?",
            ("RUNNING", limit),
        )
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def get_run(run_id: str) -> dict | None:
    """Return a single dispatch run by its run_id (directory name), or None."""
    conn = _get_conn()
    try:
        return _one(
            conn,
            "SELECT * FROM dispatch_runs WHERE id = ?",
            (run_id,),
        )
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()


def get_runs_for_bead(bead_id: str) -> list[dict]:
    """Return all dispatch runs for a bead, most recent first.

    RUNNING rows (NULL completed_at) sort before completed rows so the
    active run appears at the top.
    """
    conn = _get_conn()
    try:
        return _rows(
            conn,
            "SELECT * FROM dispatch_runs WHERE bead_id = ? "
            "ORDER BY COALESCE(completed_at, started_at) DESC",
            (bead_id,),
        )
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
