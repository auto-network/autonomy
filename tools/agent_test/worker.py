"""Detached Stage-1 run supervisor for Agent Test."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
from pathlib import Path
from typing import Any

from .store import atomic_write_json, read_json, update_manifest, utc_now


_stop_requested = False


def _handle_stop(_signum, _frame) -> None:
    global _stop_requested
    _stop_requested = True


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


def _notification_text(run_id: str, status: str, summary: dict[str, int], duration: float) -> str:
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


def _notify(directory: Path, manifest: dict[str, Any], text: str) -> None:
    mode = str(manifest.get("notify") or "auto")
    session = os.environ.get("AUTONOMY_SESSION", "").strip()
    auto_available = bool(session and os.environ.get("CROSSTALK_TOKEN") and shutil.which("graph"))
    if mode == "none" or (mode == "auto" and not auto_available):
        update_manifest(directory, {"notification": {"state": "not_requested", "text": text}})
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
        {"notification": {"state": state, "text": text, "detail": detail}},
    )


def run(directory: Path) -> int:
    manifest = read_json(directory / "run.json", {})
    run_id = str(manifest["run_id"])
    repo = Path(manifest["repo"])
    python = str(manifest["python"])
    selectors = [str(value) for value in manifest.get("selectors") or []]
    events_dir = directory / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    pytest_log = directory / "pytest.log"

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
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
        *selectors,
    ]
    update_manifest(directory, {"pytest_command": command, "pytest_log": str(pytest_log)})

    started = __import__("time").monotonic()
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
            exit_code = child.wait()
    except OSError as exc:
        launch_error = str(exc)

    duration = __import__("time").monotonic() - started
    events = _read_events(events_dir)
    summary, failures = _summarize(events)
    atomic_write_json(directory / "failures.json", failures)
    atomic_write_json(directory / "summary.json", summary)

    if _stop_requested or exit_code < 0:
        status = "stopped"
    elif launch_error:
        status = "error"
    elif exit_code == 0:
        status = "passed"
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
        },
    )
    text = _notification_text(run_id, status, summary, duration)
    _notify(directory, final, text)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    args = parser.parse_args(argv)
    return run(Path(args.run_dir).resolve())


if __name__ == "__main__":
    raise SystemExit(main())
