"""Sessions DAO — active filesystem scan + recent sessions from graph.db."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from pathlib import Path

_GRAPH_DB = Path(__file__).parents[3] / "data" / "graph.db"


def _read_latest_msg(jsonl: Path, stat) -> str:
    """Extract the latest user or assistant text from a JSONL session file."""
    last_chunk = ""
    try:
        with open(jsonl, "rb") as f:
            f.seek(max(0, stat.st_size - 2000))
            last_chunk = f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    for line in reversed(last_chunk.strip().split("\n")):
        try:
            e = json.loads(line)
            if e.get("type") in ("user", "assistant") and not e.get("isSidechain"):
                msg = e.get("message", {})
                content = msg.get("content", "")
                if isinstance(content, str) and len(content) > 5:
                    return content[:150]
                if isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            return c["text"][:150]
        except json.JSONDecodeError:
            continue
    return ""


def _project_folder_to_path(folder_name: str) -> str:
    """Convert Claude project folder name to filesystem path.

    e.g. '-workspace-repo'        -> '/workspace/repo'
         '-home-user-workspace'   -> '/home/user/workspace'
    """
    return folder_name.replace("-", "/", 1).replace("-", "/")


def _enrich_tmux_sessions(entries: list[dict]) -> None:
    """Match entries to live tmux sessions by pane cwd. Sets tmux_session in-place."""
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{session_name}\t#{pane_current_path}"],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode != 0:
            return
        pane_cwds: list[tuple[str, str]] = []
        for line in result.stdout.strip().split("\n"):
            if "\t" in line:
                name, cwd = line.split("\t", 1)
                pane_cwds.append((name.strip(), cwd.strip()))
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return

    for entry in entries:
        if entry.get("tmux_session"):
            continue
        project_path = _project_folder_to_path(entry["project"])
        for tmux_name, pane_cwd in pane_cwds:
            if pane_cwd and (
                pane_cwd == project_path or pane_cwd.startswith(project_path + "/")
            ):
                entry["tmux_session"] = tmux_name
                break


def get_active_sessions(threshold: int = 600) -> list[dict]:
    """Find active Claude Code sessions.

    Scans two locations:
    - ~/.claude/projects/ — host interactive sessions (tmux-first liveness)
    - data/agent-runs/*/sessions/ — container sessions (mtime threshold only)

    For host sessions: a session is included if it was recently modified (age <
    threshold) OR a live tmux session's pane cwd maps to its project directory.

    For container sessions: strict mtime threshold applies (no tmux).
    """
    now = time.time()
    sessions: list[dict] = []
    seen_ids: set[str] = set()

    # ── Host sessions: ~/.claude/projects/ ─────────────────────────────────
    home_projects = Path.home() / ".claude" / "projects"
    host_entries: list[dict] = []
    if home_projects.exists():
        for jsonl in home_projects.rglob("*.jsonl"):
            try:
                stat = jsonl.stat()
                age = now - stat.st_mtime
                sid = jsonl.stem
                if sid in seen_ids:
                    continue
                seen_ids.add(sid)
                latest = _read_latest_msg(jsonl, stat)
                host_entries.append({
                    "session_id": sid,
                    "project": jsonl.parent.name,
                    "size_bytes": stat.st_size,
                    "age_seconds": round(age),
                    "active": age < 60,
                    "latest": latest,
                    "type": "host",
                })
            except OSError:
                continue

    # Enrich with live tmux session names matched by pane cwd
    _enrich_tmux_sessions(host_entries)

    # Include if tmux-alive (regardless of idle time) OR recently active
    for entry in host_entries:
        if entry.get("tmux_session") or entry["age_seconds"] < threshold:
            sessions.append(entry)

    # ── Container sessions: data/agent-runs/*/sessions/ ────────────────────
    agent_runs = Path(__file__).parents[3] / "data" / "agent-runs"
    if agent_runs.exists():
        for run_dir in agent_runs.iterdir():
            sess_dir = run_dir / "sessions"
            if not sess_dir.is_dir():
                continue
            for jsonl in sess_dir.rglob("*.jsonl"):
                try:
                    stat = jsonl.stat()
                    age = now - stat.st_mtime
                    if age >= threshold:
                        continue
                    sid = jsonl.stem
                    if sid in seen_ids:
                        continue
                    seen_ids.add(sid)
                    latest = _read_latest_msg(jsonl, stat)
                    entry: dict = {
                        "session_id": sid,
                        "project": jsonl.parent.name,
                        "size_bytes": stat.st_size,
                        "age_seconds": round(age),
                        "active": age < 60,
                        "latest": latest,
                        "type": "host",
                    }
                    meta_path = jsonl.parent.parent / ".session_meta.json"
                    if meta_path.exists():
                        try:
                            meta = json.loads(meta_path.read_text())
                            if meta.get("tmux_session"):
                                entry["tmux_session"] = meta["tmux_session"]
                            entry["type"] = meta.get("type", "host")
                        except (json.JSONDecodeError, OSError):
                            pass
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
