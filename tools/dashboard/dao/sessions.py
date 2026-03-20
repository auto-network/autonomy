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

    # Enrich with tmux session names for input support
    _enrich_tmux_names(sessions)

    return sessions


def _enrich_tmux_names(sessions: list[dict]) -> None:
    """Best-effort: match active sessions to tmux sessions by checking
    which tmux panes are running claude and writing to matching project dirs."""
    import subprocess
    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode != 0:
            return
        tmux_names = [n.strip() for n in result.stdout.strip().split("\n") if n.strip()]

        for name in tmux_names:
            try:
                cmd_result = subprocess.run(
                    ["tmux", "display-message", "-p", "-t", name, "#{pane_current_command}"],
                    capture_output=True, text=True, timeout=2,
                )
                if cmd_result.returncode != 0 or "claude" not in cmd_result.stdout.lower():
                    continue
                # This tmux session runs claude — try to match to an active session
                # by checking the pane's cwd
                cwd_result = subprocess.run(
                    ["tmux", "display-message", "-p", "-t", name, "#{pane_current_path}"],
                    capture_output=True, text=True, timeout=2,
                )
                if cwd_result.returncode != 0:
                    continue
                pane_cwd = cwd_result.stdout.strip()
                # Match by project name in the session's project field
                for s in sessions:
                    if not s.get("tmux_session"):
                        project_path = s["project"].replace("-", "/").lstrip("/")
                        if project_path and project_path in pane_cwd:
                            s["tmux_session"] = name
                            break
            except Exception:
                continue
    except (FileNotFoundError, Exception):
        pass


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
