"""auto-16g9t commit C — the five failure-injection integration tests.

Runs the Node harness (client/viewer_catchup_harness.js), which loads the
REAL client stack (events.js, session-store.js, session-display.js,
session-renderer.js, session-viewer.js) against an in-memory fixture file
set implementing the chain-tail contract, injects the five production
failure classes, and reads the /api/diag counter snapshot to prove
recovery:

  (a) withheld SSE broadcasts  — on-the-fly span-gap detection, exactly
      the withheld entries fetched, buffer equals a control client, zero
      duplicate tiles, counters asserted through _diagSnapshotSessions
  (b) silent connection kill   — the iOS zombie (readyState stays OPEN):
      the wake rebuilds the stream unconditionally + one ranged catch-up.
      Demonstrated FAILING against the old readyState===2 gate.
  (c) cursor in a superseded file — rollover while asleep: remainder +
      successors served, committed lands in the successor file
  (d) scroll-up racing a catch-up — buffer converges to the
      sorted-by-tuple oracle, zero duplicate tiles
  (e) empty-window scroll-up page — paging over a pure-noise region
      never dead-ends (renderable-entries rule end to end)

Correctness proof: entry_refs make the expected buffer computable from
the fixture files alone — each test compares the final buffer against
that oracle, and asserts conclusion_contradicted == 0 (the acceptance
gate: the viewer never believed a caught-up lie).

No browser, no live server — runs in a few seconds under pytest.
xdist-safe (stateless).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "client" / "viewer_catchup_harness.js"
REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node binary not available",
)
def test_viewer_catchup_failure_injection():
    result = subprocess.run(
        ["node", str(HARNESS)],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "REPO_ROOT": str(REPO_ROOT)},
    )
    assert result.returncode == 0, (
        f"Node harness failed (exit={result.returncode}):\n"
        f"STDOUT:\n{result.stdout[-8000:]}\n"
        f"STDERR:\n{result.stderr[-4000:]}"
    )
