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
                    sessions.append({
                        "session_id": sid,
                        "project": jsonl.parent.name,
                        "size_bytes": stat.st_size,
                        "age_seconds": round(age),
                        "active": age < 60,
                        "latest": latest,
                    })
            except OSError:
                continue

    sessions.sort(key=lambda s: s["age_seconds"])

    # Attach available tmux sessions for input support
    _attach_tmux_sessions(sessions)

    return sessions


def _attach_tmux_sessions(sessions: list[dict]) -> None:
    """Best-effort: find tmux sessions running claude/docker and attach to
    active sessions. When we can't determine which tmux maps to which JSONL,
    attach all available tmux names so the viewer can auto-detect."""
    import subprocess
    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode != 0:
            return
        tmux_names = [n.strip() for n in result.stdout.strip().split("\n") if n.strip()]

        claude_tmux = []
        for name in tmux_names:
            try:
                cmd_result = subprocess.run(
                    ["tmux", "display-message", "-p", "-t", name, "#{pane_current_command}"],
                    capture_output=True, text=True, timeout=2,
                )
                pane_cmd = cmd_result.stdout.strip().lower()
                if cmd_result.returncode == 0 and pane_cmd in ("claude", "docker"):
                    claude_tmux.append({"name": name, "cmd": pane_cmd})
            except Exception:
                continue

        if not claude_tmux:
            return

        # Simple 1:1 match
        if len(claude_tmux) == 1 and len(sessions) == 1:
            sessions[0]["tmux_session"] = claude_tmux[0]["name"]
        else:
            # Can't determine mapping — attach all available to each session
            all_names = [t["name"] for t in claude_tmux]
            for s in sessions:
                s["tmux_sessions_available"] = all_names
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
