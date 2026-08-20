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
from .environment import choose_python, profiles, requested_resources, workspace_fingerprint
from .lease_client import lease_request
from .store import (
    atomic_write_json,
    guidance_first,
    list_manifests,
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
from .supervisor import start_worker, supervisor_status


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
    if manifest.get("status") == "collected":
        duration = manifest.get("duration_seconds")
        suffix = f" in {duration:.1f}s" if isinstance(duration, (int, float)) else ""
        return f"{manifest['run_id']} · collected · {summary.get('collected', 0)} test nodes{suffix}"
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
    repo, root = _root()
    configured_profiles = profiles(repo)
    profile: dict[str, Any] = {}
    if args.profile:
        profile = configured_profiles.get(args.profile, {})
        if not profile:
            print(f"Agent Test: unknown profile: {args.profile}", file=sys.stderr)
            print("Inspect configured profiles with: agent-test profiles", file=sys.stderr)
            return 2
    profile_selectors = profile.get("selectors") or []
    if not isinstance(profile_selectors, list):
        profile_selectors = []
    selectors = [str(value) for value in profile_selectors] + list(args.selectors)
    if not selectors:
        print(
            "Agent Test requires an explicit test selector or named profile; "
            "a bare full-suite run is intentionally unavailable.",
            file=sys.stderr,
        )
        return 2
    python, diagnoses = choose_python(repo, args.python)
    if python is None:
        print("Agent Test: no usable Python with pytest was found.", file=sys.stderr)
        for item in diagnoses[:8]:
            print(f"- {item['candidate']}: {item.get('reason', 'unusable')}", file=sys.stderr)
        print(
            "Fix the project environment, then run `agent-test doctor`; "
            "no dependencies were installed.",
            file=sys.stderr,
        )
        return 2
    profile_args = profile.get("pytest_args") or []
    if not isinstance(profile_args, list) or not all(isinstance(value, str) for value in profile_args):
        print(f"Agent Test: profile {args.profile} has invalid pytest_args.", file=sys.stderr)
        return 2
    mode = getattr(args, "mode", "run")
    try:
        resources = getattr(args, "resource_override", None) or requested_resources(repo, profile, mode)
    except ValueError as exc:
        print(f"Agent Test: {exc}", file=sys.stderr)
        return 2
    fingerprint = workspace_fingerprint(repo, [mode, python, *profile_args, *selectors])
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
        if not getattr(args, "allow_repeat", False):
            repeated = next(
                (
                    item
                    for item in list_manifests(root)
                    if item.get("fingerprint") == fingerprint
                    and item.get("status") not in {"starting", "running", "stopping"}
                ),
                None,
            )
            if repeated is not None:
                print(
                    f"Agent Test: unchanged run refused; {repeated['run_id']} "
                    "already records this exact code and selection.",
                    file=sys.stderr,
                )
                next_command = (
                    f"agent-test rerun-failures {repeated['run_id']}"
                    if repeated.get("status") == "failed"
                    else f"agent-test show {repeated['run_id']}"
                )
                print(f"Next: {next_command}", file=sys.stderr)
                return 4
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
            "selectors": selectors,
            "pytest_args": profile_args,
            "profile": args.profile,
            "mode": mode,
            "fingerprint": fingerprint,
            "python": python,
            "notify": args.notify,
            "resources": resources,
        }
        rerun_of = getattr(args, "rerun_of", None)
        if rerun_of:
            manifest["rerun_of"] = rerun_of
        atomic_write_json(manifest_path(directory), manifest)
        try:
            if os.environ.get("AGENT_TEST_NO_SUPERVISOR") == "1":
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
                launched = {"ok": True, "worker_pid": child.pid}
            else:
                launched = start_worker(root, repo, directory)
            if not launched.get("ok"):
                raise RuntimeError(str(launched.get("error") or "supervisor rejected run"))
        except Exception as exc:
            update_manifest(
                directory,
                {"status": "error", "message": f"launch failed: {exc}", "finished_at": utc_now()},
            )
            print(f"Agent Test: could not launch managed run: {exc}", file=sys.stderr)
            return 2
        update_manifest(
            directory,
            {
                "worker_pid": launched["worker_pid"],
                "supervisor_pid": launched.get("supervisor_pid"),
                "worker_log": str(directory / "worker.log"),
            },
        )

    selected = len(selectors)
    if guidance_first(root, "stage1-background-run-v1"):
        print(f"Started {run_id} in the background: {selected} selector(s).")
        print("Agent Test will notify this session when it finishes. Keep working; do not poll or start another run.")
        print(f"Results remain available through: agent-test show {run_id}")
    else:
        print(f"Started {run_id} in background ({selected} selector(s)). Completion will be delivered here.")
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    args.mode = "collect"
    args.allow_repeat = False
    args.rerun_of = None
    return cmd_run(args)


def cmd_doctor(args: argparse.Namespace) -> int:
    repo, _root_path = _root()
    selected, diagnoses = choose_python(repo, args.python)
    print(f"Repository: {repo}")
    for item in diagnoses[:8]:
        if item["usable"]:
            marker = "selected" if item["candidate"] == selected else "usable"
            print(f"- {marker}: {item['candidate']} · pytest {item['pytest']}")
        else:
            print(f"- unavailable: {item['candidate']} · {item.get('reason', 'unknown')}")
    if selected is None:
        print("No dependencies were changed. Repair the environment and run doctor again.")
        return 2
    print("Agent Test invokes this interpreter directly; shell activation is not required.")
    return 0


def cmd_profiles(args: argparse.Namespace) -> int:
    repo, _root_path = _root()
    configured = profiles(repo)
    if not configured:
        print("No named Agent Test profiles are configured in pyproject.toml.")
        print("Explicit selectors remain available: agent-test run PATH_OR_NODEID")
        return 0
    print(f"{len(configured)} configured profile(s):")
    for name, value in list(sorted(configured.items()))[:20]:
        selectors = value.get("selectors") or []
        print(f"- {name}: {len(selectors)} selector(s)")
    if len(configured) > 20:
        print(f"[{len(configured) - 20} more profiles omitted]")
    return 0


def cmd_inventory(args: argparse.Namespace) -> int:
    _repo, root = _root()
    manifest = _manifest_or_error(root, args.run_id)
    if manifest is None:
        return 2
    nodes = read_json(Path(manifest["_directory"]) / "collection.json", [])
    if not isinstance(nodes, list):
        nodes = []
    if args.match:
        nodes = [node for node in nodes if args.match in str(node)]
    limit = _bounded_limit(args.limit)
    shown = nodes[:limit]
    print(f"{len(shown)} of {len(nodes)} retained test node(s) from {manifest['run_id']}:")
    for node in shown:
        print(f"- {node}")
    if len(nodes) > len(shown):
        print(f"[{len(nodes) - len(shown)} more retained; use --limit N deliberately]")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    _repo, root = _root()
    manifest = _manifest_or_error(root, args.run_id)
    if manifest is None:
        return 2
    nodes = read_json(Path(manifest["_directory"]) / "collection.json", [])
    if not isinstance(nodes, list) or not nodes:
        print("Agent Test: this run retained no inventory; use `agent-test collect SELECTOR` first.", file=sys.stderr)
        return 2
    missing: list[str] = []
    for selector in args.selectors:
        normalized = selector.removeprefix("./")
        if "::" in normalized:
            matches = any(str(node) == normalized or str(node).startswith(normalized + "[") for node in nodes)
        else:
            prefix = normalized.rstrip("/")
            matches = any(
                str(node) == normalized
                or str(node).startswith(prefix + "::")
                or str(node).startswith(prefix + "/")
                for node in nodes
            )
        if not matches:
            missing.append(selector)
    if missing:
        print(
            f"Agent Test: {len(missing)} selector(s) do not exist in "
            f"inventory {manifest['run_id']}:",
            file=sys.stderr,
        )
        for selector in missing[:20]:
            print(f"- {selector}", file=sys.stderr)
        return 2
    print(f"Validated {len(args.selectors)} selector(s) against {len(nodes)} retained test nodes.")
    return 0


def cmd_rerun_failures(args: argparse.Namespace) -> int:
    _repo, root = _root()
    original = _manifest_or_error(root, args.run_id)
    if original is None:
        return 2
    nodeids = list(dict.fromkeys(str(item.get("nodeid")) for item in _failures(original) if item.get("nodeid")))
    if not nodeids:
        print(f"Agent Test: {original['run_id']} has no failed nodes to rerun.", file=sys.stderr)
        return 2
    rerun_args = argparse.Namespace(
        selectors=nodeids,
        python=original.get("python") or "auto",
        notify=args.notify,
        profile=None,
        mode="run",
        allow_repeat=True,
        rerun_of=original["run_id"],
        resource_override=original.get("resources"),
    )
    return cmd_run(rerun_args)


def cmd_supervisor(args: argparse.Namespace) -> int:
    repo, root = _root()
    status = supervisor_status(root)
    if status is None and args.start:
        from .supervisor import ensure_supervisor

        status = ensure_supervisor(root, repo)
    if status is None:
        print("Agent Test supervisor is not resident.")
        return 1
    print(f"Agent Test supervisor is resident (pid {status['pid']}).")
    return 0


def cmd_capacity(args: argparse.Namespace) -> int:
    result = lease_request("status")
    if not result.get("ok"):
        print(f"Agent Test: machine capacity unavailable: {result.get('error', 'unknown')}", file=sys.stderr)
        return 2
    print(f"Machine-wide Agent Test capacity · {result.get('active_leases', 0)} active run(s)")
    limits = result.get("limits") or {}
    used = result.get("used") or {}
    for name in sorted(limits):
        print(
            f"- {name}: {used.get(name, 0)} used · "
            f"{result['available'].get(name, 0)} available · {limits[name]} limit"
        )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    _repo, root = _root()
    manifest = live_manifest(root) or resolve_manifest(root, None)
    if manifest is None:
        print("Agent Test: no runs recorded.")
        return 0
    print(_summary_line(manifest))
    if manifest.get("status") in {"starting", "queued", "running", "stopping"}:
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
    print(f"Mode: {manifest.get('mode', 'run')}")
    if manifest.get("profile"):
        print(f"Profile: {manifest['profile']}")
    print(f"Resources: {manifest.get('resources') or {'tests': 1}}")
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
        classification = failure.get("classification", "unclassified")
        print(
            f"{index}. {classification} · {failure.get('phase', 'call')} · "
            f"{failure.get('nodeid', '<unknown>')}"
        )
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
    run.add_argument("--python", default="auto")
    run.add_argument(
        "--notify",
        choices=("auto", "none", "crosstalk"),
        default=os.environ.get("AGENT_TEST_NOTIFY", "auto"),
    )
    run.add_argument("--profile")
    run.add_argument("selectors", nargs="*")
    run.set_defaults(mode="run", allow_repeat=False, rerun_of=None)
    run.set_defaults(func=cmd_run)

    collect = sub.add_parser("collect", help="retain a background test inventory without executing tests")
    collect.add_argument("--python", default="auto")
    collect.add_argument(
        "--notify",
        choices=("auto", "none", "crosstalk"),
        default=os.environ.get("AGENT_TEST_NOTIFY", "auto"),
    )
    collect.add_argument("--profile")
    collect.add_argument("selectors", nargs="*")
    collect.set_defaults(func=cmd_collect)

    doctor = sub.add_parser("doctor", help="diagnose and select a Python environment")
    doctor.add_argument("--python", default="auto")
    doctor.set_defaults(func=cmd_doctor)

    profile_parser = sub.add_parser("profiles", help="list bounded project test profiles")
    profile_parser.set_defaults(func=cmd_profiles)

    status = sub.add_parser("status", help="show the live or latest run")
    status.set_defaults(func=cmd_status)

    show = sub.add_parser("show", help="show bounded run provenance")
    show.add_argument("run_id", nargs="?")
    show.set_defaults(func=cmd_show)

    failures = sub.add_parser("failures", help="show retained failures without executing")
    failures.add_argument("run_id", nargs="?")
    failures.add_argument("--limit", type=int, default=DEFAULT_FAILURE_LIMIT)
    failures.set_defaults(func=cmd_failures)

    inventory = sub.add_parser("inventory", help="list bounded retained test nodes")
    inventory.add_argument("run_id", nargs="?")
    inventory.add_argument("--match")
    inventory.add_argument("--limit", type=int, default=20)
    inventory.set_defaults(func=cmd_inventory)

    validate = sub.add_parser("validate", help="validate selectors against retained inventory")
    validate.add_argument("selectors", nargs="+")
    validate.add_argument("--run", dest="run_id")
    validate.set_defaults(func=cmd_validate)

    rerun = sub.add_parser("rerun-failures", help="run only failures retained from an earlier run")
    rerun.add_argument("run_id")
    rerun.add_argument(
        "--notify",
        choices=("auto", "none", "crosstalk"),
        default=os.environ.get("AGENT_TEST_NOTIFY", "auto"),
    )
    rerun.set_defaults(func=cmd_rerun_failures)

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

    supervisor = sub.add_parser("supervisor", help="show the resident launch supervisor")
    supervisor.add_argument("--start", action="store_true")
    supervisor.set_defaults(func=cmd_supervisor)

    capacity = sub.add_parser("capacity", help="show machine-wide test and browser slots")
    capacity.set_defaults(func=cmd_capacity)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))
