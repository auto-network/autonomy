"""Sessions DAO — active filesystem scan + recent sessions from graph.db."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

_GRAPH_DB = Path(__file__).parents[3] / "data" / "graph.db"


def get_active_sessions(threshold: int = 600) -> list[dict]:
    """Find active Claude Code sessions (JSONL files modified within threshold seconds)."""
    projects_dir = Path.home() / ".claude" / "projects"
    now = time.time()
    sessions = []

    if not projects_dir.exists():
        return sessions

    for jsonl in projects_dir.rglob("*.jsonl"):
        try:
            stat = jsonl.stat()
            age = now - stat.st_mtime
            if age < threshold:
                last_chunk = ""
                with open(jsonl, "rb") as f:
                    f.seek(max(0, stat.st_size - 2000))
                    last_chunk = f.read().decode("utf-8", errors="replace")

                latest = ""
                for line in reversed(last_chunk.strip().split("\n")):
                    try:
                        e = json.loads(line)
                        if e.get("type") in ("user", "assistant") and not e.get("isSidechain"):
                            msg = e.get("message", {})
                            content = msg.get("content", "")
                            if isinstance(content, str) and len(content) > 5:
                                latest = content[:150]
                                break
                            elif isinstance(content, list):
                                for c in content:
                                    if isinstance(c, dict) and c.get("type") == "text":
                                        latest = c["text"][:150]
                                        break
                                if latest:
                                    break
                    except json.JSONDecodeError:
                        continue

                sessions.append({
                    "session_id": jsonl.stem,
                    "project": jsonl.parent.name,
                    "size_bytes": stat.st_size,
                    "age_seconds": round(age),
                    "active": age < 60,
                    "latest": latest,
                })
        except OSError:
            continue

    sessions.sort(key=lambda s: s["age_seconds"])
    return sessions


def get_recent_sessions(limit: int = 20) -> list[dict]:
    """Fetch recent session sources from graph.db."""
    if not _GRAPH_DB.exists():
        return []
    try:
        conn = sqlite3.connect(str(_GRAPH_DB))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, type, project, title, created_at FROM sources"
            " WHERE type = 'session' ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()
        return [
            {
                "id": r["id"],
                "type": r["type"],
                "date": (r["created_at"] or "")[:10],
                "title": r["title"] or "",
                "project": f"[{r['project']}]" if r.get("project") else "",
            }
            for r in rows
        ]
    except Exception:
        return []
