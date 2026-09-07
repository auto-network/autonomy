"""auto-jnm58 — client-side event-driven page refresh gate.

Runs the Node harness (client/timeline_changed_refresh.js) which loads the
real pages/timeline.js inside a stub DOM and asserts:
  - init() registers a `timeline:changed` handler and only a 60 s heartbeat
    interval (the old blind 15 s poll is gone),
  - a burst of `timeline:changed` frames coalesces into ONE refetch after the
    5 s debounce,
  - the 60 s heartbeat still refetches unconditionally.

No browser, no live server. xdist-safe (stateless).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "client" / "timeline_changed_refresh.js"
REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node binary not available",
)
def test_timeline_changed_burst_triggers_one_refetch():
    result = subprocess.run(
        ["node", str(HARNESS)],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "REPO_ROOT": str(REPO_ROOT)},
    )
    assert result.returncode == 0, (
        f"Node harness failed (exit={result.returncode}):\n"
        f"STDOUT:\n{result.stdout}\n"
        f"STDERR:\n{result.stderr}"
    )
