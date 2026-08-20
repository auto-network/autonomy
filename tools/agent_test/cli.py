"""Bounded, asynchronous command-line interface for Agent Test."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .store import (
    atomic_write_json,
    guidance_first,
    live_manifest,
    manifest_path,
    new_run_id,
    read_json,
    repository_root,
    resolve_manifest,
    run_dir,
    state_lock,
    state_root,
    update_manifest,
    utc_now,
)


DEFAULT_FAILURE_LIMIT = 5
DEFAULT_OUTPUT_LINES = 80
MAX_QUERY_LIMIT = 200


def _root() -> tuple[Path, Path]:
    repo = repository_root()
    root = state_root(repo)
    root.mkdir(parents=True, exist_ok=True)
    return repo, root


def _manifest_or_error(root: Path, run_id: str | None) -> dict[str, Any] | None:
    manifest = resolve_manifest(root, run_id)
    if manifest is None:
        label = run_id or "latest"
        print(f"Agent Test: run not found: {label}", file=sys.stderr)
    return manifest


def _summary_line(manifest: dict[str, Any]) -> str:
    summary = manifest.get("summary") or {}
    counts = (
        f"{summary.get('passed', 0)} passed, "
        f"{summary.get('failed', 0)} failed, "
        f"{summary.get('errors', 0)} errors, "
        f"{summary.get('skipped', 0)} skipped"
    )
    duration = manifest.get("duration_seconds")
    suffix = f" in {duration:.1f}s" if isinstance(duration, (int, float)) else ""
    return f"{manifest['run_id']} · {manifest.get('status', 'unknown')} · {counts}{suffix}"


def cmd_run(args: argparse.Namespace) -> int:
    if not args.selectors:
        print(
            "Agent Test: Stage 1 requires at least one explicit test selector; "
            "a bare full-suite run is intentionally unavailable.",
            file=sys.stderr,
        )
        return 2
    repo, root = _root()
    with state_lock(root):
        active = live_manifest(root)
        if active is not None:
            print(
                f"Agent Test: {active['run_id']} is already {active['status']}. "
                "A second run was not started.",
                file=sys.stderr,
            )
            print(f"Inspect it with: agent-test status", file=sys.stderr)
            return 3
        run_id = new_run_id()
        directory = run_dir(root, run_id)
        directory.mkdir(parents=True, exist_ok=False)
        manifest = {
            "schema": 1,
            "agent_test_version": __version__,
            "run_id": run_id,
            "status": "starting",
            "created_at": utc_now(),
            "repo": str(repo),
            "selectors": list(args.selectors),
            "python": str(Path(args.python).resolve()) if os.path.sep in args.python else args.python,
            "notify": args.notify,
        }
        atomic_write_json(manifest_path(directory), manifest)
        worker_log = (directory / "worker.log").open("ab")
        try:
            child = subprocess.Popen(
                [sys.executable, "-m", "tools.agent_test.worker", "--run-dir", str(directory)],
                cwd=repo,
                env=os.environ.copy(),
                stdin=subprocess.DEVNULL,
                stdout=worker_log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        finally:
            worker_log.close()
        update_manifest(directory, {"worker_pid": child.pid, "worker_log": str(directory / "worker.log")})

    selected = len(args.selectors)
    if guidance_first(root, "stage1-background-run-v1"):
        print(f"Started {run_id} in the background: {selected} selector(s).")
        print("Agent Test will notify this session when it finishes. Keep working; do not poll or start another run.")
        print(f"Results remain available through: agent-test show {run_id}")
    else:
        print(f"Started {run_id} in background ({selected} selector(s)). Completion will be delivered here.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    _repo, root = _root()
    manifest = live_manifest(root) or resolve_manifest(root, None)
    if manifest is None:
        print("Agent Test: no runs recorded.")
        return 0
    print(_summary_line(manifest))
    if manifest.get("status") in {"starting", "running", "stopping"}:
        print("The run is supervised in the background; completion will be delivered automatically.")
    else:
        print(f"Evidence: {manifest['_directory']}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    _repo, root = _root()
    manifest = _manifest_or_error(root, args.run_id)
    if manifest is None:
        return 2
    print(_summary_line(manifest))
    print(f"Selectors: {len(manifest.get('selectors') or [])}")
    print(f"Python: {manifest.get('python', 'unknown')}")
    print(f"Created: {manifest.get('created_at', 'unknown')}")
    print(f"Evidence: {manifest['_directory']}")
    message = str(manifest.get("message") or "").strip()
    if message:
        print(f"Message: {message[:500]}")
    return 0


def _failures(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    directory = Path(manifest["_directory"])
    value = read_json(directory / "failures.json", [])
    return value if isinstance(value, list) else []


def _bounded_limit(value: int) -> int:
    return max(1, min(value, MAX_QUERY_LIMIT))


def cmd_failures(args: argparse.Namespace) -> int:
    _repo, root = _root()
    manifest = _manifest_or_error(root, args.run_id)
    if manifest is None:
        return 2
    failures = _failures(manifest)
    if not failures:
        print(f"{manifest['run_id']}: no retained pytest failures.")
        return 0
    limit = _bounded_limit(args.limit)
    shown = failures[:limit]
    print(f"{len(shown)} of {len(failures)} failures in {manifest['run_id']}:")
    for index, failure in enumerate(shown, 1):
        print(f"{index}. {failure.get('phase', 'call')} · {failure.get('nodeid', '<unknown>')}")
        print(f"   {str(failure.get('message') or 'pytest failure')[:500]}")
    remaining = len(failures) - len(shown)
    if remaining:
        print(
            f"{remaining} more retained. Increase deliberately with `--limit N`, "
            f"or inspect one with `agent-test trace {manifest['run_id']} INDEX`."
        )
    return 0


def _select_failure(failures: list[dict[str, Any]], selector: str) -> dict[str, Any] | None:
    if selector.isdigit():
        index = int(selector) - 1
        return failures[index] if 0 <= index < len(failures) else None
    matches = [failure for failure in failures if selector in str(failure.get("nodeid", ""))]
    return matches[0] if len(matches) == 1 else None


def _bounded_text(text: str, limit_lines: int) -> tuple[str, int]:
    lines = text.splitlines()
    limit = _bounded_limit(limit_lines)
    shown = lines[:limit]
    return "\n".join(shown), max(0, len(lines) - len(shown))


def cmd_trace(args: argparse.Namespace) -> int:
    _repo, root = _root()
    manifest = _manifest_or_error(root, args.run_id)
    if manifest is None:
        return 2
    failure = _select_failure(_failures(manifest), args.failure)
    if failure is None:
        print("Agent Test: failure selector is absent or ambiguous.", file=sys.stderr)
        return 2
    trace = str(failure.get("traceback") or "<no traceback retained>")
    bounded, remaining = _bounded_text(trace, args.limit_lines)
    print(f"{failure.get('phase', 'call')} · {failure.get('nodeid', '<unknown>')}")
    print(bounded)
    if remaining:
        print(f"[{remaining} more trace lines retained; use --limit-lines N deliberately]")
    return 0


def cmd_output(args: argparse.Namespace) -> int:
    _repo, root = _root()
    manifest = _manifest_or_error(root, args.run_id)
    if manifest is None:
        return 2
    directory = Path(manifest["_directory"])
    name = "worker.log" if args.stream == "worker" else "pytest.log"
    path = directory / name
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        print(f"Agent Test: retained stream is unavailable: {name}", file=sys.stderr)
        return 2
    limit = _bounded_limit(args.limit_lines)
    shown = lines[-limit:]
    print("\n".join(shown))
    omitted = len(lines) - len(shown)
    if omitted:
        print(f"[{omitted} earlier lines retained in {path}]")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    _repo, root = _root()
    manifest = live_manifest(root)
    if manifest is None:
        print("Agent Test: no live run to stop.")
        return 0
    pid = manifest.get("worker_pid")
    if not isinstance(pid, int):
        print("Agent Test: live run has no owned supervisor PID.", file=sys.stderr)
        return 2
    update_manifest(Path(manifest["_directory"]), {"status": "stopping"})
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    print(f"Stopping {manifest['run_id']} via its owned process group.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-test", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="start a managed background test run")
    run.add_argument("--python", default=sys.executable)
    run.add_argument("--notify", choices=("auto", "none", "crosstalk"), default=os.environ.get("AGENT_TEST_NOTIFY", "auto"))
    run.add_argument("selectors", nargs="*")
    run.set_defaults(func=cmd_run)

    status = sub.add_parser("status", help="show the live or latest run")
    status.set_defaults(func=cmd_status)

    show = sub.add_parser("show", help="show bounded run provenance")
    show.add_argument("run_id", nargs="?")
    show.set_defaults(func=cmd_show)

    failures = sub.add_parser("failures", help="show retained failures without executing")
    failures.add_argument("run_id", nargs="?")
    failures.add_argument("--limit", type=int, default=DEFAULT_FAILURE_LIMIT)
    failures.set_defaults(func=cmd_failures)

    trace = sub.add_parser("trace", help="show one retained traceback")
    trace.add_argument("run_id")
    trace.add_argument("failure", help="1-based failure index or unique node substring")
    trace.add_argument("--limit-lines", type=int, default=120)
    trace.set_defaults(func=cmd_trace)

    output = sub.add_parser("output", help="show a bounded retained output stream")
    output.add_argument("run_id", nargs="?")
    output.add_argument("--stream", choices=("pytest", "worker"), default="pytest")
    output.add_argument("--limit-lines", type=int, default=DEFAULT_OUTPUT_LINES)
    output.set_defaults(func=cmd_output)

    stop = sub.add_parser("stop", help="stop only the owned live process group")
    stop.set_defaults(func=cmd_stop)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))
