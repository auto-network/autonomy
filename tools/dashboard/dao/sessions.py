"""Sessions DAO — dashboard.db backed + recent sessions from graph.db."""

from __future__ import annotations

import json as _json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from tools.dashboard.dao.dashboard_db import get_live_sessions as _db_live_sessions
from tools.dashboard.dao.dashboard_db import find_live_session as _db_find_live
from tools.dashboard.dao.dashboard_db import get_all_sessions as _db_all_sessions
from tools.dashboard.org_identity import resolve_session_org
from tools.graph.duration import parse_duration

logger = logging.getLogger(__name__)

_GRAPH_DB = Path(__file__).parents[3] / "data" / "graph.db"

# Dropdown values → seconds. `all` disables the filter. Keys match the
# `recent_sessions?since=` query param and the `graph sessions --status --since`
# CLI (auto-0r86); see design acb2829b-4fc0 revision b39626f2.
_SINCE_WINDOWS = {"6h": "6h", "1d": "1d", "1w": "1w"}
_VALID_RECENT_SORTS = {"lastActivity", "created", "turns", "ctx"}


def _iso_to_epoch(ts: str | None) -> float:
    """Parse ISO 8601 timestamp to a unix epoch float; returns 0.0 on failure."""
    if not ts:
        return 0.0
    try:
        # Strip trailing Z or fractional seconds, support both forms
        normalized = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).timestamp()
    except (ValueError, TypeError):
        return 0.0


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
        entry = {
            "session_id": row.get("session_uuid") or row["tmux_name"],
            "project": row["project"],
            "size_bytes": 0,  # not tracked per-row cheaply; SSE has live data
            "age_seconds": round(age),
            "active": age < 60,
            "latest": row.get("last_message", ""),
            "type": row["type"],
            "tmux_session": row["tmux_name"],
            "bead_id": row.get("bead_id"),
            "activity_state": row.get("activity_state", "idle"),
        }
        entry["org"] = resolve_session_org(entry)
        sessions.append(entry)
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


def _graph_sources_have_last_activity_column(conn: sqlite3.Connection) -> bool:
    """Whether the sources table has the last_activity_at column yet."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sources)").fetchall()}
    return "last_activity_at" in cols


def get_recent_sessions(
    limit: int = 20,
    sort: str = "lastActivity",
    since: str = "1d",
) -> list[dict]:
    """Fetch recent session sources, ordered and filtered for the Recent list.

    Args:
      limit: cap rows returned after sorting.
      sort:  one of ``lastActivity|created|turns|ctx`` (design acb2829b-4fc0).
             Falls back to ``lastActivity`` on unknown values.
      since: ``6h|1d|1w|all``. Parsed via ``tools.graph.duration.parse_duration``;
             rows whose most-recent activity is older than ``now() - dur`` are
             dropped. ``all`` (or unknown) disables the filter. Matches the
             ``graph sessions --status --since`` CLI (auto-0r86).

    Strategy:
      1. Pull recent session rows from graph.db (the historical tail).
      2. Overlay rows from dashboard.db.tmux_sessions on top — for sessions
         that registered with the session monitor, dashboard.db has richer
         metadata (label, entry_count, context_tokens, role) than graph.db.
      3. Filter out currently-live sessions (they belong on Active list).
      4. Apply the ``since`` window, then sort by the requested column, then
         return the top *limit*.
    """
    if not _GRAPH_DB.exists():
        return []

    if sort not in _VALID_RECENT_SORTS:
        sort = "lastActivity"

    # Compute the since cutoff once (epoch seconds). ``all`` / unknown → None.
    since_cutoff: float | None = None
    if since and since != "all":
        dur_str = _SINCE_WINDOWS.get(since, since)
        try:
            since_cutoff = time.time() - parse_duration(dur_str)
        except ValueError:
            since_cutoff = None

    # ── Step 1: pull from graph.db ────────────────────────────────
    graph_rows: list[dict] = []
    try:
        conn = sqlite3.connect(str(_GRAPH_DB))
        conn.row_factory = sqlite3.Row
        has_la = _graph_sources_have_last_activity_column(conn)
        # Order by activity if available; oversample so the dashboard-overlay
        # step can replace stale graph titles with rich dashboard data
        # without truncating the candidate set, and so non-activity sorts
        # (turns / ctx) have enough rows to re-rank against.
        sample_limit = max(limit * 10, 200) if sort in ("turns", "ctx") else max(limit * 5, 100)
        if has_la:
            sql = (
                "SELECT id, type, project, title, created_at, last_activity_at,"
                " file_path, metadata FROM sources"
                " WHERE type = 'session'"
                " ORDER BY COALESCE(last_activity_at, created_at) DESC LIMIT ?"
            )
        else:
            sql = (
                "SELECT id, type, project, title, created_at, NULL as last_activity_at,"
                " file_path, metadata FROM sources"
                " WHERE type = 'session' ORDER BY created_at DESC LIMIT ?"
            )
        rows = conn.execute(sql, (sample_limit,)).fetchall()
        conn.close()
        for r in rows:
            meta: dict = {}
            if r["metadata"]:
                try:
                    meta = _json.loads(r["metadata"])
                except Exception:
                    pass
            file_path = r["file_path"] or ""
            session_uuid = meta.get("session_uuid", "")
            last_activity = r["last_activity_at"] or meta.get("ended_at") or r["created_at"] or ""
            graph_rows.append({
                "id": r["id"],
                "type": r["type"],
                "title": r["title"] or "",
                "project": r["project"] or "",
                "session_uuid": session_uuid,
                "file_path": file_path,
                "session_type": _derive_session_type(meta, file_path),
                "total_tokens": meta.get("total_input_tokens", 0) + meta.get("total_output_tokens", 0),
                "total_turns": meta.get("total_turns", 0),
                "created_at": r["created_at"] or "",
                "last_activity_at": last_activity,
                "ended_at": meta.get("ended_at") or last_activity,
                "bead_id": meta.get("bead_id", ""),
                "_source": "graph",
            })
    except Exception:
        return []

    # ── Step 2: overlay dashboard.db ──────────────────────────────
    # dashboard.db owns label, entry_count, context_tokens, role for any
    # session that registered with the monitor. Index by session_uuid AND
    # jsonl_path so we can match either way.
    db_by_uuid: dict[str, dict] = {}
    db_by_path: dict[str, dict] = {}
    live_uuids: set[str] = set()
    live_paths: set[str] = set()
    try:
        for row in _db_all_sessions():
            if row.get("session_uuid"):
                db_by_uuid[row["session_uuid"]] = row
            if row.get("jsonl_path"):
                db_by_path[row["jsonl_path"]] = row
            if row.get("is_live"):
                if row.get("session_uuid"):
                    live_uuids.add(row["session_uuid"])
                if row.get("jsonl_path"):
                    live_paths.add(row["jsonl_path"])
    except Exception:
        pass  # dashboard.db not initialised — skip overlay

    # ── Step 3: merge + filter live ───────────────────────────────
    merged: dict[str, dict] = {}
    for row in graph_rows:
        # Filter out currently-live sessions
        if row["session_uuid"] and row["session_uuid"] in live_uuids:
            continue
        if row["file_path"] and row["file_path"] in live_paths:
            continue

        db_row = (db_by_uuid.get(row["session_uuid"])
                  or db_by_path.get(row["file_path"]))
        if db_row:
            # dashboard.db wins on user-curated fields (label, role, topics)
            # and live-tail counters (entry_count, context_tokens). graph.db
            # wins on token totals + structural metadata.
            label = (db_row.get("label") or "").strip()
            if label:
                row["title"] = label
            row["role"] = db_row.get("role", "")
            row["entry_count"] = db_row.get("entry_count", 0) or row["total_turns"]
            row["context_tokens"] = db_row.get("context_tokens", 0)
            row["activity_state"] = db_row.get("activity_state", "dead")
            row["bead_id"] = row["bead_id"] or db_row.get("bead_id", "")
            row["tmux_session"] = db_row.get("tmux_name", "")
            # Prefer dashboard.db's last_activity (epoch float) for ordering
            # when newer than graph.db's last_activity_at (ISO).
            db_la = db_row.get("last_activity") or 0
            graph_la_epoch = _iso_to_epoch(row["last_activity_at"])
            if db_la and db_la > graph_la_epoch:
                row["last_activity_at"] = datetime.fromtimestamp(
                    db_la, tz=timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
                # Dead session's last activity IS its end time — keep ended_at in sync
                row["ended_at"] = row["last_activity_at"]
            row["_source"] = "merged"

        # Resumable: JSONL still exists on disk
        row["resumable"] = bool(row["file_path"] and Path(row["file_path"]).exists())
        # date for backwards compat
        row["date"] = (row["last_activity_at"] or row["created_at"] or "")[:10]
        # Resolve org identity from the full row (carries session_type) BEFORE bracket-wrap
        row["org"] = resolve_session_org(row)
        # Wrap project in brackets for backwards-compat with the existing UI
        row["project"] = f"[{row['project']}]" if row["project"] else ""

        merged[row["id"]] = row

    # ── Step 4: apply `since` window, sort, take top N ────────────
    rows = list(merged.values())
    if since_cutoff is not None:
        rows = [
            r for r in rows
            if _iso_to_epoch(r.get("last_activity_at") or r.get("created_at", "")) >= since_cutoff
        ]

    if sort == "created":
        rows.sort(key=lambda r: _iso_to_epoch(r.get("created_at", "")), reverse=True)
    elif sort == "turns":
        rows.sort(
            key=lambda r: r.get("entry_count") or r.get("total_turns") or 0,
            reverse=True,
        )
    elif sort == "ctx":
        rows.sort(
            key=lambda r: r.get("context_tokens") or r.get("total_tokens") or 0,
            reverse=True,
        )
    else:  # "lastActivity" (default)
        rows.sort(
            key=lambda r: _iso_to_epoch(r.get("last_activity_at", "")),
            reverse=True,
        )
    out = rows[:limit]

    # Strip internal field
    for r in out:
        r.pop("_source", None)
    return out
