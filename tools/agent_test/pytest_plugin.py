"""Pytest hooks that retain structured evidence for Agent Test."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest


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


def pytest_runtest_logreport(report) -> None:
    failed = report.outcome == "failed"
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
            try:
                path = Path(frame.f_code.co_filename).resolve().relative_to(repo_raw)
            except (OSError, ValueError):
                return trace
            if path.suffix == ".py" and not any(part in {".venv", "venv", "env"} for part in path.parts):
                lines.setdefault(path.as_posix(), set()).add(frame.f_lineno)
        return trace

    if enabled and repo_raw and previous is None:
        sys.settrace(trace)
    try:
        yield
    finally:
        if enabled and repo_raw and previous is None:
            sys.settrace(previous)
            _write(
                "line_coverage",
                nodeid=item.nodeid,
                files={name: sorted(numbers) for name, numbers in sorted(lines.items())},
            )


def pytest_sessionfinish(session, exitstatus) -> None:
    _write("session_finish", exitstatus=int(exitstatus))
