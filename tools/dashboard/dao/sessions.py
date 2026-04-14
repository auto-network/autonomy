"""Sessions DAO — dashboard.db backed + recent sessions from graph.db."""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

from tools.dashboard.dao.dashboard_db import get_live_sessions as _db_live_sessions

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
    """Fetch recent session sources from graph.db.

    Returns enriched dicts with session_uuid, file_path, and resumable
    (whether the JSONL file still exists on disk) so the frontend can
    show/hide a Resume button.
    """
    if not _GRAPH_DB.exists():
        return []
    try:
        conn = sqlite3.connect(str(_GRAPH_DB))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, type, project, title, created_at, file_path, metadata"
            " FROM sources"
            " WHERE type = 'session' ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()
        import json as _json
        results = []
        for r in rows:
            meta = {}
            if r["metadata"]:
                try:
                    meta = _json.loads(r["metadata"])
                except Exception:
                    pass
            file_path = r["file_path"] or ""
            session_uuid = meta.get("session_uuid", "")
            session_type = _derive_session_type(meta, file_path)
            results.append({
                "id": r["id"],
                "type": r["type"],
                "date": (r["created_at"] or "")[:10],
                "title": r["title"] or "",
                "project": f"[{r['project']}]" if r["project"] else "",
                "session_uuid": session_uuid,
                "file_path": file_path,
                "resumable": bool(file_path and Path(file_path).exists()),
                "session_type": session_type,
                "total_tokens": meta.get("total_tokens", 0),
                "total_turns": meta.get("total_turns", 0),
                "created_at": r["created_at"] or "",
            })
        return results
    except Exception:
        return []
