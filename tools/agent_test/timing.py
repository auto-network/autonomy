"""Stable repository identity and per-node timing aggregation."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


def repository_identity(repo: Path) -> str:
    """Return a stable, credential-free identity shared by worktrees."""
    try:
        result = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=2,
        )
        remote = result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        remote = ""
    if not remote:
        return f"local:{repo.name}"
    if "://" in remote:
        parsed = urlsplit(remote)
        host = (parsed.hostname or "").lower()
        path = parsed.path.strip("/")
        identity = f"{host}/{path}" if host else path
    elif ":" in remote:
        host_part, path = remote.split(":", 1)
        identity = f"{host_part.rsplit('@', 1)[-1].lower()}/{path.strip('/')}"
    else:
        identity = remote
    return identity.removesuffix(".git")[:1000]


def aggregate_test_durations(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sum setup/call/teardown reports into one observation per test node."""
    reports: dict[tuple[str, str], dict[str, Any]] = {}
    for event in events:
        if event.get("kind") != "report":
            continue
        nodeid = str(event.get("nodeid") or "")
        phase = str(event.get("phase") or "")
        if nodeid and phase in {"setup", "call", "teardown"}:
            reports[(nodeid, phase)] = event

    by_node: dict[str, list[dict[str, Any]]] = {}
    for (nodeid, _phase), report in reports.items():
        by_node.setdefault(nodeid, []).append(report)
    observations: list[dict[str, Any]] = []
    for nodeid, node_reports in sorted(by_node.items()):
        outcomes = {str(report.get("outcome") or "") for report in node_reports}
        phases = {str(report.get("phase") or "") for report in node_reports}
        if "failed" in outcomes:
            outcome = "failed" if any(
                report.get("phase") == "call" and report.get("outcome") == "failed"
                for report in node_reports
            ) else "error"
        elif "call" not in phases or "skipped" in outcomes:
            outcome = "skipped"
        else:
            outcome = "passed"
        duration = sum(
            max(0.0, float(report.get("duration") or 0.0))
            for report in node_reports
        )
        observations.append(
            {"nodeid": nodeid, "duration_seconds": duration, "outcome": outcome}
        )
    return observations


def format_estimate(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"
