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
    get_dispatch_nag_sessions,
    get_live_sessions,
    get_session,
    get_tailable_sessions,
    insert_session,
    mark_dead,
    delete_session,
    update_activity_state,
    update_tail_state,
    update_nag_last_sent,
    count_live,
)

from agents.workspace_manager import (
    CleanupResult,
    WORKTREES_DIR,
    cleanup_session_worktrees,
    prune_orphan_worktrees,
)

logger = logging.getLogger(__name__)

# inotify — optional, falls back to polling if unavailable
try:
    from inotify_simple import INotify, flags as _iflags
    _HAS_INOTIFY = True
except ImportError:
    _HAS_INOTIFY = False


def _find_primary_jsonls(directory: Path) -> list[Path]:
    """Find JSONL files excluding subagent traces."""
    return [f for f in directory.rglob("*.jsonl")
            if "subagents" not in f.parts]


def _extract_message_text(entry: dict) -> str:
    """Extract meaningful text from a JSONL entry (user or assistant)."""
    if entry.get("isSidechain"):
        return ""
    # Compact-summary turns carry 14K-char boilerplate — not a real "last message".
    if entry.get("isCompactSummary") or entry.get("isVisibleInTranscriptOnly"):
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


def _log_worktree_cleanup(tmux_name: str, result: CleanupResult) -> None:
    """Log the outcome of a worktree cleanup pass (removed/preserved/errors)."""
    if result.removed:
        logger.info(
            "session_monitor: worktree cleanup %s: removed %d (%s)",
            tmux_name, len(result.removed), ", ".join(result.removed),
        )
    for path, reason in result.preserved:
        logger.warning(
            "session_monitor: worktree preserved %s: %s (%s)",
            tmux_name, path, reason,
        )
    for path, err in result.errors:
        logger.warning(
            "session_monitor: worktree cleanup error %s: %s (%s)",
            tmux_name, path, err,
        )


def _cleanup_worktrees_for_dead_session(tmux_name: str) -> None:
    """Thread-safe wrapper around cleanup_session_worktrees — never raises."""
    try:
        result = cleanup_session_worktrees(tmux_name)
    except Exception:
        logger.exception("session_monitor: worktree cleanup raised for %s", tmux_name)
        return
    _log_worktree_cleanup(tmux_name, result)


def _prune_orphan_worktrees_safely(live_session_names: list[str]) -> None:
    """Thread-safe wrapper around prune_orphan_worktrees — never raises."""
    try:
        results = prune_orphan_worktrees(live_session_names)
    except Exception:
        logger.exception("session_monitor: orphan worktree prune raised")
        return
    total = sum(len(r.removed) for r in results.values())
    preserved = sum(len(r.preserved) for r in results.values())
    if total or preserved:
        logger.info(
            "session_monitor: orphan worktree prune — "
            "%d removed, %d preserved, %d sessions scanned",
            total, preserved, len(results),
        )
    for name, result in results.items():
        _log_worktree_cleanup(name, result)


def _send_nag_crosstalk(tmux_name: str, message: str) -> None:
    """Send a nag message to a session via CrossTalk envelope.

    Called from a worker thread (via asyncio.to_thread), so we use tmux
    subprocess calls directly instead of the async tmux_send path.
    """
    from tools.dashboard.tmux_send import _tmux_paste, _tmux_enter

    iso_now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    envelope = (
        f'<crosstalk from="dashboard-nag"\n'
        f'           label="Session Nag"\n'
        f'           source="" turn="0"\n'
        f'           timestamp="{iso_now}">\n'
        f'{message}\n'
        f'</crosstalk>'
    )
    try:
        _tmux_paste(tmux_name, envelope)
        time.sleep(0.3)
        _tmux_enter(tmux_name)
        time.sleep(0.5)
        _tmux_enter(tmux_name)  # retry — harmless if already submitted
    except Exception:
        logger.warning("session_monitor: nag send failed for %s", tmux_name, exc_info=True)


def _get_dispatch_pause_message() -> str | None:
    """Build a human-readable pause nag message, or None if not paused.

    Checks both the global dispatcher pause (dispatch_db) and per-label
    pauses (dispatch.state file).  Returns the first match.
    """
    # 1. Global dispatcher pause (auth failure, merge cascade)
    try:
        from agents.dispatch_db import is_paused, get_pause_reason
        if is_paused():
            info = get_pause_reason() or {}
            reason = info.get("message") or info.get("reason") or "unknown"
            paused_at = info.get("paused_at")
            duration = _format_pause_duration(paused_at)
            return f"Dispatch paused: {reason}{duration}"
    except Exception:
        logger.debug("session_monitor: dispatch_db pause check failed", exc_info=True)

    # 2. Per-label pauses (smoke failure, etc.) via dispatch.state file
    try:
        state_path = Path(__file__).resolve().parents[2] / "data" / "dispatch.state"
        if state_path.exists():
            state = json.loads(state_path.read_text())
            for key, val in state.items():
                if key.endswith("_reason") or not val:
                    continue
                reason = state.get(f"{key}_reason", f"{key} queue paused")
                return f"Dispatch paused: {reason}"
    except Exception:
        logger.debug("session_monitor: dispatch.state pause check failed", exc_info=True)

    return None


def _format_pause_duration(paused_at: str | None) -> str:
    """Format ' (Xm ago)' suffix from an ISO timestamp, or '' if unavailable."""
    if not paused_at:
        return ""
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(paused_at.replace("Z", "+00:00"))
        elapsed = datetime.now(timezone.utc) - dt
        mins = int(elapsed.total_seconds() / 60)
        if mins < 1:
            return " (<1m ago)"
        if mins < 60:
            return f" ({mins}m ago)"
        hours = mins // 60
        remaining = mins % 60
        return f" ({hours}h{remaining}m ago)"
    except Exception:
        return ""


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
    # Last queued message content — for deduping against the subsequent user entry
    last_enqueue_content: str | None = None
    # inotify watch descriptors
    watch_descriptor: int | None = None       # IN_MODIFY on active JSONL
    dir_watch_descriptor: int | None = None   # IN_CREATE on session directory
    # Activity state tracking — pending tool calls and last entry type
    pending_tool_ids: set = field(default_factory=set)
    completed_tool_ids: set = field(default_factory=set)
    last_entry_type: str = ""


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
        self._last_pause_nag_sent: float = 0.0  # timestamp of last dispatch-pause nag
        self._last_orphan_prune: float = 0.0    # timestamp of last worktree orphan prune
        # inotify state — populated by _init_inotify()
        self._inotify: Any = None                        # INotify instance
        self._use_inotify: bool = False
        self._wd_to_session: dict[int, str] = {}         # file wd → tmux_name
        self._dir_wd_sessions: dict[int, set[str]] = {}  # dir wd → set of tmux_names
        self._dir_path_to_wd: dict[str, int] = {}        # dir path → wd (dedup)
        self._wd_to_dir_path: dict[int, str] = {}        # reverse: wd → dir path

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
        resolution_dir: Path | None = None,
    ) -> None:
        """Register a new session — INSERT into dashboard.db."""
        path_is_dir = False
        path_str: str | None = None
        res_dir: Path | None = resolution_dir
        if jsonl_path is not None:
            if jsonl_path.is_dir():
                path_is_dir = True
                # Don't store directory as jsonl_path — store None, resolve later
                path_str = None
                # Use the directory as resolution_dir if not explicitly provided
                if res_dir is None:
                    res_dir = jsonl_path
            else:
                path_str = str(jsonl_path)
                # Derive resolution_dir from file parent if not explicitly provided
                if res_dir is None:
                    res_dir = jsonl_path.parent

        try:
            insert_session(
                tmux_name=tmux_name,
                session_type=session_type,
                project=project,
                bead_id=bead_id,
                jsonl_path=path_str,
                session_uuid=session_uuid,
                resolution_dir=str(res_dir) if res_dir else None,
            )
        except Exception:
            logger.warning("session_monitor: INSERT failed for tmux=%s (may already exist)", tmux_name)
            return

        if seed_message:
            update_tail_state(tmux_name, last_message=seed_message)

        # Set up ephemeral tail state
        ts = _TailState(needs_resolution=path_is_dir, resolution_dir=res_dir)
        self._tail_states[tmux_name] = ts

        # Add inotify watches if available
        if self._use_inotify:
            if path_str:
                self._add_file_watch(tmux_name, path_str)
            if res_dir:
                self._add_dir_watch(tmux_name, str(res_dir))

        logger.info(
            "session_monitor: registered %s  type=%s  project=%s  jsonl=%s",
            tmux_name, session_type, project,
            "pending" if path_str is None else Path(path_str).name,
        )
        await self._broadcast_registry()

    async def register_revived(
        self,
        tmux_name: str,
        jsonl_path: Path | None = None,
    ) -> None:
        """Re-register a revived session for tailing.

        The DB row already exists and has been set to is_live=1 by revive_session().
        This just sets up the in-memory tail state and inotify watches so the
        session monitor starts tailing the JSONL file again.
        """
        path_str: str | None = None
        res_dir: Path | None = None
        path_is_dir = False

        if jsonl_path is not None:
            if jsonl_path.is_dir():
                path_is_dir = True
                res_dir = jsonl_path
            else:
                path_str = str(jsonl_path)
                res_dir = jsonl_path.parent

        ts = _TailState(needs_resolution=path_is_dir, resolution_dir=res_dir)
        self._tail_states[tmux_name] = ts

        if self._use_inotify:
            if path_str:
                self._add_file_watch(tmux_name, path_str)
            if res_dir:
                self._add_dir_watch(tmux_name, str(res_dir))

        logger.info(
            "session_monitor: revived %s  jsonl=%s",
            tmux_name,
            "pending" if path_str is None else Path(path_str).name,
        )
        await self._broadcast_registry()

    async def deregister(self, tmux_name: str) -> None:
        """Mark a session as dead and remove from tail states."""
        self._remove_watches(tmux_name)
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
        from tools.dashboard.org_identity import resolve_session_org
        sessions = get_live_sessions()
        out = []
        for s in sessions:
            entry = {
                "session_id": s["tmux_name"],
                "project": s["project"],
                "type": s["type"],
                "is_live": bool(s["is_live"]),
                "started_at": s["created_at"],
                "graph_source_id": s.get("graph_source_id"),
                "label": s.get("label", ""),
                "role": s.get("role", ""),
                "entry_count": s.get("entry_count", 0),
                "context_tokens": s.get("context_tokens", 0),
                "last_activity": s.get("last_activity") or s["created_at"],
                "last_message": s.get("last_message", ""),
                "topics": json.loads(s.get("topics") or "[]"),
                "nag_enabled": bool(s.get("nag_enabled")),
                "nag_interval": s.get("nag_interval") or 15,
                "nag_message": s.get("nag_message") or "",
                "dispatch_nag_enabled": bool(s.get("dispatch_nag")),
                "activity_state": s.get("activity_state", "idle"),
                # jsonl_path is the legacy bridge; session_uuids is canonical after Phase 4
                "resolved": bool(s.get("jsonl_path")) or (
                    bool(s.get("session_uuids")) and s["session_uuids"] != "[]"
                ),
            }
            entry["org"] = resolve_session_org(entry)
            out.append(entry)
        return out

    # ── Background tasks ──────────────────────────────────────────

    async def start(self, event_bus=None, entry_parser=None, entry_enricher=None) -> None:
        """Start background tailer and liveness tasks."""
        if self._started:
            return
        self._started = True
        self._event_bus = event_bus
        self._entry_parser = entry_parser
        self._entry_enricher = entry_enricher
        self._init_inotify()
        self._tailer_task = asyncio.create_task(self._inotify_tailer_loop())
        self._liveness_task = asyncio.create_task(self._liveness_loop())
        logger.info("session_monitor: background tasks started (mode=inotify)")
        # Re-scan unresolved container sessions from prior server lifetime
        self._recover_unresolved_sessions()
        # Broadcast registry for any sessions that exist in DB
        if count_live() > 0:
            await self._broadcast_registry()

    async def stop(self) -> None:
        """Cancel background tasks and reset state so start() can be called again."""
        if not self._started:
            return
        tasks = [t for t in (self._tailer_task, self._liveness_task) if t and not t.done()]
        for t in tasks:
            try:
                t.cancel()
            except RuntimeError:
                pass  # task belongs to a different event loop (e.g. TestClient teardown)
        try:
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        except RuntimeError:
            pass  # cross-loop gather — tasks will be GC'd with their loop
        self._tailer_task = None
        self._liveness_task = None
        self._started = False
        logger.info("session_monitor: background tasks stopped")

    def _recover_unresolved_sessions(self) -> None:
        """On startup, recreate TailState for live sessions with NULL jsonl_path.

        For container sessions: derive resolution_dir from agent-runs, set needs_resolution.
        For host sessions: attempt .session_meta.json scan; if that fails, add dir watch.
        For all unresolved sessions with resolution_dir: add IN_CREATE dir watch.
        """
        sessions = get_live_sessions()
        agent_runs = Path(__file__).resolve().parents[2] / "data" / "agent-runs"
        recovered = 0
        for row in sessions:
            tmux_name = row["tmux_name"]
            if row.get("jsonl_path"):
                # Already resolved — ensure dir watch exists for rollover detection
                res_dir = row.get("resolution_dir") or str(Path(row["jsonl_path"]).parent)
                if self._use_inotify and res_dir:
                    if tmux_name not in self._tail_states:
                        self._tail_states[tmux_name] = _TailState(
                            resolution_dir=Path(res_dir),
                        )
                    self._add_dir_watch(tmux_name, res_dir)
                continue
            if row.get("type") == "host":
                # Host resolution: scan Claude projects dirs for .meta.json matching tmux_name
                jsonl = self._resolve_host_jsonl(tmux_name)
                if jsonl:
                    from tools.dashboard.dao.dashboard_db import link_and_enrich
                    link_and_enrich(
                        tmux_name,
                        session_uuid=jsonl.stem,
                        jsonl_path=str(jsonl),
                        project=jsonl.parent.name,
                    )
                    recovered += 1
                    logger.info("session_monitor: recovered host %s → %s", tmux_name, jsonl.name)
                    # Add dir watch for future rollovers
                    if self._use_inotify:
                        self._add_dir_watch(tmux_name, str(jsonl.parent))
                else:
                    # Unresolved host — add dir watch on resolution_dir if known
                    res_dir = row.get("resolution_dir")
                    if res_dir:
                        if tmux_name not in self._tail_states:
                            self._tail_states[tmux_name] = _TailState(
                                needs_resolution=True,
                                resolution_dir=Path(res_dir),
                            )
                        if self._use_inotify:
                            self._add_dir_watch(tmux_name, res_dir)
                        recovered += 1
                        logger.info("session_monitor: RECOVERED unresolved host %s → watch %s", tmux_name, res_dir)
                continue
            if tmux_name in self._tail_states:
                continue  # already has a tail state
            # Derive resolution_dir from agent-runs/{tmux_name}-*/sessions/
            run_dirs = sorted(
                agent_runs.glob(f"{tmux_name}-*"),
                key=lambda p: p.stat().st_mtime, reverse=True,
            ) if agent_runs.exists() else []
            if run_dirs:
                sess_dir = run_dirs[0] / "sessions"
                if sess_dir.exists():
                    self._tail_states[tmux_name] = _TailState(
                        needs_resolution=True,
                        resolution_dir=sess_dir,
                    )
                    # Add IN_CREATE dir watch for file discovery
                    if self._use_inotify:
                        self._add_dir_watch(tmux_name, str(sess_dir))
                    recovered += 1
                    logger.info("session_monitor: RECOVERED unresolved %s → %s", tmux_name, sess_dir)
        if recovered:
            logger.info("session_monitor: recovered %d unresolved sessions on startup", recovered)

    def _resolve_host_jsonl(self, tmux_name: str) -> Path | None:
        """Find JSONL for a host session by scanning .meta.json files."""
        claude_projects = Path.home() / ".claude" / "projects"
        if not claude_projects.exists():
            return None
        for meta_path in sorted(
            claude_projects.rglob("*.meta.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        ):
            try:
                data = json.loads(meta_path.read_text())
                if data.get("tmux_session") == tmux_name:
                    jsonl = meta_path.parent / (meta_path.stem.removesuffix(".meta") + ".jsonl")
                    if jsonl.exists():
                        return jsonl
            except (json.JSONDecodeError, OSError):
                continue
        return None

    async def _broadcast_registry(self) -> None:
        """Push session registry to SSE subscribers."""
        if self._event_bus is None:
            return
        await self._event_bus.broadcast("session:registry", self.get_registry())

    # ── inotify watch management ─────────────────────────────────

    def _init_inotify(self) -> None:
        """Try to initialise inotify.  Falls back to polling on failure."""
        if not _HAS_INOTIFY:
            logger.info("session_monitor: inotify_simple not available, using polling")
            return
        try:
            self._inotify = INotify()
            self._use_inotify = True
            # Add watches for sessions already in the DB
            for row in get_tailable_sessions():
                self._add_file_watch(row["tmux_name"], row["jsonl_path"])
                dir_path = row.get("resolution_dir") or str(Path(row["jsonl_path"]).parent)
                self._add_dir_watch(row["tmux_name"], dir_path)
            logger.info(
                "session_monitor: inotify initialised — %d file watches, %d dir watches",
                len(self._wd_to_session), len(self._dir_path_to_wd),
            )
        except OSError as exc:
            logger.warning("session_monitor: inotify init failed (%s), using polling", exc)
            self._inotify = None
            self._use_inotify = False

    def _add_file_watch(self, tmux_name: str, jsonl_path: str) -> None:
        """Add an IN_MODIFY watch on a session's JSONL file."""
        if not self._inotify:
            return
        ts = self._tail_states.get(tmux_name)
        # Remove stale watch if present
        if ts and ts.watch_descriptor is not None:
            try:
                self._inotify.rm_watch(ts.watch_descriptor)
            except OSError:
                pass
            self._wd_to_session.pop(ts.watch_descriptor, None)
            ts.watch_descriptor = None
        try:
            wd = self._inotify.add_watch(jsonl_path, _iflags.MODIFY)
        except OSError as exc:
            logger.warning("session_monitor: add_watch MODIFY failed for %s: %s", tmux_name, exc)
            return
        if ts is None:
            ts = _TailState()
            self._tail_states[tmux_name] = ts
        ts.watch_descriptor = wd
        self._wd_to_session[wd] = tmux_name

    def _add_dir_watch(self, tmux_name: str, dir_path: str) -> None:
        """Add an IN_CREATE watch on a session directory (deduplicated)."""
        if not self._inotify:
            return
        if dir_path in self._dir_path_to_wd:
            # Directory already watched — just add this session to the refcount set
            wd = self._dir_path_to_wd[dir_path]
            self._dir_wd_sessions.setdefault(wd, set()).add(tmux_name)
            ts = self._tail_states.get(tmux_name)
            if ts:
                ts.dir_watch_descriptor = wd
            return
        try:
            wd = self._inotify.add_watch(dir_path, _iflags.CREATE)
        except OSError as exc:
            logger.warning("session_monitor: add_watch CREATE failed for %s: %s", dir_path, exc)
            return
        self._dir_path_to_wd[dir_path] = wd
        self._wd_to_dir_path[wd] = dir_path
        self._dir_wd_sessions[wd] = {tmux_name}
        ts = self._tail_states.get(tmux_name)
        if ts:
            ts.dir_watch_descriptor = wd

    def _remove_watches(self, tmux_name: str) -> None:
        """Remove all inotify watches for a session."""
        if not self._inotify:
            return
        ts = self._tail_states.get(tmux_name)
        if not ts:
            return
        # File watch
        if ts.watch_descriptor is not None:
            try:
                self._inotify.rm_watch(ts.watch_descriptor)
            except OSError:
                pass
            self._wd_to_session.pop(ts.watch_descriptor, None)
            ts.watch_descriptor = None
        # Dir watch (deduplicated — only remove kernel watch when refcount hits 0)
        if ts.dir_watch_descriptor is not None:
            wd = ts.dir_watch_descriptor
            sessions = self._dir_wd_sessions.get(wd, set())
            sessions.discard(tmux_name)
            if not sessions:
                try:
                    self._inotify.rm_watch(wd)
                except OSError:
                    pass
                self._dir_wd_sessions.pop(wd, None)
                self._wd_to_dir_path.pop(wd, None)
                for path, w in list(self._dir_path_to_wd.items()):
                    if w == wd:
                        del self._dir_path_to_wd[path]
                        break
            ts.dir_watch_descriptor = None

    # ── JSONL Tailer ──────────────────────────────────────────────

    async def _process_tail_entries(
        self, tmux_name: str, row: dict, ts: _TailState, new_entries: list,
    ) -> None:
        """Dedup, enrich, and broadcast parsed entries from a session tail read."""
        if not new_entries:
            return
        # Dedup queued messages — tracker persists across unrelated entries
        # (assistant turns, tool_result) because the duplicate typically
        # arrives AFTER the agent's assistant response, not immediately.
        deduped = []
        for entry in new_entries:
            if entry.get("queued"):
                ts.last_enqueue_content = entry.get("content", "").strip()
                deduped.append(entry)
            elif (entry.get("type") in ("user", "crosstalk")
                  and ts.last_enqueue_content
                  and entry.get("content", "").strip() == ts.last_enqueue_content):
                ts.last_enqueue_content = None
            else:
                deduped.append(entry)
        new_entries = deduped

        # Track pending_tool_ids and last_entry_type for activity_state
        for entry in new_entries:
            etype = entry.get("type", "")
            if etype == "tool_use" and entry.get("tool_id"):
                tid = entry["tool_id"]
                if tid not in ts.completed_tool_ids:
                    ts.pending_tool_ids.add(tid)
            elif etype in ("tool_result", "semantic_bash") and entry.get("tool_id"):
                tid = entry["tool_id"]
                ts.pending_tool_ids.discard(tid)
                ts.completed_tool_ids.add(tid)
            if etype:
                ts.last_entry_type = etype

        # Derive activity_state from pending_tool_ids and last_entry_type
        if ts.pending_tool_ids:
            activity_state = "tool_running"
        elif ts.last_entry_type in ("user", "tool_result", "crosstalk"):
            activity_state = "thinking"
        else:
            activity_state = "idle"
        update_activity_state(tmux_name, activity_state)

        # Soft-update the registry cache so new SSE connections get fresh
        # metadata. No broadcast — existing clients already have current
        # data via session:messages.
        if self._event_bus:
            self._event_bus.update_cache("session:registry", self.get_registry())

        self._enrich_agent_entries(row, ts, new_entries)
        if new_entries and self._event_bus:
            ts.broadcast_seq += 1
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
                    "activity_state": activity_state,
                    "pending_tool_ids": sorted(ts.pending_tool_ids),
                },
                dedup=False,
            )

    # ── inotify tailer ────────────────────────────────────────────

    async def _inotify_tailer_loop(self) -> None:
        """inotify-driven tailer — instant delivery on IN_MODIFY, 1s fallback tick."""
        while True:
            try:
                # Block up to 1 s waiting for inotify events
                events = await asyncio.to_thread(self._inotify.read, timeout=1000)

                # Collect sessions whose JONLs were modified
                modified: set[str] = set()
                for event in events:
                    if event.mask & _iflags.MODIFY:
                        name = self._wd_to_session.get(event.wd)
                        if name:
                            modified.add(name)
                    if event.mask & _iflags.CREATE:
                        await self._handle_in_create(event)
                    if event.mask & _iflags.IGNORED:
                        # Kernel auto-removed the watch (file deleted/moved)
                        gone = self._wd_to_session.pop(event.wd, None)
                        if gone:
                            ts = self._tail_states.get(gone)
                            if ts and ts.watch_descriptor == event.wd:
                                ts.watch_descriptor = None

                # Read + broadcast for each modified session
                for tmux_name in modified:
                    row = get_session(tmux_name)
                    if not row or not row.get("jsonl_path"):
                        continue
                    if tmux_name not in self._tail_states:
                        self._tail_states[tmux_name] = _TailState()
                    ts = self._tail_states[tmux_name]
                    _, new_entries = await asyncio.to_thread(self._tail_one, row, ts)
                    await self._process_tail_entries(tmux_name, row, ts, new_entries)

            except Exception:
                logger.exception("session_monitor: inotify tailer error")
                await asyncio.sleep(1)

    # ── IN_CREATE handling ───────────────────────────────────────

    async def _handle_in_create(self, event) -> None:
        """Handle IN_CREATE on a watched directory — new file or subdirectory appeared."""
        filename = event.name
        if not filename:
            return

        dir_path = self._wd_to_dir_path.get(event.wd)
        if not dir_path:
            return
        new_path = Path(dir_path) / filename

        # Subdirectory created — extend watch into it for the same sessions.
        # This handles the Claude Code layout where JSONL lands inside a
        # project subdirectory (e.g. sessions/-workspace-repo/*.jsonl).
        if new_path.is_dir() and "subagents" not in filename:
            sessions = self._dir_wd_sessions.get(event.wd, set())
            for tmux_name in list(sessions):
                self._add_dir_watch(tmux_name, str(new_path))
            return

        if not filename.endswith(".jsonl"):
            return
        # Skip subagent paths (should not fire due to non-recursive watches,
        # but guard defensively)
        if "subagents" in filename:
            return

        new_file = new_path
        sessions = self._dir_wd_sessions.get(event.wd, set())
        if not sessions:
            return

        # Determine session types sharing this directory
        # Container sessions have isolated dirs (one session per dir)
        # Host sessions share dirs (multiple sessions per dir)
        for tmux_name in list(sessions):
            row = get_session(tmux_name)
            if not row:
                continue
            session_type = row.get("type", "container")
            if session_type == "container":
                await self._handle_container_create(tmux_name, row, new_file)
            else:
                await self._handle_host_create(tmux_name, row, new_file, dir_path)

    async def _handle_container_create(
        self, tmux_name: str, row: dict, new_file: Path,
    ) -> None:
        """Handle IN_CREATE in an isolated container directory.

        Any new JSONL in a container's resolution_dir belongs to this session.
        """
        new_uuid = new_file.stem
        uuids = json.loads(row.get("session_uuids") or "[]")
        was_empty = len(uuids) == 0

        from tools.dashboard.dao.dashboard_db import link_and_enrich
        link_and_enrich(
            tmux_name,
            session_uuid=new_uuid,
            jsonl_path=str(new_file),
            project=new_file.parent.name,
        )
        logger.info(
            "session_monitor: IN_CREATE container %s → %s%s",
            tmux_name, new_file.name,
            " (first file — resolved)" if was_empty else " (rollover)",
        )

        # Swap IN_MODIFY watch to new file
        self._add_file_watch(tmux_name, str(new_file))

        # Reset file_offset for new file
        update_tail_state(tmux_name, file_offset=0)

        # Reset ephemeral tail state, preserving resolution_dir
        ts = self._tail_states.get(tmux_name)
        old_resolution_dir = ts.resolution_dir if ts else None
        self._tail_states[tmux_name] = _TailState(resolution_dir=old_resolution_dir)

        if was_empty:
            await self._broadcast_registry()

    async def _handle_host_create(
        self, tmux_name: str, row: dict, new_file: Path, dir_path: str,
    ) -> None:
        """Handle IN_CREATE in a shared host project directory.

        Must read parentUuid to determine if this is a rollover for an existing
        session or a brand-new session. NEVER use mtime for host resolution.
        """
        # Read parentUuid from first line
        parent_uuid = await asyncio.to_thread(self._read_parent_uuid, new_file)

        if parent_uuid is None:
            # New session, not a rollover. Ignore — wait for .session_meta.json
            # or linking handshake.
            return

        # parentUuid is non-null → rollover. Find predecessor.
        predecessor_uuid = await asyncio.to_thread(
            self._find_predecessor_by_parentuuid,
            parent_uuid, new_file, dir_path,
        )

        if predecessor_uuid is None:
            logger.warning(
                "session_monitor: unexpected rollover — no predecessor found. "
                "file=%s parentUuid=%s dir=%s",
                new_file.name, parent_uuid, dir_path,
            )
            return

        # Look up which session owns the predecessor UUID
        owner = self._find_session_by_uuid(predecessor_uuid)
        if not owner:
            logger.warning(
                "session_monitor: unexpected rollover — predecessor UUID not in DB. "
                "file=%s parentUuid=%s predecessor=%s dir=%s",
                new_file.name, parent_uuid, predecessor_uuid, dir_path,
            )
            return

        new_uuid = new_file.stem
        from tools.dashboard.dao.dashboard_db import link_and_enrich
        link_and_enrich(
            owner,
            session_uuid=new_uuid,
            jsonl_path=str(new_file),
            project=new_file.parent.name,
        )
        logger.info(
            "session_monitor: IN_CREATE host rollover %s → %s (predecessor %s)",
            owner, new_file.name, predecessor_uuid,
        )

        # Swap IN_MODIFY watch to new file
        self._add_file_watch(owner, str(new_file))

        # Reset file_offset for new file
        update_tail_state(owner, file_offset=0)

        # Reset ephemeral tail state, preserving resolution_dir
        ts = self._tail_states.get(owner)
        old_resolution_dir = ts.resolution_dir if ts else None
        self._tail_states[owner] = _TailState(resolution_dir=old_resolution_dir)

    @staticmethod
    def _read_parent_uuid(jsonl_path: Path) -> str | None:
        """Read parentUuid from the first line of a JSONL file.

        Returns None if the file is empty, unreadable, or parentUuid is null.
        """
        try:
            with open(jsonl_path) as f:
                first_line = f.readline().strip()
                if not first_line:
                    return None
                entry = json.loads(first_line)
                parent = entry.get("parentUuid")
                return parent if parent else None
        except (OSError, json.JSONDecodeError):
            return None

    @staticmethod
    def _find_predecessor_by_parentuuid(
        parent_uuid: str, new_file: Path, dir_path: str,
    ) -> str | None:
        """Find which JSONL file contains the entry with uuid == parentUuid.

        Uses grep -rl to find the predecessor file, then returns its filename stem (UUID).
        """
        try:
            result = subprocess.run(
                ["grep", "-rl", "--include=*.jsonl",
                 f"--exclude={new_file.name}",
                 parent_uuid, dir_path],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                # May return multiple files; take the first match
                match_path = Path(result.stdout.strip().split("\n")[0])
                return match_path.stem
        except (subprocess.TimeoutExpired, OSError):
            pass
        return None

    @staticmethod
    def _find_session_by_uuid(uuid: str) -> str | None:
        """Find the tmux_name that owns a given JSONL UUID in session_uuids."""
        conn = get_conn()
        row = conn.execute(
            "SELECT tmux_name FROM tmux_sessions"
            " WHERE is_live=1 AND session_uuids LIKE ?",
            (f"%{uuid}%",),
        ).fetchone()
        return row["tmux_name"] if row else None

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

    # ── Liveness Checker ──────────────────────────────────────────

    _COOLDOWN_SECONDS = 30.0
    _ORPHAN_PRUNE_INTERVAL = 600.0  # seconds between orphan worktree prunes

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
                        self._remove_watches(tmux_name)
                        # Clear pending_tool_ids before marking dead —
                        # dead sessions cannot have running tools
                        ts = self._tail_states.get(tmux_name)
                        if ts:
                            ts.pending_tool_ids.clear()
                        mark_dead(tmux_name)
                        # Re-ingest completed session into graph (final state)
                        jsonl_path = row.get("jsonl_path")
                        if jsonl_path:
                            try:
                                subprocess.run(
                                    ["graph", "ingest-session", jsonl_path],
                                    capture_output=True, text=True, timeout=30,
                                )
                            except Exception:
                                pass  # best-effort; cron catch-up covers failures
                        self._tail_states.pop(tmux_name, None)
                        # Clean up the session's workspace worktrees (no-op
                        # if the session had none). Uncommitted changes or
                        # unpushed commits are preserved with a warning.
                        await asyncio.to_thread(
                            _cleanup_worktrees_for_dead_session, tmux_name,
                        )
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

                # Nag check — send CrossTalk to idle sessions with nag enabled
                for row in sessions:
                    if not row.get("nag_enabled"):
                        continue
                    if not row.get("is_live"):
                        continue
                    tmux_name = row["tmux_name"]
                    # Skip sessions whose tmux is dead (just marked above)
                    alive = await asyncio.to_thread(self._check_tmux, tmux_name)
                    if not alive:
                        continue
                    last_act = row.get("last_activity") or row["created_at"]
                    nag_interval = (row.get("nag_interval") or 15) * 60
                    nag_last_sent = row.get("nag_last_sent") or 0
                    idle_secs = now - last_act

                    if idle_secs >= nag_interval and (now - nag_last_sent) >= nag_interval:
                        logger.info("session_monitor: nag firing for %s (idle %ds)", tmux_name, int(idle_secs))
                        nag_msg = row.get("nag_message") or f"You've been idle for {int(idle_secs // 60)}m. Status update?"
                        await asyncio.to_thread(_send_nag_crosstalk, tmux_name, nag_msg)
                        update_nag_last_sent(tmux_name, now)
                    else:
                        logger.debug("session_monitor: nag skip %s (idle=%ds interval=%ds since_nag=%ds)", tmux_name, int(idle_secs), nag_interval, int(now - nag_last_sent))

                # Dispatch pause nag — alert dispatch_nag subscribers when queue is stuck
                await self._check_dispatch_pause_nag(now)

                # Periodic worktree orphan prune — every 10 minutes scan
                # data/worktrees/ and clean up anything that doesn't match
                # a live session. Picks up worktrees orphaned by crashes
                # or missed death signals.
                if (now - self._last_orphan_prune) >= self._ORPHAN_PRUNE_INTERVAL:
                    live_names = [r["tmux_name"] for r in get_live_sessions()]
                    await asyncio.to_thread(
                        _prune_orphan_worktrees_safely, live_names,
                    )
                    self._last_orphan_prune = now

            except Exception:
                logger.exception("session_monitor: liveness error")
            await asyncio.sleep(10)

    _PAUSE_NAG_INTERVAL = 15 * 60  # 15 minutes between dispatch-pause nags

    async def _check_dispatch_pause_nag(self, now: float) -> None:
        """Send periodic nag to dispatch_nag subscribers when dispatch is paused."""
        if (now - self._last_pause_nag_sent) < self._PAUSE_NAG_INTERVAL:
            return  # Too soon since last pause nag

        pause_msg = await asyncio.to_thread(_get_dispatch_pause_message)
        if not pause_msg:
            return  # Not paused

        subscribers = get_dispatch_nag_sessions()
        if not subscribers:
            return

        # Check at least one subscriber is alive before sending
        sent = False
        for tmux_name in subscribers:
            alive = await asyncio.to_thread(self._check_tmux, tmux_name)
            if alive:
                logger.info(
                    "session_monitor: dispatch pause nag → %s: %s",
                    tmux_name, pause_msg,
                )
                await asyncio.to_thread(_send_nag_crosstalk, tmux_name, pause_msg)
                sent = True

        if sent:
            self._last_pause_nag_sent = now

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
                jsonls = _find_primary_jsonls(sess_dir)
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
                    resolution_dir=str(jsonl.parent),
                    session_uuids=json.dumps([jsonl.stem]),
                    curr_jsonl_file=str(jsonl),
                    created_at=st.st_mtime - 60,
                    file_offset=st.st_size,
                    last_message=seed_msg,
                    is_live=True,
                )
                logger.info("session_monitor: seeded container %s  uuid=%s  project=%s", tmux_name, jsonl.stem[:12], jsonl.parent.name)
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
                    resolution_dir=str(jsonl.parent),
                    session_uuids=json.dumps([jsonl.stem]),
                    curr_jsonl_file=str(jsonl),
                    created_at=st.st_mtime - 60,
                    file_offset=st.st_size,
                    last_message=seed_msg,
                    is_live=True,
                )
                logger.info("session_monitor: seeded host %s  uuid=%s  project=%s", tmux_name, jsonl.stem[:12], jsonl.parent.name)
                dashboard_tmux.discard(tmux_name)
                seeded += 1

        # ENRICH: resolve graph_source_id for all seeded sessions via graph ingest-session
        enriched = 0
        for row in get_live_sessions():
            if row.get("session_uuid") and not row.get("graph_source_id") and row.get("jsonl_path"):
                try:
                    result = subprocess.run(
                        ["graph", "ingest-session", row["jsonl_path"]],
                        capture_output=True, text=True, timeout=30,
                    )
                    graph_id = result.stdout.strip()
                    if result.returncode == 0 and graph_id:
                        from tools.dashboard.dao.dashboard_db import update_graph_source
                        update_graph_source(row["tmux_name"], graph_id)
                        enriched += 1
                except Exception:
                    pass
        if enriched:
            logger.info("session_monitor: enriched %d sessions with graph_source_id", enriched)

        logger.info("session_monitor: seeded %d sessions from filesystem", seeded)


# Module-level singleton
session_monitor = SessionMonitor()
