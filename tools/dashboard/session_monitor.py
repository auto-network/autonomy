"""Session Monitor — event-driven session tracking with incremental tailing.

Replaces filesystem scanning with a single in-memory registry of active sessions.
Sessions are registered explicitly at creation time and maintained by background tasks.

Usage::

    from tools.dashboard.session_monitor import session_monitor

    # Register a session (at creation time)
    session_monitor.register(
        session_id="abc123",
        tmux_name="auto-t1",
        session_type="terminal",
        project="my-project",
        jsonl_path=Path("/path/to/session.jsonl"),
    )

    # Read from monitor (zero filesystem access)
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

logger = logging.getLogger(__name__)


@dataclass
class SessionState:
    session_id: str           # JSONL filename stem (UUID) or tmux name
    tmux_name: str            # tmux session name (auto-tN, chatwith-X)
    session_type: str         # terminal, chatwith, dispatch, librarian, host
    project: str              # project folder name
    jsonl_path: Path | None   # absolute path to JSONL file, or directory to resolve

    bead_id: str | None = None

    # Maintained by monitor
    last_message: str = ""
    entry_count: int = 0
    file_offset: int = 0
    is_live: bool = True
    started_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    size_bytes: int = 0
    context_tokens: int = 0

    # Agent subagent tracking for tool_calls enrichment
    agent_descriptions: dict = field(default_factory=dict)   # tool_id -> description
    claimed_subagents: set = field(default_factory=set)       # claimed meta.json paths

    # Internal: True if jsonl_path is a directory (needs resolution)
    _path_is_dir: bool = False
    # Cooldown timestamp: when is_live turned False
    _dead_since: float = 0.0
    # Broadcast sequence number (monotonically increasing per session)
    _broadcast_seq: int = 0


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
    """Seed initial last_message by reading tail of JSONL (recovery only)."""
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


class SessionMonitor:
    """In-memory registry of active sessions with background maintenance."""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionState] = {}
        self._lock = asyncio.Lock()
        self._tailer_task: asyncio.Task | None = None
        self._liveness_task: asyncio.Task | None = None
        self._event_bus = None  # set in start()
        self._entry_parser = None  # set in start()
        self._entry_enricher = None  # set in start()
        self._started = False

    # ── Registration ──────────────────────────────────────────────

    async def register(
        self,
        session_id: str,
        tmux_name: str,
        session_type: str,
        project: str,
        jsonl_path: Path | None = None,
        bead_id: str | None = None,
        seed_message: str = "",
    ) -> None:
        """Register a new session into the monitor."""
        path_is_dir = False
        if jsonl_path is not None and jsonl_path.is_dir():
            path_is_dir = True

        state = SessionState(
            session_id=session_id,
            tmux_name=tmux_name,
            session_type=session_type,
            project=project,
            jsonl_path=jsonl_path,
            bead_id=bead_id,
            last_message=seed_message,
            _path_is_dir=path_is_dir,
        )
        async with self._lock:
            # Dedup: if another session has this tmux_name, remove it
            for existing_id, existing in list(self._sessions.items()):
                if existing.tmux_name == tmux_name and existing_id != session_id:
                    del self._sessions[existing_id]
                    logger.info("session_monitor: dedup removed %s (same tmux=%s)", existing_id, tmux_name)
            self._sessions[session_id] = state
        logger.info(
            "session_monitor: registered %s  tmux=%s  type=%s  project=%s",
            session_id, tmux_name, session_type, project,
        )
        await self._broadcast_registry()

    async def deregister(self, session_id: str) -> None:
        """Remove a session from the monitor."""
        async with self._lock:
            removed = self._sessions.pop(session_id, None)
        if removed:
            logger.info(
                "session_monitor: deregistered %s  tmux=%s",
                session_id, removed.tmux_name,
            )
            await self._broadcast_registry()

    # ── Queries ───────────────────────────────────────────────────

    def get_all(self) -> list[SessionState]:
        """Return all registered sessions."""
        return list(self._sessions.values())

    def get_one(self, session_id: str) -> SessionState | None:
        return self._sessions.get(session_id)

    def count(self) -> int:
        """Count live sessions."""
        return sum(1 for s in self._sessions.values() if s.is_live)

    def get_registry(self) -> list[dict]:
        """Return registry of active sessions (lightweight roster)."""
        registry = []
        for s in self._sessions.values():
            if s.is_live:
                registry.append({
                    "session_id": s.session_id,
                    "project": s.project,
                    "type": s.session_type,
                    "tmux_session": s.tmux_name,
                    "is_live": s.is_live,
                    "started_at": s.started_at,
                })
        return registry

    # ── Background tasks ──────────────────────────────────────────

    async def start(self, event_bus=None, entry_parser=None, entry_enricher=None) -> None:
        """Start background tailer and liveness tasks.

        Args:
            event_bus: EventBus for SSE broadcasting.
            entry_parser: Callable (str -> dict | list | None) that parses a
                raw JSONL line into display entries for per-session SSE push.
            entry_enricher: Optional callable (list[dict] -> None) that enriches
                parsed entries in place (e.g. Agent tool_calls count).
        """
        if self._started:
            return
        self._started = True
        self._event_bus = event_bus
        self._entry_parser = entry_parser
        self._entry_enricher = entry_enricher
        self._tailer_task = asyncio.create_task(self._tailer_loop())
        self._liveness_task = asyncio.create_task(self._liveness_loop())
        logger.info("session_monitor: background tasks started")
        # Broadcast registry for any sessions registered during recover()
        if self._sessions:
            await self._broadcast_registry()

    async def _broadcast_registry(self) -> None:
        """Push session registry to SSE subscribers."""
        if self._event_bus is None:
            return
        await self._event_bus.broadcast("session:registry", self.get_registry())

    # ── JSONL Tailer ──────────────────────────────────────────────

    async def _tailer_loop(self) -> None:
        """Check all registered sessions for new JSONL content every 1s."""
        while True:
            try:
                sessions = list(self._sessions.values())
                for state in sessions:
                    if not state.is_live:
                        continue
                    _, new_entries = await asyncio.to_thread(self._tail_one, state)
                    if new_entries:
                        self._enrich_agent_entries(state, new_entries)
                    if new_entries and self._event_bus:
                        state._broadcast_seq += 1
                        await self._event_bus.broadcast(
                            "session:messages",
                            {
                                "session_id": state.session_id,
                                "entries": new_entries,
                                "is_live": state.is_live,
                                "seq": state._broadcast_seq,
                                "context_tokens": state.context_tokens,
                                "size_bytes": state.size_bytes,
                            },
                            dedup=False,
                        )
            except Exception:
                logger.exception("session_monitor: tailer error")
            await asyncio.sleep(1)

    @staticmethod
    def _enrich_agent_entries(state: SessionState, entries: list) -> None:
        """Enrich Agent tool_results with tool_calls counts from subagent JSONL.

        Uses state.agent_descriptions and state.claimed_subagents to track
        Agent tool_use/tool_result pairs across incremental SSE batches.
        """
        for entry in entries:
            if entry.get("type") == "tool_use" and entry.get("tool_name") == "Agent":
                tool_id = entry.get("tool_id", "")
                desc = entry.get("input", {}).get("description", "")
                if tool_id and desc:
                    state.agent_descriptions[tool_id] = desc

            elif entry.get("type") == "tool_result" and entry.get("tool_id"):
                tool_id = entry["tool_id"]
                if tool_id not in state.agent_descriptions:
                    continue
                if state.jsonl_path is None:
                    continue
                target_desc = state.agent_descriptions[tool_id]
                subagents_dir = state.jsonl_path.parent / state.jsonl_path.stem / "subagents"
                if not subagents_dir.is_dir():
                    continue
                for meta_path in sorted(subagents_dir.glob("*.meta.json")):
                    if str(meta_path) in state.claimed_subagents:
                        continue
                    try:
                        meta = json.loads(meta_path.read_text())
                    except (json.JSONDecodeError, OSError):
                        continue
                    if meta.get("description") == target_desc:
                        state.claimed_subagents.add(str(meta_path))
                        jsonl_path = meta_path.with_suffix("").with_suffix(".jsonl")
                        if jsonl_path.exists():
                            count = count_tool_uses(jsonl_path)
                            if count > 0:
                                entry["tool_calls"] = count
                        break

    def _tail_one(self, state: SessionState) -> tuple[bool, list]:
        """Incremental read of one session's JSONL.

        Returns (changed, parsed_entries) where *changed* indicates summary
        state was updated and *parsed_entries* is a list of display-ready
        dicts to broadcast via SSE.
        """
        # Resolve directory path to actual JSONL file
        if state._path_is_dir and state.jsonl_path is not None:
            resolved = self._resolve_jsonl_in_dir(state.jsonl_path)
            if resolved:
                state.jsonl_path = resolved
                state._path_is_dir = False
                # Keep session_id as tmux_name — changing it breaks SSE
                # routing since the client store is keyed by the original ID
                state.project = resolved.parent.name
                logger.info(
                    "session_monitor: resolved JSONL for tmux=%s → %s  project=%s",
                    state.tmux_name, resolved.stem, state.project,
                )
            else:
                return False, []

        if state.jsonl_path is None or not state.jsonl_path.exists():
            return False, []

        try:
            st = state.jsonl_path.stat()
        except OSError:
            return False, []

        # No growth since last check
        if st.st_size <= state.file_offset and st.st_mtime <= state.last_activity:
            return False, []

        changed = False

        # Update size
        if st.st_size != state.size_bytes:
            state.size_bytes = st.st_size
            changed = True

        if st.st_size <= state.file_offset:
            return changed, []

        # Read new data from offset
        try:
            with open(state.jsonl_path, "rb") as fh:
                fh.seek(state.file_offset)
                data = fh.read()
        except OSError:
            return changed, []

        # Only process complete lines
        last_nl = data.rfind(b"\n")
        if last_nl == -1:
            return changed, []

        complete = data[:last_nl + 1]
        new_offset = state.file_offset + last_nl + 1
        new_entry_count = 0
        parsed_entries: list = []

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
                state.last_message = text

            # Extract context_tokens from assistant usage
            if entry.get("type") == "assistant":
                usage = entry.get("message", {}).get("usage", {})
                if usage:
                    ctx = (usage.get("input_tokens", 0)
                           + usage.get("cache_creation_input_tokens", 0)
                           + usage.get("cache_read_input_tokens", 0))
                    if ctx > 0:
                        state.context_tokens = ctx

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

        if new_entry_count > 0:
            state.entry_count += new_entry_count
            state.last_activity = st.st_mtime
            changed = True

        state.file_offset = new_offset
        return changed, parsed_entries

    @staticmethod
    def _resolve_jsonl_in_dir(directory: Path) -> Path | None:
        """Find the JSONL file inside a session directory (container sessions)."""
        try:
            # Container sessions: data/agent-runs/{name}-*/sessions/{project}/*.jsonl
            # The directory might be the sessions/ dir or a project subdir
            jsonls = list(directory.rglob("*.jsonl"))
            if jsonls:
                # Return the most recently modified
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
                sessions = list(self._sessions.values())
                changed = False

                # Collect dead sessions past cooldown for deregistration
                to_deregister: list[str] = []
                now = time.time()

                for state in sessions:
                    if not state.is_live:
                        # Check cooldown
                        if state._dead_since and (now - state._dead_since) > self._COOLDOWN_SECONDS:
                            to_deregister.append(state.session_id)
                        continue

                    alive = await asyncio.to_thread(self._check_tmux, state.tmux_name)
                    if not alive:
                        state.is_live = False
                        state._dead_since = now
                        changed = True
                        logger.info(
                            "session_monitor: tmux dead  %s (tmux=%s)",
                            state.session_id, state.tmux_name,
                        )

                # Deregister expired dead sessions
                for sid in to_deregister:
                    async with self._lock:
                        self._sessions.pop(sid, None)
                    logger.info("session_monitor: deregistered expired %s", sid)
                    changed = True

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

    # ── Recovery ──────────────────────────────────────────────────

    async def recover(self) -> None:
        """Recover sessions from filesystem on startup.

        Called once from _on_startup(). This is the ONLY time we scan filesystems.
        """
        recovered = 0

        # 1. Get live tmux sessions
        try:
            result = subprocess.run(
                ["tmux", "list-sessions", "-F", "#{session_name}"],
                capture_output=True, text=True,
            )
            live_tmux = set(result.stdout.strip().split("\n")) if result.returncode == 0 else set()
        except (FileNotFoundError, OSError):
            live_tmux = set()

        # Filter to dashboard sessions (auto-* and chatwith-*)
        dashboard_tmux = {
            name for name in live_tmux
            if name.startswith("auto-") or name.startswith("chatwith-")
        }

        # 2. Container sessions: data/agent-runs/*/sessions/
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
                # Find the JSONL
                jsonls = list(sess_dir.rglob("*.jsonl"))
                if not jsonls:
                    continue
                jsonl = max(jsonls, key=lambda p: p.stat().st_mtime)

                seed_msg = _read_latest_msg_from_tail(jsonl)
                st = jsonl.stat()

                await self.register(
                    session_id=jsonl.stem,
                    tmux_name=tmux_name,
                    session_type=meta.get("type", "terminal"),
                    project=jsonl.parent.name,
                    jsonl_path=jsonl,
                    bead_id=meta.get("bead_id"),
                    seed_message=seed_msg,
                )
                # Seed offset to current file size so we don't re-read everything
                state = self._sessions.get(jsonl.stem)
                if state:
                    state.file_offset = st.st_size
                    state.size_bytes = st.st_size
                    state.last_activity = st.st_mtime
                    state.started_at = st.st_mtime - 60  # approximate

                dashboard_tmux.discard(tmux_name)
                recovered += 1

        # 3. Host sessions: ~/.claude/projects/**/*.meta.json
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

                # .meta.json is a double suffix; .with_suffix() only replaces
                # the last one, producing .meta.jsonl instead of .jsonl.
                jsonl = meta_path.parent / (meta_path.stem.removesuffix(".meta") + ".jsonl")
                if not jsonl.exists():
                    continue

                seed_msg = _read_latest_msg_from_tail(jsonl)
                st = jsonl.stat()

                stype = "chatwith" if tmux_name.startswith("chatwith-") else "terminal"
                await self.register(
                    session_id=jsonl.stem,
                    tmux_name=tmux_name,
                    session_type=stype,
                    project=jsonl.parent.name,
                    jsonl_path=jsonl,
                    seed_message=seed_msg,
                )
                state = self._sessions.get(jsonl.stem)
                if state:
                    state.file_offset = st.st_size
                    state.size_bytes = st.st_size
                    state.last_activity = st.st_mtime
                    state.started_at = st.st_mtime - 60

                dashboard_tmux.discard(tmux_name)
                recovered += 1

        logger.info("session_monitor: recovered %d sessions", recovered)


# Module-level singleton
session_monitor = SessionMonitor()
