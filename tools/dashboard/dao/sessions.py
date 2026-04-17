"""Sessions DAO — dashboard.db backed + recent sessions from graph.db."""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

from tools.dashboard.dao.dashboard_db import get_live_sessions as _db_live_sessions
from tools.dashboard.dao.dashboard_db import find_live_session as _db_find_live

logger = logging.getLogger(__name__)

_GRAPH_DB = Path(__file__).parents[3] / "data" / "graph.db"


def get_active_sessions(threshold: int = 600) -> list[dict]:
    """Return active sessions from dashboard.db.

    Replaces the old filesystem-scanning approach. Returns live sessions
    from the DB with the same dict shape that the old function produced.
    """
    now = time.time()
    db_rows = _db_live_sessions()
    sessions = []
    for row in db_rows:
        age = now - (row.get("last_activity") or row["created_at"])
        sessions.append({
            "session_id": row.get("session_uuid") or row["tmux_name"],
            "project": row["project"],
            "size_bytes": 0,  # not tracked per-row cheaply; SSE has live data
            "age_seconds": round(age),
            "active": age < 60,
            "latest": row.get("last_message", ""),
            "type": row["type"],
            "tmux_session": row["tmux_name"],
            "bead_id": row.get("bead_id"),
        })
    sessions.sort(key=lambda s: s["age_seconds"])
    return sessions


def _derive_session_type(meta: dict, file_path: str) -> str:
    """Derive session type from metadata or file path heuristics."""
    if meta.get("session_type"):
        return meta["session_type"]
    if meta.get("bead_id"):
        return "dispatch"
    if "agent-runs" in file_path:
        return "dispatch"
    if meta.get("role") == "librarian" or "librarian" in file_path:
        return "librarian"
    return "interactive"


def get_recent_sessions(limit: int = 20) -> list[dict]:
    """Fetch recently-dead sessions from dashboard.db.

    Returns the same field shape as active session cards so the frontend
    can render them with the same partial.  Falls back to graph.db only
    for sessions that predate dashboard.db tracking.
    """
    import json as _json
    from tools.dashboard.dao.dashboard_db import get_conn

    try:
        conn = get_conn()
        rows = conn.execute(
            "SELECT * FROM tmux_sessions"
            " WHERE is_live = 0"
            " ORDER BY last_activity DESC LIMIT ?",
            (limit,),
        ).fetchall()
    except Exception:
        return []

    results = []
    for row in rows:
        r = dict(row)
        tmux_name = r["tmux_name"]
        jsonl_path = r.get("jsonl_path") or ""
        session_type = r.get("type") or "container"
        # Map dashboard type to the card's session_type vocabulary
        if r.get("bead_id"):
            card_type = "dispatch"
        elif session_type == "host":
            card_type = "interactive"
        else:
            card_type = "terminal"

        topics = []
        try:
            topics = _json.loads(r.get("topics") or "[]")
        except Exception:
            pass

        results.append({
            "session_id": tmux_name,
            "tmux_session": tmux_name,
            "label": r.get("label") or "",
            "session_type": card_type,
            "type": session_type,
            "is_live": False,
            "project": r.get("project") or "",
            "topics": topics,
            "role": r.get("role") or "",
            "entry_count": r.get("entry_count") or 0,
            "context_tokens": r.get("context_tokens") or 0,
            "last_activity": r.get("last_activity") or r.get("created_at") or 0,
            "created_at": r.get("created_at") or 0,
            "bead_id": r.get("bead_id") or "",
            "graph_source_id": r.get("graph_source_id") or "",
            "session_uuid": r.get("session_uuid") or "",
            "file_path": jsonl_path,
            "resumable": bool(jsonl_path and Path(jsonl_path).exists()),
            "activity_state": r.get("activity_state") or "dead",
        })
    return results
