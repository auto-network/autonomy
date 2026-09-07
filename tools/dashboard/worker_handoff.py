"""Worker hand-off protocol between the reload supervisor and a dashboard worker.

Shared by both sides of a zero-downtime reload:

* the **supervisor** (``tools.dashboard.reload_with_notice``) spawns the
  replacement worker while the incumbent keeps serving, waits for the
  replacement's *ready marker*, terminates the incumbent, then writes the
  *activation marker*;
* the **worker** (``tools.dashboard.server``) writes the ready marker at the
  end of its startup and defers the handful of steps that assume the previous
  process is gone (orphan sweeps, single-writer pollers, the vault re-warm,
  fleet-sync scheduling) until it is *activated*.

Everything crosses the process boundary through two environment variables set
by the supervisor before ``Process.start()`` and two marker files under a
supervisor-owned directory. No signals: a signal delivered before the child
installs its handler would kill it, and the child cannot ``waitpid`` a sibling.

Env contract (child side):

``DASHBOARD_WORKER_READY_MARKER``
    Absolute path the worker creates once it is accepting requests.
``DASHBOARD_WORKER_PREDECESSOR_PID``
    PID of the incumbent worker, when there is one. Absent on a cold start,
    in which case the worker activates immediately.

The activation marker is ``<ready marker> + ".activate"``. A worker also
activates if the predecessor PID has vanished without a marker (the
supervisor died mid-handoff), so a lost supervisor can never leave a serving
worker half-started forever.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

READY_MARKER_ENV = "DASHBOARD_WORKER_READY_MARKER"
PREDECESSOR_PID_ENV = "DASHBOARD_WORKER_PREDECESSOR_PID"
ACTIVATE_SUFFIX = ".activate"

#: Reasons ``wait_for_activation`` resolves with; stable strings for logs/tests.
ACTIVATED_NO_PREDECESSOR = "no-predecessor"
ACTIVATED_BY_SUPERVISOR = "activated"
ACTIVATED_PREDECESSOR_GONE = "predecessor-gone"


def ready_marker_path(environ=None) -> Path | None:
    env = os.environ if environ is None else environ
    raw = env.get(READY_MARKER_ENV)
    return Path(raw) if raw else None


def predecessor_pid(environ=None) -> int | None:
    env = os.environ if environ is None else environ
    raw = env.get(PREDECESSOR_PID_ENV)
    if not raw:
        return None
    try:
        pid = int(raw)
    except ValueError:
        logger.warning("ignoring malformed %s=%r", PREDECESSOR_PID_ENV, raw)
        return None
    return pid if pid > 0 else None


def activation_marker_path(ready_marker: Path) -> Path:
    return ready_marker.with_name(ready_marker.name + ACTIVATE_SUFFIX)


def pid_alive(pid: int) -> bool:
    """True while ``pid`` exists (a zombie counts as alive until reaped)."""
    try:
        os.kill(pid, 0)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        # EPERM: exists but not ours. Anything else: assume alive, fail safe.
        return True
    return True


def _write_marker(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, path)


def mark_ready(environ=None) -> Path | None:
    """Worker side: publish readiness. Returns the marker path, or None when
    this process is not running under the hand-off supervisor."""
    marker = ready_marker_path(environ)
    if marker is None:
        return None
    _write_marker(marker, f"{os.getpid()}\n")
    return marker


def signal_activation(ready_marker: Path) -> Path:
    """Supervisor side: tell the ready worker its predecessor has exited."""
    marker = activation_marker_path(ready_marker)
    _write_marker(marker, "activate\n")
    return marker


def cleanup_markers(ready_marker: Path) -> None:
    for path in (ready_marker, activation_marker_path(ready_marker)):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            logger.debug("could not remove hand-off marker %s", path, exc_info=True)


async def wait_for_activation(
    *,
    environ=None,
    poll_seconds: float = 0.25,
    pid_alive_fn: Callable[[int], bool] = pid_alive,
) -> str:
    """Worker side: block until this worker may run its predecessor-exclusive
    steps. Resolves immediately on a cold start."""
    predecessor = predecessor_pid(environ)
    marker = ready_marker_path(environ)
    if predecessor is None or marker is None:
        return ACTIVATED_NO_PREDECESSOR
    activate = activation_marker_path(marker)
    while True:
        if activate.exists():
            return ACTIVATED_BY_SUPERVISOR
        if not pid_alive_fn(predecessor):
            return ACTIVATED_PREDECESSOR_GONE
        await asyncio.sleep(poll_seconds)
