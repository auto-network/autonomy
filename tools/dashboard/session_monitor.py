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
import copy
import json
import logging
import os
import re
import subprocess
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from tools.dashboard.session_lifecycle_worker import (
    REAPER_BELT_MARGIN_S,
    STEP_TIMEOUTS_S,
    derive_lifecycle_state,
)
from tools.dashboard import session_harness as session_harness_mod
from tools.dashboard.session_harness import (
    CLAUDE_HARNESS,
    SessionHarness,
    resolve_harness_for_path,
    resolve_harness_for_session_row,
)
from tools.dashboard import harness_usage_settings as _harness_usage_settings
from tools.graph import ops as graph_ops

from tools.dashboard.dao.dashboard_db import (
    get_conn,
    get_dispatch_nag_sessions,
    get_all_sessions,
    get_live_sessions,
    get_session,
    get_tailable_sessions,
    insert_session,
    mark_dead,
    delete_session,
    update_activity_state,
    update_tail_state,
    update_nag_last_sent,
    update_todos,
    count_live,
)

from agents.workspace_manager import (
    CleanupResult,
    WORKTREES_DIR,
    cleanup_session_worktrees,
)

logger = logging.getLogger(__name__)

# inotify — optional, falls back to polling if unavailable
try:
    from inotify_simple import INotify, flags as _iflags
    _HAS_INOTIFY = True
except ImportError:
    _HAS_INOTIFY = False


# ── Unified startup-state FSM (worker-owned) ────────────────────────
#
# Single column ``startup_state`` on tmux_sessions. NULL is the meaningful
# default (existing rows / sessions past launching). Non-NULL values are
# the in-progress launching states the session cards render as chips.
#
# WRITER — exactly one, by contract (graph://92ed929a-3ec): the
# transition authority (SessionLifecycleStateWriter.transition on the
# shared STATE_AUTHORITY instance). The worker's steps, arm_startup_state
# below (launch entry), mark_dead (death detection), and boot recovery are
# all causes routed through it; it validates the move against the legality
# matrix and stamps state + the legacy write-through projections in one
# atomic UPDATE. The historical multi-writer design (setup-exit watcher,
# screen-poller, inject tasks, tailer clear) raced — the launching
# flip-flop/stuck class validated 11× in graph://afb67d11-7c4 — and was
# removed with the FSM completion.


def _codex_identity_for_row(row: dict[str, Any]) -> tuple[str, str]:
    # Future-proof the key space for multiple Codex subscriptions. Launch-time
    # auth-slot metadata is not available on session rows yet, so we fall back
    # to the current singleton identity.
    _ = row
    return ("default", "default")


def _publish_codex_harness_usage_setting(
    row: dict[str, Any],
    harness_state: dict[str, Any] | None,
) -> bool:
    state = harness_state if isinstance(harness_state, dict) else {}
    windows = state.get("windows")
    if state.get("kind") != "rate_limits" or not isinstance(windows, dict) or not windows:
        return False

    identity_id, identity_label = _codex_identity_for_row(row)
    key = _harness_usage_settings.make_harness_usage_key("codex", identity_id)
    payload = _harness_usage_settings.normalize_codex_usage_payload(
        state,
        identity_id=identity_id,
        identity_label=identity_label,
    )
    return _harness_usage_settings.publish_if_changed(
        key,
        payload,
        upsert_by_key=graph_ops.upsert_by_key,
    )


@dataclass(frozen=True)
class _CodexRolloutClassification:
    """Identity decision for a rollout candidate.

    ``unknown`` is intentionally distinct from ``main``.  Treating an empty or
    partially-written file as "not a subagent" caused the monitor to persist a
    spawned child as a parent-session rollover.
    """

    kind: Literal["main", "subagent", "unknown"]
    reason: str
    size_bytes: int | None


def _classify_codex_rollout(jsonl_path: Path) -> _CodexRolloutClassification:
    """Classify a Codex rollout without failing open on incomplete content."""

    # Claude and other harnesses do not use the rollout filename contract.
    if not jsonl_path.name.startswith("rollout-"):
        return _CodexRolloutClassification("main", "not_codex_rollout", None)

    try:
        size_bytes = jsonl_path.stat().st_size
    except OSError:
        return _CodexRolloutClassification("unknown", "stat_failed", None)

    try:
        with open(jsonl_path, encoding="utf-8") as f:
            line = f.readline().strip()
    except OSError:
        return _CodexRolloutClassification("unknown", "read_failed", size_bytes)
    except UnicodeDecodeError:
        return _CodexRolloutClassification("unknown", "decode_failed", size_bytes)
    if not line:
        return _CodexRolloutClassification("unknown", "empty", size_bytes)
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return _CodexRolloutClassification("unknown", "partial_json", size_bytes)
    if not isinstance(entry, dict) or entry.get("type") != "session_meta":
        return _CodexRolloutClassification(
            "unknown", "missing_session_meta", size_bytes,
        )
    payload = entry.get("payload")
    if not isinstance(payload, dict):
        return _CodexRolloutClassification("unknown", "invalid_payload", size_bytes)
    if payload.get("forked_from_id"):
        return _CodexRolloutClassification(
            "subagent", "forked_from_id", size_bytes,
        )
    source = payload.get("source")
    if isinstance(source, dict) and "subagent" in source:
        return _CodexRolloutClassification(
            "subagent", "source.subagent", size_bytes,
        )
    return _CodexRolloutClassification("main", "session_meta_main", size_bytes)


def _is_primary_jsonl(jsonl_path: Path) -> bool:
    """True only for a path that is positively identified as a main trace."""

    if "subagents" in jsonl_path.parts:
        return False
    return _classify_codex_rollout(jsonl_path).kind == "main"


def _find_primary_jsonls(directory: Path) -> list[Path]:
    """Find a session's main-thread JSONL rollouts, excluding subagent traces.

    Two DIFFERENT subagent representations must both be excluded, or the
    selection can latch onto a subagent and freeze the viewer:
      - Claude subagents live under a ``subagents/`` subdirectory (path-based).
      - Codex forked subagents write sibling ``rollout-*.jsonl`` files in the
        SAME directory as the parent; they're only distinguishable by their
        ``session_meta`` header (``forked_from_id`` / ``source.subagent``) — see
        ``_is_codex_subagent_rollout``. The path check can't catch these.
    """
    return [f for f in directory.rglob("*.jsonl") if _is_primary_jsonl(f)]


def _is_codex_subagent_rollout(jsonl_path: Path) -> bool:
    """True when *jsonl_path* is a forked Codex subagent rollout.

    Forked subagents write sibling ``rollout-*.jsonl`` files in the same
    directory as the parent rollout. Those files must never be treated as
    parent-session rollovers by the session monitor.
    """
    return _classify_codex_rollout(jsonl_path).kind == "subagent"


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
    harness = resolve_harness_for_path(jsonl)
    for line in reversed(chunk.strip().split("\n")):
        try:
            e = json.loads(line)
            text = harness.extract_message_text(e)
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


# A terminal session's worktrees are garbage-collected only after this
# tombstone horizon (seconds since ended_at). Ending a session must never
# synchronously destroy anything: one wrong transition — a raced reaper, a
# false death — must not be able to delete a session's work. The horizon
# gives resumes, retries, and operators an hours-scale window; the
# preserve policy (uncommitted changes / local commits) still applies on
# top, and the operator's explicit Worktrees "Clean up" button remains
# immediate.
WORKTREE_GC_HORIZON_S = float(
    os.environ.get("DASHBOARD_WORKTREE_GC_HORIZON_S", 6 * 3600)
)


def _gc_worktrees_for_terminal_session(tmux_name: str) -> None:
    """One GC step — never raises. The guard re-reads the row at EXECUTION
    time: a resume/retry can re-enter the FSM between scheduling and
    execution (proven: auto-0708-122535 revived 19s after its stop and a
    queued cleanup removed the worktree 1s into the relaunch), so removal
    requires the row to be terminal AND past the tombstone horizon NOW.
    A dir with no row at all is an orphan and proceeds straight to the
    preserve-policy cleanup.
    """
    try:
        row = get_session(tmux_name)
    except Exception:
        row = None
    if row is not None:
        state = derive_lifecycle_state(row)
        if state not in ("ENDED", "FAILED"):
            logger.info(
                "session_monitor: worktree GC skipped for %s — state=%s",
                tmux_name, state,
            )
            return
        ended_at = row.get("ended_at") or 0
        if (time.time() - ended_at) < WORKTREE_GC_HORIZON_S:
            return
    try:
        result = cleanup_session_worktrees(tmux_name, worktrees_dir=WORKTREES_DIR)
    except Exception:
        logger.exception("session_monitor: worktree GC raised for %s", tmux_name)
        return
    _log_worktree_cleanup(tmux_name, result)


def _worktree_gc_pass() -> None:
    """Tombstoned worktree GC — the ONLY path that removes worktree dirs
    outside an explicit operator action or a fresh-create failure cleanup.
    Scans data/worktrees/ and runs the per-dir step for every dir whose
    session is not kept. Kept: every non-terminal session, and terminal
    sessions still inside the tombstone horizon. Absorbs the old
    orphan-prune (a dir with no row is just a keep-set miss here).
    """
    try:
        if not WORKTREES_DIR.exists():
            return
        now = time.time()
        keep: set[str] = set()
        for row in get_all_sessions():
            state = derive_lifecycle_state(row)
            if state not in ("ENDED", "FAILED"):
                keep.add(row["tmux_name"])
            elif (now - (row.get("ended_at") or 0)) < WORKTREE_GC_HORIZON_S:
                keep.add(row["tmux_name"])
        for entry in sorted(WORKTREES_DIR.iterdir()):
            if not entry.is_dir() or entry.name in keep:
                continue
            _gc_worktrees_for_terminal_session(entry.name)
    except Exception:
        logger.exception("session_monitor: worktree GC pass raised")


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
        f'           harness="dashboard" model=""\n'
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


def _build_mission_control_nag_message(entries: list[dict]) -> str:
    """entries: open (unanswered) mission_conversation rows for one
    coordinator_session, as returned by
    mission_control_db.list_coordinators_with_open_questions."""
    n = len(entries)
    lines = [f"You have {n} outstanding Mission Control question{'s' if n != 1 else ''}:"]
    for entry in entries[:5]:
        question = entry["question"]
        preview = question if len(question) <= 120 else question[:117] + "..."
        # WHAT IT IS ABOUT, THEN WHAT WAS SAID. A short answer to something a
        # screen raised reads as "- Do it" on its own, which is not a
        # reminder of anything.
        about = entry.get("anchor_title") or entry.get("anchor")
        head = f"- [{about}] {preview}" if about else f"- {preview}"
        lines.append(head)
        # SELF-SUFFICIENT. This used to say "see the reply route in the
        # original relay message", which fails exactly when a reminder
        # matters most: a long-running session whose scrollback is gone.
        # Emit the answer route only when the entry carries the ids it
        # needs. A single malformed entry must not KeyError the whole
        # message — that would blank a coordinator's entire reminder over
        # one bad row. Production entries always carry these; a degraded
        # entry simply loses its route line, not everyone else's.
        scope = None
        if entry.get("pillar_id"):
            scope = f"pillars/{entry['pillar_id']}"
        elif entry.get("mission_id"):
            scope = f"missions/{entry['mission_id']}"
        if scope and entry.get("entry_id"):
            lines.append(
                f"  POST /api/{scope}/questions/{entry['entry_id']}/answer"
                ' {"answer": "..."}'
            )
    if n > 5:
        lines.append(f"...and {n - 5} more.")
    # SAY WHAT THIS IS AND WHAT STOPS IT. A reminder whose recipient cannot
    # identify it cannot be acted on: this is not the session's own idle nag,
    # so `graph set-nag --off` does nothing to it, and a coordinator hunting
    # for a switch finds one that turns off something else. It has no switch
    # by design -- it exists so a question put to a person does not rot -- so
    # the message has to name the thing that actually ends it.
    lines.append(
        "This is Mission Control, not your session's idle nag — "
        "`graph set-nag --off` does not affect it. It stops when the question "
        "is dealt with: answer it, close it if it no longer needs an answer, "
        "or retire it if its subject is gone."
    )
    return "\n".join(lines)


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
class _TaskInfo:
    """Per-task state tracked by TaskStateTracker."""
    task_id: str
    subject: str = ""
    status: str = "pending"
    description: str = ""
    activeForm: str = ""


def dedup_queued_entries(
    entries: list, last_enqueue_content: str | None,
) -> tuple[list, str | None, int]:
    """Drop the log echo of a message already shown as its queued tile.

    Shared by the live tailer and the HTTP read paths (auto-16g9t) so both
    serve the same payload for the same bytes. Returns
    (deduped_entries, new_last_enqueue_content, dropped_count).
    """
    deduped = []
    dropped = 0
    for entry in entries:
        if entry.get("queued"):
            last_enqueue_content = entry.get("content", "").strip()
            deduped.append(entry)
        elif (entry.get("type") in ("user", "crosstalk")
              and last_enqueue_content
              and entry.get("content", "").strip() == last_enqueue_content):
            last_enqueue_content = None
            dropped += 1
        else:
            deduped.append(entry)
    return deduped, last_enqueue_content, dropped


class TaskStateTracker:
    """Walks Task* tool_use entries in order, maintaining taskId→state per session.

    TaskCreate seeds new state; TaskUpdate applies deltas (may rename subject).
    Each Task* entry is mutated in place with a ``todo_annotation`` dict so the
    renderer can display subject, status, and description without replaying the
    log client-side. JSONL is the sole durable source of truth; tracker state
    is ephemeral and rebuilt by replaying entries from offset 0 when needed.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, _SessionTaskState] = {}

    def reset(self, tmux_name: str) -> None:
        """Drop tracked state for a session (used before a full-history replay)."""
        self._sessions.pop(tmux_name, None)

    def enrich(self, tmux_name: str, entries: list[dict]) -> None:
        """Annotate Task* tool_use entries in ``entries`` in place.

        Safe to call with non-Task entries; only TaskCreate/TaskUpdate are touched.
        Entries with no tmux_name (e.g. HTTP-replay) use a stable key — callers
        scope isolation by creating a dedicated tracker instance.
        """
        state = self._sessions.setdefault(tmux_name, _SessionTaskState())
        for entry in entries:
            if entry.get("type") != "tool_use":
                continue
            name = entry.get("tool_name", "")
            if name == "TaskCreate":
                entry["todo_annotation"] = state.on_create(entry.get("input") or {})
            elif name == "TaskUpdate":
                entry["todo_annotation"] = state.on_update(entry.get("input") or {})

    def fork_session(self, tmux_name: str) -> "TaskStateTracker":
        """A NEW tracker seeded with a deep copy of one session's state.

        auto-16g9t: HTTP read paths enrich against a fork of the live
        tracker so Task* tiles resolve taskId→subject exactly as the live
        stream did, without the read mutating (or racing) live state.
        An unknown session forks to an empty tracker — same as the old
        fresh-instance behavior.
        """
        fork = TaskStateTracker()
        state = self._sessions.get(tmux_name)
        if state is not None:
            fork._sessions[tmux_name] = copy.deepcopy(state)
        return fork

    def snapshot(self, tmux_name: str) -> list[dict]:
        """Return the current per-task state for a session, ordered by taskId.

        Each entry is a fresh dict (caller may mutate freely). Returns ``[]``
        for unknown sessions. Ordering follows the ``_next_id`` counter so the
        UI renders tasks in insertion order even when taskIds are reused.
        """
        state = self._sessions.get(tmux_name)
        if state is None or not state.tasks:
            return []

        def _key(task_id: str) -> tuple[int, str]:
            try:
                return (int(task_id), task_id)
            except ValueError:
                return (2**31, task_id)

        out: list[dict] = []
        for tid in sorted(state.tasks.keys(), key=_key):
            info = state.tasks[tid]
            out.append({
                "task_id": info.task_id,
                "subject": info.subject,
                "status": info.status,
                "description": info.description,
                "activeForm": info.activeForm,
            })
        return out


class _SessionTaskState:
    """Per-session map of taskId → _TaskInfo with a sequential id counter.

    Claude Code's TaskCreate payload carries no taskId; ids are assigned by
    observation order starting at "1". TaskUpdate always carries taskId and
    may mutate any field (status, subject, description, activeForm).
    """

    def __init__(self) -> None:
        self.tasks: dict[str, _TaskInfo] = {}
        self._next_id: int = 1

    def on_create(self, inp: dict) -> dict:
        raw_id = inp.get("taskId")
        if raw_id is not None and str(raw_id) != "":
            task_id = str(raw_id)
            # Keep the counter past any explicitly provided numeric id
            try:
                n = int(task_id)
                if n >= self._next_id:
                    self._next_id = n + 1
            except ValueError:
                pass
        else:
            task_id = str(self._next_id)
            self._next_id += 1

        info = _TaskInfo(
            task_id=task_id,
            subject=inp.get("subject", "") or "",
            status=inp.get("status", "") or "pending",
            description=inp.get("description", "") or "",
            activeForm=inp.get("activeForm", "") or "",
        )
        self.tasks[task_id] = info
        return {
            "action": "create",
            "task_id": task_id,
            "subject": info.subject,
            "status": info.status,
            "description": info.description,
            "activeForm": info.activeForm,
        }

    def on_update(self, inp: dict) -> dict:
        task_id = str(inp.get("taskId") or "")
        info = self.tasks.get(task_id)
        prev_status = info.status if info else ""
        prev_subject = info.subject if info else ""
        if info is None:
            info = _TaskInfo(task_id=task_id)
            self.tasks[task_id] = info

        if (v := inp.get("subject")) is not None:
            info.subject = v or ""
        if (v := inp.get("status")) is not None:
            info.status = v or ""
        if (v := inp.get("description")) is not None:
            info.description = v or ""
        if (v := inp.get("activeForm")) is not None:
            info.activeForm = v or ""

        return {
            "action": "update",
            "task_id": task_id,
            "subject": info.subject,
            "status": info.status,
            "description": info.description,
            "activeForm": info.activeForm,
            "prev_status": prev_status,
            "prev_subject": prev_subject if prev_subject != info.subject else "",
        }


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
    # auto-16g9t: per-stream harness state, owned by the LOOP task only.
    # parse_ctx holds cross-line parse state (Codex functions.exec wrapper
    # pairing); postprocess_state holds the progress/tool-name maps. Read
    # workers operate on a COPY (returned in the window dict) and the loop
    # commits it after the shielded await — a cancelled worker finishing
    # late can never mutate live state (B4 purity). HTTP read paths get a
    # deep-copy snapshot via SessionMonitor.snapshot_read_context.
    parse_ctx: dict = field(default_factory=dict)
    postprocess_state: dict | None = None
    # Last queued message content — for deduping against the subsequent user entry
    last_enqueue_content: str | None = None
    # inotify watch descriptors
    watch_descriptor: int | None = None       # IN_MODIFY on active JSONL
    dir_watch_descriptor: int | None = None   # IN_CREATE on session directory
    # Inode of the JSONL the most-recent file watch was registered against.
    # Used as the safety-net signal in the reconciliation loop: if the
    # current path's inode != last_known_inode, the file was replaced (e.g.
    # compaction) and we missed the IN_CREATE event, so we must rewatch.
    last_known_inode: int = 0
    # Activity state tracking — pending tool calls and last entry type
    pending_tool_ids: set = field(default_factory=set)
    completed_tool_ids: set = field(default_factory=set)
    last_entry_type: str = ""
    # True once the tracker has been warmed from historical entries on this run
    task_tracker_warmed: bool = False
    # Last JSON-serialized todos snapshot persisted to DB — used to debounce
    # update_todos() writes so a no-op TaskUpdate doesn't churn the row.
    # None sentinel means "never written on this process run" (forces one write
    # after warm-up so restarts repopulate the DB even without new events).
    last_todos_json: str | None = None
    # /api/diag exposes the last N processed entries (post-_apply_activity_entries)
    # as (type, timestamp, identity) tuples so file/server/client tails can be
    # compared. Bounded ring; never read on the hot path.
    recent_processed: deque = field(default_factory=lambda: deque(maxlen=10))
    # Wall-clock timestamp of the last session:messages broadcast for this
    # session. Used by /api/diag to compute last_broadcast_ago_s.
    last_broadcast_ts: float = 0.0
    # /api/diag depth fields — never read on the hot path.
    parse_errors_count: int = 0
    last_parse_error: str | None = None  # one-line snippet of the most recent error
    last_parse_error_ts: float = 0.0
    lines_processed_total: int = 0  # raw JSONL lines seen by the tailer (parsed or not)
    inotify_events_received: int = 0  # IN_MODIFY events on this session's file watch
    last_inotify_event_ts: float = 0.0
    enqueue_dedup_count: int = 0  # times the queued/user dedup matched
    last_enqueue_dedup_ts: float = 0.0
    last_full_rescan_ts: float = 0.0  # wall-clock ts of last reconciliation hit
    full_rescan_count: int = 0  # times reconciliation promoted this session


# ── Rollout-ingestion state machine (bead auto-suvcp) ───────────────
#
# One `_FileTrack` per (tmux_name, path) — the sole authority for that
# rollout's discovery, characterization, promotion, streaming, and
# cleanup — plus ONE `_SessionGate` per session for drain ownership.
# Formal model: tools/dashboard/TLA/RolloutIngestion.tla (change rule in
# TLA/README.md — observation/dispatch/drain/persistence order changes
# edit the model in the same commit).

# Track states (model: trackState domain).
TRACK_DISCOVERED = "DISCOVERED"
TRACK_CHARACTERIZING = "CHARACTERIZING"
TRACK_STREAMING = "STREAMING"
TRACK_IGNORED = "IGNORED"
TRACK_CLOSED = "CLOSED"

# Characterization deadline (Rule 7 / T110): enforced by the TIME-driven
# reconciliation tick, so worst-case expiry is deadline + one tick
# interval. Sized against container-under-load startup (B3), not local
# tests — a slow Codex boot can take tens of seconds to write its header.
_CHARACTERIZE_DEADLINE_S = float(
    os.environ.get("DASHBOARD_CHARACTERIZE_DEADLINE_S", 120.0)
)

# Rule 5: dirty passes per gate claim are bounded; exhaustion releases the
# gate and schedules a continuation (responsibility retained, never dropped).
_MAX_CONSECUTIVE_DIRTY_PASSES = 10

# Rule 4: handover steps per gate claim are bounded the same way — a
# predecessor being actively written could otherwise pin the owner; the
# reconciliation tick re-enters (WF backstop).
_MAX_HANDOVER_STEPS = 20


@dataclass
class _FileTrack:
    """Per-(session, rollout-file) ingestion track [model: tracks]."""

    tmux_name: str
    path: str
    state: str = TRACK_DISCOVERED
    provenance: str | None = None          # birth | persisted | late
    generation: tuple[int, int] | None = None   # (st_dev, st_ino) at observation
    expected_previous: str | None = None   # captured at observation, for the CAS
    characterize_deadline: float | None = None
    # Watch identity (Rule 8/D3): dispatch is keyed by (wd, epoch) jointly —
    # wds are kernel-recycled, so a stale queued event must never dispatch
    # into whichever track now holds the number.
    wd: int | None = None
    wd_epoch: int = 0
    # Rule 4 bookkeeping: bytes of THIS file published through the ordered
    # handover (for files the row never linked), and the seal — the
    # complete-line size at the final pre-advance check (None = unchecked).
    published_up_to: int = 0
    checked_size: int | None = None
    # D6: terminal tombstone (size, mtime_ns, st_dev, st_ino) — the periodic
    # scan is stat-only against this; classification re-runs only on
    # observable progress (B3+A6).
    tombstone: tuple | None = None
    close_reason: str | None = None
    # Whether this file may REPLACE an existing link. True for CREATE
    # events in the session's own directory and for rollout-chain files
    # (succession ordering applies — N2/N3). False for ambient
    # scan/reconciliation observations of non-chain files: in a SHARED
    # directory (Claude host layout) those are other sessions' files, and
    # promoting one over an existing link is cross-session adoption
    # (NoChildAdoption; the pre-fix scans' linked-row guard). First
    # resolution of an UNLINKED row is always allowed regardless.
    supersede_candidate: bool = False


@dataclass
class _SessionGate:
    """ONE drain gate per session (Rule 5 / A1), event-loop-owned.

    ``busy`` is claimed synchronously with no await between check and
    claim; ``dirty`` transfers a contended request's responsibility (T87);
    ``needs_drain`` is the D4 scheduling flag sync-context entry points set
    without ever touching ``busy`` — the loop-start pump claims ``busy``
    only after task creation succeeds (B2).
    """

    busy: bool = False
    dirty: bool = False
    needs_drain: bool = False
    publish_registry_after_drain: bool = False
    # Rule 4: deferred successor link + the CAS expectation captured when
    # the defer was decided.
    pending_link: str | None = None
    pending_expected: str | None = None
    # A5/D4: the UNCANCELLABLE inner executor future of the in-flight
    # blocking read. The awaiter waits on asyncio.shield(inflight_future),
    # so cancelling the owner task never cancels this future — it resolves
    # only when the worker thread actually returns, which makes
    # ``inflight_future.done()`` a truthful "worker stopped" signal. The
    # gate is released only on that signal (done-callback), never in a
    # ``finally`` on the awaiter.
    inflight_future: Any = None
    # The current owner task (set by the pump) — held so teardown (B5) and
    # stop() (S2) can cancel the owner under the same release contract.
    owner_task: Any = None
    # Set when the owner task failed with an exception: the continuation is
    # delayed by a short backoff so a deterministic crash can't hot-spin
    # the loop (responsibility still retained — never dropped).
    crashed: bool = False
    # B5: a retired gate belongs to a dead/deregistered session — requests
    # are refused and the release path removes it instead of rescheduling.
    retired: bool = False


def _entry_identity(entry: dict) -> str:
    """Stable identity key for an entry — mirrors the JS _entryIdentity."""
    etype = entry.get("type", "?") or "?"
    tid = entry.get("tool_id")
    if etype == "tool_use" and tid:
        return f"tu:{tid}"
    if etype == "tool_result" and tid:
        return f"tr:{tid}"
    ts_str = entry.get("timestamp", "") or ""
    content = entry.get("content")
    if isinstance(content, str):
        snippet = content[:200]
    elif content is None:
        snippet = ""
    else:
        try:
            snippet = json.dumps(content)[:200]
        except Exception:
            snippet = ""
    return f"{etype}:{ts_str}:{snippet}"


def _apply_activity_entries(ts: _TailState, entries: list[dict]) -> str:
    """Update pending/completed tool tracking and return the derived state."""
    for entry in entries:
        etype = entry.get("type", "")
        if etype == "tool_use" and entry.get("tool_id"):
            tid = entry["tool_id"]
            if tid not in ts.completed_tool_ids:
                ts.pending_tool_ids.add(tid)
        elif etype in ("tool_result", "semantic_bash") and entry.get("tool_id"):
            tid = entry["tool_id"]
            if entry.get("status") == "running":
                if tid not in ts.completed_tool_ids:
                    ts.pending_tool_ids.add(tid)
            else:
                ts.pending_tool_ids.discard(tid)
                ts.completed_tool_ids.add(tid)
        elif etype == "codex_task_complete":
            ts.completed_tool_ids.update(ts.pending_tool_ids)
            ts.pending_tool_ids.clear()
        if etype:
            ts.last_entry_type = etype
            ts.recent_processed.append((
                etype,
                entry.get("timestamp", "") or "",
                _entry_identity(entry),
            ))

    if ts.pending_tool_ids:
        return "tool_running"
    if ts.last_entry_type in ("user", "tool_result", "crosstalk"):
        return "thinking"
    return "idle"


# Entry kinds that count as operator input — typed user messages and
# CrossTalk pings (which wake an agent the same way a typed message does).
_OPERATOR_INPUT_TYPES = frozenset({"user", "crosstalk"})


def _record_operator_input(timestamp_iso: str) -> None:
    """Single-row operator activity write.

    Fire-and-forget on a thread so the SSE broadcast pipeline is never
    gated on graph-DB I/O. Lazy-imports settings_ops/surface so this
    module's import doesn't drag the whole graph stack in at server boot.

    Uses :func:`settings_ops.upsert_by_key` so the singleton row stays a
    single row instead of accumulating one per call — earlier writes
    update the same setting id rather than appending a fresh base.
    """
    try:
        from tools.graph import settings_ops
        from tools.graph.surface import (
            OPERATOR_ACTIVITY_SET_ID,
            SCHEMA_REVISION as _OPERATOR_ACTIVITY_REVISION,
        )
        settings_ops.upsert_by_key(
            OPERATOR_ACTIVITY_SET_ID,
            _OPERATOR_ACTIVITY_REVISION,
            "operator",
            {"last_input_at": timestamp_iso},
            org="personal",
        )
    except Exception:
        logger.exception(
            "session_monitor: operator activity write failed",
        )


def _read_harness_token_from_meta(
    *,
    run_dir: Path | str | None,
    resolution_dir: Path | None,
) -> str | None:
    """Pull ``harness_token`` from the launcher's ``.session_meta.json``.

    The launcher writes ``<run_dir>/sessions/.session_meta.json`` (auto-10lsv,
    renamed in auto-ghhdg). Either ``run_dir`` or the already-resolved
    ``resolution_dir`` (which the launcher uses as the sessions/ directory)
    suffices. Returns ``None`` if no meta file is found, the JSON is
    malformed, or the field is absent. Best-effort by design — a missing
    token never blocks registration.
    """
    candidates: list[Path] = []
    if run_dir is not None:
        rd = run_dir if isinstance(run_dir, Path) else Path(run_dir)
        candidates.append(rd / "sessions" / ".session_meta.json")
        candidates.append(rd / ".session_meta.json")
    if resolution_dir is not None:
        candidates.append(resolution_dir / ".session_meta.json")
    for path in candidates:
        try:
            text = path.read_text()
        except OSError:
            continue
        try:
            doc = json.loads(text)
        except (ValueError, TypeError):
            continue
        if not isinstance(doc, dict):
            continue
        token = doc.get("harness_token")
        if isinstance(token, str) and token.strip():
            return token.strip()
    return None


class SessionMonitor:
    """DB-backed session registry with background tailing and liveness checking."""

    # Reconciliation backstop — how often to re-scan pending container
    # sessions for JSONLs that scan-on-add and IN_CREATE both missed.
    # 5 min matches the bead spec; tests can monkey-patch the interval.
    _reconciliation_interval_seconds: float = 300.0

    def __init__(self) -> None:
        self._tail_states: dict[str, _TailState] = {}
        self._tailer_task: asyncio.Task | None = None
        self._liveness_task: asyncio.Task | None = None
        self._reconciliation_task: asyncio.Task | None = None
        self._event_bus = None
        self._entry_parser = None
        self._entry_enricher = None
        self._harness: SessionHarness = CLAUDE_HARNESS
        self._todo_snapshot = None
        # auto-eerfx: per-session monotonic timestamp of when a session was
        # first observed at harness_starting with setup already complete.
        # Used by the screen-poll loop's grace fallback to promote
        # composer_ready when the docker-bridged pane can't be screen-read.
        self._harness_ready_grace: dict[str, float] = {}
        # Screen-poll self-repair: when a session first entered the poll
        # window (to measure stuck-time) and which sessions we've already
        # filed a self-repair ticket for (file once, not every 2s poll).
        self._screen_stuck_since: dict[str, float] = {}
        self._self_repair_filed: set[str] = set()
        # Sessions explicitly armed for composer detection. The pane-poller
        # watches this set IN ADDITION to the startup_state value window, so
        # composer_ready keeps being detected even after the tailer's
        # backfill clear drops startup_state to NULL mid-launch (the resume
        # race, graph://afb67d11-7c4). Armed by arm_startup_state (create /
        # resume entrypoints); cleared by the poller at composer_ready or
        # when the session leaves the live set. This is the permanent
        # poller contract for the FSM worker's wait-for-composer step.
        self._screen_poll_armed: set[str] = set()
        # Consecutive authoritative liveness misses per session — the reap
        # gate (_LIVENESS_MISS_THRESHOLD). Probe failures don't count.
        self._liveness_misses: dict[str, int] = {}
        self._started = False
        # S2: stop-scoped quiesce — while True, drain continuations are
        # RETAINED (needs_drain) but never pumped, so stop() returns with
        # zero live owners; start() clears it and its loop-start pump
        # resumes the retained work.
        self._stopping = False
        self._last_pause_nag_sent: float = 0.0  # timestamp of last dispatch-pause nag
        self._last_orphan_prune: float = time.time()  # defer first prune one full interval
        # auto-ja51w: transient per-session phase progress dict (e.g.
        # {"repo_index": 2, "total": 3, "current_repo": "enterprise_ng"})
        # written by update_phase(progress=...) and surfaced by
        # get_registry under "phase_progress". Cleared when a session
        # transitions out of preparing_workspace.
        self._phase_progress: dict[str, dict] = {}
        # inotify state — populated by _init_inotify()
        self._inotify: Any = None                        # INotify instance
        self._use_inotify: bool = False
        self._dir_wd_sessions: dict[int, set[str]] = {}  # dir wd → set of tmux_names
        self._dir_path_to_wd: dict[str, int] = {}        # dir path → wd (dedup)
        self._wd_to_dir_path: dict[int, str] = {}        # reverse: wd → dir path
        # ── Rollout-ingestion state machine (auto-suvcp) ──────────────
        # One track per (tmux_name, path); one gate per session. Gates are
        # NEVER reset by attach/re-attach — a reset while a worker owns
        # `busy` would break SingleOwner.
        self._tracks: dict[tuple[str, str], _FileTrack] = {}
        self._session_gates: dict[str, _SessionGate] = {}
        # ALL file watches (streaming sessions AND characterizing tracks)
        # are owned PER INODE (Rule 8/D3, review R2): the kernel dedups
        # watches by inode per inotify instance, so any two watchers of
        # one file share one wd — ownership must be the inode's, with a
        # TYPED subscriber set, or one watcher's release drops the
        # others' watch and a scalar map shadows all but one subscriber.
        #   _inode_watches: (st_dev, st_ino) → {
        #       "wd": int,
        #       "subscribers": set[("session", tmux) | ("track", tmux, path)],
        #       "paths": {subscriber: path},   # for IGNORED revalidation
        #   }
        #   _wd_to_inode: wd → (st_dev, st_ino)   (dispatch-side reverse)
        # MODIFY fans out to EVERY subscriber (drain per session,
        # reclassify per track); IN_IGNORED revalidates by re-stat before
        # tearing down (a recycled wd's stale IGNORED must not kill a
        # live entry).
        self._inode_watches: dict[tuple[int, int], dict] = {}
        self._wd_to_inode: dict[int, tuple[int, int]] = {}
        # Registration-epoch snapshots per (tmux_name, dir): A7/N1 birth
        # trust needs "armed over an EMPTY directory" + "first CREATE of
        # this registration epoch".
        self._dir_epochs: dict[tuple[str, str], dict] = {}
        # W6 reliability: reconciliation_tick failure-streak tracking.
        # Incremented when ANY step of a tick raises; reset to 0 on a tick
        # where every step completes cleanly. degraded_since is the
        # monotonic timestamp of the first failing tick in the current
        # streak (None while healthy) — health surfacing uses it to flag
        # "degraded > N minutes" without needing a wall-clock.
        self._reconcile_failure_streak: int = 0
        self._reconcile_degraded_since: float | None = None
        self._reconcile_last_error: str | None = None
        # W3: per-session tail-primary graph appenders. Keyed by tmux_name;
        # in-memory only (not persisted — a dashboard restart re-establishes
        # via _build_graph_appender's gap catch-up, resuming from the
        # graph_ingest_offset already persisted in the source's metadata).
        self._graph_appenders: dict = {}

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
        harness: str = "claude",
        model: str | None = None,
        harness_state: str = "{}",
        harness_token: str | None = None,
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

        # auto-ja51w: register() may be called after register_pending has
        # already inserted a stub row at api_session_create's POST entry.
        # In that case the INSERT fails with IntegrityError and we MUST
        # still proceed — backfill the now-known jsonl_path / resolution_dir
        # / session_uuid / harness_token onto the existing row, set up the
        # tail state, attach inotify watches, and broadcast. The previous
        # early-return left the row orphaned (no tail state, no watches),
        # and reconciliation purged it within a few seconds.
        import sqlite3
        row_existed = False
        try:
            insert_session(
                tmux_name=tmux_name,
                session_type=session_type,
                project=project,
                harness=harness,
                model=model,
                harness_state=harness_state,
                bead_id=bead_id,
                jsonl_path=path_str,
                session_uuid=session_uuid,
                resolution_dir=str(res_dir) if res_dir else None,
                harness_token=harness_token,
            )
        except sqlite3.IntegrityError:
            row_existed = True
            logger.debug(
                "session_monitor: row already exists for %s (likely from register_pending); "
                "backfilling jsonl_path + resolution_dir",
                tmux_name,
            )
        except Exception:
            logger.warning(
                "session_monitor: INSERT failed for tmux=%s — unexpected error, aborting register",
                tmux_name, exc_info=True,
            )
            return

        if row_existed:
            # Backfill the columns insert_session would have written. The
            # session_uuid is appended via update_jsonl_link if known; for
            # the typical container-create path it's still pending so we
            # only touch the file paths.
            from tools.dashboard.dao.dashboard_db import get_conn
            conn = get_conn()
            parts: list[str] = []
            vals: list = []
            if path_str is not None:
                parts.append("jsonl_path=?")
                vals.append(path_str)
                parts.append("curr_jsonl_file=?")
                vals.append(path_str)
            if res_dir is not None:
                parts.append("resolution_dir=?")
                vals.append(str(res_dir))
            if session_uuid is not None:
                parts.append("session_uuid=?")
                vals.append(session_uuid)
            if bead_id is not None:
                parts.append("bead_id=?")
                vals.append(bead_id)
            if harness_token is not None:
                parts.append("harness_token=?")
                vals.append(harness_token)
            # type + project + harness — refresh in case register_pending
            # used defaults that differ from the now-known values.
            parts.append("type=?")
            vals.append(session_type)
            parts.append("project=?")
            vals.append(project)
            parts.append("harness=?")
            vals.append(harness)
            if model is not None:
                parts.append("model=?")
                vals.append(model)
            if parts:
                vals.append(tmux_name)
                conn.execute(
                    f"UPDATE tmux_sessions SET {', '.join(parts)} WHERE tmux_name=?",
                    vals,
                )
                conn.commit()

        if seed_message:
            update_tail_state(tmux_name, last_message=seed_message)

        # Set up ephemeral tail state. ``needs_resolution`` flags "DB row has
        # no jsonl_path yet" — covers both directory-only registrations
        # (path_is_dir) and jsonl_path=None container registrations (run_dir
        # provided but the JSONL hasn't landed).
        ts = _TailState(
            needs_resolution=(path_str is None and res_dir is not None),
            resolution_dir=res_dir,
        )
        self._tail_states[tmux_name] = ts

        # Add inotify watches if available
        if self._use_inotify:
            if path_str:
                self._add_file_watch(tmux_name, path_str)
            if res_dir:
                self._add_dir_watch(tmux_name, str(res_dir))

        # An explicit-file registration is a persisted-identity observation
        # (auto-suvcp Rule 1): stream it and catch up any existing bytes.
        if path_str:
            self.observe_rollout(tmux_name, Path(path_str), source="register")

        logger.info(
            "session_monitor: registered %s  type=%s  project=%s  jsonl=%s",
            tmux_name, session_type, project,
            "pending" if path_str is None else Path(path_str).name,
        )
        await self._broadcast_registry()

    async def arm_startup_state(self, tmux_name: str, state: str) -> bool:
        """Explicit FSM entry: put a session INTO the LAUNCHING state.

        Call it exactly when a launch begins: create/resume/retry seed the
        row here before enqueueing. Routes through the transition authority
        (→ LAUNCHING with the given chip phase; legality matrix allows
        entry from ENDED and FAILED — an explicit relaunch is the retry
        path — and refuses it from ACTIVE/STOPPING).
        """
        from tools.dashboard.session_lifecycle_worker import STATE_AUTHORITY

        changed = STATE_AUTHORITY.transition(
            tmux_name, "LAUNCHING", phase=state, cause="launch-entry",
        )
        # Arm the pane-poller alongside the FSM entry regardless of the row
        # write outcome — composer detection must run for this launch even if
        # the row was already in the proposed state.
        self._screen_poll_armed.add(tmux_name)
        if changed:
            await self._broadcast_registry()
        return changed

    async def register_pending(
        self,
        tmux_name: str,
        *,
        session_type: str = "container",
        project: str = "",
        harness: str = "claude",
        harness_token: str | None = None,
    ) -> None:
        """Register a session row IMMEDIATELY at session-create POST entry,
        before ``prepare_session_mounts`` and ``launch_session`` run.

        Seeds the unified ``startup_state`` FSM at ``requesting``; the
        lifecycle worker's writer drives all subsequent progression. The normal :meth:`register` call later (post-tmux-spawn)
        is idempotent — its INSERT fails, the existing row gets its
        jsonl_path / resolution_dir backfilled, and the tail-state setup
        proceeds.
        """
        try:
            insert_session(
                tmux_name=tmux_name,
                session_type=session_type,
                project=project,
                harness=harness,
                harness_token=harness_token,
                state="LAUNCHING",
            )
        except Exception:
            # Duplicate-name or other INSERT failure — surfaces as a no-op so
            # the create handler can still call this safely from a re-entrant
            # path. Existing rows just get the state advance below.
            logger.debug(
                "session_monitor.register_pending: INSERT failed for %s "
                "(likely already exists; proceeding to arm_startup_state)",
                tmux_name,
            )
        await self.arm_startup_state(tmux_name, "requesting")
        # Mark as needing resolution so the normal register() call later is
        # idempotent — the tail-state will get rebuilt cleanly with the
        # jsonl_path / resolution_dir once those are known.
        if tmux_name not in self._tail_states:
            self._tail_states[tmux_name] = _TailState(needs_resolution=True)
        logger.info(
            "session_monitor: registered pending %s  type=%s  project=%s  startup_state=requesting",
            tmux_name, session_type, project,
        )
        await self._broadcast_registry()

    async def update_phase(
        self,
        tmux_name: str,
        *,
        progress: dict | None = None,
    ) -> None:
        """Update transient launch progress and broadcast.

        Lifecycle STATE writes live on the lifecycle worker's writer (and
        ``arm_startup_state`` for FSM entry) — this method only carries the
        sub-phase ``progress`` dict, held in memory on
        :attr:`_phase_progress` and surfaced in the registry payload under
        ``phase_progress`` (e.g. ``{"repo_index": 2, "total": 3,
        "current_repo": "enterprise_ng"}``). Pass ``progress={}`` to clear.

        Safe to call from a worker thread via
        ``asyncio.run_coroutine_threadsafe`` — that's the per-repo callback
        path from inside :func:`prepare_session_mounts`.
        """
        if progress is None:
            return
        if progress:
            self._phase_progress[tmux_name] = dict(progress)
        else:
            self._phase_progress.pop(tmux_name, None)
        await self._broadcast_registry()

    async def register_session(
        self,
        tmux_name: str,
        type: str,
        jsonl_path: Path | str | None = None,
        *,
        run_dir: Path | str | None = None,
        bead_id: str | None = None,
        project: str | None = None,
        harness: str = "claude",
        model: str | None = None,
    ) -> None:
        """Register a session of any type (container/host/dispatch/librarian).

        Thin wrapper over register() that lets the dispatcher and other callers
        register dispatch + librarian sessions with the monitor. Run_dir is
        accepted for dispatch sessions where the JSONL is inside an agent-run
        tree; it is used as resolution_dir when jsonl_path is a directory.

        When ``run_dir`` is supplied, the launcher's ``.session_meta.json``
        is read for the ``harness_token`` field (auto-10lsv, renamed in
        auto-ghhdg) so the dashboard surfaces which Anthropic account each
        container is burning. Missing or unreadable meta is non-fatal.
        """
        jp: Path | None = None
        if jsonl_path is not None:
            jp = jsonl_path if isinstance(jsonl_path, Path) else Path(jsonl_path)

        res_dir: Path | None = None
        if run_dir is not None:
            rd = run_dir if isinstance(run_dir, Path) else Path(run_dir)
            sess_dir = rd / "sessions"
            res_dir = sess_dir if sess_dir.exists() else rd
        elif jp is not None:
            res_dir = jp if jp.is_dir() else jp.parent

        session_uuid = None
        if jp is not None and not jp.is_dir() and jp.suffix == ".jsonl":
            session_uuid = jp.stem

        proj = project
        if proj is None and jp is not None and not jp.is_dir():
            proj = jp.parent.name
        if proj is None:
            proj = "autonomy"

        harness_token = _read_harness_token_from_meta(
            run_dir=run_dir, resolution_dir=res_dir,
        )

        await self.register(
            tmux_name=tmux_name,
            session_type=type,
            project=proj,
            jsonl_path=jp,
            bead_id=bead_id,
            session_uuid=session_uuid,
            resolution_dir=res_dir,
            harness=harness,
            model=model,
            harness_token=harness_token,
        )
    async def deregister_session(self, tmux_name: str) -> None:
        """Mark a session dead but preserve the DB row (keeps history)."""
        await self.deregister(tmux_name)

    def get_session_stats(self, session_id: str) -> dict | None:
        """Return tail statistics for a session — the single source of truth.

        Callers (dispatcher card stats, server tail endpoint, dashboard UI)
        read from this helper instead of maintaining their own JSONL readers.
        Resolves by tmux_name primary key, then falls through to session_uuid
        lookup for dispatch callers that pass a JSONL UUID.
        """
        row = get_session(session_id)
        if row is None:
            owner = self._find_session_by_uuid(session_id)
            if owner:
                row = get_session(owner)
        if row is None:
            return None
        return {
            "tmux_name": row.get("tmux_name"),
            "session_id": row.get("tmux_name"),
            "entry_count": int(row.get("entry_count") or 0),
            "context_tokens": int(row.get("context_tokens") or 0),
            "last_message": row.get("last_message") or "",
            "last_activity": row.get("last_activity"),
            "activity_state": row.get("attention") or "idle",
            "file_offset": int(row.get("file_offset") or 0),
            "is_live": derive_lifecycle_state(row) not in ("ENDED", "FAILED"),
            "jsonl_path": row.get("jsonl_path"),
            "type": row.get("type"),
            "harness": row.get("harness") or "claude",
            "model": row.get("model") or None,
        }

    def resolve_session_file(self, session_id: str) -> Path | None:
        """Resolve session_id to a JSONL file, scanning agent-runs as fallback.

        Order of resolution:
          1. DB by tmux_name — returns jsonl_path if present
          2. DB by session_uuid or session_uuids JSON array — returns jsonl_path
          3. Fallback: scan data/agent-runs/*/sessions/**/{session_id}.jsonl
             and data/agent-runs/{session_id}*/sessions/**/*.jsonl
        """
        import os as _os
        row = get_session(session_id)
        if row is None:
            owner = self._find_session_by_uuid(session_id)
            if owner:
                row = get_session(owner)
        if row and row.get("jsonl_path"):
            p = Path(row["jsonl_path"])
            if p.exists():
                return p

        env_override = _os.environ.get("DASHBOARD_AGENT_RUNS_DIR")
        if env_override:
            agent_runs = Path(env_override)
        else:
            agent_runs = Path(__file__).resolve().parents[2] / "data" / "agent-runs"
        if not agent_runs.exists():
            return None

        for jsonl in agent_runs.rglob(f"{session_id}.jsonl"):
            if "subagents" in jsonl.parts:
                continue
            return jsonl
        for run_dir in agent_runs.glob(f"{session_id}*"):
            sess_dir = run_dir / "sessions"
            if not sess_dir.exists():
                continue
            primaries = _find_primary_jsonls(sess_dir)
            if primaries:
                return primaries[0]
        return None

    async def register_revived(
        self,
        tmux_name: str,
        jsonl_path: Path | None = None,
    ) -> None:
        """Re-register a revived session for tailing.

        The DB row already exists; revive_session() reset its tail offset and
        harness_state, and the arm that follows moves it to LAUNCHING through
        the transition authority. This just sets up the in-memory tail state
        and inotify watches so the session monitor starts tailing the JSONL
        file again.
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

        ts = _TailState(
            needs_resolution=(path_str is None and res_dir is not None),
            resolution_dir=res_dir,
        )
        self._tail_states[tmux_name] = ts

        if self._use_inotify:
            if path_str:
                self._add_file_watch(tmux_name, path_str)
            if res_dir:
                self._add_dir_watch(tmux_name, str(res_dir))

        # A revive with a known file is a persisted-identity observation
        # (auto-suvcp Rule 1): re-attach and catch up existing bytes.
        if path_str:
            self.observe_rollout(tmux_name, Path(path_str), source="revive")

        logger.info(
            "session_monitor: revived %s  jsonl=%s",
            tmux_name,
            "pending" if path_str is None else Path(path_str).name,
        )
        await self._broadcast_registry()

    async def deregister(self, tmux_name: str, *, record_death: bool = True) -> None:
        """Remove a session from tailing; optionally record its death.

        ``record_death=False`` is for the worker's FAILED-session cleanup:
        the row is already terminal FAILED (with its retryable detail) and
        must stay there — recording ENDED mid-cleanup would need a
        postmortem ENDED→FAILED restore, which the legality matrix
        deliberately does not contain.
        """
        self._remove_watches(tmux_name)
        if record_death:
            mark_dead(tmux_name)
        # Final disk footprint for the ended card; drops the session from
        # the resource poll set. Fire-and-forget, never raises.
        from tools.dashboard.resource_monitor import resource_monitor
        asyncio.create_task(resource_monitor.on_session_dead(tmux_name))
        self._tail_states.pop(tmux_name, None)
        # auto-ja51w: clear transient phase progress on deregister.
        self._phase_progress.pop(tmux_name, None)
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
            _state = derive_lifecycle_state(s)
            entry = {
                "session_id": s["tmux_name"],
                "project": s["project"],
                "type": s["type"],
                # Compat key, computed from the one state — no longer read
                # from the projection column (drop-ready for Phase D).
                "is_live": _state not in ("ENDED", "FAILED"),
                "started_at": s["created_at"],
                "graph_source_id": s.get("graph_source_id"),
                "label": s.get("label", ""),
                "role": s.get("role", ""),
                "entry_count": s.get("entry_count", 0),
                "context_tokens": s.get("context_tokens", 0),
                "last_activity": s.get("last_activity") or s["created_at"],
                "last_input_at": s.get("last_input_at"),
                "last_message": s.get("last_message", ""),
                "topics": json.loads(s.get("topics") or "[]"),
                "todos": json.loads(s.get("todos") or "[]"),
                "nag_enabled": bool(s.get("nag_enabled")),
                "nag_interval": s.get("nag_interval") or 15,
                "nag_message": s.get("nag_message") or "",
                "dispatch_nag_enabled": bool(s.get("dispatch_nag")),
                # Compat key for consumers not yet on ``attention``:
                # registry rows are non-terminal, so the tracker value (or
                # idle) is the honest answer.
                "activity_state": s.get("attention") or "idle",
                "harness": s.get("harness", "claude"),
                # Most-recent observed model; the canonical session-card
                # badge renders its compact form and keeps this raw id in the
                # tooltip/data attribute.
                "model": s.get("model") or None,
                # jsonl_path is the legacy bridge; session_uuids is canonical after Phase 4
                "resolved": bool(s.get("jsonl_path")) or (
                    bool(s.get("session_uuids")) and s["session_uuids"] != "[]"
                ),
                # Unified startup FSM. NULL = not in launching (existing
                # sessions, post-launch sessions, dead sessions).
                "startup_state": s.get("startup_state"),
                # The one lifecycle truth + its telemetry sidecar. The
                # legacy ``lifecycle_state`` key carries the same value for
                # consumers that already read it.
                "state": _state,
                "attention": s.get("attention"),
                "lifecycle_state": _state,
            }
            # auto-ja51w: transient per-session phase progress (e.g. per-repo
            # tick from inside prepare_session_mounts). Set via
            # SessionMonitor.update_phase(progress=...). Omitted from the
            # payload when no progress is active so the field stays opt-in
            # at the wire level.
            prog = self._phase_progress.get(s["tmux_name"])
            if prog:
                entry["phase_progress"] = prog
            entry["org"] = resolve_session_org(entry)
            out.append(entry)
        return out

    # ── Background tasks ──────────────────────────────────────────

    async def start(
        self, event_bus=None, entry_parser=None, entry_enricher=None,
        harness: SessionHarness | None = None,
        todo_snapshot=None,
    ) -> None:
        """Start background tailer and liveness tasks.

        ``todo_snapshot`` is an optional ``(tmux_name) -> list[dict]`` callable
        used to persist the tracker's current per-session todo list after each
        enrichment pass and after warm-up. Wired via the same tracker instance
        that provides ``entry_enricher``.
        """
        if self._started:
            return
        self._started = True
        self._stopping = False
        self._event_bus = event_bus
        self._entry_parser = entry_parser
        self._entry_enricher = entry_enricher
        if harness is not None:
            self._harness = harness
        self._todo_snapshot = todo_snapshot
        self._init_inotify()
        self._tailer_task = asyncio.create_task(self._inotify_tailer_loop())
        self._liveness_task = asyncio.create_task(self._liveness_loop())
        self._reconciliation_task = asyncio.create_task(self._reconciliation_loop())
        # auto-eerfx: per-2s tmux capture-pane poll for harness state.
        self._screen_poll_task = asyncio.create_task(self._screen_poll_loop())
        logger.info("session_monitor: background tasks started (mode=inotify)")
        # Re-scan unresolved container sessions from prior server lifetime
        await self._recover_unresolved_sessions()
        # B2 loop-start pump: drains requested from sync contexts before
        # the loop ran (needs_drain set, busy untouched) get their task now.
        self._pump_pending_drains()
        # Broadcast registry for any sessions that exist in DB
        if count_live() > 0:
            await self._broadcast_registry()

    async def stop(self) -> None:
        """Cancel background tasks and reset state so start() can be called again."""
        if not self._started:
            return
        # S2: quiesce first — the cancelled owners' release callbacks
        # must retain (needs_drain) instead of pumping fresh owners.
        self._stopping = True
        tasks = [
            t for t in (self._tailer_task, self._liveness_task,
                        self._reconciliation_task, self._screen_poll_task)
            if t and not t.done()
        ]
        # S2: drain owners are tracked work — cancel them and wait for
        # their shielded workers to actually stop (B4 contract), so no
        # executor thread outlives the monitor into loop teardown.
        drain_waits: list = []
        for gate in list(self._session_gates.values()):
            if gate.owner_task is not None and not gate.owner_task.done():
                gate.owner_task.cancel()
                tasks.append(gate.owner_task)
            if gate.inflight_future is not None and not gate.inflight_future.done():
                drain_waits.append(gate.inflight_future)
        for t in tasks:
            try:
                t.cancel()
            except RuntimeError:
                pass  # task belongs to a different event loop (e.g. TestClient teardown)
        try:
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if drain_waits:
                await asyncio.gather(*drain_waits, return_exceptions=True)
        except RuntimeError:
            pass  # cross-loop gather — tasks will be GC'd with their loop
        self._tailer_task = None
        self._liveness_task = None
        self._reconciliation_task = None
        # auto-eerfx: the screen-poll task was started in start() but historically
        # not cancelled here, so it leaked one running task per start/stop cycle.
        self._screen_poll_task = None
        self._started = False
        logger.info("session_monitor: background tasks stopped")

    async def _recover_unresolved_sessions(self) -> None:
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
                # Already resolved — ensure dir watch exists for rollover
                # detection, then re-observe the linked path as a persisted
                # re-attach (auto-suvcp Rule 1): offset preserved, catch-up
                # drain requested, so bytes written while the dashboard was
                # down become visible with no further write.
                res_dir = row.get("resolution_dir") or str(Path(row["jsonl_path"]).parent)
                if self._use_inotify and res_dir:
                    if tmux_name not in self._tail_states:
                        self._tail_states[tmux_name] = _TailState(
                            resolution_dir=Path(res_dir),
                        )
                    self._add_dir_watch(tmux_name, res_dir)
                self.observe_rollout(
                    tmux_name, Path(row["jsonl_path"]),
                    source="startup_recovery",
                )
                continue
            if row.get("type") == "host":
                harness = resolve_harness_for_session_row(row)
                linked = harness.resolve_session(tmux_name=tmux_name, row=row)
                if linked:
                    harness.attach_live_monitoring(
                        monitor=self,
                        tmux_name=tmux_name,
                        jsonl_path=linked["jsonl_path"],
                        resolution_dir=Path(row["resolution_dir"]) if row.get("resolution_dir") else linked["resolution_dir"],
                    )
                    recovered += 1
                    logger.info(
                        "session_monitor: recovered host %s → %s",
                        tmux_name, linked["jsonl_path"].name,
                    )
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
        """Compatibility shim for tests; host resolution now lives in the harness."""
        from tools.dashboard.session_harness import _resolve_claude_host_jsonl

        return _resolve_claude_host_jsonl(tmux_name)

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
            # nonblocking=True hardens the inotify fd: the tailer runs read() in
            # a to_thread worker, and when that task is cancelled at stop() the
            # worker can be orphaned mid-read. On a blocking fd a concurrent-
            # reader race makes the underlying os.read block forever ignoring its
            # timeout (inotify_simple's own documented warning), which can wedge
            # the event loop's shutdown_default_executor() at teardown. Non-
            # blocking makes that racy read raise EAGAIN instead of blocking. Per
            # inotify_simple's docs this does NOT change normal read() behaviour
            # (read() is FIONREAD+poll gated), so the live single-reader monitor
            # is unaffected and events deliver identically.
            self._inotify = INotify(nonblocking=True)
            self._use_inotify = True
            # Add watches for sessions already in the DB
            for row in get_tailable_sessions():
                self._add_file_watch(row["tmux_name"], row["jsonl_path"])
                dir_path = row.get("resolution_dir") or str(Path(row["jsonl_path"]).parent)
                self._add_dir_watch(row["tmux_name"], dir_path)
            logger.info(
                "session_monitor: inotify initialised — %d file watches, %d dir watches",
                len(self._inode_watches), len(self._dir_path_to_wd),
            )
        except OSError as exc:
            logger.warning("session_monitor: inotify init failed (%s), using polling", exc)
            self._inotify = None
            self._use_inotify = False

    def _add_file_watch(self, tmux_name: str, jsonl_path: str) -> None:
        """Subscribe a session's streaming tail to its file's inode watch
        (R2: one inode-owned structure for ALL watch kinds — the old
        scalar wd→session map collided whenever two watchers shared an
        inode and let one watcher's release blind the others)."""
        if not self._inotify:
            return
        ts = self._tail_states.get(tmux_name)
        if ts is None:
            ts = _TailState()
            self._tail_states[tmux_name] = ts
        # Drop this session's previous file subscription (re-link).
        self._unsubscribe_watch(("session", tmux_name))
        ts.watch_descriptor = None
        try:
            st = os.stat(jsonl_path)
        except OSError as exc:
            logger.warning(
                "session_monitor: add_watch MODIFY failed for %s: %s",
                tmux_name, exc,
            )
            ts.last_known_inode = 0
            return
        entry = self._subscribe_inode(
            (st.st_dev, st.st_ino), ("session", tmux_name), jsonl_path,
        )
        if entry is None:
            return
        ts.watch_descriptor = entry["wd"]
        ts.last_known_inode = st.st_ino

    def _subscribe_inode(
        self, inode_key: tuple[int, int], subscriber: tuple, path: str | Path,
    ) -> dict | None:
        """Attach *subscriber* to the inode's (possibly shared) watch entry."""
        entry = self._inode_watches.get(inode_key)
        if entry is None:
            try:
                wd = self._inotify.add_watch(str(path), _iflags.MODIFY)
            except OSError as exc:
                logger.warning(
                    "session_monitor: add_watch MODIFY failed for %s: %s",
                    path, exc,
                )
                return None
            # wd-reuse guard (Rule 8): the kernel may hand back a wd whose
            # previous incarnation's IN_IGNORED is still queued. If that
            # number is still mapped to a DIFFERENT inode entry, that
            # entry is dead — detach it now so the stale mapping can't
            # shadow or misdispatch this one.
            stale_key = self._wd_to_inode.get(wd)
            if stale_key is not None and stale_key != inode_key:
                self._detach_inode_entry(wd, stale_key)
            entry = {"wd": wd, "subscribers": set(), "paths": {}}
            self._inode_watches[inode_key] = entry
            self._wd_to_inode[wd] = inode_key
        entry["subscribers"].add(subscriber)
        entry["paths"][subscriber] = str(path)
        return entry

    def _unsubscribe_watch(self, subscriber: tuple) -> None:
        """Detach *subscriber*; the kernel watch is removed only when the
        LAST subscriber leaves (refcounted — one watcher's release never
        drops another's watch)."""
        for inode_key, entry in list(self._inode_watches.items()):
            if subscriber not in entry["subscribers"]:
                continue
            entry["subscribers"].discard(subscriber)
            entry["paths"].pop(subscriber, None)
            if not entry["subscribers"]:
                self._inode_watches.pop(inode_key, None)
                self._wd_to_inode.pop(entry["wd"], None)
                if self._inotify:
                    try:
                        self._inotify.rm_watch(entry["wd"])
                    except OSError:
                        pass

    def _detach_inode_entry(
        self, wd: int, inode_key: tuple[int, int],
    ) -> None:
        """Drop an inode entry and null out every subscriber's wd state."""
        entry = self._inode_watches.pop(inode_key, None)
        self._wd_to_inode.pop(wd, None)
        if entry is None:
            return
        for sub in entry["subscribers"]:
            if sub[0] == "session":
                ts = self._tail_states.get(sub[1])
                if ts is not None and ts.watch_descriptor == wd:
                    ts.watch_descriptor = None
            else:
                track = self._tracks.get((sub[1], sub[2]))
                if track is not None and track.wd == wd:
                    track.wd = None
                    track.wd_epoch += 1   # invalidate queued dispatch

    def _dispatch_modify_wd(self, wd: int) -> set[str]:
        """MODIFY fan-out (R2): every subscriber of the inode acts —
        streaming sessions get a drain request (returned to the caller),
        characterizing tracks re-run classification. Track dispatch
        validates (wd, generation) jointly (Rule 8)."""
        modified: set[str] = set()
        inode_key = self._wd_to_inode.get(wd)
        entry = self._inode_watches.get(inode_key) if inode_key else None
        if entry is None:
            return modified
        for sub in list(entry["subscribers"]):
            if sub[0] == "session":
                name = sub[1]
                modified.add(name)
                ts = self._tail_states.get(name)
                if ts is not None:
                    ts.inotify_events_received += 1
                    ts.last_inotify_event_ts = time.time()
            else:
                track = self._tracks.get((sub[1], sub[2]))
                if (
                    track is not None
                    and track.wd == wd
                    and track.generation == inode_key
                ):
                    self._classify_and_step(track)
        return modified

    def _dispatch_ignored_wd(self, wd: int) -> None:
        """IN_IGNORED teardown with re-stat revalidation (Rule 8/D3): a
        recycled wd's STALE queued IGNORED must not tear down the live
        entry that now owns the number — if any subscribed path still
        resolves to the entry's inode, the watch is alive and the event
        belonged to a previous incarnation."""
        inode_key = self._wd_to_inode.get(wd)
        if inode_key is None:
            return
        entry = self._inode_watches.get(inode_key)
        if entry is None:
            self._wd_to_inode.pop(wd, None)
            return
        for sub, sub_path in entry["paths"].items():
            try:
                st = os.stat(sub_path)
            except OSError:
                continue
            if (st.st_dev, st.st_ino) == inode_key:
                return   # stale IGNORED — the entry's inode is still live
        self._detach_inode_entry(wd, inode_key)

    def _add_dir_watch(self, tmux_name: str, dir_path: str) -> None:
        """Add an IN_CREATE watch on a session directory (deduplicated).

        After the watch is set up, scan the directory for pre-existing
        JSONL files and subdirectories. Covers two races that IN_CREATE
        events cannot close on their own:
          (a) JSONL created in the same kernel batch as the subdir — the
              inotify watch on the subdir lands *after* the write event,
              so the event is never delivered.
          (b) JSONL pre-existed at watch-add time (dashboard restart,
              rescheduled watch, etc.) — no IN_CREATE will ever fire.
        """
        if not self._inotify:
            return
        if dir_path in self._dir_path_to_wd:
            # Directory already watched — attach this session to the refcount
            # set, then still scan: another session's initial scan may have
            # pre-dated this session's registration.
            wd = self._dir_path_to_wd[dir_path]
            self._dir_wd_sessions.setdefault(wd, set()).add(tmux_name)
            ts = self._tail_states.get(tmux_name)
            if ts:
                ts.dir_watch_descriptor = wd
            self._record_dir_epoch(tmux_name, dir_path)
            self._scan_dir_for_existing_jsonls(tmux_name, dir_path)
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
        self._record_dir_epoch(tmux_name, dir_path)
        self._scan_dir_for_existing_jsonls(tmux_name, dir_path)

    def _record_dir_epoch(self, tmux_name: str, dir_path: str) -> None:
        """Snapshot the registration epoch for birth trust (Rule 1, N1+A7).

        The epoch belongs to THIS registration, not the wd's lifetime (A7).
        A directory that already contained any rollout at arming can never
        birth-trust a later CREATE (N1: "armed before this file existed" +
        "first CREATE" is NOT sufficient — a subagent forked after a late
        arming satisfies both). An existing epoch is left alone: re-scans
        of an already-registered watch are not a new registration.
        """
        key = (tmux_name, dir_path)
        if key in self._dir_epochs:
            return
        try:
            snapshot_empty = not any(Path(dir_path).glob("*.jsonl"))
        except OSError:
            snapshot_empty = False
        self._dir_epochs[key] = {
            "snapshot_empty": snapshot_empty,
            "create_seen": False,
        }

    def _scan_dir_for_existing_jsonls(self, tmux_name: str, dir_path: str) -> None:
        """Discover JSONLs that already exist in a newly-watched directory.

        Also recurses into pre-existing subdirectories: Claude Code writes
        JSONLs under ``sessions/<project-slug>/*.jsonl``, so a watch on
        ``sessions/`` without a scan of its subdirs would miss every file
        that appeared before watch-add.

        Idempotent: ``_handle_jsonl_appeared`` no-ops when the session is
        already resolved for the given UUID, so repeated calls (e.g. a real
        IN_CREATE arriving after the scan already discovered the file) are
        safe.
        """
        try:
            dp = Path(dir_path)
            if not dp.is_dir():
                return
            for p in sorted(dp.glob("*.jsonl")):
                self._handle_jsonl_appeared(
                    tmux_name, p, source="watch_scan",
                )
            for sub in sorted(dp.iterdir()):
                if sub.is_dir() and sub.name != "subagents":
                    # Recurse — adds the subdir watch + scans its contents.
                    self._add_dir_watch(tmux_name, str(sub))
        except OSError:
            pass

    def _handle_jsonl_appeared(
        self,
        tmux_name: str,
        jsonl_path: Path,
        *,
        source: str = "discovery",
    ) -> bool:
        """Idempotently observe a discovered JSONL for an existing session.

        Thin compatibility wrapper over :meth:`observe_rollout` (the one
        ingestion entry point — bead auto-suvcp Rule 1). Shared by
        scan-on-watch-add, the tail-endpoint fallback, and reconciliation.
        Returns True iff this call moved the row's link onto *jsonl_path*.
        """
        if "subagents" in jsonl_path.parts:
            return False
        if jsonl_path.suffix != ".jsonl":
            return False
        row = get_session(tmux_name)
        if row is None:
            return False
        before = row.get("jsonl_path")
        self.observe_rollout(tmux_name, jsonl_path, source=source)
        after = (get_session(tmux_name) or {}).get("jsonl_path")
        return after == str(jsonl_path) and before != after

    # ── Rollout-ingestion state machine (bead auto-suvcp) ─────────────
    #
    # observe_rollout is the ONE entry point every discovery mechanism
    # calls; provenance selects only the next state (Rule 1). The methods
    # below mirror the model's actions — action names cited per block.
    # Everything through promotion runs synchronously on the event loop
    # (or on the startup thread before the loop runs), so promotion is
    # serialized per session by construction.

    def _gate(self, tmux_name: str) -> _SessionGate:
        return self._session_gates.setdefault(tmux_name, _SessionGate())

    def _get_track(self, tmux_name: str, path: str | Path) -> _FileTrack:
        key = (tmux_name, str(path))
        track = self._tracks.get(key)
        if track is None:
            track = _FileTrack(tmux_name=tmux_name, path=str(path))
            try:
                st = os.stat(str(path))
                track.generation = (st.st_dev, st.st_ino)
            except OSError:
                pass
            self._tracks[key] = track
        return track

    def observe_rollout(
        self,
        tmux_name: str,
        jsonl_path: Path | str,
        *,
        source: str = "discovery",
        create_event: bool = False,
    ) -> bool:
        """[model: ObserveOutcome / DeliverCreate] — the single entry point.

        Returns True when the observation left the file's track STREAMING.
        """
        path = Path(jsonl_path)
        if "subagents" in path.parts or path.suffix != ".jsonl":
            return False
        key = (tmux_name, str(path))
        track = self._tracks.get(key)

        # Birth-trust bookkeeping: ANY create observation on this epoch
        # consumes "first CREATE" (A7 — epoch = registration).
        epoch = self._dir_epochs.get((tmux_name, str(path.parent)))
        birth_candidate = False
        if create_event and epoch is not None:
            birth_candidate = epoch["snapshot_empty"] and not epoch["create_seen"]
            epoch["create_seen"] = True

        if track is not None and track.state in (TRACK_IGNORED, TRACK_CLOSED):
            # D6: stat-only tombstone gate; terminal tracks re-open ONLY on
            # observable progress (B3+A6).
            if not self._observable_progress(track):
                return False
            row = get_session(tmux_name)
            if row is None:
                return False
            try:
                st = os.stat(track.path)
            except OSError:
                return False
            track.generation = (st.st_dev, st.st_ino)
            track.state = TRACK_CHARACTERIZING
            track.characterize_deadline = time.time() + _CHARACTERIZE_DEADLINE_S
            # B3: the published level survives the reopen — it was folded
            # into the tombstone at close, and losing it here is exactly
            # how a re-observation turns into a failure-free re-publish.
            if track.tombstone is not None and len(track.tombstone) >= 5:
                track.published_up_to = max(
                    track.published_up_to, track.tombstone[4],
                )
            track.tombstone = None
            track.close_reason = None
            track.expected_previous = row.get("jsonl_path")
            logger.info(
                "session_monitor: track reopened on progress tmux=%s path=%s source=%s",
                tmux_name, path.name, source,
            )
        row = get_session(tmux_name)
        if row is None:
            return False
        # B1: ambient (non-create) observation of a file in a HOST row's
        # SHARED directory has no ownership evidence — the file is very
        # likely another session's transcript. Host first-resolution goes
        # through positive-ownership channels only (launch watcher, meta
        # files, handshake, parentUuid rollover); the row's own linked
        # path still re-attaches below. Skip EARLY, before any track or
        # stat churn — a shared project dir can hold years of history.
        if (
            row.get("type") == "host"
            and not create_event
            and row.get("jsonl_path") != str(path)
        ):
            return False
        if track is None:
            try:
                st = os.stat(str(path))
            except OSError:
                return False
            track = _FileTrack(
                tmux_name=tmux_name,
                path=str(path),
                generation=(st.st_dev, st.st_ino),
                expected_previous=row.get("jsonl_path"),
            )
            self._tracks[key] = track

        if create_event or path.name.startswith("rollout-"):
            track.supersede_candidate = True
        if row.get("jsonl_path") == str(path):
            # N5 / N3 refinement: the row's own linked path ALWAYS
            # re-observes as a persisted re-attach, never through the CAS.
            self._promote(track, "persisted")
            return track.state == TRACK_STREAMING
        if track.state == TRACK_STREAMING:
            # Streaming but no longer the linked path: the link moved on
            # without closing this track (shouldn't happen — defensive).
            track.state = TRACK_CHARACTERIZING
            track.expected_previous = row.get("jsonl_path")
        if birth_candidate:
            # Rule 1 birth trust (N1+A7); the CAS inside _promote compares
            # against NULL (V1).
            self._promote(track, "birth")
            return track.state == TRACK_STREAMING
        self._classify_and_step(track, source=source)
        return track.state == TRACK_STREAMING

    def _classify_and_step(self, track: _FileTrack, *, source: str = "recheck") -> None:
        """[model: EffClassify + recheck paths] — tri-state, never fails open."""
        if track.state in (TRACK_IGNORED, TRACK_CLOSED, TRACK_STREAMING):
            return
        path = Path(track.path)
        classification = _classify_codex_rollout(path)
        if classification.kind == "unknown":
            # Retain the watch and responsibility; no bytes released.
            track.state = TRACK_CHARACTERIZING
            if track.characterize_deadline is None:
                track.characterize_deadline = time.time() + _CHARACTERIZE_DEADLINE_S
            self._arm_track_watch(track)
            logger.info(
                "session_monitor: candidate tmux=%s old_path=%s candidate=%s "
                "classification=unknown reason=%s size_bytes=%s "
                "action=characterize source=%s",
                track.tmux_name, track.expected_previous, track.path,
                classification.reason, classification.size_bytes, source,
            )
            return
        if classification.kind == "subagent":
            self._close_track(track, state=TRACK_IGNORED,
                              reason=f"subagent:{classification.reason}")
            logger.info(
                "session_monitor: candidate tmux=%s old_path=%s candidate=%s "
                "classification=subagent reason=%s size_bytes=%s action=skip "
                "source=%s",
                track.tmux_name, track.expected_previous, track.path,
                classification.reason, classification.size_bytes, source,
            )
            return
        # A verified main that is NOT a supersede candidate (an ambient
        # scan's sighting of a non-chain file) may only FIRST-resolve an
        # unlinked row — in a shared directory it is another session's
        # file, and replacing an existing link with it is cross-session
        # adoption (NoChildAdoption; the pre-fix scans' linked-row guard).
        if not track.supersede_candidate:
            row = get_session(track.tmux_name)
            if row is not None and row.get("jsonl_path") and \
                    row.get("jsonl_path") != track.path:
                self._close_track(
                    track, state=TRACK_IGNORED, reason="nonchain_not_ours",
                )
                return
        # Verified main — Rule 3: ordering among mains (N2 + refinement).
        if self._viable_newer_exists(track.tmux_name, track.path):
            self._close_track(track, state=TRACK_CLOSED, reason="superseded")
            logger.info(
                "session_monitor: candidate tmux=%s old_path=%s candidate=%s "
                "classification=main action=superseded source=%s (viable "
                "newer exists; content publishes via ordered handover)",
                track.tmux_name, track.expected_previous, track.path, source,
            )
            return
        self._promote(track, "late")

    def _promote(self, track: _FileTrack, provenance: str) -> None:
        """[model: Promote] — serialized per session (event-loop-synchronous)."""
        tmux_name = track.tmux_name
        row = get_session(tmux_name)
        if row is None:
            return
        current = row.get("jsonl_path")
        if provenance != "persisted" and current == track.path:
            provenance = "persisted"   # N5, all paths
        if provenance == "persisted":
            self._link_and_stream(track, "persisted")
            return
        # First-resolution / rollover CAS [model: CASOk]. Birth compares
        # against NULL (V1 — never the current row); late compares against
        # the row value captured at observation.
        expected = None if provenance == "birth" else track.expected_previous
        if current != expected:
            # CAS-retry (B4): the loser is never terminal.
            track.state = TRACK_CHARACTERIZING
            track.expected_previous = current
            if track.characterize_deadline is None:
                track.characterize_deadline = time.time() + _CHARACTERIZE_DEADLINE_S
            self._arm_track_watch(track)
            return
        if self._unready_predecessors(tmux_name, track.path):
            # Rule 4: DEFER the link until every predecessor is ready.
            gate = self._gate(tmux_name)
            gate.pending_link = track.path
            gate.pending_expected = track.expected_previous
            self.request_drain(tmux_name)   # the handover pump runs under the gate
            return
        self._link_and_stream(track, provenance)

    def _link_and_stream(self, track: _FileTrack, provenance: str) -> None:
        """[model: LinkEffect] — commit the link, arm streaming, request drain."""
        tmux_name = track.tmux_name
        path = Path(track.path)
        row = get_session(tmux_name)
        if row is None:
            return
        gate = self._gate(tmux_name)
        try:
            st = os.stat(track.path)
        except OSError:
            track.state = TRACK_CHARACTERIZING
            track.expected_previous = row.get("jsonl_path")
            return
        # Re-stat identity equality between classification and promotion
        # (Rule 6): a swapped inode re-characterizes instead of linking.
        if track.generation is not None and track.generation != (st.st_dev, st.st_ino):
            track.generation = (st.st_dev, st.st_ino)
            track.state = TRACK_CHARACTERIZING
            track.expected_previous = row.get("jsonl_path")
            if track.characterize_deadline is None:
                track.characterize_deadline = time.time() + _CHARACTERIZE_DEADLINE_S
            return
        from tools.dashboard.dao.dashboard_db import (
            next_link_seq,
            set_jsonl_generation,
            update_jsonl_link,
        )
        old_path = row.get("jsonl_path")
        if provenance == "persisted" and old_path == str(path):
            # Re-attach (N5/N3): PRESERVE the persisted offset; backfill a
            # missing generation, and REPAIR a stale one (B6).
            row_generation = str(row.get("jsonl_generation") or "")
            generation_current = row_generation.startswith(
                f"{st.st_dev}:{st.st_ino}:"
            )
            if (
                track.state == TRACK_STREAMING
                and generation_current
                and (self._tail_states.get(tmux_name) is not None
                     and self._tail_states[tmux_name].watch_descriptor is not None)
            ):
                # Healthy steady state — catch up only when bytes are unread.
                if st.st_size > (row.get("file_offset", 0) or 0):
                    self.request_drain(tmux_name)
                return
            if not row_generation or not generation_current:
                # B6: a stale TRUTHY generation (inode changed while the
                # path stayed) must be repaired, not just a falsy one
                # backfilled — otherwise every read re-stats, mismatches,
                # and rejects the new inode forever.
                # R1: ANY (dev,ino) mismatch resets the cursor to 0, in
                # the SAME UPDATE as the generation write. The old byte
                # cursor describes the REPLACED file's content; keeping it
                # against a same-or-larger replacement reads mid-line and
                # silently loses the line spanning the cursor. Loss is
                # never acceptable; the replacement is a failure event and
                # the full re-read's duplicates are the accepted residual.
                seq = next_link_seq(tmux_name)
                repaired = f"{st.st_dev}:{st.st_ino}:{seq}"
                stale = bool(row_generation) and not generation_current
                set_jsonl_generation(
                    tmux_name, repaired, expect_path=str(path),
                    file_offset=0 if stale else None,
                )
                if stale:
                    logger.info(
                        "session_monitor: repaired stale generation for %s "
                        "(%s → %s) — cursor reset to 0 (replacement inode)",
                        tmux_name, row_generation, repaired,
                    )
        else:
            # First resolution or rollover advance.
            if old_path and old_path != str(path):
                old_track = self._tracks.get((tmux_name, old_path))
                if old_track is not None:
                    self._close_track(
                        old_track, state=TRACK_CLOSED, reason="superseded",
                    )
            seq = next_link_seq(tmux_name)
            generation = f"{st.st_dev}:{st.st_ino}:{seq}"
            update_jsonl_link(
                tmux_name,
                session_uuid=path.stem,
                jsonl_path=str(path),
                project=row.get("project"),
                generation=generation,
                # Linking never rewinds publication (Rule 6, forbidden
                # route 2): start at the file's already-published level.
                file_offset=track.published_up_to,
            )
            logger.info(
                "session_monitor: linked %s → %s provenance=%s generation=%s "
                "init_offset=%d old_path=%s",
                tmux_name, path.name, provenance, generation,
                track.published_up_to, old_path,
            )
            # Stream-derived ephemeral state belongs to the old file.
            old_ts = self._tail_states.get(tmux_name)
            self._tail_states[tmux_name] = _TailState(
                resolution_dir=(old_ts.resolution_dir if old_ts else None)
                or path.parent,
                needs_resolution=False,
            )
            # W2: eager-create the graph source row at link time.
            self._eager_create_source(tmux_name, path)
        track.state = TRACK_STREAMING
        track.provenance = provenance
        track.generation = (st.st_dev, st.st_ino)
        track.characterize_deadline = None
        # The streaming file is watched through the session file watch.
        self._release_track_watch(track)
        if self._use_inotify:
            self._add_file_watch(tmux_name, str(path))
            self._add_dir_watch(tmux_name, str(path.parent))
        # Invariant 9: NO registry broadcast at link time — the registry
        # publishes AFTER the catch-up drain, so a linked-but-zero card is
        # never durable (CalStartupStall's third leg).
        gate.publish_registry_after_drain = True
        self.request_drain(tmux_name)

    def snapshot_read_context(self, tmux_name: str) -> dict | None:
        """Deep-copied per-session enrichment state for HTTP read paths.

        auto-16g9t: read paths (tail endpoint backfills) must NEVER share
        the live stream's mutable parse/postprocess state — they get this
        snapshot instead. Loop-safe by construction: handlers and the
        monitor share one event loop, and every field here is committed
        only by loop tasks (worker copies are committed post-await), so a
        plain deep-copy sees a consistent picture. No locks, no threads.

        Returns None when the session has no live tail state (dead or
        never-tailed sessions read with fresh, self-contained state).
        """
        ts = self._tail_states.get(tmux_name)
        if ts is None:
            return None
        try:
            return {
                "parse_ctx": copy.deepcopy(ts.parse_ctx),
                "postprocess_state": copy.deepcopy(ts.postprocess_state),
                "agent_descriptions": dict(ts.agent_descriptions),
                "claimed_subagents": set(ts.claimed_subagents),
                "last_enqueue_content": ts.last_enqueue_content,
            }
        except Exception:
            logger.exception(
                "session_monitor: snapshot_read_context failed for %s",
                tmux_name,
            )
            return None

    def _close_track(self, track: _FileTrack, *, state: str, reason: str) -> None:
        """Terminal transition — records the D6 tombstone, releases the wd
        (A8), and logs the late-flush residual (the theorem's explicit
        cession) when a superseded file holds bytes past its checked level.

        B3 (failure-free dup route #3): while a file is LINKED its
        publication progress lives only in the row's ``file_offset``; that
        level is folded into ``published_up_to`` here — BEFORE the link
        advances and rewrites the row — and rides the tombstone, so a
        later handover walk (or a progress-gated reopen) never mistakes a
        fully-drained predecessor for an unpublished one and re-reads it
        from byte 0.
        """
        row = get_session(track.tmux_name)
        if row is not None and row.get("jsonl_path") == track.path:
            row_generation = str(row.get("jsonl_generation") or "")
            gen_matches = True
            if row_generation and track.generation is not None:
                gen_matches = row_generation.startswith(
                    f"{track.generation[0]}:{track.generation[1]}:"
                )
            if gen_matches:
                track.published_up_to = max(
                    track.published_up_to, row.get("file_offset", 0) or 0,
                )
        track.state = state
        track.close_reason = reason
        track.characterize_deadline = None
        self._release_track_watch(track)
        try:
            st = os.stat(track.path)
            track.tombstone = (
                st.st_size, st.st_mtime_ns, st.st_dev, st.st_ino,
                track.published_up_to,
            )
        except OSError:
            track.tombstone = None
        if reason == "superseded" and track.checked_size is not None:
            lco = self._last_complete_offset(track.path)
            if lco > track.checked_size:
                logger.warning(
                    "session_monitor: late-flush residual ceded for %s — "
                    "%d bytes past checked level %d (%s); by the ordered-"
                    "handover theorem these bytes are the explicit residual",
                    track.tmux_name, lco - track.checked_size,
                    track.checked_size, track.path,
                )

    def _quarantine_track(self, track: _FileTrack) -> None:
        """[model: DeadlinePass] — time-driven expiry with a terminal
        diagnostic. The caller's still-CHARACTERIZING check and this call
        are await-free as a pair (a track that promoted meanwhile must not
        be quarantined under it)."""
        classification = _classify_codex_rollout(Path(track.path))
        logger.warning(
            "session_monitor: characterization deadline expired tmux=%s "
            "path=%s reason=%s size_bytes=%s — quarantined (re-opens only "
            "on observable progress)",
            track.tmux_name, track.path,
            classification.reason, classification.size_bytes,
        )
        self._close_track(track, state=TRACK_CLOSED, reason="quarantined")

    def _observable_progress(self, track: _FileTrack) -> bool:
        """B3+A6: terminal tracks re-open only on dev/ino/size/mtime change.

        Only the stat prefix of the tombstone participates — its trailing
        published-level element (B3) is bookkeeping, not progress.
        """
        try:
            st = os.stat(track.path)
        except OSError:
            return False
        if track.tombstone is None:
            return True
        return (
            (st.st_size, st.st_mtime_ns, st.st_dev, st.st_ino)
            != tuple(track.tombstone[:4])
        )

    def _arm_track_watch(self, track: _FileTrack) -> None:
        """IN_MODIFY watch for a CHARACTERIZING track (classification
        re-runs on MODIFY — Rule 2). Ownership is per inode (D3): the
        kernel returns the SAME wd for every add_watch on one inode, so
        the track subscribes to the inode's watch entry rather than
        claiming the wd for itself.
        """
        if not self._inotify or track.wd is not None:
            return
        try:
            st = os.stat(track.path)
        except OSError:
            return
        inode_key = (st.st_dev, st.st_ino)
        entry = self._subscribe_inode(
            inode_key, ("track", track.tmux_name, track.path), track.path,
        )
        if entry is None:
            return
        track.generation = inode_key
        track.wd_epoch += 1
        track.wd = entry["wd"]

    def _release_track_watch(self, track: _FileTrack) -> None:
        """Unsubscribe a track from its inode watch (Rule 8/D3). The
        kernel watch is removed only when the LAST subscriber leaves —
        releasing one track never drops another's watch."""
        if track.wd is None:
            return
        track.wd = None
        track.wd_epoch += 1   # invalidate any queued dispatch for this track
        self._unsubscribe_watch(("track", track.tmux_name, track.path))

    # ── Succession ordering (Rule 3 / D5) ─────────────────────────────

    @staticmethod
    def _chain_ts_normalize(ts_text: str) -> str:
        """Normalize a timestamp (header ISO or filename form) to a
        digits-only, second-granularity sortable key."""
        digits = re.sub(r"[^0-9T]", "", ts_text or "")
        return digits[:15]  # YYYYMMDDThhmmss

    def _chain_key(self, path: Path) -> tuple[str, str] | None:
        """D5: (session_meta header timestamp, rollout UUID tiebreak) —
        total order over verified mains with ≥1 complete line. Returns
        None when the file has no complete first line yet."""
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                first = fh.readline()
            if not first.endswith("\n"):
                return None
            entry = json.loads(first)
        except (OSError, json.JSONDecodeError):
            return None
        payload = entry.get("payload") if isinstance(entry, dict) else None
        ts_text = ""
        uuid_text = path.stem
        if isinstance(payload, dict):
            ts_text = str(payload.get("timestamp") or "")
            uuid_text = str(payload.get("id") or path.stem)
        if not ts_text:
            ts_text = str(entry.get("timestamp") or "") if isinstance(entry, dict) else ""
        if not ts_text:
            # Fallback: the rollout filename embeds the same clock.
            m = re.match(r"rollout-(.+?)-[0-9a-f]{8}-", path.name)
            ts_text = m.group(1) if m else ""
        return (self._chain_ts_normalize(ts_text), uuid_text)

    def _succession_chain(self, tmux_name: str, sibling_of: Path) -> list[tuple[tuple[str, str], str]]:
        """The session's rollover chain: verified mains with at least one
        complete line, in (header ts, uuid) order. Non-rollout files (the
        Claude layout) have no chain."""
        directory = sibling_of.parent
        out: list[tuple[tuple[str, str], str]] = []
        try:
            candidates = sorted(directory.glob("rollout-*.jsonl"))
        except OSError:
            return out
        for f in candidates:
            if "subagents" in f.parts:
                continue
            if _classify_codex_rollout(f).kind != "main":
                continue
            key = self._chain_key(f)
            if key is None:
                continue  # no complete line — not viable (N2 refinement)
            out.append((key, str(f)))
        out.sort()
        return out

    def _target_chain_position(self, target: Path) -> tuple[str, str]:
        """Chain position for the promotion target itself. Falls back to
        the filename timestamp when the file is still empty (a birth-
        trusted empty rollout has no header yet)."""
        key = self._chain_key(target)
        if key is not None:
            return key
        m = re.match(r"rollout-(.+?)-([0-9a-f]{8}-[0-9a-f-]+)\.jsonl", target.name)
        if m:
            return (self._chain_ts_normalize(m.group(1)), m.group(2))
        return (self._chain_ts_normalize(""), target.stem)

    def _viable_newer_exists(self, tmux_name: str, path: str) -> bool:
        """Rule 3 (N2): viable = exists AND has content; an EMPTY successor
        does not supersede."""
        target = Path(path)
        if not target.name.startswith("rollout-"):
            return False
        pos = self._target_chain_position(target)
        for key, f in self._succession_chain(tmux_name, target):
            if f != str(target) and key > pos:
                return True
        return False

    def _predecessors(self, tmux_name: str, target: str) -> list[str]:
        """Chain-earlier viable mains, OLDEST FIRST (Rule 4 walk order)."""
        target_path = Path(target)
        if not target_path.name.startswith("rollout-"):
            return []
        pos = self._target_chain_position(target_path)
        return [
            f for key, f in self._succession_chain(tmux_name, target_path)
            if f != target and key < pos
        ]

    @staticmethod
    def _last_complete_offset(path: str | Path) -> int:
        """Byte offset just past the last newline — the complete-line size."""
        try:
            size = os.stat(str(path)).st_size
        except OSError:
            return 0
        if size == 0:
            return 0
        try:
            with open(str(path), "rb") as fh:
                pos = size
                while pos > 0:
                    start = max(0, pos - 65536)
                    fh.seek(start)
                    chunk = fh.read(pos - start)
                    nl = chunk.rfind(b"\n")
                    if nl != -1:
                        return start + nl + 1
                    pos = start
        except OSError:
            return 0
        return 0

    def _published_level(self, tmux_name: str, path: str, row: dict | None) -> int:
        """Bytes of *path* already published for this session — the row's
        offset when *path* is the linked file, else the track's handover
        watermark."""
        if row and row.get("jsonl_path") == path:
            return row.get("file_offset", 0) or 0
        return self._get_track(tmux_name, path).published_up_to

    def _unready_predecessors(self, tmux_name: str, target: str) -> list[str]:
        """Rule 4 readiness: a predecessor is READY iff it is published to
        its current complete-line EOF AND final-checked at that level. This
        re-reads the filesystem on every call — the pre-advance final check
        is structural, not procedural."""
        row = get_session(tmux_name)
        out: list[str] = []
        for p in self._predecessors(tmux_name, target):
            lco = self._last_complete_offset(p)
            track = self._get_track(tmux_name, p)
            published = self._published_level(tmux_name, p, row)
            if published < lco or track.checked_size != lco:
                out.append(p)
        return out

    # ── Drain ownership (Rule 5 / D4 / A5 / N4) ───────────────────────

    def request_drain(self, tmux_name: str) -> None:
        """[model: ClaimDrain / ClaimContended] — synchronous, await-free.

        A contended request transfers responsibility through the session
        dirty flag (T87: never skip — the skipped signal may be the only
        one those bytes get).
        """
        # A session torn down by B5 has neither a gate nor a tail state; a
        # stray request must not auto-recreate a fresh (non-retired) gate
        # for it. (Currently unreachable — dispatch maps are purged first
        # — kept as a one-line belt per review round 2.)
        if (
            tmux_name not in self._session_gates
            and tmux_name not in self._tail_states
        ):
            return
        gate = self._gate(tmux_name)
        if gate.retired:
            return   # B5: dead session — no new work is ever accepted
        if gate.busy:
            gate.dirty = True
            return
        gate.needs_drain = True
        self._pump_drain(tmux_name)

    def _pump_drain(self, tmux_name: str) -> None:
        """D4 pump — claims ``busy`` only AFTER task creation succeeds.

        Sync-context callers (no running loop — e.g. tests driving
        ``_init_inotify`` directly) leave ``needs_drain`` set; the
        loop-start pump / reconciliation tick picks it up (B2: a
        synchronous busy-claim whose schedule() no-ops would pin the gate
        forever).
        """
        gate = self._gate(tmux_name)
        if self._stopping or gate.retired or gate.busy or not gate.needs_drain:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        coro = self._drain_as_owner(tmux_name)
        try:
            task = loop.create_task(coro)
        except RuntimeError:
            coro.close()
            return
        gate.needs_drain = False
        gate.busy = True
        gate.owner_task = task
        task.add_done_callback(
            lambda t, s=tmux_name: self._on_drain_task_done(s, t)
        )

    def _pump_pending_drains(self) -> None:
        """Loop-start pump (B2): drains requested from sync contexts."""
        for tmux_name, gate in list(self._session_gates.items()):
            if gate.needs_drain and not gate.busy:
                self._pump_drain(tmux_name)

    def _on_drain_task_done(self, tmux_name: str, task: asyncio.Task) -> None:
        """Owner-task-completion callback (D4) — runs on the loop, await-free.

        Cancellation of the awaiter does NOT stop an executor worker (A5).
        The owner awaits ``asyncio.shield(inflight_future)``, so the inner
        executor future is UNCANCELLABLE from the awaiter side and resolves
        only when the worker thread actually returns — which makes
        ``inflight_future.done()`` a truthful "worker stopped" signal (the
        unshielded wrapper reports done-cancelled the moment the awaiter is
        cancelled, while the thread runs on — the exact early-release bug
        this replaces). When a read is still in flight, the release is
        deferred to that future's own done-callback; never a ``finally`` on
        the awaiter (finally-release admits a double-tail; no release pins
        forever).
        """
        gate = self._session_gates.get(tmux_name)
        if gate is None:
            return   # gate already retired and removed (B5)
        if gate.owner_task is task:
            gate.owner_task = None
        if task.cancelled():
            gate.dirty = True   # responsibility retained
        else:
            exc = task.exception()
            if exc is not None:
                gate.dirty = True   # worker failure: retain responsibility
                gate.crashed = True
                logger.error(
                    "session_monitor: drain worker failed for %s: %r",
                    tmux_name, exc,
                )
        inflight = gate.inflight_future
        if inflight is not None and not inflight.done():
            inflight.add_done_callback(
                lambda _f, s=tmux_name: self._release_drain_gate(s)
            )
        else:
            self._release_drain_gate(tmux_name)

    def _release_drain_gate(self, tmux_name: str) -> None:
        """[model: WkFinal] — the final dirty-check / release / reschedule
        sequence. Await-free (bead invariant 7); runs on the event loop."""
        gate = self._session_gates.get(tmux_name)
        if gate is None:
            return   # gate already retired and removed (B5)
        gate.inflight_future = None
        gate.busy = False
        if gate.retired:
            # B5: the session is gone — drop the gate instead of
            # rescheduling; any bytes the dead session never published
            # are the final-graph-catchup path's business, not a drain's.
            self._session_gates.pop(tmux_name, None)
            return
        needs_continuation = gate.dirty or gate.pending_link is not None
        if self._stopping:
            # S2: retain responsibility for the next start(); never pump.
            if needs_continuation:
                gate.needs_drain = True
                gate.dirty = False
            return
        if needs_continuation:
            # Continuation targets the row's CURRENT path (N4 corollary) —
            # the next owner re-reads the row at every pass.
            gate.needs_drain = True
            if gate.crashed:
                # Backoff after a worker exception so a deterministic crash
                # can't hot-spin; the tick remains the level-triggered
                # backstop if the loop is gone.
                gate.crashed = False
                try:
                    asyncio.get_running_loop().call_later(
                        1.0, self._pump_drain, tmux_name,
                    )
                except RuntimeError:
                    pass
            else:
                self._pump_drain(tmux_name)
        elif gate.publish_registry_after_drain:
            gate.publish_registry_after_drain = False
            try:
                asyncio.get_running_loop().create_task(self._broadcast_registry())
            except RuntimeError:
                pass

    async def _drain_as_owner(self, tmux_name: str) -> None:
        """[model: WkReadRow..WkFinal] — the owner drains the SESSION, not
        the track (N4): every pass re-reads the row and drains the row's
        CURRENT path. Publication order is publish-then-persist (Rule 6):
        broadcast the read range, THEN persist the offset — a crash between
        the two re-delivers at most this one in-flight window (the accepted
        BoundedDuplicates residual); persist-first would silently lose the
        startup burst (CalOffsetAckFirst)."""
        gate = self._gate(tmux_name)
        loop = asyncio.get_running_loop()
        passes = 0
        while passes < _MAX_CONSECUTIVE_DIRTY_PASSES:
            passes += 1
            gate.dirty = False
            if gate.pending_link is not None:
                await self._handover_step(tmux_name)
            row = get_session(tmux_name)   # fresh row AND path (N4)
            if row is None:
                return
            if row.get("jsonl_path"):
                if tmux_name not in self._tail_states:
                    self._tail_states[tmux_name] = _TailState()
                fut = loop.run_in_executor(
                    None, self._read_tail_window, dict(row),
                    self._tail_states[tmux_name].parse_ctx,
                )
                gate.inflight_future = fut
                window = await asyncio.shield(fut)
                gate.inflight_future = None
                if window is not None:
                    await self._publish_tail_window(tmux_name, row, window)
                    self._persist_tail_window(tmux_name, row, window)
                    gate.dirty = True   # re-check: more may have landed mid-read
                    continue
            if not gate.dirty and gate.pending_link is None:
                return
        # Bounded-pass exhaustion: leave dirty set — the release path
        # schedules the continuation (responsibility never dropped).
        gate.dirty = True

    # ── Ordered handover (Rule 4) ─────────────────────────────────────

    async def _handover_step(self, tmux_name: str) -> None:
        """[model: HandoverDrain / CommitLink] — runs under the session
        drain gate. Predecessors are made ready OLDEST FIRST; the link
        commits only through a guard that re-reads the filesystem."""
        gate = self._gate(tmux_name)
        loop = asyncio.get_running_loop()
        steps = 0
        while gate.pending_link is not None and steps < _MAX_HANDOVER_STEPS:
            steps += 1
            target = gate.pending_link
            row = get_session(tmux_name)
            if row is None:
                gate.pending_link = None
                gate.pending_expected = None
                return
            unready = self._unready_predecessors(tmux_name, target)
            if unready:
                p = unready[0]   # oldest first
                if p == row.get("jsonl_path"):
                    # The currently linked predecessor drains through the
                    # ORDINARY path, advancing the session's persisted
                    # file_offset (Rule 6, forbidden dup route 1).
                    if tmux_name not in self._tail_states:
                        self._tail_states[tmux_name] = _TailState()
                    fut = loop.run_in_executor(
                        None, self._read_tail_window, dict(row),
                        self._tail_states[tmux_name].parse_ctx,
                    )
                    gate.inflight_future = fut
                    window = await asyncio.shield(fut)
                    gate.inflight_future = None
                    if window is not None:
                        await self._publish_tail_window(tmux_name, row, window)
                        self._persist_tail_window(tmux_name, row, window)
                    else:
                        self._mark_predecessor_checked(tmux_name, p)
                else:
                    progressed = await self._publish_unlinked_segment(
                        tmux_name, p,
                    )
                    if not progressed:
                        self._mark_predecessor_checked(tmux_name, p)
                continue
            # CommitLink: every predecessor passed the structural final
            # check (readiness above re-stats the filesystem).
            current = (get_session(tmux_name) or {}).get("jsonl_path")
            track = self._get_track(tmux_name, target)
            expected = gate.pending_expected
            gate.pending_link = None
            gate.pending_expected = None
            if current == expected:
                self._link_and_stream(track, "late")
            elif current == target:
                self._link_and_stream(track, "persisted")   # N5 again
            else:
                # Genuinely stale (the row moved elsewhere) — re-arm.
                track.state = TRACK_CHARACTERIZING
                track.expected_previous = current
                track.characterize_deadline = (
                    time.time() + _CHARACTERIZE_DEADLINE_S
                )
            logger.info(
                "session_monitor: handover commit tmux=%s target=%s "
                "expected=%s row=%s outcome=%s",
                tmux_name, Path(target).name, expected, current,
                self._get_track(tmux_name, target).state,
            )

    def _mark_predecessor_checked(self, tmux_name: str, path: str) -> None:
        """Seal a predecessor at its current complete-line EOF once it is
        fully published there. A later grow reopens it: the readiness check
        compares ``checked_size`` against a fresh stat, so a grown file is
        drained again before the advance."""
        row = get_session(tmux_name)
        lco = self._last_complete_offset(path)
        published = self._published_level(tmux_name, path, row)
        track = self._get_track(tmux_name, path)
        if published >= lco:
            track.checked_size = lco
            logger.info(
                "session_monitor: handover sealed %s at %d bytes (%s)",
                tmux_name, lco, Path(path).name,
            )

    async def _publish_unlinked_segment(self, tmux_name: str, path: str) -> bool:
        """Publish a never-linked predecessor's next window through the
        common publication path with EXPLICIT file identity (D2 — the batch
        never assumes the DB row links the file being published). Advances
        the session's cumulative entry_count but never file_offset.
        Returns True when new bytes were published."""
        gate = self._gate(tmux_name)
        row = get_session(tmux_name)
        if row is None:
            return False
        track = self._get_track(tmux_name, path)
        loop = asyncio.get_running_loop()
        if tmux_name not in self._tail_states:
            self._tail_states[tmux_name] = _TailState()
        fut = loop.run_in_executor(
            None, self._read_segment_window,
            dict(row), path, track.published_up_to, track.generation,
            self._tail_states[tmux_name].parse_ctx,
        )
        gate.inflight_future = fut
        window = await asyncio.shield(fut)
        gate.inflight_future = None
        if window is None:
            return False
        # Post-await guard (round-1 addendum item 3): a deregister landing
        # during the shielded read can pop this session's tail state — the
        # pre-executor creation above does NOT survive the await. Re-create
        # rather than KeyError into the crash path (load-bearing before
        # commit A moved the creation earlier; both are needed).
        if tmux_name not in self._tail_states:
            self._tail_states[tmux_name] = _TailState()
        ts = self._tail_states[tmux_name]
        if window.get("parse_ctx_after") is not None:
            ts.parse_ctx = window["parse_ctx_after"]
        await self._process_tail_entries(
            tmux_name, row, ts, window["entries"], source_path=Path(path),
            span={
                "file": Path(path).stem,
                "from": window["start_offset"],
                "to": window["new_offset"],
            },
        )
        await self._graph_appender_tick(tmux_name, Path(path))
        from tools.dashboard.dao.dashboard_db import increment_entry_count
        increment_entry_count(
            tmux_name,
            window["raw_count"],
            last_message=window["last_message"],
            last_activity=window["mtime"],
        )
        track.published_up_to = window["new_offset"]
        logger.info(
            "session_monitor: handover published %s bytes %d..%d of %s",
            tmux_name, window["start_offset"], window["new_offset"],
            Path(path).name,
        )
        return True

    # ── Read / publish / persist (Rule 6) ─────────────────────────────

    def _read_tail_window(
        self, row: dict, parse_ctx: dict | None = None,
    ) -> dict | None:
        """Blocking read of the linked file's next complete-line window.

        WORKER-PURITY INVARIANT (pinned by
        test_rollout_ingestion.py::test_b4_purity_read_workers_never_write_or_broadcast): this
        function and :meth:`_read_segment_window` run on executor threads
        that CANNOT be stopped by cancelling their awaiter (A5). They must
        therefore never write the DB, never broadcast, never mutate
        monitor state — purity is the only thing that makes a cancelled
        worker finishing late harmless. Publication and persistence happen
        on the loop task, after the shielded await.

        Returns None when there is nothing new or the file's identity no
        longer matches the row's generation (re-stat equality, Rule 6)."""
        jsonl_path_str = row.get("jsonl_path")
        if not jsonl_path_str:
            return None
        jsonl_path = Path(jsonl_path_str)
        try:
            st = jsonl_path.stat()
        except OSError:
            return None
        expect_generation = row.get("jsonl_generation") or ""
        if expect_generation:
            try:
                dev_s, ino_s = expect_generation.split(":")[:2]
                if (st.st_dev, st.st_ino) != (int(dev_s), int(ino_s)):
                    return None
            except ValueError:
                pass
        file_offset = row.get("file_offset", 0) or 0
        if st.st_size <= file_offset:
            return None
        try:
            with open(jsonl_path, "rb") as fh:
                fh.seek(file_offset)
                data = fh.read()
        except OSError:
            return None
        last_nl = data.rfind(b"\n")
        if last_nl == -1:
            return None
        # Purity: parse against a COPY of the live parse ctx; the loop task
        # commits window["parse_ctx_after"] only when the window publishes.
        ctx_after = copy.deepcopy(parse_ctx) if parse_ctx else {}
        window = self._parse_window(
            row, data[:last_nl + 1],
            stem=jsonl_path.stem, base_offset=file_offset,
            parse_ctx=ctx_after, source_path=jsonl_path,
        )
        window.update({
            "path": jsonl_path_str,
            "expect_generation": expect_generation,
            "start_offset": file_offset,
            "new_offset": file_offset + last_nl + 1,
            "mtime": st.st_mtime,
            "size": st.st_size,
            "parse_ctx_after": ctx_after,
        })
        return window

    def _read_segment_window(
        self, row: dict, path: str, start_offset: int,
        expect_generation: tuple[int, int] | None = None,
        parse_ctx: dict | None = None,
    ) -> dict | None:
        """Blocking read of a NON-linked file's next complete-line window
        (handover publication). Same WORKER-PURITY INVARIANT as
        :meth:`_read_tail_window` — pure read, no writes/broadcasts/state.
        ``expect_generation`` (S1) is the track's recorded
        (st_dev, st_ino): a swapped inode returns None instead of
        publishing another file's bytes under this identity."""
        p = Path(path)
        try:
            st = p.stat()
        except OSError:
            return None
        if expect_generation is not None and (
            (st.st_dev, st.st_ino) != expect_generation
        ):
            return None
        if st.st_size <= start_offset:
            return None
        try:
            with open(p, "rb") as fh:
                fh.seek(start_offset)
                data = fh.read()
        except OSError:
            return None
        last_nl = data.rfind(b"\n")
        if last_nl == -1:
            return None
        ctx_after = copy.deepcopy(parse_ctx) if parse_ctx else {}
        window = self._parse_window(
            row, data[:last_nl + 1],
            stem=p.stem, base_offset=start_offset,
            parse_ctx=ctx_after, source_path=p,
        )
        window.update({
            "path": path,
            "start_offset": start_offset,
            "new_offset": start_offset + last_nl + 1,
            "mtime": st.st_mtime,
            "size": st.st_size,
            "parse_ctx_after": ctx_after,
        })
        return window

    def _parse_window(
        self,
        row: dict,
        complete: bytes,
        *,
        stem: str | None = None,
        base_offset: int = 0,
        parse_ctx: dict | None = None,
        source_path: Path | None = None,
    ) -> dict:
        """Parse a complete-line byte window into publication material.

        ``stem``/``base_offset`` thread the canonical entry identity
        (auto-16g9t): every parsed entry is stamped with
        ``entry_ref = (stem, line start offset, sub_index)``. ``parse_ctx``
        scopes cross-line parse state to this stream — the caller owns it.
        """
        transcript_path = source_path or Path(str(row.get("jsonl_path") or ""))
        reader = session_harness_mod.resolve_harness_for_path(
            transcript_path, ctx=parse_ctx,
        )
        harness = reader.harness
        raw_count = 0
        parsed_entries: list = []
        last_message: str | None = None
        context_tokens = row.get("context_tokens", 0)
        prior_model = row.get("model") or None
        model = prior_model
        prior_harness_state_raw = row.get("harness_state") or "{}"
        try:
            prior_harness_state = json.loads(prior_harness_state_raw)
            if not isinstance(prior_harness_state, dict):
                prior_harness_state = {}
        except (TypeError, ValueError, json.JSONDecodeError):
            prior_harness_state = {}
        harness_state = dict(prior_harness_state)
        parse_errors: list[str] = []
        line_off = base_offset
        for raw_line in complete.splitlines(keepends=True):
            this_line_off = line_off
            line_off += len(raw_line)
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                parse_errors.append(f"json: {str(exc)[:160]} | line: {line[:160]}")
                continue
            raw_count += 1
            text = harness.extract_message_text(entry)
            if text:
                last_message = text
            context_tokens = harness.extract_context_tokens(entry, context_tokens)
            model = harness.extract_model(entry, model)
            harness_state = harness.extract_harness_state(entry, harness_state) or {}
            try:
                parsed = reader.parse_line(line)
            except session_harness_mod.TranscriptParseContextError:
                raise
            except Exception as exc:
                parse_errors.append(
                    f"harness.parse_line: {str(exc)[:160]} | line: {line[:160]}"
                )
                parsed = None
            if parsed is not None:
                if stem is not None:
                    parsed_entries.extend(
                        session_harness_mod.stamp_entry_refs(
                            parsed, stem, this_line_off,
                        )
                    )
                elif isinstance(parsed, list):
                    parsed_entries.extend(parsed)
                else:
                    parsed_entries.append(parsed)
        # B7: the persist patch carries ONLY the keys this window itself
        # changed, merged into the row inside the UPDATE statement — a
        # concurrent poller write (composer_ready) survives.
        patch = {
            k: v for k, v in (harness_state or {}).items()
            if prior_harness_state.get(k) != v
        }
        return {
            "raw_count": raw_count,
            "entries": parsed_entries,
            "last_message": last_message,
            "context_tokens": context_tokens,
            "model_to_write": model if (model and model != prior_model) else None,
            "harness_state_patch": (
                json.dumps(patch, sort_keys=True) if patch else None
            ),
            "harness_state_full": harness_state,
            "parse_errors": parse_errors,
        }

    async def _publish_tail_window(
        self, tmux_name: str, row: dict, window: dict,
    ) -> None:
        """Publish (broadcast + graph append) BEFORE the offset persists."""
        if tmux_name not in self._tail_states:
            self._tail_states[tmux_name] = _TailState()
        ts = self._tail_states[tmux_name]
        ts.lines_processed_total += window["raw_count"] + len(window["parse_errors"])
        for err in window["parse_errors"]:
            ts.parse_errors_count += 1
            ts.last_parse_error = err
            ts.last_parse_error_ts = time.time()
        # Commit the worker's parse-ctx copy now that this window publishes
        # (a cancelled worker's late finish mutated only its own copy).
        if window.get("parse_ctx_after") is not None:
            ts.parse_ctx = window["parse_ctx_after"]
        span = {
            "file": Path(window["path"]).stem,
            "from": window["start_offset"],
            "to": window["new_offset"],
        }
        await self._process_tail_entries(
            tmux_name, row, ts, window["entries"], span=span,
            observed_model=window["model_to_write"] or row.get("model"),
            model_changed=bool(window["model_to_write"]),
        )
        await self._graph_appender_tick(tmux_name, Path(window["path"]))

    def _persist_tail_window(
        self, tmux_name: str, row: dict, window: dict,
    ) -> bool:
        """[model: WkPersist] — acknowledge the published window under the
        path+generation CAS. A stale write (rollover/re-link since the
        read) is dropped, never applied (OffsetCoherent)."""
        from tools.dashboard.dao.dashboard_db import persist_tail_state
        ok = persist_tail_state(
            tmux_name,
            expect_path=window["path"],
            expect_generation=window["expect_generation"],
            file_offset=window["new_offset"],
            last_activity=window["mtime"],
            last_message=window["last_message"],
            entry_count_add=window["raw_count"],
            context_tokens=window["context_tokens"],
            model=window["model_to_write"],
            harness_state_patch=window["harness_state_patch"],
        )
        if not ok:
            logger.info(
                "session_monitor: stale drain ack dropped for %s "
                "(path/generation moved since read)",
                tmux_name,
            )
            return False
        # Publish runs before persistence, so refresh the replay cache only
        # after this model/offset CAS lands. Open clients receive the model
        # directly on session:messages; new subscribers get the same value.
        if self._event_bus:
            self._event_bus.update_cache("session:registry", self.get_registry())
        if str(row.get("harness") or "claude").strip().lower() == "codex":
            try:
                _publish_codex_harness_usage_setting(
                    row, window["harness_state_full"],
                )
            except Exception:
                logger.exception(
                    "session_monitor: failed to publish codex harness usage for %s",
                    tmux_name,
                )
        return True

    def _eager_create_source(self, tmux_name: str, jsonl_path: Path) -> bool:
        """W2: eager-create the graph source row at JSONL-discovery time.

        Generalizes the ``agentic_source_id`` pattern
        (``insert_agentic_session``) to every session type: a zero-turn
        ``type='session'`` row appears immediately, so the Recent-sessions
        list and ``graph_source_id`` linking never wait on an ingest sweep
        (which may be minutes behind, or never run for a session whose
        every turn gets noise-filtered — the codex brief-only case).

        Gated by the ``ingest.eager_sources`` feature flag
        (``dashboard.feature_flags``) — unflagged default is ON as of W4
        (soak validated the W2/W3 rollout); an explicit
        ``{"enabled": false}`` row still opts a deployment out. Best-
        effort throughout: flag lookup, org resolution, and the write are
        all guarded — any failure here must never block JSONL linking.
        Returns True iff
        ``tmux_sessions.graph_source_id`` was written this call. Unresolvable
        org (fail-closed, matches pre-W2 behavior for org-less sessions) and
        transient failures both return False and are retried by
        :meth:`_eager_create_missing_sources` on the next reconciliation
        tick, so this method does not need its own retry loop.
        """
        try:
            from tools.dashboard import feature_flags
            # W4: eager sources is the unflagged default — a fresh
            # deployment (no seeded Settings row) gets it without needing
            # `graph set add dashboard.feature_flags#1 ...`. An explicit
            # {"enabled": false} row still disables it.
            if not feature_flags.is_enabled("ingest.eager_sources", default=True):
                return False
        except Exception:
            return False

        try:
            from tools.graph.ingest import session_target_org, _normalize_session_path
            row = get_session(tmux_name)
            default_org = row.get("project") if row else None
            org = session_target_org(jsonl_path, default=default_org)
            if not org:
                logger.info(
                    "session_monitor: eager-source skipped for %s — no resolvable org",
                    tmux_name,
                )
                return False

            abs_path = _normalize_session_path(jsonl_path)
            src = graph_ops.insert_eager_session_source(
                org=org,
                file_path=abs_path,
                session_uuid=jsonl_path.stem,
                platform=(row.get("harness") if row else None) or "claude",
                container_name=tmux_name,
            )
            from tools.dashboard.dao.dashboard_db import set_graph_source_validated
            set_graph_source_validated(tmux_name, src["id"])
            return True
        except Exception:
            logger.exception(
                "session_monitor: eager-source creation failed for %s", tmux_name,
            )
            return False

    def _eager_create_missing_sources(self) -> int:
        """Retry sweep (W2) — catches sessions whose eager-source creation
        didn't happen at discovery time (org unresolved then, the flag was
        off then on, a transient failure). Idempotent via
        ``insert_eager_session_source``'s existing-by-path check, so
        calling this every reconciliation tick is cheap and safe. Also
        used as the in-process replacement for the old ENRICH subprocess
        loop after ``seed_from_filesystem`` seeds sessions from disk.

        Synchronous (may open several org DBs) — callers on the event loop
        must wrap with ``asyncio.to_thread``. Returns the number of
        sessions that actually got a ``graph_source_id`` written this pass.
        """
        created = 0
        for row in get_live_sessions():
            jsonl_path_str = row.get("jsonl_path")
            if not jsonl_path_str or row.get("graph_source_id"):
                continue
            if self._eager_create_source(row["tmux_name"], Path(jsonl_path_str)):
                created += 1
        return created

    # ── W3: tail-primary GraphAppender ───────────────────────────────

    def _build_graph_appender(self, tmux_name: str, jsonl_path: Path):
        """Sync (runs off the event loop). Resolves org, ensures a linked
        graph source exists (reuses ``_eager_create_source``'s idempotent-
        by-path logic — a no-op if one already exists), and restores a
        ``GraphAppender`` from that source's persisted metadata.

        This IS the gap-catch-up mechanism for "tail (re)establishment":
        an existing source's stored ``graph_ingest_offset``/extractor
        state means the returned appender naturally resumes rather than
        re-reading from byte 0 — no separate resume path needed. A
        genuinely new ``jsonl_path`` (rollover) resolves to a *different*
        source by path, so the returned appender starts fresh at offset 0
        against the new file, matching "codex rollover creates new
        source."
        """
        from tools.graph.appender import GraphAppender, _org_lock
        from tools.graph.db import GraphDB, resolve_caller_db_path
        from tools.graph.ingest import _load_session_meta, session_target_org

        row = get_session(tmux_name)
        if row is None:
            return None
        org = session_target_org(jsonl_path, default=row.get("project"))
        if not org:
            return None

        # Always (re-)resolve against jsonl_path, not just when
        # graph_source_id is empty. _build_graph_appender only runs when
        # no appender is registered yet OR the tracked file_path changed
        # (rollover) — in the rollover case the row's graph_source_id
        # still points at the OLD file's source, and insert_eager_session_
        # source's idempotent-by-path lookup is exactly what re-resolves
        # it to the (new-or-existing) source for THIS path. Cheap: at most
        # one get_source_by_path + one UPDATE, and only on this
        # (re)establishment path, never per tail tick.
        self._eager_create_source(tmux_name, jsonl_path)
        row = get_session(tmux_name)
        source_id = row.get("graph_source_id") if row else None
        if not source_id:
            return None

        # Same fix as GraphAppender.feed_lines (auto-ea9g3): NEVER touch
        # the pooled GraphDB.for_org connection from a method that runs via
        # asyncio.to_thread — the default executor hands concurrent
        # submissions genuinely distinct OS threads, and a pooled
        # connection first opened on one thread raises ProgrammingError
        # the moment a different thread touches it. Fresh connection,
        # opened and closed within this call, under the same per-org lock
        # feed_lines uses (defense against SQLITE_BUSY from a concurrent
        # writer — GraphDB sets no busy_timeout).
        with _org_lock(org):
            db = GraphDB(resolve_caller_db_path(org))
            try:
                source = db.get_source(source_id)
            finally:
                db.close()
        if source is None:
            return None

        session_meta = _load_session_meta(jsonl_path)
        harness = str(row.get("harness") or "claude").strip().lower()
        default_model = "codex-cli" if harness == "codex" else "claude-code"
        return GraphAppender.from_source(
            source, org=org, file_path=jsonl_path, session_meta=session_meta,
            harness=harness, default_model=default_model,
        )

    def _feed_graph_appender(self, appender) -> None:
        """Sync tail-delta read + feed — deliberately independent of the
        viewer's ``_tail_one`` read (separate cursor, separate offset; see
        ``appender.py``'s module docstring). Implements the AC-offset gap-
        catch-up triggers: ``size < offset`` (truncation) forces a full
        reset before reading; ``size > offset`` resumes from the appender's
        own persisted offset, reading only the tail delta.
        """
        try:
            st = appender.file_path.stat()
        except OSError:
            return
        if st.st_size < appender.graph_ingest_offset:
            logger.info(
                "session_monitor: graph appender detected truncation for %s — resetting",
                appender.source_id,
            )
            appender.reset()

        offset = appender.graph_ingest_offset
        if st.st_size <= offset:
            return

        try:
            with open(appender.file_path, "rb") as fh:
                fh.seek(offset)
                data = fh.read()
        except OSError:
            return

        last_nl = data.rfind(b"\n")
        if last_nl == -1:
            return  # only a partial trailing line available — wait for more

        complete = data[:last_nl + 1]
        lines = complete.splitlines()
        if not lines:
            return
        new_offset = offset + last_nl + 1

        try:
            appender.feed_lines(lines, new_byte_offset=new_offset)
        except Exception:
            logger.exception(
                "session_monitor: graph appender feed failed for source %s",
                appender.source_id,
            )

    async def _graph_appender_tick(self, tmux_name: str, jsonl_path: Path) -> None:
        """W3: feed the session's GraphAppender with any bytes beyond its
        own ``graph_ingest_offset``. The single integration point for tail-
        primary ingest — called after every viewer tail read (IN_MODIFY,
        rollover re-watch); self-heals by lazily (re)building the appender
        when none is registered yet for this ``tmux_name`` or the tracked
        ``file_path`` no longer matches (rollover), so no separate wiring
        is needed for dashboard-restart or retry-sweep-created sources —
        they pick up an appender on their next tail event.

        Behind the ``ingest.tail_appender`` flag. Best-effort: any failure
        here must never affect the live viewer's own tailing.
        """
        try:
            from tools.dashboard import feature_flags
            if not feature_flags.is_enabled("ingest.tail_appender"):
                return
        except Exception:
            return

        appender = self._graph_appenders.get(tmux_name)
        if appender is None or appender.file_path != jsonl_path:
            appender = await asyncio.to_thread(self._build_graph_appender, tmux_name, jsonl_path)
            if appender is None:
                return
            self._graph_appenders[tmux_name] = appender

        await asyncio.to_thread(self._feed_graph_appender, appender)

    async def _final_graph_catchup(self, tmux_name: str, jsonl_path: str) -> None:
        """W3 death path: final gap check + summary refresh, in-process —
        replaces the blocking ``subprocess.run(["graph", "ingest-session",
        ...])`` call this used to make (up to 30s, synchronously, on the
        event loop thread).

        If a GraphAppender was already active for this session, one last
        catch-up tick covers any trailing bytes and the appender is
        dropped (the session is dead; no more ticks needed). Otherwise
        (flag off, or the session died before its first tail event) falls
        back to an in-process full-reparse ingest — still no subprocess,
        unlike the code this replaces.
        """
        try:
            appender = self._graph_appenders.pop(tmux_name, None)
            if appender is not None:
                await asyncio.to_thread(self._feed_graph_appender, appender)
                return
            await asyncio.to_thread(self._final_full_reparse, jsonl_path)
        except Exception:
            logger.exception(
                "session_monitor: final graph catch-up failed for %s", tmux_name,
            )

    @staticmethod
    def _final_full_reparse(jsonl_path: str) -> None:
        """Sync fallback for the death path when no GraphAppender was
        active. Same in-process ingest the sweep already uses — no
        subprocess, unlike the code this replaces."""
        from tools.graph.ingest import _open_db_for_session, ingest_session_file

        path = Path(jsonl_path)
        db = _open_db_for_session(path)
        if db is None:
            return
        try:
            ingest_session_file(db, path)
        finally:
            db.close()

    def _remove_watches(self, tmux_name: str) -> None:
        """Remove all inotify watches for a session, and retire its
        rollout-ingestion state (B5): FileTracks, inode-watch
        subscriptions, dir epochs, and the drain gate. A running owner is
        cancelled and its gate is released under the A5 contract (the
        shielded worker finishes, the release callback sees ``retired``
        and drops the gate) — post-death events must dispatch nothing.
        """
        # Rollout-ingestion teardown first — track/inode maps exist even
        # when inotify itself is unavailable.
        for key in [k for k in self._tracks if k[0] == tmux_name]:
            self._release_track_watch(self._tracks[key])
            del self._tracks[key]
        for key in [k for k in self._dir_epochs if k[0] == tmux_name]:
            del self._dir_epochs[key]
        gate = self._session_gates.get(tmux_name)
        if gate is not None:
            gate.retired = True
            gate.dirty = False
            gate.needs_drain = False
            gate.pending_link = None
            gate.pending_expected = None
            gate.publish_registry_after_drain = False
            task = gate.owner_task
            if task is not None and not task.done():
                task.cancel()   # release arrives via the B4 done-callbacks
            if not gate.busy:
                self._session_gates.pop(tmux_name, None)
        if not self._inotify:
            return
        ts = self._tail_states.get(tmux_name)
        if not ts:
            return
        # File watch (refcounted inode subscription — R2)
        self._unsubscribe_watch(("session", tmux_name))
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
        *,
        source_path: Path | None = None,
        span: dict | None = None,
        observed_model: str | None = None,
        model_changed: bool = False,
    ) -> None:
        """Dedup, enrich, and broadcast parsed entries from a session tail read.

        ``source_path`` (D2, auto-suvcp) carries the batch's explicit file
        identity for publications that do NOT come from the row's linked
        file (ordered-handover predecessor segments) — the batch never
        assumes the DB row links the file being published.

        ``span`` (auto-16g9t) is the batch's raw byte range
        ``{file, from, to}`` — the client's gap detector advances its
        committed high-water from contiguous spans, never from entry
        counts, so server-side dedup can never fake progress.
        """
        if not new_entries and not model_changed:
            return
        # (The legacy clear-startup_state-on-first-assistant-turn hook lived
        # here. The lifecycle worker now writes `running` (startup_state
        # NULL) itself when the launch completes — the tailer no longer
        # writes lifecycle state.)
        # Dedup queued messages — tracker persists across unrelated entries
        # (assistant turns, tool_result) because the duplicate typically
        # arrives AFTER the agent's assistant response, not immediately.
        new_entries, ts.last_enqueue_content, dropped = dedup_queued_entries(
            new_entries, ts.last_enqueue_content,
        )
        if dropped:
            ts.enqueue_dedup_count += dropped
            ts.last_enqueue_dedup_ts = time.time()

        harness = resolve_harness_for_session_row(row)
        session_dir = None
        batch_path = source_path or (
            Path(row["jsonl_path"]) if row.get("jsonl_path") else None
        )
        if batch_path is not None:
            session_dir = batch_path.parent / batch_path.stem
        if ts.postprocess_state is None:
            ts.postprocess_state = harness.new_postprocess_state()
        new_entries = harness.postprocess_entries(
            new_entries,
            session_dir=session_dir,
            state=ts.postprocess_state,
        )
        session_harness_mod.finalize_entry_refs(new_entries)

        # Stamp trusted session identity onto entry types whose serve URLs
        # depend on it. ``viewer_attachment`` payloads come from agent
        # tool_result content, so any ``session`` they claim is untrusted —
        # the monitor knows authoritatively which tmux session this batch
        # came from and is the only correct source. (See
        # ``_upconvert_viewer_attachment`` docstring for the threat model.)
        for entry in new_entries:
            if entry.get("type") == "viewer_attachment":
                entry["session"] = tmux_name

        activity_state = _apply_activity_entries(ts, new_entries)
        update_activity_state(tmux_name, activity_state)

        # Singleton OperatorActivity row that backs ``OperatorActivity.is_idle()``.
        # Fire-and-forget on a thread so the SSE broadcast below is never
        # gated on graph-DB I/O. Only one write per batch is needed; the
        # singleton row only cares about the most recent operator-input
        # timestamp.
        for entry in new_entries:
            if entry.get("type") in _OPERATOR_INPUT_TYPES:
                ts_iso = entry.get("timestamp", "") or ""
                if ts_iso:
                    asyncio.create_task(
                        asyncio.to_thread(
                            _record_operator_input, ts_iso,
                        ),
                        name="operator-activity",
                    )
                    break

        self._enrich_agent_entries(row, ts, new_entries)
        # Warm-up rehydrates task-tracker state from history even when the
        # enricher is not wired. Run it unconditionally so overlays survive
        # restarts on minimal harness configurations.
        try:
            await self._warm_task_tracker_if_needed(tmux_name, row, ts)
        except Exception:
            logger.exception("session_monitor: warm-up failed for %s", tmux_name)
        if self._entry_enricher:
            try:
                self._entry_enricher(tmux_name, new_entries)
                await self._persist_todos_if_changed(tmux_name, ts)
            except Exception:
                logger.exception("session_monitor: entry_enricher failed for %s", tmux_name)
        if (new_entries or model_changed) and self._event_bus:
            # auto-rsvzk: match user-typed echoes back to the original
            # client_id stashed by api_session_send. The optimistic
            # frontend uses the round-tripped id to dedup its locally-
            # rendered "sending" entry (it promotes to "confirmed"
            # instead of appending a duplicate row). User-turns that
            # were never sent through the API path (typed directly into
            # tmux) carry no client_id and the frontend renders them
            # normally.
            try:
                from tools.dashboard import pending_outbound
                for entry in new_entries:
                    if entry.get("type") != "user":
                        continue
                    if entry.get("client_id"):
                        continue
                    # Extract the operator's typed text. Different
                    # harnesses store it differently; try the common
                    # shapes before giving up.
                    text = entry.get("text") or entry.get("content") or ""
                    if isinstance(text, list):
                        text = "".join(
                            block.get("text", "")
                            for block in text
                            if isinstance(block, dict)
                        )
                    text = text.strip() if isinstance(text, str) else ""
                    if not text:
                        continue
                    matched = pending_outbound.match_and_consume(
                        tmux_name, text,
                    )
                    if matched:
                        entry["client_id"] = matched
            except Exception:
                logger.exception(
                    "session_monitor: pending_outbound match failed for %s",
                    tmux_name,
                )

            ts.broadcast_seq += 1
            ts.last_broadcast_ts = time.time()
            updated = get_session(tmux_name)
            await self._event_bus.broadcast(
                "session:messages",
                {
                    "session_id": tmux_name,
                    "entries": new_entries,
                    "is_live": (
                        derive_lifecycle_state(updated) not in ("ENDED", "FAILED")
                        if updated else True
                    ),
                    "seq": ts.broadcast_seq,
                    "context_tokens": updated["context_tokens"] if updated else 0,
                    # This tail window may carry a newer model than the DB row
                    # because broadcast intentionally precedes persistence.
                    "model": observed_model or (
                        updated.get("model") if updated else row.get("model")
                    ),
                    "size_bytes": (
                        batch_path.stat().st_size
                        if batch_path is not None and batch_path.exists() else 0
                    ),
                    "activity_state": activity_state,
                    "pending_tool_ids": sorted(ts.pending_tool_ids),
                    # auto-16g9t: raw byte range this batch was parsed from
                    # — the client's span-contiguity gap detector input.
                    **({"span": span} if span is not None else {}),
                },
                dedup=False,
            )

        # ── auto-eerfx screen-state poller ────────────────────────────

    SCREEN_POLL_INTERVAL_S = 2.0
    # Grace before promoting harness_starting → composer_ready WITHOUT a
    # screen-read confirmation. Container sessions run the harness inside
    # docker; the host tmux pane bridges via attach and `capture-pane`
    # frequently returns a blank/desynced grid, so screen-reading can
    # never confirm composer_ready for them. Once setup_phase is complete
    # the entrypoint has already exec'd the harness and it accepts input
    # within a couple seconds — so after this grace we promote anyway.
    # Without it, container cards stick on "Booting" forever (the exact
    # symptom: session online + accepting input while the tile shows
    # in-progress).
    HARNESS_READY_GRACE_S = 12.0
    # Self-repair trigger: a session stuck in the poll window this long
    # WITHOUT reaching composer_ready means the harness is up but its screen
    # won't parse (e.g. a trust/login prompt whose wording drifted past our
    # regex). Generous vs HARNESS_READY_GRACE_S so a merely-slow setup never
    # files a false ticket — only a genuinely stuck startup does.
    SELF_REPAIR_TIMEOUT_S = 120.0

    async def _screen_poll_loop(self) -> None:
        """auto-eerfx: per-2s tmux capture-pane → harness.read_screen_state.

        Drives harness_phase progression from first_turn_written →
        composer_ready, detects + auto-confirms the Claude trust dialog,
        surfaces planning mode + blocking modals to the registry SSE.

        Targets sessions whose harness_phase is in
        {harness_starting, first_turn_written} — past container ready
        but not yet known-good-input. Sessions in composer_ready or any
        failed state are skipped (no transition expected).
        """
        import subprocess as _subprocess
        from tools.dashboard.tmux_send import tmux_send_keys
        while True:
            try:
                await asyncio.sleep(self.SCREEN_POLL_INTERVAL_S)
                rows = get_live_sessions()
                # Drop armed entries whose session left the live set (died
                # mid-launch, killed, failed) so the set can't leak.
                live_names = {r["tmux_name"] for r in rows}
                self._screen_poll_armed &= live_names
                for row in rows:
                    tmux_name = row["tmux_name"]
                    # Watch exactly the sessions armed for composer
                    # detection. Arming happens at the launch entrypoints
                    # (arm_startup_state); the poller disarms itself at
                    # composer_ready or when the session leaves the live
                    # set. startup_state is deliberately NOT a watch
                    # predicate — the poller reads panes and writes
                    # harness_state; the lifecycle worker owns state.
                    startup_state = row.get("startup_state")
                    armed = tmux_name in self._screen_poll_armed
                    if not armed:
                        continue
                    harness = resolve_harness_for_session_row(row)
                    try:
                        result = await asyncio.to_thread(
                            _subprocess.run,
                            ["tmux", "capture-pane", "-p", "-t", tmux_name],
                            capture_output=True, text=True, timeout=5,
                        )
                    except Exception:
                        logger.debug(
                            "screen_poll: capture-pane failed for %s",
                            tmux_name, exc_info=True,
                        )
                        continue
                    if result.returncode != 0:
                        # Pane gone (session dead). Liveness sweep handles
                        # the dead transition; we just skip.
                        continue
                    pane_text = result.stdout or ""

                    current_state_str = row.get("harness_state") or "{}"
                    try:
                        current_state = json.loads(current_state_str)
                        if not isinstance(current_state, dict):
                            current_state = {}
                    except (json.JSONDecodeError, TypeError):
                        current_state = {}

                    try:
                        new_state, keystrokes = harness.read_screen_state(
                            pane_text, current_state,
                        )
                    except Exception:
                        logger.exception(
                            "screen_poll: read_screen_state failed for %s",
                            tmux_name,
                        )
                        continue

                    # Inject any keystrokes the adapter asked for (e.g.
                    # the trust-dialog confirm). Fire and forget — the
                    # next poll observes the result.
                    if keystrokes:
                        try:
                            await tmux_send_keys(tmux_name, keystrokes)
                        except Exception:
                            logger.exception(
                                "screen_poll: tmux_send_keys failed for %s",
                                tmux_name,
                            )

                    # Persist the state delta. The screen adapter signals
                    # composer_ready and trust-confirm in the new_state
                    # dict; the lifecycle worker reads them from
                    # harness_state and mirrors them onto the chip.
                    changed = (new_state != current_state)
                    next_state: str | None = None
                    if new_state.get("confirming_trust_prompt") and startup_state == "harness_starting":
                        next_state = "confirming_trust"
                    elif bool(new_state.get("composer_ready")) and startup_state != "composer_ready":
                        next_state = "composer_ready"
                    # Grace fallback for sessions whose pane can't be
                    # screen-read (docker-bridged container panes return a
                    # blank capture). Once setup is complete the harness is
                    # exec'd and accepting input within seconds; if the
                    # screen-read hasn't confirmed composer_ready within
                    # HARNESS_READY_GRACE_S of first seeing the session in
                    # this state, promote anyway so the card clears.
                    # Grace applies once the composer is expected imminently
                    # (chip in the harness window, or already cleared). It
                    # must NOT apply while the chip still shows the early
                    # states (requesting…setup_running): the pane can be
                    # blank for minutes legitimately during worktree prep or
                    # setup and a force-promote would lie.
                    if (
                        next_state is None
                        and startup_state in ("harness_starting", None)
                    ):
                        first = self._harness_ready_grace.setdefault(
                            tmux_name, time.monotonic()
                        )
                        if time.monotonic() - first >= self.HARNESS_READY_GRACE_S:
                            next_state = "composer_ready"
                            if not new_state.get("composer_ready"):
                                new_state["composer_ready"] = True
                                changed = True
                    else:
                        self._harness_ready_grace.pop(tmux_name, None)

                    # ── self-repair: stuck-in-startup detector miss ──
                    # A session sitting in the poll window past the timeout
                    # without reaching composer_ready means the harness is up
                    # but we can't parse its screen (e.g. a trust/login prompt
                    # whose wording drifted past our regex → composer never
                    # shows → stuck). Capture the pane as evidence and file a
                    # ticket the librarian can fix. Filing is deduped + rate-
                    # limited inside report_detector_miss; the per-session
                    # guard files once and is cleared when the session
                    # advances.
                    if next_state == "composer_ready" or new_state.get("composer_ready"):
                        self._screen_stuck_since.pop(tmux_name, None)
                        self._self_repair_filed.discard(tmux_name)
                        # Composer reached — this launch no longer needs the
                        # armed watch; harness_state.composer_ready is now
                        # the durable signal downstream gates read.
                        self._screen_poll_armed.discard(tmux_name)
                    elif startup_state in ("harness_starting", "confirming_trust", None):
                        # Stuck-tracking only where the composer is expected
                        # imminently — an armed session still in the early
                        # launch states (requesting…setup_running) can sit on
                        # a blank/booting pane for minutes legitimately and
                        # must not file detector-miss tickets.
                        stuck_since = self._screen_stuck_since.setdefault(
                            tmux_name, time.monotonic()
                        )
                        stuck_for = time.monotonic() - stuck_since
                        if (
                            stuck_for >= self.SELF_REPAIR_TIMEOUT_S
                            and tmux_name not in self._self_repair_filed
                            and pane_text.strip()
                        ):
                            self._self_repair_filed.add(tmux_name)
                            try:
                                from tools.dashboard import self_repair
                                await self_repair.report_detector_miss(
                                    f"{harness.name}_screen_state",
                                    expected="composer_ready",
                                    evidence=pane_text,
                                    evidence_kind="screen_capture",
                                    code_ref=(
                                        "tools/dashboard/session_harness.py"
                                        ":_claude_read_screen_state"
                                    ),
                                    context={
                                        "session": tmux_name,
                                        "harness": harness.name,
                                        "startup_state": startup_state,
                                        "stuck_for_s": round(stuck_for),
                                    },
                                )
                            except Exception:
                                logger.exception(
                                    "screen_poll: self_repair report failed for %s",
                                    tmux_name,
                                )

                    if changed or next_state is not None:
                        if changed:
                            try:
                                update_tail_state(tmux_name, harness_state=json.dumps(new_state))
                            except Exception:
                                logger.exception(
                                    "screen_poll: update_tail_state failed for %s",
                                    tmux_name,
                                )
                                continue
                        # The poller is a signal producer only: it persists
                        # harness_state and the lifecycle worker (the single
                        # startup_state writer) mirrors it into chip states.
                        if next_state == "composer_ready":
                            logger.info(
                                "phase-trace: composer_ready  tmux=%s  ts_ms=%d",
                                tmux_name, int(time.time() * 1000),
                            )
                        if self._event_bus:
                            try:
                                await self._event_bus.broadcast(
                                    "session:registry", self.get_registry(),
                                )
                            except Exception:
                                logger.exception(
                                    "screen_poll: broadcast failed for %s",
                                    tmux_name,
                                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("screen_poll: loop iteration failed")

    # ── inotify tailer ────────────────────────────────────────────

    async def _inotify_tailer_loop(self) -> None:
        """inotify-driven tailer — instant delivery on IN_MODIFY, 1s fallback tick.

        MODIFY on a linked file requests a drain through the per-session
        gate (auto-suvcp Rule 5) — never a direct read, so at most one
        owner reads at a time and a contended signal transfers instead of
        racing. MODIFY on a CHARACTERIZING track re-runs classification
        (Rule 2); dispatch validates (wd, epoch) jointly (Rule 8/D3).
        """
        while True:
            try:
                # Block up to 1 s waiting for inotify events
                events = await asyncio.to_thread(self._inotify.read, timeout=1000)

                # Collect sessions whose JSONLs were modified
                modified: set[str] = set()
                for event in events:
                    if event.mask & _iflags.MODIFY:
                        modified |= self._dispatch_modify_wd(event.wd)
                    if event.mask & _iflags.CREATE:
                        await self._handle_in_create(event)
                    if event.mask & _iflags.IGNORED:
                        # Kernel auto-removed the watch (file deleted/moved).
                        self._dispatch_ignored_wd(event.wd)

                # Request a drain for each modified session — the gate
                # serializes owners; the drain re-reads the row's current
                # path (N4), so a stale wd routing is harmless.
                for tmux_name in modified:
                    self.request_drain(tmux_name)

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

        # Compaction recovery — same path, new inode. If the type-specific
        # dispatch above didn't end up reattaching the watch (e.g.
        # _handle_host_create returns early when parentUuid is null, which is
        # exactly the shape of a compacted JSONL's first line), fall back to
        # re-registering the IN_MODIFY watch on the new inode at the tracked
        # path so the session is no longer blind to writes after compaction.
        for tmux_name in list(sessions):
            ts = self._tail_states.get(tmux_name)
            if ts is None or ts.watch_descriptor is not None:
                continue
            row = get_session(tmux_name)
            if not row:
                continue
            tracked_path = row.get("jsonl_path")
            if not tracked_path:
                continue
            if Path(tracked_path) != new_file:
                continue
            await self._rewatch_replaced_jsonl(
                tmux_name, new_file, source="IN_CREATE",
            )

    async def _rewatch_replaced_jsonl(
        self, tmux_name: str, new_path: Path, *, source: str,
    ) -> None:
        """Re-register an IN_MODIFY watch for a JSONL replaced at the same path.

        Triggered when the file's inode changes (e.g. compaction unlinks the
        old file and creates a new one at the same path). Resets all state
        derived from the prior file's content, then tails the new file so
        post-replacement entries broadcast immediately.

        ``source`` is included in the INFO log so an operator can tell which
        recovery path fired (``IN_CREATE`` for the inotify-event path,
        ``reconciliation`` for the periodic backstop).
        """
        ts = self._tail_states.get(tmux_name)
        if ts is None:
            return
        old_inode = ts.last_known_inode
        try:
            new_inode = new_path.stat().st_ino
        except OSError as exc:
            logger.warning(
                "session_monitor: rewatch stat failed for %s: %s", tmux_name, exc,
            )
            return

        if not self._inotify:
            return
        # R2: re-subscribe through the inode-owned structure — this drops
        # the session's stale subscription (the kernel already removed the
        # old inode's watch) and attaches to the replacement inode.
        self._add_file_watch(tmux_name, str(new_path))
        if ts.watch_descriptor is None:
            logger.warning(
                "session_monitor: rewatch add_watch failed for %s", tmux_name,
            )
            return
        ts.last_known_inode = new_inode
        # State derived from the old file's stream is no longer valid.
        ts.recent_processed.clear()
        ts.pending_tool_ids.clear()
        ts.completed_tool_ids.clear()

        # Same path, new inode = a new generation (auto-suvcp Rule 6):
        # stamp it in the same UPDATE that resets the cursor so a stale
        # drain of the old inode CAS-fails instead of acking bytes the new
        # file never delivered.
        row = get_session(tmux_name)
        if row and row.get("jsonl_path") == str(new_path):
            from tools.dashboard.dao.dashboard_db import (
                next_link_seq,
                update_jsonl_link,
            )
            try:
                st_dev = new_path.stat().st_dev
            except OSError:
                st_dev = 0
            seq = next_link_seq(tmux_name)
            update_jsonl_link(
                tmux_name,
                session_uuid=new_path.stem,
                jsonl_path=str(new_path),
                generation=f"{st_dev}:{new_inode}:{seq}",
                file_offset=0,
            )
            track = self._get_track(tmux_name, str(new_path))
            track.generation = (st_dev, new_inode)
            track.state = TRACK_STREAMING
            track.published_up_to = 0
            track.checked_size = None
        else:
            # File offset must still reset so the new file reads from byte 0.
            update_tail_state(tmux_name, file_offset=0)

        logger.info(
            "session_monitor: rewatched %s %s → %s after %s",
            tmux_name, old_inode, new_inode, source,
        )

        # Drain the new content through the session gate so the first
        # post-replacement entries broadcast without waiting for the next
        # IN_MODIFY event — and without a second reader racing the owner.
        # (W3: new generation → _graph_appender_tick rebuilds the appender
        # against the new file inside the drain's publish step.)
        self.request_drain(tmux_name)

    async def _handle_container_create(
        self, tmux_name: str, row: dict, new_file: Path,
    ) -> None:
        """Handle IN_CREATE in an isolated container directory.

        One entry point (auto-suvcp Rule 1): ``observe_rollout`` decides
        birth trust (empty registration snapshot + first CREATE of the
        epoch + CAS against NULL) or characterization. An incomplete header
        is never abandoned — the file's track retains the watch and
        responsibility until classification resolves or the time-driven
        deadline quarantines it (the pre-fix retry-then-abandon here is the
        exact responsibility gap of incident auto-0807-225218).
        """
        _ = row  # the observation path re-reads the row (N4 discipline)
        self.observe_rollout(
            tmux_name, new_file, source="IN_CREATE", create_event=True,
        )

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
        from tools.dashboard.dao.dashboard_db import (
            link_and_enrich,
            next_link_seq,
        )
        # B6 atomicity: generation + reset cursor land in the SAME UPDATE
        # as the link — no window where the link points at the new file
        # while generation/cursor still describe the old one.
        generation = None
        try:
            st = new_file.stat()
            seq = next_link_seq(owner)
            generation = f"{st.st_dev}:{st.st_ino}:{seq}"
        except OSError:
            st = None
        link_and_enrich(
            owner,
            session_uuid=new_uuid,
            jsonl_path=str(new_file),
            project=new_file.parent.name,
            generation=generation,
            file_offset=0,
        )
        logger.info(
            "session_monitor: IN_CREATE host rollover %s → %s (predecessor %s)",
            owner, new_file.name, predecessor_uuid,
        )

        # Swap IN_MODIFY watch to new file
        self._add_file_watch(owner, str(new_file))

        if st is not None:
            track = self._get_track(owner, str(new_file))
            track.generation = (st.st_dev, st.st_ino)
            track.state = TRACK_STREAMING

        # Reset ephemeral tail state, preserving resolution_dir
        ts = self._tail_states.get(owner)
        old_resolution_dir = ts.resolution_dir if ts else None
        self._tail_states[owner] = _TailState(resolution_dir=old_resolution_dir)

        # Catch up any bytes already present in the new file (the attach-
        # without-catch-up gap is CalStartupStall's second leg).
        self.request_drain(owner)

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
            " WHERE state NOT IN ('ENDED','FAILED') AND session_uuids LIKE ?",
            (f"%{uuid}%",),
        ).fetchone()
        return row["tmux_name"] if row else None

    async def _warm_task_tracker_if_needed(
        self, tmux_name: str, row: dict, ts: _TailState,
    ) -> None:
        """Replay prior JSONL entries through the enricher once per session.

        Guarantees post-restart Task* tiles resolve against a complete
        taskId→subject map. No broadcast; state only.
        """
        if ts.task_tracker_warmed:
            return
        ts.task_tracker_warmed = True  # set first — retry loops would double-warm
        jsonl_path_str = row.get("jsonl_path")
        if not jsonl_path_str:
            return
        try:
            jsonl_path = Path(jsonl_path_str)
            if not jsonl_path.exists():
                return
            # Current file_offset marks the boundary; everything before it is history.
            file_offset = row.get("file_offset", 0) or 0
            if file_offset <= 0:
                return
            with open(jsonl_path, "rb") as fh:
                data = fh.read(file_offset)
        except OSError:
            return
        reader = session_harness_mod.resolve_harness_for_path(jsonl_path)
        prior: list = []
        for raw_line in data.splitlines():
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            parsed = reader.parse_line(line)
            if parsed is None:
                continue
            if isinstance(parsed, list):
                prior.extend(parsed)
            else:
                prior.append(parsed)
        if self._entry_enricher is None:
            return
        if prior:
            try:
                self._entry_enricher(tmux_name, prior)
            except Exception:
                logger.exception("session_monitor: task tracker warm-up failed for %s", tmux_name)
        # Persist the post-warm-up snapshot so restarts repopulate the DB even
        # when no fresh TaskUpdate arrives. Runs even when `prior` is empty:
        # the tracker's snapshot may legitimately be empty, but we still want
        # to flush the sentinel to the DB on first read after a restart.
        await self._persist_todos_if_changed(tmux_name, ts)

    async def _persist_todos_if_changed(self, tmux_name: str, ts: _TailState) -> None:
        """Diff the tracker's snapshot against the last persisted value and
        write to ``tmux_sessions.todos`` only when it actually changed.

        Called at two points:
          - after live enrichment in the tailer loop
          - after warm-up replay (so a cold dashboard repopulates the DB
            without needing a new TaskUpdate event)

        The per-session ``last_todos_json`` cache on ``_TailState`` means we
        skip the DB write for no-op updates (e.g. repeated status transitions
        with no net change) but still flush the first post-warm-up snapshot.
        """
        if self._todo_snapshot is None:
            return
        try:
            snapshot = self._todo_snapshot(tmux_name)
        except Exception:
            logger.exception("session_monitor: todo snapshot failed for %s", tmux_name)
            return
        encoded = json.dumps(snapshot)
        if encoded == ts.last_todos_json:
            return
        try:
            update_todos(tmux_name, snapshot)
        except Exception:
            logger.exception("session_monitor: update_todos failed for %s", tmux_name)
            return
        ts.last_todos_json = encoded
        if self._event_bus:
            await self._broadcast_registry()

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

    # ── Liveness Checker ──────────────────────────────────────────

    _COOLDOWN_SECONDS = 30.0
    _ORPHAN_PRUNE_INTERVAL = 600.0  # seconds between orphan worktree prunes
    # startup_state values that mean "still booting" — a container session in
    # any of these may not yet have a live host tmux (a resume's old tmux is
    # gone; a fresh launch hasn't spawned it), so the tmux-liveness check below
    # must not treat the miss as death. setup_running + confirming_trust were
    # MISSING here and got sessions reaped ~12s into setup (auto-0709-092918,
    # 2026-07-09) — added as a STOPGAP ahead of the single-state FSM
    # consolidation. composer_ready / awaiting_first_response stay excluded: by
    # then tmux exists and a real miss is a real death.
    # A session is only reaped after this many CONSECUTIVE authoritative
    # "no such session" probe results (~10s apart). One authoritative miss
    # can race a tmux server restart; probe FAILURES (None) never count —
    # they say nothing about the session (2026-07-08 18:1x incident: a
    # fork-EAGAIN spike failed every probe in one tick and a fail-dead
    # probe reaped the entire live fleet).
    _LIVENESS_MISS_THRESHOLD = 2

    async def _sweep_tmux_liveness(self, sessions: list[dict], now: float) -> bool:
        """One liveness pass over the live rows. Returns True if any died.

        Extracted from the loop so the reap decision is testable: the
        fail-safe rules (probe-failure ≠ dead; N consecutive confirmed
        misses required) are the contract this method owns.
        """
        changed = False
        for row in sessions:
            tmux_name = row["tmux_name"]
            # Dispatch + librarian + agentic sessions are owned by
            # their respective dispatchers/watchers (see
            # agents/dispatcher.py — poll_and_collect for dispatch,
            # poll_and_collect_agentic for kind='agentic'). They
            # never had a tmux session, so has-session always
            # returns False and polling would mark them dead within
            # 10s — and the cleanup_session_worktrees that follows
            # would yank /workspace/repo out from under a still-running
            # container. Their death signal is an explicit POST to
            # /api/monitor/deregister or container-exit collection,
            # not tmux polling.
            if row.get("type") in ("dispatch", "librarian", "agentic"):
                continue
            # The reaper owns exactly ONE decision: is an ACTIVE
            # session's tmux gone. Everything else belongs to the FSM:
            #
            # - LAUNCHING: tmux legitimately does not exist for most of a
            #   launch, and the worker already fails a stuck step at its
            #   own budget (STEP_TIMEOUTS_S, enforced inside each blocking
            #   call). The reaper keeps a single belt for the exotic
            #   orphan case — the worker lost the job without the process
            #   dying — using THE SAME budget table plus a margin, from
            #   the last transition (the writer stamps last_activity on
            #   every one). One table, two consumers: a reaper grace that
            #   undercuts a worker deadline is unrepresentable. An
            #   orphaned launch becomes FAILED (retryable), not ENDED —
            #   nothing was ever running.
            # - STOPPING: the worker is mid-teardown; never double-fire.
            state = derive_lifecycle_state(row)
            if state == "STOPPING":
                continue
            if state == "LAUNCHING":
                phase = row.get("startup_state") or "requesting"
                budget = STEP_TIMEOUTS_S.get(phase, max(STEP_TIMEOUTS_S.values()))
                anchor = row.get("last_activity") or row.get("created_at") or 0
                if (now - anchor) <= budget + REAPER_BELT_MARGIN_S:
                    continue
                from tools.dashboard.session_lifecycle_worker import STATE_AUTHORITY
                if STATE_AUTHORITY.transition(
                    tmux_name,
                    "FAILED",
                    cause="reaper:launch-orphaned",
                    reason=(
                        f"launch orphaned: no transition for {int(now - anchor)}s "
                        f"in phase {phase} (budget {int(budget)}s + belt)"
                    ),
                    failed_phase=phase,
                ):
                    self._remove_watches(tmux_name)
                    self._tail_states.pop(tmux_name, None)
                    self._screen_poll_armed.discard(tmux_name)
                    changed = True
                    logger.warning(
                        "session_monitor: orphaned launch failed  %s (phase=%s)",
                        tmux_name, phase,
                    )
                continue
            if state != "ACTIVE":
                continue
            alive = await asyncio.to_thread(self._check_tmux, tmux_name)
            if alive is None:
                # Probe failure — liveness UNKNOWN. Fail-safe: neither a
                # miss nor a confirmation; try again next tick.
                continue
            if alive:
                self._liveness_misses.pop(tmux_name, None)
                continue
            misses = self._liveness_misses.get(tmux_name, 0) + 1
            self._liveness_misses[tmux_name] = misses
            if misses < self._LIVENESS_MISS_THRESHOLD:
                logger.info(
                    "session_monitor: tmux missing for %s (miss %d/%d) — "
                    "awaiting confirmation",
                    tmux_name, misses, self._LIVENESS_MISS_THRESHOLD,
                )
                continue
            self._liveness_misses.pop(tmux_name, None)
            self._remove_watches(tmux_name)
            # Clear pending_tool_ids before marking dead —
            # dead sessions cannot have running tools
            ts = self._tail_states.get(tmux_name)
            if ts:
                ts.pending_tool_ids.clear()
            mark_dead(tmux_name)
            # W3: final in-process graph catch-up (final state) —
            # replaces the blocking `graph ingest-session`
            # subprocess (up to 30s on the event loop thread)
            # this used to be.
            jsonl_path = row.get("jsonl_path")
            if jsonl_path:
                await self._final_graph_catchup(tmux_name, jsonl_path)
            self._tail_states.pop(tmux_name, None)
            # Deliberately NO worktree cleanup here: ending a session is a
            # state transition, never destruction. The tombstoned worktree
            # GC removes eligible dirs hours later (WORKTREE_GC_HORIZON_S)
            # with an execution-time state re-check.
            # One final disk measurement so the ended card keeps
            # a footprint; the session leaves the resource poll
            # set for good. Fire-and-forget: never blocks the sweep.
            from tools.dashboard.resource_monitor import (
                resource_monitor,
            )
            asyncio.create_task(
                resource_monitor.on_session_dead(tmux_name, row),
            )
            changed = True
            logger.info(
                "session_monitor: tmux dead  %s (type=%s, confirmed %d probes)",
                tmux_name, row["type"], self._LIVENESS_MISS_THRESHOLD,
            )
        return changed

    async def _liveness_loop(self) -> None:
        """Check tmux liveness for all sessions every 10s."""
        while True:
            try:
                sessions = get_live_sessions()
                now = time.time()
                changed = await self._sweep_tmux_liveness(sessions, now)

                # Clean up old dead sessions from tail states
                # (Dead sessions with _COOLDOWN expired get deleted from DB)
                conn = get_conn()
                expired = conn.execute(
                    "SELECT tmux_name FROM tmux_sessions"
                    " WHERE state IN ('ENDED','FAILED') AND last_activity IS NOT NULL"
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
                    if derive_lifecycle_state(row) in ("ENDED", "FAILED"):
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

                # Mission Control outstanding-questions nag — independent of
                # the general nag_enabled slot above (see coordinator_nag_state's
                # schema comment in mission_control_db.py for why: writing
                # this feature's message into a session's own general
                # check-in nag slot would silently clobber it). One bulk
                # query for the whole sweep, not one per session -- most
                # live sessions coordinate nothing in Mission Control.
                await self._check_mission_control_nag(sessions, now)

                # Dispatch pause nag — alert dispatch_nag subscribers when queue is stuck
                await self._check_dispatch_pause_nag(now)

                # Periodic tombstoned worktree GC — every 10 minutes scan
                # data/worktrees/ and remove dirs whose session has been
                # terminal past the horizon (or has no row at all), with
                # the preserve policy and an execution-time state re-check.
                if (now - self._last_orphan_prune) >= self._ORPHAN_PRUNE_INTERVAL:
                    await asyncio.to_thread(_worktree_gc_pass)
                    self._last_orphan_prune = now

            except Exception:
                logger.exception("session_monitor: liveness error")
            await asyncio.sleep(10)

    _PAUSE_NAG_INTERVAL = 15 * 60  # 15 minutes between dispatch-pause nags

    # Idle threshold before nagging a coordinator about outstanding Mission
    # Control questions. Deliberately short (60s, not the general nag
    # system's default 15 MINUTES) per the operator's explicit preference
    # ("1 is better if it doesn't false positive") -- flagged as needing
    # real-world observation to confirm 60s doesn't false-positive against
    # normal thinking/tool-call pauses before treating it as settled; it's
    # a threshold constant, cheap to retune, not a schema commitment.
    _MC_NAG_IDLE_THRESHOLD = 60

    # ── Periodic reconciliation (backstop) ───────────────────────

    async def _reconciliation_loop(self) -> None:
        """Every ``_reconciliation_interval_seconds``, scan pending sessions.

        Backstop for the case where both scan-on-watch-add and IN_CREATE
        miss the JSONL. Primary path remains the inotify event + the
        post-watch scan; this loop just makes the failure mode
        eventual-consistent instead of permanent.
        """
        while True:
            try:
                await asyncio.sleep(self._reconciliation_interval_seconds)
                await self.reconciliation_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("session_monitor: reconciliation loop error")

    async def reconciliation_tick(self) -> int:
        """Run one reconciliation pass. Returns count of newly-resolved sessions.

        Iterates over pending sessions (``jsonl_path IS NULL`` in DB and/or
        ``needs_resolution=True`` in the tail state), scans their
        ``resolution_dir`` for JSONLs, and promotes them via the shared
        ``_handle_jsonl_appeared`` path.

        Exposed as a coroutine so tests can drive reconciliation directly
        without waiting for the 5-minute loop.
        """
        resolved = 0
        tick_ok = True

        # Step 1: level-triggered observation backstop (auto-suvcp Rule 7,
        # finding N3): scan EVERY live session's resolution dir and observe
        # ANY rollout — NOT just rows with jsonl_path IS NULL. The old
        # pending-only gate was itself a responsibility gap: a restart
        # between a rollover's CREATE and its link left a linked row and a
        # successor file no mechanism ever observed (bead auto-ok297).
        # Terminal tracks are a stat-only tombstone compare (D6); the row's
        # own linked path re-observes as a persisted re-attach (N3
        # refinement). Isolated (W6) so a bug here can't prevent step 3's
        # graph_source_id backfill from running.
        try:
            now = time.time()
            for row in get_live_sessions():
                tmux_name = row["tmux_name"]
                res_dir = row.get("resolution_dir") or (
                    str(Path(row["jsonl_path"]).parent)
                    if row.get("jsonl_path") else None
                )
                if not res_dir:
                    continue
                if tmux_name not in self._tail_states:
                    self._tail_states[tmux_name] = _TailState(
                        needs_resolution=not row.get("jsonl_path"),
                        resolution_dir=Path(res_dir),
                    )
                was_linked = bool(row.get("jsonl_path"))
                try:
                    dp = Path(res_dir)
                    if dp.is_dir():
                        for jsonl in sorted(dp.rglob("*.jsonl")):
                            if "subagents" in jsonl.parts:
                                continue
                            self.observe_rollout(
                                tmux_name, jsonl, source="reconciliation",
                            )
                except OSError as exc:
                    logger.warning(
                        "session_monitor: reconciliation scan failed for %s: %s",
                        tmux_name, exc,
                    )
                # Characterization rechecks + TIME-driven deadlines (T110:
                # a deadline evaluated only inside on_file_event never
                # fires when no more events come). The still-CHARACTERIZING
                # check and the quarantine are an await-free pair.
                for track in [
                    t for t in self._tracks.values()
                    if t.tmux_name == tmux_name
                    and t.state == TRACK_CHARACTERIZING
                ]:
                    self._classify_and_step(track)
                    if (
                        track.state == TRACK_CHARACTERIZING
                        and track.characterize_deadline is not None
                        and now >= track.characterize_deadline
                    ):
                        self._quarantine_track(track)
                # Handover pump (Rule 4 WF backstop) + B2 loop-start pump.
                gate = self._session_gates.get(tmux_name)
                if gate is not None and (
                    gate.pending_link is not None or gate.needs_drain
                ):
                    self.request_drain(tmux_name)
                after = get_session(tmux_name)
                if not was_linked and after and after.get("jsonl_path"):
                    resolved += 1
                    ts_obj = self._tail_states.get(tmux_name)
                    if ts_obj is not None:
                        ts_obj.full_rescan_count += 1
                        ts_obj.last_full_rescan_ts = time.time()
                    logger.info(
                        "session_monitor: reconciliation resolved %s → %s",
                        tmux_name, after["jsonl_path"],
                    )
        except Exception:
            tick_ok = False
            logger.exception("session_monitor: reconciliation pending-resolution step failed")

        # Step 2: inode safety net — backstop for the case where IN_CREATE was
        # never delivered (kernel queue overflow, IN_IGNORED arriving without
        # a subsequent IN_CREATE we noticed, etc.). For every tracked session
        # whose stored inode disagrees with the file's current inode, run the
        # rewatch sequence. Isolated (W6) — same reasoning as step 1.
        try:
            for tmux_name, ts in list(self._tail_states.items()):
                row = get_session(tmux_name)
                if not row:
                    continue
                jsonl_path_str = row.get("jsonl_path")
                if not jsonl_path_str:
                    continue
                jsonl_path = Path(jsonl_path_str)
                try:
                    current_inode = jsonl_path.stat().st_ino
                except OSError:
                    continue
                if ts.last_known_inode == 0:
                    # Watch never ran (e.g. inotify unavailable, or stat failed
                    # at add-watch time). Don't treat 0 → real-inode as a
                    # replacement; just record the current value.
                    ts.last_known_inode = current_inode
                    continue
                if current_inode == ts.last_known_inode:
                    continue
                await self._rewatch_replaced_jsonl(
                    tmux_name, jsonl_path, source="reconciliation",
                )
        except Exception:
            tick_ok = False
            logger.exception("session_monitor: reconciliation inode-safety-net step failed")

        # Step 2.5: eager-source retry sweep (W2). Catches sessions whose
        # eager creation didn't happen at discovery time (org unresolved
        # then, flag toggled on since, transient failure). Isolated same as
        # steps 1/2 — must not block step 3's reconciler.
        try:
            eager_created = await asyncio.to_thread(self._eager_create_missing_sources)
            if eager_created:
                logger.info(
                    "session_monitor: eager-source retry sweep created %d source(s)",
                    eager_created,
                )
        except Exception:
            tick_ok = False
            logger.exception("session_monitor: eager-source retry sweep failed")

        # Step 3: graph_source_id reconciler (auto-4jpa8). Backfill empty IDs
        # and repair drifted ones every tick. Idempotent: rows whose stored ID
        # already resolves are left alone, and rows whose JSONL hasn't been
        # ingested yet stay empty until the next pass. Must run even when
        # steps 1/2 raised — see isolation note above.
        try:
            from tools.dashboard.dao.dashboard_db import reconcile_graph_source_ids
            repaired = await asyncio.to_thread(reconcile_graph_source_ids)
            if repaired:
                logger.info(
                    "session_monitor: reconciled graph_source_id for %d session(s)",
                    repaired,
                )
        except Exception:
            tick_ok = False
            logger.exception("session_monitor: graph_source_id reconcile failed")

        self._record_reconcile_outcome(tick_ok)

        if resolved:
            await self._broadcast_registry()
        return resolved

    # Minutes a reconciliation failure streak must persist before it counts
    # as "degraded" for health surfacing (W6). Default 5, per sprint plan §5.
    _DEGRADED_THRESHOLD_SECONDS: float = 5 * 60

    def _record_reconcile_outcome(self, tick_ok: bool) -> None:
        """Update the reconciliation failure streak after one tick.

        A clean tick resets the streak immediately. A failing tick
        increments it and stamps ``degraded_since`` on the first failure of
        a new streak — ``get_health()`` compares that timestamp against
        ``_DEGRADED_THRESHOLD_SECONDS`` so a single blip doesn't page anyone.
        """
        if tick_ok:
            self._reconcile_failure_streak = 0
            self._reconcile_degraded_since = None
            self._reconcile_last_error = None
            return
        self._reconcile_failure_streak += 1
        if self._reconcile_degraded_since is None:
            self._reconcile_degraded_since = time.time()

    def get_health(self) -> dict:
        """Health/degradation snapshot for the reconciliation loop.

        Surfaced via ``/api/health`` so the dashboard can show a banner
        (and CrossTalk can nag) when the loop has been failing for longer
        than ``_DEGRADED_THRESHOLD_SECONDS`` — instead of the prior
        log-only failure mode that was invisible until an operator went
        looking (2026-07-02 incident).
        """
        degraded_since = self._reconcile_degraded_since
        degraded_seconds = (time.time() - degraded_since) if degraded_since else 0.0
        return {
            "reconcile_failure_streak": self._reconcile_failure_streak,
            "reconcile_degraded_since": degraded_since,
            "reconcile_degraded_seconds": degraded_seconds,
            "reconcile_degraded": degraded_seconds >= self._DEGRADED_THRESHOLD_SECONDS,
        }

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

    async def _check_mission_control_nag(self, sessions: list, now: float) -> None:
        """Nag a mission or pillar coordinator that's gone idle while it has
        outstanding Mission Control questions -- models sometimes finish
        their turn but forget to file the answer. Independent of the
        general nag_enabled slot checked just above (see
        coordinator_nag_state's schema comment in mission_control_db.py).
        """
        try:
            from tools.dashboard.dao import mission_control_db
            open_by_session = await asyncio.to_thread(
                mission_control_db.list_coordinators_with_open_questions,
            )
        except Exception:
            logger.exception("session_monitor: mission_control nag query failed")
            return
        if not open_by_session:
            return

        sessions_by_name = {row["tmux_name"]: row for row in sessions}
        for tmux_name, entries in open_by_session.items():
            row = sessions_by_name.get(tmux_name)
            if not row or derive_lifecycle_state(row) in ("ENDED", "FAILED"):
                continue
            last_act = row.get("last_activity") or row["created_at"]
            idle_secs = now - last_act
            if idle_secs < self._MC_NAG_IDLE_THRESHOLD:
                continue
            try:
                last_nagged = await asyncio.to_thread(
                    mission_control_db.get_coordinator_nag_state, tmux_name,
                ) or 0
            except Exception:
                logger.exception("session_monitor: mission_control nag state read failed for %s", tmux_name)
                continue
            if (now - last_nagged) < self._MC_NAG_IDLE_THRESHOLD:
                continue
            alive = await asyncio.to_thread(self._check_tmux, tmux_name)
            if not alive:
                continue
            logger.info(
                "session_monitor: mission_control nag firing for %s (idle %ds, %d open)",
                tmux_name, int(idle_secs), len(entries),
            )
            await asyncio.to_thread(_send_nag_crosstalk, tmux_name, _build_mission_control_nag_message(entries))
            try:
                await asyncio.to_thread(mission_control_db.mark_coordinator_nagged, tmux_name)
            except Exception:
                logger.exception("session_monitor: mission_control nag state write failed for %s", tmux_name)

    @staticmethod
    def _check_tmux(name: str) -> bool | None:
        """Probe tmux liveness (runs in thread). Tri-state:

        - True / False: the probe RAN and its answer is authoritative.
        - None: the probe itself failed — tmux could not be spawned
          (fork EAGAIN under process pressure, missing binary, any
          OSError). That says nothing about the session; callers must
          treat it as UNKNOWN and never as dead. A user-level fork-EAGAIN
          spike makes every spawn raise BlockingIOError (an OSError) at
          once — a bool probe returning False here reaps the entire
          live fleet in a single sweep tick.
        """
        try:
            return subprocess.run(
                ["tmux", "has-session", "-t", name],
                capture_output=True,
            ).returncode == 0
        except (FileNotFoundError, OSError):
            logger.warning(
                "session_monitor: liveness probe failed for %s — UNKNOWN, not dead",
                name, exc_info=True,
            )
            return None

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
                    project=str(meta.get("project") or jsonl.parent.name),
                    harness=str(meta.get("harness") or "claude"),
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
                    harness="claude",
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

        # W2: eager-create graph_source_id for all seeded sessions, in-process.
        # Replaces the old ENRICH loop, which force-ingested each session's
        # full content via a blocking `graph ingest-session` subprocess (up
        # to 30s each) purely to mint an id faster than the next sweep tick.
        # Eager creation solves the same visibility problem — a zero-turn
        # source row + graph_source_id, immediately — without a subprocess;
        # the normal ingest sweep (and, once W3 lands, live tailing) still
        # owns backfilling these sessions' actual content, exactly as it
        # always has for every other JSONL on disk.
        enriched = await asyncio.to_thread(self._eager_create_missing_sources)
        if enriched:
            logger.info("session_monitor: eager-created graph sources for %d session(s)", enriched)

        logger.info("session_monitor: seeded %d sessions from filesystem", seeded)


# Module-level singleton
session_monitor = SessionMonitor()
