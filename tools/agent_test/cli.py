"""Bounded, asynchronous command-line interface for Agent Test."""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .environment import (
    choose_python,
    profiles,
    pytest_parallelism,
    requested_resources,
    workspace_fingerprint,
)
from .lease_client import duration_request, error_request, lease_request, telemetry_request
from .planning import build_plan, changed_coverage, changed_lines
from .store import (
    atomic_write_json,
    guidance_first,
    list_manifests,
    live_manifest,
    manifest_path,
    new_run_id,
    read_json,
    repository_root,
    retained_root,
    resolve_manifest,
    run_dir,
    state_lock,
    state_root,
    update_manifest,
    utc_now,
)
from .supervisor import start_worker, supervisor_status
from .timing import format_estimate, repository_identity


DEFAULT_FAILURE_LIMIT = 5
DEFAULT_OUTPUT_LINES = 80
MAX_QUERY_LIMIT = 200


def _root() -> tuple[Path, Path]:
    repo = repository_root()
    root = state_root(repo)
    root.mkdir(parents=True, exist_ok=True)
    # The default is a hidden per-session directory under /tmp. Keep the
    # evidence owner-only even on hosts whose temporary directory is shared.
    root.chmod(0o700)
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


def _progress_line(manifest: dict[str, Any]) -> str | None:
    if manifest.get("status") not in {"starting", "queued", "running", "stopping"}:
        return None
    progress = manifest.get("progress") or {}
    if not isinstance(progress, dict):
        return None
    status = str(progress.get("status") or "")
    if status == "collecting" or not int(progress.get("total") or 0):
        return "Progress: —% (test inventory not complete)."
    completed = int(progress.get("completed") or 0)
    total = int(progress.get("total") or 0)
    percent = max(0, min(100, int(progress.get("percent") or 0)))
    return f"Progress: {percent}% ({completed}/{total} test nodes)."


def _duration_estimate(repository: str, selectors: list[str], parallelism: int = 1) -> dict[str, Any]:
    return duration_request(
        "estimate",
        repository=repository,
        selectors=selectors,
        parallelism=max(1, parallelism),
    )


def _estimate_text(estimate: dict[str, Any]) -> str | None:
    if not estimate.get("ok"):
        return None
    seconds = estimate.get("estimated_seconds")
    if not isinstance(seconds, (int, float)):
        return "Estimated test time: unknown; no retained timing history matches this selection."
    sampled = int(estimate.get("sampled_tests") or 0)
    samples = int(estimate.get("sample_count") or 0)
    parallelism = int(estimate.get("effective_parallelism") or estimate.get("parallelism") or 1)
    suffix = f" across {parallelism} workers" if parallelism > 1 else ""
    unknown = len(estimate.get("unknown_selectors") or [])
    open_ended = len(estimate.get("open_ended_selectors") or [])
    low = estimate.get("estimated_low_seconds")
    high = estimate.get("estimated_high_seconds")
    range_text = (
        f"; observed range ~{format_estimate(float(low))}–{format_estimate(float(high))}"
        if isinstance(low, (int, float)) and isinstance(high, (int, float)) else ""
    )
    if unknown or open_ended:
        incomplete = []
        if unknown:
            incomplete.append(f"{unknown} selector(s) have no history")
        if open_ended:
            incomplete.append(f"{open_ended} broad selector(s) may collect unseen tests")
        return (
            f"Known-history floor: ~{format_estimate(float(seconds))}{suffix}, "
            f"from {samples} retained sample(s) across {sampled} test(s){range_text}; "
            f"{' and '.join(incomplete)}, so total ETA is unknown."
        )
    return (
        f"Estimated test time: ~{format_estimate(float(seconds))}{suffix}, "
        f"from {samples} retained sample(s) across {sampled} test(s){range_text}."
    )


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
    plan = None
    if getattr(args, "changed", False):
        plan = build_plan(repo, root)
        if not plan["auto_selectable"]:
            print(
                f"Agent Test: diff plan produced {len(plan['recommendations'])} selectors, "
                "above the automatic safety limit of 50.",
                file=sys.stderr,
            )
            print("Use a named profile or explicit selectors.", file=sys.stderr)
            return 2
        selectors.extend(item["selector"] for item in plan["recommendations"])
    selectors = list(dict.fromkeys(selectors))
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
    requested_line_coverage = getattr(args, "line_coverage", None)
    line_coverage = (
        False
        if mode == "collect"
        else bool(profile.get("line_coverage", True))
        if requested_line_coverage is None
        else bool(requested_line_coverage)
    )
    try:
        resources = getattr(args, "resource_override", None) or requested_resources(repo, profile, mode)
    except ValueError as exc:
        print(f"Agent Test: {exc}", file=sys.stderr)
        return 2
    repository = repository_identity(repo)
    parallelism = pytest_parallelism(repo, profile)
    estimate = _duration_estimate(repository, selectors, parallelism)
    fingerprint = workspace_fingerprint(
        repo, [mode, python, f"coverage={line_coverage}", *profile_args, *selectors]
    )
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
                telemetry_request(event="repeat_refused")
                return 4
        if not getattr(args, "force", False):
            failed_selection = next(
                (
                    item
                    for item in list_manifests(root)
                    if item.get("status") == "failed"
                    and list(item.get("selectors") or []) == selectors
                ),
                None,
            )
            if failed_selection is not None:
                print(
                    "Agent Test: broad selection previously failed; refusing to rerun "
                    f"the same {len(selectors)} selector(s) unchanged in scope.",
                    file=sys.stderr,
                )
                print(
                    f"Run only the failures: agent-test rerun-failures {failed_selection['run_id']}",
                    file=sys.stderr,
                )
                print("To intentionally rerun the broad selection: add --force.", file=sys.stderr)
                telemetry_request(event="broad_recovery_refused")
                return 4
        run_id = new_run_id()
        estimate_text = _estimate_text(estimate)
        if estimate_text:
            print(estimate_text, flush=True)
        directory = run_dir(root, run_id)
        directory.mkdir(parents=True, exist_ok=False)
        manifest = {
            "schema": 1,
            "agent_test_version": __version__,
            "run_id": run_id,
            "status": "starting",
            "created_at": utc_now(),
            "repo": str(repo),
            "repository": repository,
            "selectors": selectors,
            "pytest_args": profile_args,
            "profile": args.profile,
            "mode": mode,
            "fingerprint": fingerprint,
            "python": python,
            "notify": args.notify,
            "resources": resources,
            "parallelism": parallelism,
            "line_coverage": line_coverage,
            "changed_lines": (plan or {}).get("changed_lines") or changed_lines(repo),
            "duration_estimate": {
                key: estimate.get(key)
                for key in (
                    "estimated_seconds",
                    "estimated_low_seconds",
                    "estimated_high_seconds",
                    "serial_seconds",
                    "parallelism",
                    "effective_parallelism",
                    "sampled_tests",
                    "sample_count",
                    "unknown_selectors",
                    "open_ended_selectors",
                    "known_selector_count",
                    "requested_selector_count",
                    "selector_history_coverage",
                    "estimate_complete",
                )
                if key in estimate
            },
        }
        if plan is not None:
            manifest["plan"] = {
                "recommendations": plan["recommendations"],
                "gaps": plan["gaps"],
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
                        [sys.executable, "-m", f"{__package__}.worker", "--run-dir", str(directory)],
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
            error_request("launch", "supervisor_start", str(exc), run_id=run_id)
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
        telemetry_request(event="run_started")

    selected = len(selectors)
    if guidance_first(root, "stage1-background-run-v1"):
        print(f"Started {run_id} in the background: {selected} selector(s).")
        print("Agent Test will notify this session when it finishes. Keep working; do not poll or start another run.")
        print(f"Temporary evidence is available through: agent-test show {run_id}")
        print(f"Keep it after this session only if needed: agent-test retain {run_id}")
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
        line_coverage=original.get("line_coverage", True),
        changed=False,
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


def cmd_plan(args: argparse.Namespace) -> int:
    repo, root = _root()
    plan = build_plan(repo, root)
    recommendations = plan["recommendations"]
    print(
        f"Agent Test plan · {plan['changed_files']} changed Python file(s) · "
        f"{len(recommendations)} recommended selector(s)"
    )
    selectors = [item["selector"] for item in recommendations]
    estimate = _duration_estimate(repository_identity(repo), selectors)
    estimate_text = _estimate_text(estimate)
    if estimate_text:
        print(estimate_text)
    limit = _bounded_limit(args.limit)
    for item in recommendations[:limit]:
        print(f"- {item['selector']} · confidence {item['confidence']} · {item['reasons'][0]}")
    if len(recommendations) > limit:
        print(f"[{len(recommendations) - limit} more recommendations omitted; use --limit N]")
    if plan["gaps"]:
        gaps = plan["gaps"][:10]
        print(f"Coverage-history gaps ({len(gaps)} of {len(plan['gaps'])}):")
        for name in gaps:
            print(f"- {name}")
    if not plan["auto_selectable"]:
        print("Automatic execution refused: more than 50 selectors require a named profile.")
        return 2
    if not recommendations:
        print("No defensible test selection was found; add a named profile or explicit selector.")
        return 2
    print("Run this retained plan asynchronously with: agent-test run --changed")
    return 0


def cmd_coverage(args: argparse.Namespace) -> int:
    _repo, root = _root()
    manifest = _manifest_or_error(root, args.run_id)
    if manifest is None:
        return 2
    summary = changed_coverage(manifest)
    if summary["percent"] is None:
        print(f"{manifest['run_id']}: no changed Python lines were recorded at launch.")
        return 0
    print(
        f"{manifest['run_id']} · changed-line coverage {summary['covered']}/{summary['changed']} "
        f"({summary['percent']:.1f}%)"
    )
    limit = _bounded_limit(args.limit)
    for item in summary["files"][:limit]:
        line = f"- {item['file']}: {item['covered']}/{item['changed']}"
        if item["missing"]:
            missing = ",".join(str(value) for value in item["missing"][:20])
            line += f" · missing lines {missing}"
            if len(item["missing"]) > 20:
                line += f" (+{len(item['missing']) - 20} retained)"
        print(line)
    if len(summary["files"]) > limit:
        print(f"[{len(summary['files']) - limit} more changed files omitted; use --limit N]")
    return 0


def cmd_metrics(args: argparse.Namespace) -> int:
    result = telemetry_request(action="status")
    if not result.get("ok"):
        print(f"Agent Test: telemetry unavailable: {result.get('error', 'unknown')}", file=sys.stderr)
        return 2
    counts = result.get("counts") or {}
    print(f"Machine-wide Agent Test telemetry · {result.get('sessions', 0)} session(s)")
    for name, amount in sorted(counts.items())[:20]:
        print(f"- {name}: {amount}")
    if len(counts) > 20:
        print(f"[{len(counts) - 20} more event types omitted]")
    return 0


def cmd_timings(args: argparse.Namespace) -> int:
    repo, _root_path = _root()
    repository = repository_identity(repo)
    estimate = _duration_estimate(repository, args.selectors)
    if not estimate.get("ok"):
        print(
            f"Agent Test: timing history unavailable: {estimate.get('error', 'unknown')}",
            file=sys.stderr,
        )
        return 2
    estimate_text = _estimate_text(estimate)
    if estimate_text:
        print(estimate_text)
    history = duration_request(
        "history",
        repository=repository,
        selectors=args.selectors,
        limit_tests=_bounded_limit(args.limit_tests),
    )
    if not history.get("ok"):
        print(
            f"Agent Test: timing history unavailable: {history.get('error', 'unknown')}",
            file=sys.stderr,
        )
        return 2
    tests = history.get("tests") or []
    print(
        f"Timing history · {len(tests)} of {history.get('matched_tests', 0)} matching test(s) · "
        f"latest {history.get('history_limit', 10)} retained per test"
    )
    sample_limit = max(1, min(int(args.samples), 10))
    for item in tests:
        observations = (item.get("observations") or [])[:sample_limit]
        durations = [
            float(observation["duration_seconds"])
            for observation in item.get("observations") or []
        ]
        minimum = float(item.get(
            "minimum_seconds", min(durations) if durations else item["median_seconds"],
        ))
        maximum = float(item.get(
            "maximum_seconds", max(durations) if durations else item["median_seconds"],
        ))
        print(
            f"- {item['nodeid']} · median {format_estimate(float(item['median_seconds']))} "
            f"· range {format_estimate(minimum)}–"
            f"{format_estimate(maximum)} "
            f"· {len(item.get('observations') or [])} sample(s)"
        )
        recent = ", ".join(
            f"{format_estimate(float(observation['duration_seconds']))} {observation['outcome']}"
            for observation in observations
        )
        print(f"  recent: {recent}")
    omitted = int(history.get("omitted_tests") or 0)
    if omitted:
        print(f"[{omitted} more matching tests omitted; use --limit-tests N deliberately]")
    return 0


def cmd_baseline(args: argparse.Namespace) -> int:
    _repo, root = _root()
    manifest = _manifest_or_error(root, args.run_id)
    if manifest is None:
        return 2
    if manifest.get("status") in {"starting", "queued", "running", "stopping"}:
        print("Agent Test: a live run cannot become a baseline.", file=sys.stderr)
        return 2
    failure_nodes = list(
        dict.fromkeys(
            str(item.get("nodeid"))
            for item in _failures(manifest)
            if item.get("nodeid")
        )
    )
    payload = {
        "schema": 1,
        "published_at": utc_now(),
        "run_id": manifest["run_id"],
        "fingerprint": manifest.get("fingerprint"),
        "status": manifest.get("status"),
        "failures": failure_nodes,
        "summary": manifest.get("summary") or {},
    }
    atomic_write_json(root / "baseline.json", payload)
    print(
        f"Published baseline {manifest['run_id']}: {len(failure_nodes)} known failure node(s)."
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
        progress_line = _progress_line(manifest)
        if progress_line:
            print(progress_line)
        estimate_data = manifest.get("duration_estimate") or {}
        estimate = estimate_data.get("estimated_seconds")
        estimate_low = estimate_data.get("estimated_low_seconds")
        estimate_high = estimate_data.get("estimated_high_seconds")
        unknown = len(estimate_data.get("unknown_selectors") or [])
        open_ended = len(estimate_data.get("open_ended_selectors") or [])
        started_at = manifest.get("started_at")
        if (unknown or open_ended) and isinstance(estimate, (int, float)) and estimate > 0:
            floor = estimate_low if isinstance(estimate_low, (int, float)) else estimate
            reasons = []
            if unknown:
                reasons.append(f"{unknown} selector(s) have no timing history")
            if open_ended:
                reasons.append(f"{open_ended} broad selector(s) may collect unseen tests")
            print(
                f"ETA unknown: observed known work is at least ~{format_estimate(float(floor))}; "
                f"{' and '.join(reasons)}."
            )
        elif manifest.get("status") == "running" and isinstance(estimate, (int, float)) and estimate > 0 and started_at:
            try:
                from datetime import datetime, timezone
                elapsed = max(
                    0.0,
                    (datetime.now(timezone.utc) - datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))).total_seconds(),
                )
            except (TypeError, ValueError):
                elapsed = 0.0
            remaining = float(estimate) - elapsed
            if remaining > 0:
                range_text = ""
                if isinstance(estimate_low, (int, float)) and isinstance(estimate_high, (int, float)):
                    low_remaining = max(0.0, float(estimate_low) - elapsed)
                    high_remaining = max(0.0, float(estimate_high) - elapsed)
                    range_text = (
                        f" Observed remaining range: {format_estimate(low_remaining)}–"
                        f"{format_estimate(high_remaining)}."
                    )
                print(
                    f"ETA: about {format_estimate(remaining)} remaining "
                    f"({format_estimate(elapsed)} elapsed of ~{format_estimate(float(estimate))})."
                    f"{range_text}"
                )
            else:
                print(
                    f"ETA: original ~{format_estimate(float(estimate))} estimate exceeded; "
                    f"{format_estimate(elapsed)} elapsed. Completion notification is still authoritative."
                )
        elif manifest.get("status") == "queued":
            lease = manifest.get("machine_lease") or {}
            position = lease.get("queue_position")
            depth = lease.get("queue_depth")
            wait_low = lease.get("estimated_wait_low_seconds")
            wait_high = lease.get("estimated_wait_high_seconds")
            queue_text = f" Queue position {position} of {depth}." if position and depth else ""
            wait_text = ""
            if isinstance(wait_low, (int, float)) and isinstance(wait_high, (int, float)):
                wait_text = f" Estimated wait: ~{format_estimate(float(wait_low))}–{format_estimate(float(wait_high))}."
            elif isinstance(lease.get("estimated_wait_seconds"), (int, float)):
                wait_text = f" Estimated wait: ~{format_estimate(float(lease['estimated_wait_seconds']))}."
            estimate_text = (
                f" Estimated runtime after admission: ~{format_estimate(float(estimate))}."
                if isinstance(estimate, (int, float)) and estimate > 0 else ""
            )
            print(f"Waiting for machine capacity.{queue_text}{wait_text}{estimate_text}")
        else:
            print("ETA unknown: matching timing history is not available yet.")
        print("The run is supervised in the background; completion will be delivered automatically. Do not poll status again.")
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


def cmd_retain(args: argparse.Namespace) -> int:
    """Copy one completed run from temporary state into workspace output."""
    _repo, root = _root()
    manifest = _manifest_or_error(root, args.run_id)
    if manifest is None:
        return 2
    if manifest.get("status") in {"starting", "queued", "running", "stopping"}:
        print("Agent Test: a live run cannot be retained; wait for its completion notification.", file=sys.stderr)
        return 3
    source = Path(manifest["_directory"])
    destination = run_dir(retained_root(root), str(manifest["run_id"]))
    if source.resolve() == destination.resolve() or destination.exists():
        print(f"Agent Test: {manifest['run_id']} is already retained at {destination}")
        return 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copytree(source, destination)
        update_manifest(
            destination,
            {
                "retained_at": utc_now(),
                "evidence_lifecycle": "durable",
            },
        )
    except OSError as exc:
        error_request("retention", "copy_failed", str(exc), run_id=str(manifest["run_id"]))
        print(f"Agent Test: could not retain run: {exc}", file=sys.stderr)
        return 2
    print(f"Retained {manifest['run_id']} at {destination}")
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
    run.add_argument(
        "--force",
        action="store_true",
        help="allow a broad selection after a previous run with the same selectors failed",
    )
    run.add_argument("--profile")
    run.add_argument(
        "--changed",
        action="store_true",
        help="run the bounded test plan inferred from the current Python diff",
    )
    run.add_argument(
        "--coverage",
        dest="line_coverage",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="retain changed-line coverage (enabled by default)",
    )
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

    plan = sub.add_parser("plan", help="recommend tests for the current Python diff")
    plan.add_argument("--limit", type=int, default=10)
    plan.set_defaults(func=cmd_plan)

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

    coverage = sub.add_parser("coverage", help="show retained changed-line coverage")
    coverage.add_argument("run_id", nargs="?")
    coverage.add_argument("--limit", type=int, default=20)
    coverage.set_defaults(func=cmd_coverage)

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

    retain = sub.add_parser("retain", help="copy one completed run into durable workspace output")
    retain.add_argument("run_id", nargs="?")
    retain.set_defaults(func=cmd_retain)

    stop = sub.add_parser("stop", help="stop only the owned live process group")
    stop.set_defaults(func=cmd_stop)

    supervisor = sub.add_parser("supervisor", help="show the resident launch supervisor")
    supervisor.add_argument("--start", action="store_true")
    supervisor.set_defaults(func=cmd_supervisor)

    capacity = sub.add_parser("capacity", help="show machine-wide test and browser slots")
    capacity.set_defaults(func=cmd_capacity)

    metrics = sub.add_parser("metrics", help="show bounded machine-wide Agent Test usage")
    metrics.set_defaults(func=cmd_metrics)

    timings = sub.add_parser("timings", help="show capped per-test duration history and estimate")
    timings.add_argument("selectors", nargs="+")
    timings.add_argument("--limit-tests", type=int, default=5)
    timings.add_argument("--samples", type=int, default=10)
    timings.set_defaults(func=cmd_timings)

    baseline = sub.add_parser("baseline", help="publish a retained run as the workspace baseline")
    baseline.add_argument("run_id", nargs="?")
    baseline.set_defaults(func=cmd_baseline)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = str(args.command).replace("-", "_")
    if command == "run":
        if getattr(args, "changed", False):
            command = "run_changed"
        elif getattr(args, "profile", None):
            command = "run_profile"
        else:
            command = "run_explicit"
    telemetry_request(event=f"command_{command}"[:32])
    return int(args.func(args))
