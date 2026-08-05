"""Minimal session lifecycle worker.

One background thread owns lifecycle jobs. Request handlers should enqueue work
without waiting for startup/teardown to finish; the worker is the only lifecycle
state writer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import queue
import threading
import time
from typing import Any, Callable, Literal

logger = logging.getLogger(__name__)

LifecycleAction = Literal["start", "stop", "retry", "restart"]
LifecycleState = Literal[
    "requested",
    "preparing",
    "launching",
    "setup",
    "waiting_ready",
    "confirming_trust",
    "composer_ready",
    "injecting",
    "running",
    "failed",
    "stopping",
    "cleaning",
    "dead",
]

_STOP = object()

# ── The single lifecycle truth ───────────────────────────────────────
#
# One closed set. Every session is in exactly one of these at all times.
# Only the transition authority below writes this column (enforced by
# test_no_racing_writers.py). The legacy is_live/activity_state columns
# are DROPPED — liveness is state-derived everywhere.
SessionState = Literal["LAUNCHING", "ACTIVE", "STOPPING", "ENDED", "FAILED"]

# Legal moves. A transition outside this table is refused and logged at
# WARNING — an illegal request is a bug announcing itself, and refusing
# keeps it from corrupting state. ``None`` covers rows that predate the
# state column (un-backfilled snapshots); they may enter anywhere once.
_LEGAL_TRANSITIONS: dict[str | None, frozenset[str]] = {
    None: frozenset({"LAUNCHING", "ACTIVE", "STOPPING", "ENDED", "FAILED"}),
    "LAUNCHING": frozenset({"LAUNCHING", "ACTIVE", "STOPPING", "ENDED", "FAILED"}),
    "ACTIVE": frozenset({"ACTIVE", "STOPPING", "ENDED", "FAILED"}),
    "STOPPING": frozenset({"STOPPING", "ENDED", "FAILED"}),
    # ENDED→LAUNCHING is resume/retry entry; ENDED→ACTIVE is boot-recovery
    # adopting a row whose process outlived a dashboard restart. ENDED
    # never becomes FAILED: the one path that wanted it (failure-cleanup's
    # deregister recording death mid-cleanup) suppresses death-recording
    # instead — the worker knows the session is failing.
    "ENDED": frozenset({"ENDED", "LAUNCHING", "ACTIVE"}),
    # FAILED→STOPPING is the failure-cleanup pass (fail is recorded first,
    # bounded cleanup renders the cleaning chip, then fail is restored);
    # FAILED→LAUNCHING is retry. FAILED never becomes ENDED — it is its
    # own terminal, kept operator-visible until retried.
    "FAILED": frozenset({"FAILED", "LAUNCHING", "STOPPING"}),
}

# Per-phase time budgets, seconds — THE single source for both consumers:
# the worker enforces them inside its blocking steps, and the liveness
# reaper's orphaned-launch belt reads the same table (budget + margin since
# the last transition). One table, two consumers: a reaper grace that
# undercuts a worker deadline is unrepresentable. Keyed by the chip phase
# (the value the row's startup_state carries while LAUNCHING/STOPPING).
STEP_TIMEOUTS_S: dict[str, float] = {
    # Queue wait before the worker picks the job up. Generous: launches
    # serialize on one worker thread and may sit behind a slow setup.
    "requesting": 900.0,
    "preparing_workspace": 120.0,
    "launching_container": 60.0,
    "setup_running": 600.0,
    "harness_starting": 60.0,
    "confirming_trust": 60.0,
    "composer_ready": 30.0,
    "awaiting_first_response": 30.0,
    "stopping": 30.0,
    "cleaning": 70.0,
}

# The reaper's slack on top of a phase budget before it may treat a
# LAUNCHING/STOPPING session as orphaned (worker lost the job without the
# process dying). The worker itself fails a stuck step at the budget; the
# belt exists only for the exotic worker-death case.
REAPER_BELT_MARGIN_S = 60.0

# Internal worker step → (coarse state, chip phase). The chip values are
# the granular launch phases the UI renders; they are sub-state, only
# meaningful while LAUNCHING/STOPPING.
_SESSION_STATE_FOR_LIFECYCLE: dict[str, tuple[str, str | None]] = {
    "requested": ("LAUNCHING", "requesting"),
    "preparing": ("LAUNCHING", "preparing_workspace"),
    "launching": ("LAUNCHING", "launching_container"),
    "setup": ("LAUNCHING", "setup_running"),
    "waiting_ready": ("LAUNCHING", "harness_starting"),
    "confirming_trust": ("LAUNCHING", "confirming_trust"),
    "composer_ready": ("LAUNCHING", "composer_ready"),
    "injecting": ("LAUNCHING", "awaiting_first_response"),
    "running": ("ACTIVE", None),
    "failed": ("FAILED", None),
    "stopping": ("STOPPING", "stopping"),
    "cleaning": ("STOPPING", "cleaning"),
    "dead": ("ENDED", None),
}

def derive_lifecycle_state(row: dict) -> str:
    """Coarse lifecycle state for a session row.

    The stored ``state`` column is the truth. The legacy-column computation
    below covers rows that don't carry it (pre-backfill snapshots, mock
    fixtures) and is also the migration backfill's definition. Order
    matters in the fallback: failed rows also carry is_live=0, so FAILED
    must win over ENDED.
    """
    stored = row.get("state")
    if stored:
        return stored
    activity = row.get("activity_state")
    if activity == "failed" or row.get("startup_state") == "setup_failed":
        return "FAILED"
    if activity in ("stopping", "cleaning"):
        return "STOPPING"
    if activity == "dead" or not row.get("is_live"):
        return "ENDED"
    if row.get("startup_state"):
        return "LAUNCHING"
    return "ACTIVE"


@dataclass(frozen=True)
class LifecycleJob:
    action: LifecycleAction
    tmux_name: str
    config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LifecycleTransition:
    tmux_name: str
    state: LifecycleState
    phase: str | None = None
    reason: str | None = None
    at: float = field(default_factory=time.time)


class LifecycleQueueFull(Exception):
    """Raised when lifecycle work cannot be accepted immediately."""


class SessionLifecycleStateWriter:
    """Write lifecycle state into existing ``tmux_sessions`` columns."""

    def __init__(
        self,
        *,
        on_transition: Callable[[LifecycleTransition], None] | None = None,
    ) -> None:
        self._on_transition = on_transition

    def set_transition_hook(
        self, hook: Callable[[LifecycleTransition], None] | None,
    ) -> None:
        """Install the transition observer after construction.

        The worker and its writer are built at import time, but the hook
        needs the running event loop (it schedules the registry SSE
        broadcast) — _on_startup wires it once the loop exists.
        """
        self._on_transition = hook

    def transition(
        self,
        tmux_name: str,
        to_state: str,
        *,
        phase: str | None = None,
        cause: str = "",
        reason: str | None = None,
        failed_phase: str | None = None,
        retryable: bool = True,
        attempt: int = 1,
    ) -> bool:
        """THE transition authority — the only writer of session state.

        Validates the move against ``_LEGAL_TRANSITIONS``, then writes the
        full column set atomically: ``state``, the chip ``startup_state``
        (granular phase, meaningful while LAUNCHING; sticky setup_failed
        for FAILED), ``last_activity``, ``ended_at``, ``attention`` (reset
        to idle on ACTIVE entry, cleared on terminal entry), and
        ``lifecycle_detail``.

        Returns True when the row changed. An illegal move is REFUSED,
        logged at WARNING, and returns False — a caller requesting an
        impossible transition is a bug announcing itself, and refusal keeps
        it from corrupting state.
        """
        from tools.dashboard.dao import dashboard_db

        assert to_state in ("LAUNCHING", "ACTIVE", "STOPPING", "ENDED", "FAILED")
        conn = dashboard_db.get_conn()
        row = conn.execute(
            "SELECT state FROM tmux_sessions WHERE tmux_name=?", (tmux_name,),
        ).fetchone()
        if row is None:
            logger.warning(
                "session_lifecycle: transition skipped for missing row tmux=%s to=%s cause=%s",
                tmux_name, to_state, cause,
            )
            # The transition event still fires (pre-consolidation contract:
            # observers hear about the attempt even when the row is gone —
            # e.g. a failure recorded for an already-purged session).
            if self._on_transition is not None:
                self._on_transition(LifecycleTransition(
                    tmux_name=tmux_name,
                    state=to_state,  # type: ignore[arg-type]
                    phase=phase,
                    reason=reason,
                ))
            return False
        current = row[0] or None
        if to_state not in _LEGAL_TRANSITIONS.get(current, frozenset()):
            logger.warning(
                "session_lifecycle: ILLEGAL transition refused tmux=%s %s→%s cause=%s reason=%s",
                tmux_name, current, to_state, cause, reason or "",
            )
            return False

        now = time.time()
        # Chip phase: LAUNCHING carries the granular launch phase; FAILED
        # keeps the sticky setup_failed chip; ACTIVE/STOPPING/ENDED clear it.
        if to_state == "LAUNCHING":
            startup_state = phase
        elif to_state == "FAILED":
            startup_state = "setup_failed"
        else:
            startup_state = None

        lifecycle_detail = None
        if to_state == "FAILED":
            lifecycle_detail = json.dumps(
                {
                    "failed_phase": failed_phase or "",
                    "reason": reason or "",
                    "retryable": bool(retryable),
                    "attempt": int(attempt),
                    "last_progress_at": now,
                },
                sort_keys=True,
            )

        if to_state in ("ENDED", "FAILED"):
            ended_at_sql = "COALESCE(ended_at, ?)"
            ended_at_val: float | None = now
            attention_sql = "NULL"
        elif to_state == "ACTIVE":
            ended_at_sql = "NULL"
            ended_at_val = None
            # attention's domain is the tracker vocabulary
            # (tool_running|thinking|idle, CHECK-enforced); entry matches
            # birth and the tracker converges from tailed entries. The
            # legacy activity_state projection keeps 'running' for
            # byte-compatibility until the column drop.
            attention_sql = "'idle'"
        else:
            ended_at_sql = "NULL"
            ended_at_val = None
            attention_sql = "attention"

        params: list = [to_state, startup_state, now, lifecycle_detail]
        if ended_at_val is not None:
            params.append(ended_at_val)
        params.append(tmux_name)
        cur = conn.execute(
            "UPDATE tmux_sessions"
            f" SET state=?, startup_state=?,"
            f" last_activity=?, lifecycle_detail=?,"
            f" ended_at={ended_at_sql}, attention={attention_sql}"
            " WHERE tmux_name=?",
            params,
        )
        conn.commit()
        if cur.rowcount == 0:
            # Row vanished between the legality SELECT and the UPDATE.
            # Same contract as the missing-row branch above: observers
            # still hear about the attempt.
            logger.warning(
                "session_lifecycle: state write skipped for missing row tmux=%s state=%s",
                tmux_name, to_state,
            )
            if self._on_transition is not None:
                self._on_transition(LifecycleTransition(
                    tmux_name=tmux_name,
                    state=to_state,  # type: ignore[arg-type]
                    phase=phase,
                    reason=reason,
                ))
            return False
        logger.info(
            "session_lifecycle: state tmux=%s state=%s phase=%s cause=%s reason=%s",
            tmux_name, to_state, phase or "", cause, reason or "",
        )
        if self._on_transition is not None:
            self._on_transition(LifecycleTransition(
                tmux_name=tmux_name,
                state=to_state,  # type: ignore[arg-type]
                phase=phase,
                reason=reason,
            ))
        return True

    def set_state(
        self,
        tmux_name: str,
        state: LifecycleState,
        *,
        phase: str | None = None,
        reason: str | None = None,
        retryable: bool = True,
        attempt: int = 1,
    ) -> None:
        """Worker-step wrapper: internal step name → transition()."""
        to_state, chip = _SESSION_STATE_FOR_LIFECYCLE[state]
        self.transition(
            tmux_name,
            to_state,
            phase=chip if to_state in ("LAUNCHING", "STOPPING") else None,
            cause=f"worker:{state}",
            reason=reason,
            failed_phase=phase if to_state == "FAILED" else None,
            retryable=retryable,
            attempt=attempt,
        )

    def fail(
        self,
        tmux_name: str,
        *,
        phase: str,
        reason: str,
        retryable: bool = True,
        attempt: int = 1,
    ) -> None:
        self.set_state(
            tmux_name,
            "failed",
            phase=phase,
            reason=reason,
            retryable=retryable,
            attempt=attempt,
        )


# The one shared authority instance. Everything that records lifecycle
# state — the worker's steps, arm_startup_state, mark_dead, boot recovery —
# goes through this object so the transition hook (wired in _on_startup)
# broadcasts every change.
STATE_AUTHORITY = SessionLifecycleStateWriter()


LifecycleHandler = Callable[[LifecycleJob, SessionLifecycleStateWriter], None]


class SessionLifecycleWorker:
    """Single-thread lifecycle queue with nonblocking enqueue."""

    def __init__(
        self,
        *,
        max_queue_size: int = 64,
        state_writer: SessionLifecycleStateWriter | None = None,
        handlers: dict[LifecycleAction, LifecycleHandler] | None = None,
        name: str = "session-lifecycle",
    ) -> None:
        self._queue: queue.Queue[LifecycleJob | object] = queue.Queue(maxsize=max_queue_size)
        self._state_writer = state_writer or STATE_AUTHORITY
        self._handlers: dict[LifecycleAction, LifecycleHandler] = dict(handlers or {})
        self._name = name
        self._thread: threading.Thread | None = None
        self._stop_requested = threading.Event()

    @property
    def state_writer(self) -> SessionLifecycleStateWriter:
        """The single lifecycle state writer this worker drives.

        Request handlers that must write lifecycle state outside a job
        (e.g. the enqueue-failed 503 path) go through this instance so the
        transition hook fires for every write.
        """
        return self._state_writer

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    def register_handler(self, action: LifecycleAction, handler: LifecycleHandler) -> None:
        if self.is_running:
            raise RuntimeError("cannot register lifecycle handler after worker starts")
        self._handlers[action] = handler

    def start(self) -> None:
        if self.is_running:
            return
        self._stop_requested.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=self._name,
            daemon=True,
        )
        self._thread.start()

    def shutdown(self, *, timeout: float = 5.0) -> None:
        self._stop_requested.set()
        try:
            self._queue.put_nowait(_STOP)
        except queue.Full:
            logger.warning("session_lifecycle: shutdown queue full; worker will stop after current jobs")
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)

    def enqueue(self, job: LifecycleJob) -> None:
        try:
            self._queue.put_nowait(job)
        except queue.Full as exc:
            raise LifecycleQueueFull(
                f"session lifecycle queue is full ({self._queue.maxsize})"
            ) from exc

    def try_enqueue(self, job: LifecycleJob) -> bool:
        try:
            self.enqueue(job)
            return True
        except LifecycleQueueFull:
            return False

    def _run(self) -> None:
        logger.info("session_lifecycle: worker started")
        while not self._stop_requested.is_set():
            item = self._queue.get()
            try:
                if item is _STOP:
                    return
                self._handle_job(item)
            finally:
                self._queue.task_done()
        logger.info("session_lifecycle: worker stopped")

    def _handle_job(self, job: LifecycleJob) -> None:
        handler = self._handlers.get(job.action)
        if handler is None:
            self._state_writer.fail(
                job.tmux_name,
                phase=job.action,
                reason=f"no lifecycle handler registered for {job.action}",
            )
            return
        try:
            handler(job, self._state_writer)
        except Exception as exc:
            logger.exception(
                "session_lifecycle: job failed action=%s tmux=%s",
                job.action,
                job.tmux_name,
            )
            self._state_writer.fail(
                job.tmux_name,
                phase=job.action,
                reason=f"{type(exc).__name__}: {exc}",
            )
