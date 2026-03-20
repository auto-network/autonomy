"""Sessions DAO — active filesystem scan + recent sessions from graph.db."""

from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

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


def _write_host_session_meta(jsonl_path: Path, tmux_name: str) -> None:
    """Persist the matched tmux session name alongside the JSONL file."""
    meta = jsonl_path.with_suffix(".meta.json")
    try:
        meta.write_text(json.dumps({"tmux_session": tmux_name}))
        logger.info("Wrote meta %s  tmux=%s", meta, tmux_name)
    except OSError:
        logger.warning("Failed to write meta %s", meta, exc_info=True)


def get_active_sessions(threshold: int = 600) -> list[dict]:
    """Find active Claude Code sessions.

    Scans two locations:
    - ~/.claude/projects/ — host interactive sessions (tmux-first liveness)
    - data/agent-runs/*/sessions/ — container sessions (mtime threshold only)

    For host sessions: a session is included if it has a live tmux session
    (via .meta.json written by the JSONL watcher) OR was recently modified
    (age < threshold).

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
                entry: dict = {
                    "session_id": sid,
                    "project": jsonl.parent.name,
                    "size_bytes": stat.st_size,
                    "age_seconds": round(age),
                    "active": age < 60,
                    "latest": latest,
                    "type": "host",
                    "_jsonl_path": jsonl,
                }
                # Load persisted tmux session name from meta file if present
                meta_file = jsonl.with_suffix(".meta.json")
                if meta_file.exists():
                    try:
                        meta = json.loads(meta_file.read_text())
                        if meta.get("tmux_session"):
                            entry["tmux_session"] = meta["tmux_session"]
                    except (json.JSONDecodeError, OSError):
                        pass
                host_entries.append(entry)
            except OSError:
                continue

    # Include if tmux-alive (regardless of idle time) OR recently active
    for entry in host_entries:
        tmux = entry.get("tmux_session")
        if tmux:
            # Verify the tmux session is still alive
            try:
                alive = subprocess.run(
                    ["tmux", "has-session", "-t", tmux],
                    capture_output=True,
                ).returncode == 0
            except (FileNotFoundError, OSError):
                alive = False
            if alive:
                sessions.append(entry)
                continue
            # Dead tmux — clear stale meta so it doesn't block re-match
            meta_file = entry.get("_jsonl_path")
            if meta_file is not None:
                stale = Path(meta_file).with_suffix(".meta.json")
                try:
                    stale.unlink(missing_ok=True)
                except OSError:
                    pass
            entry.pop("tmux_session", None)
        if entry["age_seconds"] < threshold:
            sessions.append(entry)

    # Strip internal-only fields before returning
    for s in sessions:
        s.pop("_jsonl_path", None)

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
    host_count = sum(1 for s in sessions if s.get("type") == "host")
    container_count = len(sessions) - host_count
    logger.debug(
        "get_active_sessions: %d total (%d host, %d container)",
        len(sessions), host_count, container_count,
    )
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
