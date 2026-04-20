"""Client-side SSE gap-replay dedup regression gate.

Runs the Node harness (harness_gap_dedup.js) which loads the real
events.js + session-store.js inside a stub DOM and asserts the
gap-recovery dedup behaviour.

Failing today: Tests B and C demonstrate the silent-drop bug in
appendSessionEntries' per-session seq dedup guard.

After fix (entry-identity dedup in session-store.js): all three tests pass.

No browser, no live server — runs in ~1s under pytest. xdist-safe (stateless).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "client" / "harness_gap_dedup.js"
REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node binary not available",
)
def test_sse_gap_dedup_reproduction():
    """Fails today on the dedup bug; passes once entry-identity dedup lands."""
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
