"""Pytest hooks that retain structured evidence for Agent Test."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest


# A traced Python line can execute millions of times during a run.  Resolving
# the same source filename for every one of those lines turns coverage into a
# filesystem benchmark, especially for tests that reload a large module.
_RESOLVED: dict[tuple[str, str], Path | None] = {}


def _relative_source_path(filename: str, repo_raw: str) -> Path | None:
    """Resolve one source filename once per repository/run process."""
    key = (repo_raw, filename)
    if key not in _RESOLVED:
        try:
            path = Path(filename).resolve().relative_to(repo_raw)
        except (OSError, ValueError):
            path = None
        if path is not None and (
            path.suffix != ".py"
            or any(part in {".venv", "venv", "env"} for part in path.parts)
        ):
            path = None
        _RESOLVED[key] = path
    return _RESOLVED[key]


# The xdist controller receives every worker's reports as well; only the
# process that ran a test writes its passing output.
_XDIST_CONTROLLER = False


def pytest_configure(config) -> None:
    global _XDIST_CONTROLLER
    _XDIST_CONTROLLER = config.pluginmanager.hasplugin("dsession")


def _events_path() -> Path | None:
    raw = os.environ.get("AGENT_TEST_EVENTS_DIR", "").strip()
    if not raw:
        return None
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    return Path(raw) / f"events-{worker}.ndjson"


def _write(kind: str, **payload: Any) -> None:
    path = _events_path()
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"kind": kind, **payload}
    encoded = (json.dumps(record, sort_keys=True, default=str) + "\n").encode()
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(fd, encoded)
    finally:
        os.close(fd)


def pytest_collection_finish(session) -> None:
    _write(
        "collection",
        nodes=[item.nodeid for item in session.items],
        count=len(session.items),
    )


def _write_passing_output(report) -> None:
    """Append a passing test's captured output to the run's passes log.

    This replaces ``-rP``, which made the xdist controller format every
    passing test's output in one process after the last test had finished
    (about 190s of a 570s full sweep). Each worker now appends its own as it
    goes. Records are written whole with O_APPEND, so workers never
    interleave inside one.
    """
    events = _events_path()
    sections = [(name, text) for name, text in report.sections if text]
    if events is None or not sections:
        return
    parts = [f"{'_' * 20} {report.nodeid} [{report.when}] {'_' * 20}\n"]
    for name, text in sections:
        parts.append(f"{'-' * 10} {name} {'-' * 10}\n{text}")
        if not text.endswith("\n"):
            parts.append("\n")
    fd = os.open(events.parent.parent / "passes.log",
                 os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        os.write(fd, "".join(parts).encode("utf-8", "replace"))
    finally:
        os.close(fd)


def pytest_runtest_logreport(report) -> None:
    failed = report.outcome == "failed"
    if report.outcome == "passed" and not _XDIST_CONTROLLER:
        _write_passing_output(report)
    _write(
        "report",
        nodeid=report.nodeid,
        phase=report.when,
        outcome=report.outcome,
        duration=report.duration,
        longrepr=str(report.longrepr) if failed else "",
        stdout=getattr(report, "capstdout", "") if failed else "",
        stderr=getattr(report, "capstderr", "") if failed else "",
    )


def _start_monitoring(repo_raw: str, lines: dict[str, set[int]]) -> int | None:
    """Record executed lines through ``sys.monitoring`` (PEP 669).

    Each (code, line) reports once and is then disabled until the next test
    re-arms it with ``restart_events``, so a hot loop costs one callback, not
    one per iteration. ``sys.settrace`` called back on every executed line and
    roughly doubled test time. Monitoring is process-wide, so lines run on
    other threads (the app thread behind Starlette's TestClient) count too.
    Returns the tool id, or None when another tool (coverage.py) holds it.
    """
    monitoring = getattr(sys, "monitoring", None)
    if monitoring is None:
        return None
    tool = monitoring.COVERAGE_ID
    try:
        monitoring.use_tool_id(tool, "agent-test")
    except ValueError:
        return None
    disable = monitoring.DISABLE

    def line(code, line_number):
        path = _relative_source_path(code.co_filename, repo_raw)
        if path is not None:
            lines.setdefault(path.as_posix(), set()).add(line_number)
        return disable

    monitoring.register_callback(tool, monitoring.events.LINE, line)
    monitoring.set_events(tool, monitoring.events.LINE)
    monitoring.restart_events()
    return tool


def _stop_monitoring(tool: int) -> None:
    monitoring = sys.monitoring
    monitoring.set_events(tool, 0)
    monitoring.register_callback(tool, monitoring.events.LINE, None)
    monitoring.free_tool_id(tool)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    """Retain setup, call, and teardown lines without requiring pytest-cov."""
    del nextitem
    enabled = os.environ.get("AGENT_TEST_LINE_COVERAGE") == "1"
    repo_raw = os.environ.get("AGENT_TEST_REPO", "")
    previous = sys.gettrace()
    lines: dict[str, set[int]] = {}

    def trace(frame, event, _arg):
        if event == "line":
            path = _relative_source_path(frame.f_code.co_filename, repo_raw)
            if path is not None:
                lines.setdefault(path.as_posix(), set()).add(frame.f_lineno)
        return trace

    active = enabled and bool(repo_raw) and previous is None
    tool = _start_monitoring(repo_raw, lines) if active else None
    if active and tool is None:
        sys.settrace(trace)
    try:
        yield
    finally:
        if active:
            if tool is not None:
                _stop_monitoring(tool)
            else:
                sys.settrace(previous)
            _write(
                "line_coverage",
                nodeid=item.nodeid,
                files={name: sorted(numbers) for name, numbers in sorted(lines.items())},
            )


def pytest_sessionfinish(session, exitstatus) -> None:
    _write("session_finish", exitstatus=int(exitstatus))
