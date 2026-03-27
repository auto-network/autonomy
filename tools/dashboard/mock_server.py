"""
Self-daemonizing mock dashboard server for agent worktree testing.

Solves the SIGPIPE problem: Claude Code's bash tool closes stdout after the
command returns. Any background process that inherited that stdout gets SIGPIPE
when it writes. Fork + DEVNULL avoids this — the child never inherits the
doomed pipe.

Usage:
    dashboard-mock start [--port PORT] [--fixture PATH]
    dashboard-mock stop  [--port PORT]
    dashboard-mock status [--port PORT]
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PORT = 8082
RUN_DIR = Path(tempfile.gettempdir()) / "dashboard-mock"


def _pid_file(port: int) -> Path:
    return RUN_DIR / f"port-{port}.pid"


def _log_file(port: int) -> Path:
    return RUN_DIR / f"port-{port}.log"


def _fixture_file(port: int) -> Path:
    return RUN_DIR / f"port-{port}.fixture.json"


def _events_file(port: int) -> Path:
    return RUN_DIR / f"port-{port}.events.jsonl"


def _default_fixture() -> dict:
    """Minimal fixture so the server boots with something to show."""
    return {
        "beads": [
            {"id": "auto-mock1", "title": "Mock bead (dashboard-mock)", "priority": 1, "status": "open", "issue_type": "task", "labels": []},
        ],
        "runs": [],
        "active_sessions": [],
    }


def _is_process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_pid(port: int) -> int | None:
    pf = _pid_file(port)
    if not pf.exists():
        return None
    try:
        pid = int(pf.read_text().strip())
        return pid if _is_process_alive(pid) else None
    except (ValueError, OSError):
        return None


def _wait_for_server(port: int, timeout: float = 10.0) -> bool:
    """Poll HTTP until the server responds or timeout."""
    import urllib.request
    import urllib.error
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/api/beads/list"
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def cmd_start(args: argparse.Namespace) -> int:
    port = args.port

    # Check if already running on this port
    existing_pid = _read_pid(port)
    if existing_pid is not None:
        print(f"ERROR: Mock dashboard already running on :{port} (PID {existing_pid})", file=sys.stderr)
        print(f"  Run: dashboard-mock stop --port {port}", file=sys.stderr)
        return 1

    # Set up run directory and files
    RUN_DIR.mkdir(parents=True, exist_ok=True)

    # Fixture: use provided path or generate a default
    if args.fixture:
        fixture_path = Path(args.fixture).resolve()
        if not fixture_path.exists():
            print(f"ERROR: Fixture file not found: {fixture_path}", file=sys.stderr)
            return 1
    else:
        fixture_path = _fixture_file(port)
        fixture_path.write_text(json.dumps(_default_fixture(), indent=2))

    # Events file
    events_path = _events_file(port)
    if not events_path.exists():
        events_path.write_text("")

    log_path = _log_file(port)
    pid_path = _pid_file(port)

    # Fork: child becomes the server, parent waits for readiness
    child_pid = os.fork()
    if child_pid == 0:
        # ── Child process ──
        # Detach from parent's session to avoid SIGPIPE
        os.setsid()

        # Redirect all stdio to /dev/null (write logs via uvicorn's log file)
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 0)

        log_fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        os.dup2(log_fd, 1)
        os.dup2(log_fd, 2)
        os.close(devnull)
        os.close(log_fd)

        env = os.environ.copy()
        env["DASHBOARD_MOCK"] = str(fixture_path)
        env["DASHBOARD_MOCK_EVENTS"] = str(events_path)
        env["PYTHONPATH"] = str(REPO_ROOT)

        os.execvpe(
            sys.executable,
            [
                sys.executable, "-m", "uvicorn",
                "tools.dashboard.server:app",
                "--host", "127.0.0.1",
                "--port", str(port),
            ],
            env,
        )
        # execvpe replaces the process — this line is never reached

    # ── Parent process ──
    # Write PID file immediately so stop works even during startup
    pid_path.write_text(str(child_pid))

    print(f"Starting mock dashboard on :{port} (PID {child_pid})...")

    if _wait_for_server(port, timeout=15.0):
        # Verify child is still alive (didn't crash right after responding)
        if _is_process_alive(child_pid):
            print(f"Mock dashboard ready on :{port}")
            print(f"  Fixture: {fixture_path}")
            print(f"  Events:  {events_path}")
            print(f"  Log:     {log_path}")
            print(f"  PID:     {child_pid}")
            return 0
        else:
            print(f"ERROR: Server process exited unexpectedly. Check {log_path}", file=sys.stderr)
            pid_path.unlink(missing_ok=True)
            return 1
    else:
        # Timeout — check if child is still alive
        if _is_process_alive(child_pid):
            print(f"ERROR: Server started but not responding on :{port}. Check {log_path}", file=sys.stderr)
            # Kill it since it's not healthy
            os.kill(child_pid, signal.SIGTERM)
        else:
            print(f"ERROR: Server process exited before becoming ready. Check {log_path}", file=sys.stderr)
        pid_path.unlink(missing_ok=True)
        return 1


def cmd_stop(args: argparse.Namespace) -> int:
    port = args.port
    pid_path = _pid_file(port)
    pid = _read_pid(port)

    if pid is None:
        # Clean up stale pid file
        pid_path.unlink(missing_ok=True)
        print(f"No mock dashboard running on :{port}")
        return 0

    print(f"Stopping mock dashboard on :{port} (PID {pid})...")
    os.kill(pid, signal.SIGTERM)

    # Wait for clean shutdown
    for _ in range(20):
        if not _is_process_alive(pid):
            break
        time.sleep(0.25)
    else:
        # Force kill
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    pid_path.unlink(missing_ok=True)
    print("Stopped.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    port = args.port
    pid = _read_pid(port)

    if pid is None:
        _pid_file(port).unlink(missing_ok=True)
        print(f"No mock dashboard running on :{port}")
        return 1

    fixture_path = _fixture_file(port)
    print(f"Mock dashboard running on :{port}")
    print(f"  PID:     {pid}")
    print(f"  Log:     {_log_file(port)}")
    if fixture_path.exists():
        print(f"  Fixture: {fixture_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dashboard-mock",
        description="Self-daemonizing mock dashboard for agent worktree testing",
    )
    sub = parser.add_subparsers(dest="command")

    p_start = sub.add_parser("start", help="Start a mock dashboard server")
    p_start.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port (default: {DEFAULT_PORT})")
    p_start.add_argument("--fixture", type=str, default=None, help="Path to fixture JSON file")

    p_stop = sub.add_parser("stop", help="Stop the mock dashboard server")
    p_stop.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port (default: {DEFAULT_PORT})")

    p_status = sub.add_parser("status", help="Check if mock dashboard is running")
    p_status.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port (default: {DEFAULT_PORT})")

    args = parser.parse_args(argv)

    if args.command == "start":
        return cmd_start(args)
    elif args.command == "stop":
        return cmd_stop(args)
    elif args.command == "status":
        return cmd_status(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
