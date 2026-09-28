"""Lease watchdog: ends the lease container at its expiry time (auto-8q7oe.5).

Runs inside every lease container beside the lease agent. The expiry time is
passed at launch in ``BROWSER_LEASE_EXPIRES_AT`` (Unix seconds); the lease agent
writes each later value it receives on ``POST /expiry`` to ``EXPIRY_FILE``,
and the newest of those wins. When the time arrives the watchdog asks the lease
agent to stop (SIGTERM, so Chrome closes its profile cleanly), waits briefly,
and exits; the entrypoint then ends the container. No request from the
dashboard is needed, so a lease cannot outlive its expiry by losing the
dashboard.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

EXPIRY_ENV = "BROWSER_LEASE_EXPIRES_AT"
EXPIRY_FILE = Path("/tmp/lease/expiry")
AGENT_PID_FILE = Path("/tmp/lease/agent.pid")
POLL_S = 1.0
#: How long the lease agent gets to close Chrome before the watchdog exits
#: anyway; the container's 15-second exit budget includes this.
STOP_GRACE_S = 8.0


def parse_expiry(value: Optional[str]) -> Optional[float]:
    """A finite, positive Unix time, or None."""
    try:
        expiry = float((value or "").strip())
    except ValueError:
        return None
    if expiry != expiry or expiry <= 0 or expiry == float("inf"):
        return None
    return expiry


def current_expiry(launch_value: Optional[str], file_value: Optional[str]) -> Optional[float]:
    """The expiry in force: the newest one from ``/expiry``, else the launch value."""
    return parse_expiry(file_value) or parse_expiry(launch_value)


def _read_file(path: Path) -> Optional[str]:
    try:
        return path.read_text()
    except OSError:
        return None


def wait_for_expiry(launch_value: Optional[str], read_file: Callable[[], Optional[str]],
                    clock: Callable[[], float] = time.time,
                    sleep: Callable[[float], None] = time.sleep) -> float:
    """Block until the expiry in force has passed; return it.

    An expiry that cannot be read ends the lease at once: a lease with no
    valid limit must not run unbounded.
    """
    while True:
        expiry = current_expiry(launch_value, read_file())
        now = clock()
        if expiry is None or now >= expiry:
            return expiry or now
        sleep(min(POLL_S, expiry - now))


def stop_agent() -> None:
    try:
        pid = int(AGENT_PID_FILE.read_text().strip())
        os.kill(pid, signal.SIGTERM)
    except (OSError, ValueError):
        pid = None
    deadline = time.monotonic() + STOP_GRACE_S
    while pid and time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.2)
    # The agent did not stop in time; stop the browser directly. Exiting then
    # ends the container, which kills whatever remains.
    subprocess.run(["pkill", "-KILL", "-f", "/opt/google/chrome/"], check=False)


def main() -> int:
    expiry = wait_for_expiry(os.environ.get(EXPIRY_ENV), lambda: _read_file(EXPIRY_FILE))
    print(f"lease watchdog: expiry {expiry:.0f} reached; stopping the lease", file=sys.stderr)
    stop_agent()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
