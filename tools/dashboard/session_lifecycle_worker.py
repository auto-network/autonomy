"""Minimal session lifecycle worker.

One background thread owns lifecycle jobs. Request handlers should enqueue work
without waiting for startup/teardown to finish; the worker is the only lifecycle
state writer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import queue
import threading
import time
from typing import Any, Callable, Literal

logger = logging.getLogger(__name__)

LifecycleAction = Literal["start", "stop", "retry"]
LifecycleState = Literal[
    "requested",
    "preparing",
    "launching",
    "setup",
    "waiting_ready",
    "injecting",
    "running",
    "failed",
    "stopping",
    "cleaning",
    "dead",
]

_STOP = object()

_STARTUP_STATE_FOR_LIFECYCLE: dict[str, str | None] = {
    "requested": "requesting",
    "preparing": "preparing_workspace",
    "launching": "launching_container",
    "setup": "setup_running",
    "waiting_ready": "harness_starting",
    "injecting": "awaiting_first_response",
    "running": None,
    "failed": "setup_failed",
    "stopping": None,
    "cleaning": None,
    "dead": None,
}

_ACTIVITY_STATE_FOR_LIFECYCLE: dict[str, str] = {
    "requested": "starting",
    "preparing": "starting",
    "launching": "starting",
    "setup": "starting",
    "waiting_ready": "starting",
    "injecting": "starting",
    "running": "idle",
    "failed": "failed",
    "stopping": "stopping",
    "cleaning": "cleaning",
    "dead": "dead",
}


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

    def set_state(
        self,
        tmux_name: str,
        state: LifecycleState,
        *,
        phase: str | None = None,
        reason: str | None = None,
    ) -> None:
        from tools.dashboard.dao import dashboard_db

        startup_state = _STARTUP_STATE_FOR_LIFECYCLE[state]
        activity_state = _ACTIVITY_STATE_FOR_LIFECYCLE[state]
        is_live = 0 if state == "dead" else 1

        conn = dashboard_db.get_conn()
        cur = conn.execute(
            "UPDATE tmux_sessions"
            " SET startup_state=?, activity_state=?, is_live=?, last_activity=?"
            " WHERE tmux_name=?",
            (startup_state, activity_state, is_live, time.time(), tmux_name),
        )
        conn.commit()
        if cur.rowcount == 0:
            logger.warning(
                "session_lifecycle: state write skipped for missing row tmux=%s state=%s",
                tmux_name,
                state,
            )
        transition = LifecycleTransition(
            tmux_name=tmux_name,
            state=state,
            phase=phase,
            reason=reason,
        )
        logger.info(
            "session_lifecycle: state tmux=%s state=%s phase=%s reason=%s",
            tmux_name,
            state,
            phase or "",
            reason or "",
        )
        if self._on_transition is not None:
            self._on_transition(transition)

    def fail(self, tmux_name: str, *, phase: str, reason: str) -> None:
        self.set_state(tmux_name, "failed", phase=phase, reason=reason)


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
        self._state_writer = state_writer or SessionLifecycleStateWriter()
        self._handlers: dict[LifecycleAction, LifecycleHandler] = dict(handlers or {})
        self._name = name
        self._thread: threading.Thread | None = None
        self._stop_requested = threading.Event()

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
