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
    """Fetch recent dead sessions — dashboard.db primary, graph.db backfill.

    dashboard.db has rich metadata (label, topics, entry_count, tokens) but
    only contains sessions registered with the session_monitor.  graph.db has
    the full historical corpus (~2K sessions) but sparse metadata.  We merge:
    dashboard.db rows first (sorted by last_activity), then fill remaining
    slots from graph.db for sessions not in dashboard.db.
    """
    import json as _json
    from tools.dashboard.dao.dashboard_db import get_conn

    results = []
    seen_paths: set[str] = set()
    seen_uuids: set[str] = set()

    # ── Primary: dashboard.db dead sessions (rich metadata) ─────────
    try:
        conn = get_conn()
        rows = conn.execute(
            "SELECT * FROM tmux_sessions"
            " WHERE is_live = 0"
            " ORDER BY last_activity DESC LIMIT ?",
            (limit,),
        ).fetchall()
        for row in rows:
            r = dict(row)
            tmux_name = r["tmux_name"]
            jsonl_path = r.get("jsonl_path") or ""
            session_type = r.get("type") or "container"
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
            if jsonl_path:
                seen_paths.add(jsonl_path)
            if r.get("session_uuid"):
                seen_uuids.add(r["session_uuid"])
    except Exception:
        pass

    # ── Backfill: graph.db sessions not in dashboard.db ─────────────
    remaining = limit - len(results)
    if remaining > 0 and _GRAPH_DB.exists():
        # Collect live session identifiers to exclude
        live_uuids: set[str] = set()
        live_paths: set[str] = set()
        try:
            for ls in _db_live_sessions():
                if ls.get("session_uuid"):
                    live_uuids.add(ls["session_uuid"])
                if ls.get("jsonl_path"):
                    live_paths.add(ls["jsonl_path"])
        except Exception:
            pass

        try:
            gconn = sqlite3.connect(str(_GRAPH_DB))
            gconn.row_factory = sqlite3.Row
            grows = gconn.execute(
                "SELECT id, type, project, title, created_at, file_path, metadata"
                " FROM sources WHERE type = 'session'"
                " ORDER BY created_at DESC LIMIT ?",
                (limit * 3,),  # over-fetch to account for dedup filtering
            ).fetchall()
            gconn.close()

            for gr in grows:
                if len(results) >= limit:
                    break
                meta = {}
                if gr["metadata"]:
                    try:
                        meta = _json.loads(gr["metadata"])
                    except Exception:
                        pass
                file_path = gr["file_path"] or ""
                session_uuid = meta.get("session_uuid", "")

                # Skip if already in dashboard.db results or currently live
                if file_path and file_path in seen_paths:
                    continue
                if session_uuid and session_uuid in seen_uuids:
                    continue
                if session_uuid and session_uuid in live_uuids:
                    continue
                if file_path and file_path in live_paths:
                    continue

                card_type = _derive_session_type(meta, file_path)
                results.append({
                    "session_id": gr["id"],
                    "tmux_session": meta.get("container_name", gr["id"][:12]),
                    "label": gr["title"] or "",
                    "session_type": card_type,
                    "type": "container",
                    "is_live": False,
                    "project": gr["project"] or "",
                    "topics": [],
                    "role": "",
                    "entry_count": meta.get("total_turns", 0),
                    "context_tokens": meta.get("total_input_tokens", 0) + meta.get("total_output_tokens", 0),
                    "last_activity": 0,
                    "created_at": gr["created_at"] or "",
                    "bead_id": meta.get("bead_id", ""),
                    "graph_source_id": gr["id"],
                    "session_uuid": session_uuid,
                    "file_path": file_path,
                    "resumable": bool(file_path and Path(file_path).exists()),
                    "activity_state": "dead",
                })
        except Exception:
            pass

    return results
