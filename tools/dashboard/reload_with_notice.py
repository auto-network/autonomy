"""Uvicorn reload launcher with a zero-downtime worker hand-off.

Stock ``uvicorn --reload`` restarts a worker as terminate → join → spawn, so
every merge costs the full worker startup (15-25 s typical, 181 s observed on
2026-09-07 while ``org_ops.ensure_bootstrap_orgs`` rebuilt catalogs) with the
listening socket accepting into the kernel backlog and nobody answering.

This launcher changes only Uvicorn's ``BaseReload`` seams (``startup``,
``restart``, ``run``, ``shutdown``, plus ``__next__`` to capture what
changed); file watching, socket binding and the worker itself remain
Uvicorn's. The reload becomes:

1. spawn the replacement worker on the same inherited listening socket, with
   the incumbent's PID and a ready-marker path in its environment;
2. keep the incumbent serving until the replacement writes its ready marker
   (it does so at the end of ``_on_startup``, i.e. immediately before Uvicorn
   starts accepting on the shared socket);
3. terminate and join the incumbent, then write the activation marker so the
   replacement runs the steps that require the predecessor to be gone
   (``tools.dashboard.worker_handoff``).

Failure handling keeps the incumbent up: a replacement that exits before
ready, or never reaches ready within the timeout, is logged and discarded
while the old worker keeps serving; further file changes arriving while a
replacement is still starting supersede it (latest code wins, the incumbent
is untouched). A serving worker that dies on its own is respawned with a
backoff instead of leaving the socket orphaned until the next merge.

Before spawning, the incumbent is told over the authenticated
``/api/internal/restart-notice`` route (mode ``handoff``, with the changed
file paths so the worker can attribute the reload to a merge or a direct host
edit) so it snapshots the EventBus replay buffer and the vault key cache for
the replacement to restore at its own startup. That call is best-effort and
never blocks the reload.

This module is itself watched, so committed probe commits still exercise the
full path; the supervisor process must be restarted once to pick up changes
to this file, because Uvicorn's reloader never re-imports itself.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import ssl
import tempfile
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from urllib import error, request

from uvicorn._subprocess import get_subprocess
from uvicorn.main import main as uvicorn_main
from uvicorn.supervisors.basereload import HANDLED_SIGNALS, BaseReload

from tools.dashboard import worker_handoff

logger = logging.getLogger("uvicorn.error")

_NOTICE_PATH = "/api/internal/restart-notice"
_TOKEN_HEADER = "X-Dashboard-Restart-Token"
_REQUEST_TIMEOUT_SECONDS = 1

#: How long a replacement may take to reach ready before it is abandoned and
#: the incumbent kept. Startup has been observed at three minutes; the default
#: is deliberately generous because the incumbent keeps serving throughout.
READY_TIMEOUT_ENV = "DASHBOARD_WORKER_READY_TIMEOUT_SECONDS"
_DEFAULT_READY_TIMEOUT_SECONDS = 900.0
#: Wait for the incumbent's graceful shutdown before escalating to SIGKILL.
_STOP_GRACE_SECONDS = 15.0
#: A serving worker that died is respawned no more often than this.
_RESPAWN_BACKOFF_SECONDS = 10.0

OUTCOME_READY = "ready"
OUTCOME_DIED = "died"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_SUPERSEDED = "superseded"
OUTCOME_EXIT = "exit"

_handoff_dir: Path | None = None
_original_shutdown = BaseReload.shutdown
_original_next = BaseReload.__next__


def _handoff_directory() -> Path:
    global _handoff_dir
    if _handoff_dir is None:
        _handoff_dir = Path(tempfile.mkdtemp(prefix="dashboard-handoff-"))
    return _handoff_dir


def _ready_timeout_seconds() -> float:
    raw = os.environ.get(READY_TIMEOUT_ENV)
    if not raw:
        return _DEFAULT_READY_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        logger.warning("ignoring malformed %s=%r", READY_TIMEOUT_ENV, raw)
        return _DEFAULT_READY_TIMEOUT_SECONDS
    return value if value > 0 else _DEFAULT_READY_TIMEOUT_SECONDS


# ── what changed ───────────────────────────────────────────────────────────

def _remember_changes(self: BaseReload, changes) -> None:
    """Stash the changed paths so the pending hand-off can be attributed.
    Best-effort — never let capture affect the reload decision."""
    try:
        if changes:
            self._last_changed_paths = [str(p) for p in changes]
    except Exception:
        pass


def _next_capturing(self: BaseReload):
    """``should_restart`` (via ``__next__``) is the one place the reloader
    knows *what* changed; ``restart()`` does not receive it."""
    changes = _original_next(self)
    _remember_changes(self, changes)
    return changes


# ── incumbent notice ───────────────────────────────────────────────────────

def _restart_notice_url(config: SimpleNamespace) -> str:
    scheme = "https" if getattr(config, "ssl_certfile", None) else "http"
    return f"{scheme}://127.0.0.1:{config.port}{_NOTICE_PATH}"


def _notify_dashboard(
    config: SimpleNamespace, changed_paths=None, *, mode: str = "handoff"
) -> bool:
    """Ask the incumbent to snapshot hand-off state. Failure never blocks.

    ``changed_paths`` (the files uvicorn saw change) is forwarded so the worker
    can attribute the reload to a merge or a direct host edit.
    """
    token = os.environ.get("DASHBOARD_RESTART_TOKEN")
    if not token:
        logger.warning("hand-off notice skipped: DASHBOARD_RESTART_TOKEN is unset")
        return False
    body = json.dumps({
        "mode": mode,
        "changed_files": [str(p) for p in (changed_paths or []) if p],
    }).encode("utf-8")
    req = request.Request(
        _restart_notice_url(config),
        data=body,
        headers={_TOKEN_HEADER: token, "Content-Type": "application/json"},
        method="POST",
    )
    context = ssl._create_unverified_context() if getattr(config, "ssl_certfile", None) else None
    try:
        with request.urlopen(req, timeout=_REQUEST_TIMEOUT_SECONDS, context=context) as response:
            if 200 <= response.status < 300:
                return True
            logger.warning("hand-off notice refused with HTTP %s", response.status)
    except (OSError, error.URLError, error.HTTPError):
        logger.warning("hand-off notice request failed; reloading without a snapshot")
    return False


# ── process helpers ────────────────────────────────────────────────────────

def _spawn(self: BaseReload, predecessor_pid: int | None):
    """Start a worker with the hand-off environment; returns (process, marker)."""
    marker = _handoff_directory() / f"{uuid.uuid4().hex}.ready"
    previous = {
        key: os.environ.get(key)
        for key in (worker_handoff.READY_MARKER_ENV, worker_handoff.PREDECESSOR_PID_ENV)
    }
    os.environ[worker_handoff.READY_MARKER_ENV] = str(marker)
    if predecessor_pid is not None:
        os.environ[worker_handoff.PREDECESSOR_PID_ENV] = str(predecessor_pid)
    else:
        os.environ.pop(worker_handoff.PREDECESSOR_PID_ENV, None)
    try:
        process = get_subprocess(config=self.config, target=self.target, sockets=self.sockets)
        process.start()
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return process, marker


def _stop(process, *, grace: float = _STOP_GRACE_SECONDS) -> None:
    """Terminate a worker, escalating to SIGKILL if it ignores SIGTERM."""
    if not process.is_alive():
        process.join()
        return
    process.terminate()
    process.join(grace)
    if process.is_alive():
        logger.error("worker [%s] ignored SIGTERM for %.0fs; killing", process.pid, grace)
        process.kill()
        process.join()


def _await_ready(self: BaseReload, child, marker: Path, *, timeout: float) -> str:
    """Block until the replacement is ready, dies, times out, is superseded by
    newer file changes, or the supervisor is asked to exit.

    Change detection reuses ``should_restart`` so the watcher keeps draining;
    it raises ``StopIteration`` when the supervisor's exit event is set.
    """
    deadline = time.monotonic() + timeout
    while True:
        if marker.exists():
            return OUTCOME_READY
        if not child.is_alive():
            return OUTCOME_DIED
        if time.monotonic() >= deadline:
            return OUTCOME_TIMEOUT
        try:
            changes = self.should_restart()
        except StopIteration:
            return OUTCOME_EXIT
        if changes:
            _remember_changes(self, changes)
            logger.warning(
                "%s detected changes in %s while a replacement worker was "
                "still starting; superseding it",
                self.reloader_name, ", ".join(str(c) for c in changes),
            )
            return OUTCOME_SUPERSEDED


# ── BaseReload seams ───────────────────────────────────────────────────────

def _startup_with_handoff(self: BaseReload) -> None:
    logger.info(
        "Started reloader process [%s] using %s (zero-downtime hand-off)",
        self.pid, self.reloader_name,
    )
    for sig in HANDLED_SIGNALS:
        signal.signal(sig, self.signal_handler)
    self.process, self._ready_marker = _spawn(self, None)
    self._last_respawn = time.monotonic()


def _restart_with_handoff(self: BaseReload) -> None:
    old = self.process
    old_marker = getattr(self, "_ready_marker", None)
    incumbent_alive = old.is_alive()
    if incumbent_alive:
        _notify_dashboard(self.config, getattr(self, "_last_changed_paths", None))
    else:
        logger.warning(
            "incumbent worker [%s] is not running; the replacement activates "
            "immediately", old.pid,
        )
    t0 = time.monotonic()
    timeout = _ready_timeout_seconds()
    while True:
        child, marker = _spawn(self, old.pid if incumbent_alive else None)
        logger.info(
            "spawned replacement worker [%s]; incumbent [%s] keeps serving until it is ready",
            child.pid, old.pid,
        )
        outcome = _await_ready(self, child, marker, timeout=timeout)
        if outcome == OUTCOME_SUPERSEDED:
            _stop(child)
            worker_handoff.cleanup_markers(marker)
            continue
        break

    if outcome == OUTCOME_EXIT:
        _stop(child)
        worker_handoff.cleanup_markers(marker)
        return  # shutdown() will stop the incumbent

    if outcome == OUTCOME_DIED:
        logger.error(
            "replacement worker [%s] exited with code %s before becoming ready "
            "after %.1fs; incumbent [%s] keeps serving the previous code",
            child.pid, child.exitcode, time.monotonic() - t0, old.pid,
        )
        child.join()
        worker_handoff.cleanup_markers(marker)
        return

    if outcome == OUTCOME_TIMEOUT:
        logger.error(
            "replacement worker [%s] did not become ready within %.0fs; "
            "stopping it, incumbent [%s] keeps serving the previous code",
            child.pid, timeout, old.pid,
        )
        _stop(child)
        worker_handoff.cleanup_markers(marker)
        return

    ready_after = time.monotonic() - t0
    t1 = time.monotonic()
    _stop(old)
    self.process = child
    self._ready_marker = marker
    self._last_respawn = time.monotonic()
    worker_handoff.signal_activation(marker)
    if old_marker is not None:
        worker_handoff.cleanup_markers(old_marker)
    logger.info(
        "hand-off complete: worker [%s] -> [%s]; replacement ready after %.1fs, "
        "incumbent drained in %.1fs",
        old.pid, child.pid, ready_after, time.monotonic() - t1,
    )


def _respawn_dead_worker(self: BaseReload) -> None:
    now = time.monotonic()
    since = now - getattr(self, "_last_respawn", 0.0)
    if since < _RESPAWN_BACKOFF_SECONDS:
        return
    dead = self.process
    dead.join()
    logger.error(
        "worker [%s] exited unexpectedly with code %s; respawning",
        dead.pid, dead.exitcode,
    )
    old_marker = getattr(self, "_ready_marker", None)
    if old_marker is not None:
        worker_handoff.cleanup_markers(old_marker)
    self.process, self._ready_marker = _spawn(self, None)
    self._last_respawn = now


def _run_with_handoff(self: BaseReload) -> None:
    self.startup()
    for changes in self:
        if changes:
            logger.warning(
                "%s detected changes in %s. Reloading...",
                self.reloader_name, ", ".join(str(c) for c in changes),
            )
            self.restart()
        elif not self.process.is_alive():
            _respawn_dead_worker(self)
    self.shutdown()


def _shutdown_with_handoff(self: BaseReload) -> None:
    try:
        _original_shutdown(self)
    finally:
        marker = getattr(self, "_ready_marker", None)
        if marker is not None:
            worker_handoff.cleanup_markers(marker)


BaseReload.__next__ = _next_capturing
BaseReload.startup = _startup_with_handoff
BaseReload.restart = _restart_with_handoff
BaseReload.run = _run_with_handoff
BaseReload.shutdown = _shutdown_with_handoff


if __name__ == "__main__":
    uvicorn_main(prog_name="python -m tools.dashboard.reload_with_notice")
