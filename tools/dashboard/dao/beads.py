"""Dolt/MySQL DAO for bead data (read-only dashboard queries).

Connects directly to the Dolt SQL server on :3306 via pymysql, bypassing
the `bd` CLI subprocess.  This module never writes — all mutations still
go through `bd` (state machine, validation, audit trail).

Connection strategy: thread-local lazy connection with ping/reconnect.
The dashboard server calls these functions via asyncio.to_thread(), so
each worker thread gets its own pymysql connection.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

import pymysql
import pymysql.cursors

_DOLT_HOST = "127.0.0.1"
_DOLT_PORT = 3306
_DOLT_USER = "root"
_DOLT_PASSWORD = ""
_DOLT_DB = "auto"

_local = threading.local()


def _connect() -> pymysql.Connection:
    return pymysql.connect(
        host=_DOLT_HOST,
        port=_DOLT_PORT,
        user=_DOLT_USER,
        password=_DOLT_PASSWORD,
        database=_DOLT_DB,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
        connect_timeout=5,
    )


def _get_conn() -> pymysql.Connection:
    """Get or create a thread-local pymysql connection, reconnecting on error."""
    conn: pymysql.Connection | None = getattr(_local, "conn", None)
    if conn is None:
        conn = _connect()
        _local.conn = conn
        return conn
    try:
        conn.ping(reconnect=True)
    except Exception:
        conn = _connect()
        _local.conn = conn
    return conn


def _rows(cur: pymysql.cursors.DictCursor) -> list[dict]:
    return list(cur.fetchall())


def _parse_labels(raw: str | None) -> list[str]:
    """Split a GROUP_CONCAT label string into a list."""
    if not raw:
        return []
    return raw.split(",")


def _coerce(row: dict) -> dict:
    """Convert datetime objects to ISO strings for JSON-safe output."""
    out = {}
    for k, v in row.items():
        if isinstance(v, datetime):
            out[k] = v.isoformat() + "Z"
        elif k == "labels" and isinstance(v, str):
            out[k] = _parse_labels(v)
        else:
            out[k] = v
    return out


# ── Shared SQL fragments ───────────────────────────────────────────────

_BEAD_COLS = """
    i.id, i.title, i.status, i.priority, i.issue_type,
    i.description, i.created_at, i.updated_at,
    i.assignee, i.estimated_minutes, i.close_reason,
    GROUP_CONCAT(l.label ORDER BY l.label SEPARATOR ',') AS labels
"""


# ── Public API ─────────────────────────────────────────────────────────

def get_dispatch_beads() -> dict[str, list[dict]]:
    """Return beads grouped by dispatch role for the Dispatch page.

    Returns a dict with two keys:
    - "approved_waiting": readiness:approved, open, all blocking deps
      satisfied (closed).  Active dispatches are driven by SQLite
      dispatch_runs (status=RUNNING), not Dolt labels.
    - "approved_blocked": readiness:approved, open, at least one open
      non-parent-child dependency.

    Deps are resolved server-side via SQL — no N+1 bd subprocess calls.
    """
    conn = _get_conn()
    with conn.cursor() as cur:

        # Approved waiting: open, readiness:approved, no open blocking deps
        cur.execute(
            f"""
            SELECT {_BEAD_COLS}
            FROM issues i
            JOIN labels la ON la.issue_id = i.id AND la.label = %s
            LEFT JOIN labels l ON l.issue_id = i.id
            WHERE i.status = %s
              AND NOT EXISTS (
                  SELECT 1 FROM dependencies d
                  JOIN issues di ON di.id = d.depends_on_id
                  WHERE d.issue_id = i.id
                    AND d.type != %s
                    AND di.status != %s
              )
            GROUP BY i.id
            ORDER BY i.priority ASC, i.updated_at DESC
            """,
            ("readiness:approved", "open", "parent-child", "closed"),
        )
        approved_waiting = [_coerce(r) for r in _rows(cur)]

        # Approved blocked: same as waiting but has at least one open dep.
        # Include the IDs of open blockers for the frontend to link to.
        cur.execute(
            """
            SELECT
                i.id, i.title, i.status, i.priority, i.issue_type,
                i.description, i.created_at, i.updated_at,
                i.assignee, i.estimated_minutes, i.close_reason,
                GROUP_CONCAT(DISTINCT l.label ORDER BY l.label SEPARATOR ',') AS labels,
                GROUP_CONCAT(DISTINCT di.id ORDER BY di.id SEPARATOR ',') AS open_blocker_ids,
                GROUP_CONCAT(DISTINCT di.title ORDER BY di.id SEPARATOR '\x1f') AS open_blocker_titles
            FROM issues i
            JOIN labels la ON la.issue_id = i.id AND la.label = %s
            JOIN dependencies d ON d.issue_id = i.id AND d.type != %s
            JOIN issues di ON di.id = d.depends_on_id AND di.status != %s
            LEFT JOIN labels l ON l.issue_id = i.id
            WHERE i.status = %s
            GROUP BY i.id
            ORDER BY i.priority ASC, i.updated_at DESC
            """,
            ("readiness:approved", "parent-child", "closed", "open"),
        )
        approved_blocked_raw = _rows(cur)

    # Post-process blocked: split the unit-separator-delimited blocker titles
    approved_blocked = []
    for row in approved_blocked_raw:
        row = _coerce(row)
        ids = row.pop("open_blocker_ids", None) or ""
        titles = row.pop("open_blocker_titles", None) or ""
        id_list = ids.split(",") if ids else []
        title_list = titles.split("\x1f") if titles else []
        row["open_blockers"] = [
            {"id": bid, "title": t}
            for bid, t in zip(id_list, title_list)
        ]
        approved_blocked.append(row)

    return {
        "approved_waiting": approved_waiting,
        "approved_blocked": approved_blocked,
    }


def get_bead_title_priority(bead_ids: list[str]) -> dict[str, dict]:
    """Return a mapping of bead_id → {id, title, priority, labels} for the given IDs.

    Used to enrich SQLite dispatch_runs rows with Dolt bead metadata.
    Missing bead IDs are silently omitted from the result.
    """
    if not bead_ids:
        return {}
    conn = _get_conn()
    placeholders = ", ".join(["%s"] * len(bead_ids))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT i.id, i.title, i.priority,
                   GROUP_CONCAT(l.label ORDER BY l.label SEPARATOR ',') AS labels
            FROM issues i
            LEFT JOIN labels l ON l.issue_id = i.id
            WHERE i.id IN ({placeholders})
            GROUP BY i.id
            """,
            tuple(bead_ids),
        )
        rows = _rows(cur)
    return {r["id"]: _coerce(r) for r in rows}


def get_bead(bead_id: str) -> dict | None:
    """Return a single bead with its labels, deps, and comments.

    Returns None if the bead does not exist.
    """
    conn = _get_conn()
    with conn.cursor() as cur:

        # Main bead row with all text fields
        cur.execute(
            """
            SELECT
                i.id, i.title, i.status, i.priority, i.issue_type,
                i.description, i.design, i.acceptance_criteria, i.notes,
                i.created_at, i.updated_at, i.closed_at,
                i.assignee, i.estimated_minutes, i.close_reason,
                i.created_by, i.owner,
                GROUP_CONCAT(l.label ORDER BY l.label SEPARATOR ',') AS labels
            FROM issues i
            LEFT JOIN labels l ON l.issue_id = i.id
            WHERE i.id = %s
            GROUP BY i.id
            """,
            (bead_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        bead = _coerce(row)

        # Dependencies with dep bead metadata
        cur.execute(
            """
            SELECT d.depends_on_id AS id, d.type,
                   di.title, di.status, di.priority
            FROM dependencies d
            JOIN issues di ON di.id = d.depends_on_id
            WHERE d.issue_id = %s
            ORDER BY d.type, d.depends_on_id
            """,
            (bead_id,),
        )
        bead["deps"] = [_coerce(r) for r in _rows(cur)]

        # Comments oldest-first
        cur.execute(
            """
            SELECT id, author, text, created_at
            FROM comments
            WHERE issue_id = %s
            ORDER BY created_at ASC
            """,
            (bead_id,),
        )
        bead["comments"] = [_coerce(r) for r in _rows(cur)]

    return bead


def get_open_beads(limit: int = 200) -> list[dict]:
    """Return the working set — all beads that are not closed.

    Ordered by priority ASC (lower number = higher priority), then
    updated_at DESC within the same priority.
    """
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_BEAD_COLS}
            FROM issues i
            LEFT JOIN labels l ON l.issue_id = i.id
            WHERE i.status != %s
            GROUP BY i.id
            ORDER BY i.priority ASC, i.updated_at DESC
            LIMIT %s
            """,
            ("closed", limit),
        )
        return [_coerce(r) for r in _rows(cur)]


def get_bead_counts() -> dict[str, int]:
    """Return lightweight counts for nav badges and dashboard header.

    Returns:
        open_count            — beads with status='open'
        in_progress_count     — beads with status='in_progress'
        approved_count        — open beads with readiness:approved label
        approved_blocked_count — approved open beads with at least one open blocker
        total_open_count      — all non-closed beads
    """
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                SUM(CASE WHEN i.status = %s THEN 1 ELSE 0 END)
                    AS open_count,
                SUM(CASE WHEN i.status = %s THEN 1 ELSE 0 END)
                    AS in_progress_count,
                SUM(CASE WHEN EXISTS (
                    SELECT 1 FROM labels l
                    WHERE l.issue_id = i.id AND l.label = %s
                ) THEN 1 ELSE 0 END)
                    AS approved_count,
                SUM(CASE WHEN i.status = %s
                    AND EXISTS (
                        SELECT 1 FROM labels la
                        WHERE la.issue_id = i.id AND la.label = %s
                    )
                    AND EXISTS (
                        SELECT 1 FROM dependencies d
                        JOIN issues di ON di.id = d.depends_on_id
                        WHERE d.issue_id = i.id
                          AND d.type != %s
                          AND di.status != %s
                    )
                    THEN 1 ELSE 0 END)
                    AS approved_blocked_count,
                COUNT(*)
                    AS total_open_count
            FROM issues i
            WHERE i.status != %s
            """,
            ("open", "in_progress", "readiness:approved",
             "open", "readiness:approved", "parent-child", "closed",
             "closed"),
        )
        row = cur.fetchone()

    if not row:
        return {
            "open_count": 0,
            "in_progress_count": 0,
            "approved_count": 0,
            "approved_blocked_count": 0,
            "total_open_count": 0,
        }
    return {k: int(v or 0) for k, v in row.items()}
