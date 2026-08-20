"""Detached Stage-1 run supervisor for Agent Test."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .environment import project_config
from .lease_client import lease_request, telemetry_request
from .store import atomic_write_json, read_json, update_manifest, utc_now


_stop_requested = False


def _handle_stop(_signum, _frame) -> None:
    global _stop_requested
    _stop_requested = True


def _acquire_machine_lease(directory: Path, manifest: dict[str, Any]) -> tuple[str | None, str | None]:
    mode = os.environ.get("AGENT_TEST_MACHINE_LEASE", "auto")
    session = os.environ.get("AUTONOMY_SESSION", "").strip()
    if mode == "none" or not session:
        update_manifest(directory, {"machine_lease": {"state": "not_requested"}})
        return None, None
    run_id = str(manifest["run_id"])
    lease_id = f"{session}:{run_id}"
    resources = manifest.get("resources") or {"tests": 1}
    while not _stop_requested:
        result = lease_request(
            "acquire",
            lease_id=lease_id,
            session=session,
            run_id=run_id,
            resources=resources,
        )
        if result.get("ok") and result.get("state") == "granted":
            update_manifest(
                directory,
                {
                    "machine_lease": {
                        "state": "granted",
                        "lease_id": lease_id,
                        "resources": resources,
                        "expires_at": result.get("expires_at"),
                    }
                },
            )
            return lease_id, None
        if result.get("ok") and result.get("state") == "queued":
            update_manifest(
                directory,
                {
                    "status": "queued",
                    "machine_lease": {
                        "state": "queued",
                        "lease_id": lease_id,
                        "resources": resources,
                        "unavailable": result.get("unavailable"),
                    },
                },
            )
            time.sleep(2)
            continue
        if result.get("unavailable") and mode == "auto":
            update_manifest(
                directory,
                {
                    "status": "queued",
                    "machine_lease": {
                        "state": "coordinator_unavailable",
                        "lease_id": lease_id,
                        "resources": resources,
                        "reason": result.get("error", "dashboard unavailable"),
                    },
                },
            )
            time.sleep(2)
            continue
        return None, str(result.get("error") or "machine capacity request failed")
    return None, "stopped while waiting for machine capacity"


def _read_events(events_dir: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for path in sorted(events_dir.glob("events-*.ndjson")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                events.append(value)
    return events


def _summarize(events: list[dict[str, Any]]) -> tuple[dict[str, int], list[dict[str, Any]]]:
    reports: dict[tuple[str, str], dict[str, Any]] = {}
    collected: set[str] = set()
    for event in events:
        if event.get("kind") == "collection":
            collected.update(event.get("nodes") or [])
        elif event.get("kind") == "report":
            key = (str(event.get("nodeid", "")), str(event.get("phase", "")))
            reports[key] = event

    failures: list[dict[str, Any]] = []
    passed: set[str] = set()
    skipped: set[str] = set()
    for (nodeid, phase), report in reports.items():
        outcome = report.get("outcome")
        if outcome == "failed":
            longrepr = str(report.get("longrepr") or "")
            message = next(
                (line.strip() for line in reversed(longrepr.splitlines()) if line.strip()),
                "pytest failure",
            )
            failures.append(
                {
                    "nodeid": nodeid,
                    "phase": phase,
                    "message": message[:500],
                    "traceback": longrepr,
                    "stdout": report.get("stdout") or "",
                    "stderr": report.get("stderr") or "",
                }
            )
        elif phase == "call" and outcome == "passed":
            passed.add(nodeid)
        elif outcome == "skipped":
            skipped.add(nodeid)

    failures.sort(key=lambda item: (item["nodeid"], item["phase"]))
    summary = {
        "collected": len(collected),
        "passed": len(passed),
        "failed": sum(1 for item in failures if item["phase"] == "call"),
        "errors": sum(1 for item in failures if item["phase"] != "call"),
        "skipped": len(skipped - passed),
    }
    return summary, failures


def _collection(events: list[dict[str, Any]]) -> list[str]:
    nodes: set[str] = set()
    for event in events:
        if event.get("kind") == "collection":
            nodes.update(str(node) for node in event.get("nodes") or [])
    return sorted(nodes)


def _coverage(events: list[dict[str, Any]]) -> dict[str, Any]:
    tests: dict[str, dict[str, list[int]]] = {}
    files: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("kind") != "line_coverage":
            continue
        nodeid = str(event.get("nodeid") or "")
        event_files = event.get("files") or {}
        if not nodeid or not isinstance(event_files, dict):
            continue
        tests[nodeid] = {}
        for name, raw_lines in event_files.items():
            lines = sorted({int(value) for value in raw_lines if isinstance(value, int)})
            tests[nodeid][str(name)] = lines
            record = files.setdefault(str(name), {"lines": set(), "tests": set()})
            record["lines"].update(lines)
            record["tests"].add(nodeid)
    serialized_files = {
        name: {"lines": sorted(value["lines"]), "tests": sorted(value["tests"])}
        for name, value in sorted(files.items())
    }
    return {"files": serialized_files, "tests": tests}


def _quarantine_nodes(repo: Path) -> set[str]:
    configured = project_config(repo).get("quarantine")
    candidates = []
    if isinstance(configured, str) and configured.strip():
        candidates.append(repo / configured)
    candidates.extend(sorted((repo / "tests").glob("quarantine_baseline_*.txt")))
    for path in candidates:
        try:
            return {
                line.strip()
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            }
        except OSError:
            continue
    return set()


def _classify_failures(directory: Path, repo: Path, failures: list[dict[str, Any]]) -> dict[str, int]:
    known: set[str] = set()
    baseline = read_json(directory.parent.parent / "baseline.json", {})
    if isinstance(baseline, dict):
        known.update(str(value) for value in baseline.get("failures") or [])
    for path in (directory.parent).glob("*/failures.json"):
        if path.parent == directory:
            continue
        previous = read_json(path, [])
        if isinstance(previous, list):
            known.update(str(item.get("nodeid")) for item in previous if item.get("nodeid"))
    quarantine = _quarantine_nodes(repo)
    counts = {"new_failures": 0, "known_failures": 0, "quarantined_failures": 0}
    for failure in failures:
        nodeid = str(failure.get("nodeid") or "")
        base = nodeid.split("[")[0]
        if nodeid in quarantine or base in quarantine:
            classification = "quarantined"
        elif nodeid in known:
            classification = "known"
        else:
            classification = "new"
        failure["classification"] = classification
        counts[f"{classification}_failures"] += 1
    return counts


def _notification_text(run_id: str, status: str, summary: dict[str, int], duration: float) -> str:
    if status == "collected":
        return (
            f"Agent Test {run_id} collected {summary['collected']} test nodes in {duration:.1f}s. "
            f"Inspect them with `agent-test inventory {run_id}`."
        )
    if status == "passed":
        return (
            f"Agent Test {run_id} passed: {summary['passed']} passed in {duration:.1f}s. "
            "Evidence retained; no verification rerun is needed."
        )
    if status == "failed":
        total = summary["failed"] + summary["errors"]
        return (
            f"Agent Test {run_id} failed: {total} failure(s), {summary['passed']} passed "
            f"in {duration:.1f}s. Full traces are retained. Next: "
            f"`agent-test failures {run_id}`. Do not rerun unchanged."
        )
    return (
        f"Agent Test {run_id} ended with status {status}; no passing result was recorded. "
        f"Inspect with `agent-test show {run_id}`."
    )


def _dashboard_notify(session: str, run_id: str, status: str, text: str) -> tuple[bool, str]:
    base = os.environ.get("AGENT_TEST_DASHBOARD", "https://localhost:8080").rstrip("/")
    payload = json.dumps(
        {
            "tmux_session": session,
            "notification_id": f"agent-test:{run_id}",
            "kind": "agent-test",
            "status": status,
            "summary": text,
        }
    ).encode()
    request = urllib.request.Request(
        f"{base}/api/session/notify",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    context = ssl._create_unverified_context() if base.startswith("https://") else None
    try:
        with urllib.request.urlopen(request, timeout=10, context=context) as response:
            body = response.read().decode(errors="replace")
            return response.status < 300, body[:1000]
    except (OSError, urllib.error.URLError) as exc:
        return False, str(exc)[:1000]


def _notify(directory: Path, manifest: dict[str, Any], text: str) -> None:
    mode = str(manifest.get("notify") or "auto")
    session = os.environ.get("AUTONOMY_SESSION", "").strip()
    if mode == "none":
        update_manifest(directory, {"notification": {"state": "not_requested", "text": text}})
        return
    if mode == "auto" and session:
        delivered, detail = _dashboard_notify(
            session,
            str(manifest["run_id"]),
            str(manifest.get("status") or "complete"),
            text,
        )
        if delivered:
            update_manifest(
                directory,
                {"notification": {"state": "delivered", "transport": "dashboard", "text": text, "detail": detail}},
            )
            return
    auto_available = bool(session and os.environ.get("CROSSTALK_TOKEN") and shutil.which("graph"))
    if mode == "auto" and not auto_available:
        update_manifest(directory, {"notification": {"state": "unavailable", "text": text}})
        return
    if not session or not shutil.which("graph"):
        update_manifest(directory, {"notification": {"state": "unavailable", "text": text}})
        return
    try:
        result = subprocess.run(
            ["graph", "crosstalk", "send", session, text],
            capture_output=True,
            text=True,
            timeout=30,
        )
        state = "delivered" if result.returncode == 0 else "failed"
        detail = (result.stderr or result.stdout).strip()[:1000]
    except (OSError, subprocess.SubprocessError) as exc:
        state = "failed"
        detail = str(exc)
    update_manifest(
        directory,
        {"notification": {"state": state, "transport": "crosstalk", "text": text, "detail": detail}},
    )


def run(directory: Path) -> int:
    manifest = read_json(directory / "run.json", {})
    run_id = str(manifest["run_id"])
    repo = Path(manifest["repo"])
    python = str(manifest["python"])
    selectors = [str(value) for value in manifest.get("selectors") or []]
    pytest_args = [str(value) for value in manifest.get("pytest_args") or []]
    mode = str(manifest.get("mode") or "run")
    events_dir = directory / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    pytest_log = directory / "pytest.log"

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    lease_id, lease_error = _acquire_machine_lease(directory, manifest)
    if lease_error:
        status = "stopped" if _stop_requested else "error"
        summary = {
            "collected": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0,
            "new_failures": 0, "known_failures": 0, "quarantined_failures": 0,
        }
        atomic_write_json(directory / "failures.json", [])
        atomic_write_json(directory / "summary.json", summary)
        atomic_write_json(directory / "collection.json", [])
        final = update_manifest(
            directory,
            {"status": status, "finished_at": utc_now(), "summary": summary, "message": lease_error},
        )
        _notify(directory, final, _notification_text(run_id, status, summary, 0))
        telemetry_request(event=f"run_{status}")
        return 0
    update_manifest(
        directory,
        {"status": "running", "started_at": utc_now(), "worker_pid": os.getpid()},
    )

    package_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    current_path = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(package_root), current_path) if part
    )
    env["AGENT_TEST_EVENTS_DIR"] = str(events_dir)
    env["AGENT_TEST_INTERNAL"] = "1"
    env["AGENT_TEST_REPO"] = str(repo)
    env["AGENT_TEST_LINE_COVERAGE"] = "1" if manifest.get("line_coverage") else "0"
    # Pytest normally suppresses captured output for passing tests. ``-rP``
    # writes that output to the retained log without streaming it into the
    # agent's context, so a successful run does not discard useful evidence.
    command = [
        python,
        "-m",
        "pytest",
        "-rP",
        "-p",
        "tools.agent_test.pytest_plugin",
        *pytest_args,
        *(["--collect-only"] if mode == "collect" else []),
        *selectors,
    ]
    update_manifest(directory, {"pytest_command": command, "pytest_log": str(pytest_log)})

    started = time.monotonic()
    exit_code = 3
    launch_error = ""
    try:
        with pytest_log.open("wb") as log_handle:
            child = subprocess.Popen(
                command,
                cwd=repo,
                env=env,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
            )
            update_manifest(directory, {"pytest_pid": child.pid})
            renew_failures = 0
            while True:
                try:
                    exit_code = child.wait(timeout=20)
                    break
                except subprocess.TimeoutExpired:
                    if lease_id:
                        renewed = lease_request("renew", lease_id=lease_id)
                        renew_failures = 0 if renewed.get("ok") else renew_failures + 1
                        update_manifest(
                            directory,
                            {
                                "machine_lease": {
                                    "state": renewed.get("state", "renew_failed"),
                                    "lease_id": lease_id,
                                    "resources": manifest.get("resources") or {"tests": 1},
                                    "expires_at": renewed.get("expires_at"),
                                    "detail": renewed.get("error"),
                                }
                            },
                        )
                        if renewed.get("state") == "expired" or renew_failures >= 3:
                            launch_error = "machine-capacity lease could not be renewed safely"
                            os.killpg(os.getpid(), signal.SIGTERM)
    except OSError as exc:
        launch_error = str(exc)
    finally:
        if lease_id:
            released = lease_request("release", lease_id=lease_id)
            update_manifest(
                directory,
                {
                    "machine_lease": {
                        "state": released.get("state", "release_failed"),
                        "lease_id": lease_id,
                        "resources": manifest.get("resources") or {"tests": 1},
                        "detail": released.get("error"),
                    }
                },
            )

    duration = time.monotonic() - started
    events = _read_events(events_dir)
    summary, failures = _summarize(events)
    collection = _collection(events)
    coverage = _coverage(events)
    summary["collected"] = len(collection)
    summary.update(_classify_failures(directory, repo, failures))
    atomic_write_json(directory / "failures.json", failures)
    atomic_write_json(directory / "summary.json", summary)
    atomic_write_json(directory / "collection.json", collection)
    atomic_write_json(directory / "coverage.json", coverage)

    if _stop_requested or exit_code < 0:
        status = "stopped"
    elif launch_error:
        status = "error"
    elif exit_code == 0:
        status = "collected" if mode == "collect" else "passed"
    elif exit_code == 1:
        status = "failed"
    else:
        status = "error"

    final = update_manifest(
        directory,
        {
            "status": status,
            "finished_at": utc_now(),
            "duration_seconds": duration,
            "exit_code": exit_code,
            "summary": summary,
            "message": launch_error,
            "failures_path": str(directory / "failures.json"),
            "events_dir": str(events_dir),
            "collection_path": str(directory / "collection.json"),
            "coverage_path": str(directory / "coverage.json"),
        },
    )
    text = _notification_text(run_id, status, summary, duration)
    _notify(directory, final, text)
    telemetry_request(event=f"run_{status}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    args = parser.parse_args(argv)
    return run(Path(args.run_dir).resolve())


if __name__ == "__main__":
    raise SystemExit(main())
