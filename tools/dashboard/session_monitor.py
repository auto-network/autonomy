"""Session Monitor — DB-backed session tracking with incremental tailing.

All session state lives in dashboard.db (tmux_sessions table).
No in-memory dicts, no recovery heuristics, no meta file scanning.

Usage::

    from tools.dashboard.session_monitor import session_monitor

    # Register a session (at creation time — INSERTs into DB)
    await session_monitor.register(
        tmux_name="host-0322-111522",
        session_type="host",
        project="my-project",
    )

    # Read from monitor (reads DB)
    all_sessions = session_monitor.get_registry()
    count = session_monitor.count()

    # Start background tasks (called from _on_startup)
    await session_monitor.start()
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tools.dashboard.dao.dashboard_db import (
    get_conn,
    get_live_sessions,
    get_session,
    get_tailable_sessions,
    insert_session,
    mark_dead,
    delete_session,
    update_jsonl_link,
    update_tail_state,
    count_live,
)

logger = logging.getLogger(__name__)


def _extract_message_text(entry: dict) -> str:
    """Extract meaningful text from a JSONL entry (user or assistant)."""
    if entry.get("isSidechain"):
        return ""
    etype = entry.get("type")
    if etype not in ("user", "assistant"):
        return ""
    msg = entry.get("message", {})
    content = msg.get("content", "")
    if isinstance(content, str) and len(content) > 5:
        return content[:150]
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                if len(text) > 5:
                    return text[:150]
    return ""


def _read_latest_msg_from_tail(jsonl: Path) -> str:
    """Seed initial last_message by reading tail of JSONL (seeding only)."""
    try:
        size = jsonl.stat().st_size
        with open(jsonl, "rb") as f:
            f.seek(max(0, size - 4000))
            chunk = f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    for line in reversed(chunk.strip().split("\n")):
        try:
            e = json.loads(line)
            text = _extract_message_text(e)
            if text:
                return text
        except json.JSONDecodeError:
            continue
    return ""


def count_tool_uses(jsonl_path: Path) -> int:
    """Count tool_use blocks in a subagent JSONL file."""
    count = 0
    try:
        with open(jsonl_path) as f:
            for line in f:
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if raw.get("type") == "assistant":
                    for block in raw.get("message", {}).get("content", []):
                        if block.get("type") == "tool_use":
                            count += 1
    except OSError:
        pass
    return count


@dataclass
class _TailState:
    """Ephemeral per-session state for the tailer (not persisted in DB)."""
    # Agent subagent tracking for tool_calls enrichment
    agent_descriptions: dict = field(default_factory=dict)   # tool_id -> description
    claimed_subagents: set = field(default_factory=set)       # claimed meta.json paths
    # Track if path needs directory resolution (container sessions)
    needs_resolution: bool = False
    # Resolution directory (for container sessions where jsonl_path is a dir)
    resolution_dir: Path | None = None
    # Broadcast sequence number (monotonically increasing per session)
    broadcast_seq: int = 0


class SessionMonitor:
    """DB-backed session registry with background tailing and liveness checking."""

    def __init__(self) -> None:
        self._tail_states: dict[str, _TailState] = {}
        self._tailer_task: asyncio.Task | None = None
        self._liveness_task: asyncio.Task | None = None
        self._event_bus = None
        self._entry_parser = None
        self._entry_enricher = None
        self._started = False

    # ── Registration ──────────────────────────────────────────────

    async def register(
        self,
        tmux_name: str,
        session_type: str,
        project: str,
        *,
        jsonl_path: Path | None = None,
        bead_id: str | None = None,
        seed_message: str = "",
        session_uuid: str | None = None,
    ) -> None:
        """Register a new session — INSERT into dashboard.db."""
        path_is_dir = False
        path_str: str | None = None
        if jsonl_path is not None:
            if jsonl_path.is_dir():
                path_is_dir = True
                # Don't store directory as jsonl_path — store None, resolve later
                path_str = None
            else:
                path_str = str(jsonl_path)

        try:
            insert_session(
                tmux_name=tmux_name,
                session_type=session_type,
                project=project,
                bead_id=bead_id,
                jsonl_path=path_str,
                session_uuid=session_uuid,
            )
        except Exception:
            logger.warning("session_monitor: INSERT failed for tmux=%s (may already exist)", tmux_name)
            return

        if seed_message:
            update_tail_state(tmux_name, last_message=seed_message)

        # Set up ephemeral tail state
        ts = _TailState(needs_resolution=path_is_dir, resolution_dir=jsonl_path if path_is_dir else None)
        self._tail_states[tmux_name] = ts

        logger.info(
            "session_monitor: registered %s  type=%s  project=%s",
            tmux_name, session_type, project,
        )
        await self._broadcast_registry()

    async def deregister(self, tmux_name: str) -> None:
        """Mark a session as dead and remove from tail states."""
        mark_dead(tmux_name)
        self._tail_states.pop(tmux_name, None)
        logger.info("session_monitor: deregistered %s", tmux_name)
        await self._broadcast_registry()

    # ── Queries ───────────────────────────────────────────────────

    def get_all(self) -> list[dict]:
        """Return all live sessions from DB."""
        return get_live_sessions()

    def get_one(self, tmux_name: str) -> dict | None:
        """Return a single session by tmux_name."""
        return get_session(tmux_name)

    def count(self) -> int:
        """Count live sessions."""
        return count_live()

    def get_registry(self) -> list[dict]:
        """Return registry of active sessions (lightweight roster for SSE)."""
        sessions = get_live_sessions()
        return [
            {
                "session_id": s["tmux_name"],
                "project": s["project"],
                "type": s["type"],
                "tmux_session": s["tmux_name"],
                "is_live": bool(s["is_live"]),
                "started_at": s["created_at"],
            }
            for s in sessions
        ]

    # ── Background tasks ──────────────────────────────────────────

    async def start(self, event_bus=None, entry_parser=None, entry_enricher=None) -> None:
        """Start background tailer and liveness tasks."""
        if self._started:
            return
        self._started = True
        self._event_bus = event_bus
        self._entry_parser = entry_parser
        self._entry_enricher = entry_enricher
        self._tailer_task = asyncio.create_task(self._tailer_loop())
        self._liveness_task = asyncio.create_task(self._liveness_loop())
        logger.info("session_monitor: background tasks started")
        # Broadcast registry for any sessions that exist in DB
        if count_live() > 0:
            await self._broadcast_registry()

    async def _broadcast_registry(self) -> None:
        """Push session registry to SSE subscribers."""
        if self._event_bus is None:
            return
        await self._event_bus.broadcast("session:registry", self.get_registry())

    # ── JSONL Tailer ──────────────────────────────────────────────

    async def _tailer_loop(self) -> None:
        """Check all live sessions for new JSONL content every 1s."""
        while True:
            try:
                sessions = get_tailable_sessions()
                for row in sessions:
                    tmux_name = row["tmux_name"]
                    # Ensure we have ephemeral tail state
                    if tmux_name not in self._tail_states:
                        self._tail_states[tmux_name] = _TailState()

                    ts = self._tail_states[tmux_name]
                    _, new_entries = await asyncio.to_thread(
                        self._tail_one, row, ts
                    )
                    if new_entries:
                        self._enrich_agent_entries(row, ts, new_entries)
                    if new_entries and self._event_bus:
                        ts.broadcast_seq += 1
                        # Re-read to get updated values
                        updated = get_session(tmux_name)
                        await self._event_bus.broadcast(
                            "session:messages",
                            {
                                "session_id": tmux_name,
                                "entries": new_entries,
                                "is_live": bool(updated["is_live"]) if updated else True,
                                "seq": ts.broadcast_seq,
                                "context_tokens": updated["context_tokens"] if updated else 0,
                                "size_bytes": Path(row["jsonl_path"]).stat().st_size if row.get("jsonl_path") else 0,
                            },
                            dedup=False,
                        )

                # Also try to resolve sessions that need directory resolution
                unresolved = [
                    (name, ts) for name, ts in self._tail_states.items()
                    if ts.needs_resolution and ts.resolution_dir is not None
                ]
                for tmux_name, ts in unresolved:
                    resolved = await asyncio.to_thread(self._resolve_jsonl_in_dir, ts.resolution_dir)
                    if resolved:
                        ts.needs_resolution = False
                        ts.resolution_dir = None
                        update_jsonl_link(
                            tmux_name,
                            session_uuid=resolved.stem,
                            jsonl_path=str(resolved),
                            project=resolved.parent.name,
                        )
                        logger.info(
                            "session_monitor: resolved JSONL for tmux=%s → %s",
                            tmux_name, resolved.stem,
                        )

            except Exception:
                logger.exception("session_monitor: tailer error")
            await asyncio.sleep(1)

    @staticmethod
    def _enrich_agent_entries(row: dict, ts: _TailState, entries: list) -> None:
        """Enrich Agent tool_results with tool_calls counts from subagent JSONL."""
        jsonl_path_str = row.get("jsonl_path")
        if not jsonl_path_str:
            return
        jsonl_path = Path(jsonl_path_str)

        for entry in entries:
            if entry.get("type") == "tool_use" and entry.get("tool_name") == "Agent":
                tool_id = entry.get("tool_id", "")
                desc = entry.get("input", {}).get("description", "")
                if tool_id and desc:
                    ts.agent_descriptions[tool_id] = desc

            elif entry.get("type") == "tool_result" and entry.get("tool_id"):
                tool_id = entry["tool_id"]
                if tool_id not in ts.agent_descriptions:
                    continue
                target_desc = ts.agent_descriptions[tool_id]
                subagents_dir = jsonl_path.parent / jsonl_path.stem / "subagents"
                if not subagents_dir.is_dir():
                    continue
                for meta_path in sorted(subagents_dir.glob("*.meta.json")):
                    if str(meta_path) in ts.claimed_subagents:
                        continue
                    try:
                        meta = json.loads(meta_path.read_text())
                    except (json.JSONDecodeError, OSError):
                        continue
                    if meta.get("description") == target_desc:
                        ts.claimed_subagents.add(str(meta_path))
                        jsonl_sub = meta_path.with_suffix("").with_suffix(".jsonl")
                        if jsonl_sub.exists():
                            count = count_tool_uses(jsonl_sub)
                            if count > 0:
                                entry["tool_calls"] = count
                        break

    def _tail_one(self, row: dict, ts: _TailState) -> tuple[bool, list]:
        """Incremental read of one session's JSONL. Updates DB with new state."""
        jsonl_path_str = row.get("jsonl_path")
        if not jsonl_path_str:
            return False, []

        jsonl_path = Path(jsonl_path_str)
        if not jsonl_path.exists():
            return False, []

        try:
            st = jsonl_path.stat()
        except OSError:
            return False, []

        file_offset = row.get("file_offset", 0)
        last_activity = row.get("last_activity", 0) or 0

        # No growth since last check
        if st.st_size <= file_offset and st.st_mtime <= last_activity:
            return False, []

        if st.st_size <= file_offset:
            return False, []

        # Read new data from offset
        try:
            with open(jsonl_path, "rb") as fh:
                fh.seek(file_offset)
                data = fh.read()
        except OSError:
            return False, []

        # Only process complete lines
        last_nl = data.rfind(b"\n")
        if last_nl == -1:
            return False, []

        complete = data[:last_nl + 1]
        new_offset = file_offset + last_nl + 1
        new_entry_count = 0
        parsed_entries: list = []
        last_message = row.get("last_message", "")
        context_tokens = row.get("context_tokens", 0)

        for raw_line in complete.splitlines():
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            new_entry_count += 1
            text = _extract_message_text(entry)
            if text:
                last_message = text

            # Extract context_tokens from assistant usage
            if entry.get("type") == "assistant":
                usage = entry.get("message", {}).get("usage", {})
                if usage:
                    ctx = (usage.get("input_tokens", 0)
                           + usage.get("cache_creation_input_tokens", 0)
                           + usage.get("cache_read_input_tokens", 0))
                    if ctx > 0:
                        context_tokens = ctx

            # Parse full entry for SSE broadcast
            if self._entry_parser:
                try:
                    parsed = self._entry_parser(line)
                except Exception:
                    parsed = None
                if isinstance(parsed, list):
                    parsed_entries.extend(parsed)
                elif parsed is not None:
                    parsed_entries.append(parsed)

        tmux_name = row["tmux_name"]
        if new_entry_count > 0:
            update_tail_state(
                tmux_name,
                file_offset=new_offset,
                last_activity=st.st_mtime,
                last_message=last_message,
                entry_count=(row.get("entry_count", 0) + new_entry_count),
                context_tokens=context_tokens,
            )
            return True, parsed_entries

        # Update offset even if no entries parsed (whitespace lines)
        update_tail_state(tmux_name, file_offset=new_offset)
        return False, parsed_entries

    @staticmethod
    def _resolve_jsonl_in_dir(directory: Path) -> Path | None:
        """Find the JSONL file inside a session directory (container sessions)."""
        try:
            jsonls = list(directory.rglob("*.jsonl"))
            if jsonls:
                return max(jsonls, key=lambda p: p.stat().st_mtime)
        except OSError:
            pass
        return None

    # ── Liveness Checker ──────────────────────────────────────────

    _COOLDOWN_SECONDS = 30.0

    async def _liveness_loop(self) -> None:
        """Check tmux liveness for all sessions every 10s."""
        while True:
            try:
                sessions = get_live_sessions()
                changed = False
                now = time.time()

                for row in sessions:
                    tmux_name = row["tmux_name"]
                    alive = await asyncio.to_thread(self._check_tmux, tmux_name)
                    if not alive:
                        mark_dead(tmux_name)
                        self._tail_states.pop(tmux_name, None)
                        changed = True
                        logger.info(
                            "session_monitor: tmux dead  %s (type=%s)",
                            tmux_name, row["type"],
                        )

                # Clean up old dead sessions from tail states
                # (Dead sessions with _COOLDOWN expired get deleted from DB)
                conn = get_conn()
                expired = conn.execute(
                    "SELECT tmux_name FROM tmux_sessions"
                    " WHERE is_live=0 AND last_activity IS NOT NULL"
                    "   AND (? - COALESCE(last_activity, created_at)) > ?",
                    (now, self._COOLDOWN_SECONDS),
                ).fetchall()
                for exp_row in expired:
                    # Don't actually delete from DB — keep for history
                    # Just ensure tail states are cleaned up
                    self._tail_states.pop(exp_row["tmux_name"], None)

                if changed:
                    await self._broadcast_registry()

            except Exception:
                logger.exception("session_monitor: liveness error")
            await asyncio.sleep(10)

    @staticmethod
    def _check_tmux(name: str) -> bool:
        """Check if a tmux session is alive (runs in thread)."""
        try:
            return subprocess.run(
                ["tmux", "has-session", "-t", name],
                capture_output=True,
            ).returncode == 0
        except (FileNotFoundError, OSError):
            return False

    # ── Seed (replaces recover) ────────────────────────────────────

    async def seed_from_filesystem(self) -> None:
        """One-time seed: read existing live tmux sessions from filesystem.

        Called on first startup when dashboard.db has no live sessions.
        Reads .meta.json and .session_meta.json files ONE FINAL TIME.
        After this, those files are never consulted again.
        """
        # Check if we already have live sessions — skip seeding
        if count_live() > 0:
            logger.info("session_monitor: DB has live sessions, skipping seed")
            return

        seeded = 0

        # Get live tmux sessions
        try:
            result = subprocess.run(
                ["tmux", "list-sessions", "-F", "#{session_name}"],
                capture_output=True, text=True,
            )
            live_tmux = set(result.stdout.strip().split("\n")) if result.returncode == 0 else set()
        except (FileNotFoundError, OSError):
            live_tmux = set()

        # Filter to dashboard sessions
        dashboard_tmux = {
            name for name in live_tmux
            if (name.startswith("auto-") or name.startswith("chatwith-")
                or name.startswith("host-") or name.startswith("chat-"))
        }

        if not dashboard_tmux:
            logger.info("session_monitor: no dashboard tmux sessions to seed")
            return

        # Container sessions: data/agent-runs/*/sessions/
        repo_root = Path(__file__).resolve().parent.parent.parent
        agent_runs = repo_root / "data" / "agent-runs"
        if agent_runs.exists():
            for run_dir in sorted(agent_runs.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
                meta_path = run_dir / "sessions" / ".session_meta.json"
                if not meta_path.exists():
                    continue
                try:
                    meta = json.loads(meta_path.read_text())
                except (json.JSONDecodeError, OSError):
                    continue

                tmux_name = meta.get("tmux_session")
                if not tmux_name or tmux_name not in dashboard_tmux:
                    continue

                sess_dir = run_dir / "sessions"
                jsonls = list(sess_dir.rglob("*.jsonl"))
                if not jsonls:
                    continue
                jsonl = max(jsonls, key=lambda p: p.stat().st_mtime)

                seed_msg = _read_latest_msg_from_tail(jsonl)
                st = jsonl.stat()

                stype = meta.get("type", "container")
                from tools.dashboard.dao.dashboard_db import upsert_session
                upsert_session(
                    tmux_name=tmux_name,
                    session_type=stype,
                    project=jsonl.parent.name,
                    bead_id=meta.get("bead_id"),
                    jsonl_path=str(jsonl),
                    session_uuid=jsonl.stem,
                    created_at=st.st_mtime - 60,
                    file_offset=st.st_size,
                    last_message=seed_msg,
                    is_live=True,
                )
                dashboard_tmux.discard(tmux_name)
                seeded += 1

        # Host sessions: ~/.claude/projects/**/*.meta.json
        home_projects = Path.home() / ".claude" / "projects"
        if home_projects.exists():
            for meta_path in home_projects.rglob("*.meta.json"):
                try:
                    data = json.loads(meta_path.read_text())
                except (json.JSONDecodeError, OSError):
                    continue

                tmux_name = data.get("tmux_session")
                if not tmux_name or tmux_name not in dashboard_tmux:
                    continue

                jsonl = meta_path.parent / (meta_path.stem.removesuffix(".meta") + ".jsonl")
                if not jsonl.exists():
                    continue

                seed_msg = _read_latest_msg_from_tail(jsonl)
                st = jsonl.stat()

                stype = "chatwith" if tmux_name.startswith("chatwith-") or tmux_name.startswith("chat-") else "host"
                from tools.dashboard.dao.dashboard_db import upsert_session
                upsert_session(
                    tmux_name=tmux_name,
                    session_type=stype,
                    project=jsonl.parent.name,
                    jsonl_path=str(jsonl),
                    session_uuid=jsonl.stem,
                    created_at=st.st_mtime - 60,
                    file_offset=st.st_size,
                    last_message=seed_msg,
                    is_live=True,
                )
                dashboard_tmux.discard(tmux_name)
                seeded += 1

        logger.info("session_monitor: seeded %d sessions from filesystem", seeded)


# Module-level singleton
session_monitor = SessionMonitor()
