"""Sessions DAO — active filesystem scan + recent sessions from graph.db."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

_GRAPH_DB = Path(__file__).parents[3] / "data" / "graph.db"


def get_active_sessions(threshold: int = 600) -> list[dict]:
    """Find active Claude Code sessions (JSONL files modified within threshold seconds).

    Scans two locations:
    - ~/.claude/projects/ — host interactive sessions
    - data/agent-runs/*/sessions/ — container sessions (terminal, dispatch, chatwith)
    """
    scan_dirs = [Path.home() / ".claude" / "projects"]
    # Add container session directories
    agent_runs = Path(__file__).parents[3] / "data" / "agent-runs"
    if agent_runs.exists():
        for run_dir in agent_runs.iterdir():
            sess_dir = run_dir / "sessions"
            if sess_dir.is_dir():
                scan_dirs.append(sess_dir)

    now = time.time()
    sessions = []
    seen_ids: set[str] = set()  # dedupe by session_id

    for projects_dir in scan_dirs:
        if not projects_dir.exists():
            continue
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

                    sid = jsonl.stem
                    if sid in seen_ids:
                        continue
                    seen_ids.add(sid)
                    entry = {
                        "session_id": sid,
                        "project": jsonl.parent.name,
                        "size_bytes": stat.st_size,
                        "age_seconds": round(age),
                        "active": age < 60,
                        "latest": latest,
                    }
                    # Read tmux_session and type from .session_meta.json if available
                    meta_path = jsonl.parent.parent / ".session_meta.json"
                    if meta_path.exists():
                        try:
                            meta = json.loads(meta_path.read_text())
                            if meta.get("tmux_session"):
                                entry["tmux_session"] = meta["tmux_session"]
                            entry["type"] = meta.get("type", "host")
                        except (json.JSONDecodeError, OSError):
                            entry["type"] = "host"
                    else:
                        entry["type"] = "host"
                    sessions.append(entry)
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
                "project": f"[{r['project']}]" if r["project"] else "",
            }
            for r in rows
        ]
    except Exception:
        return []
