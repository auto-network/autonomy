"""auto-8bnq0 Defect 3 — end-to-end dispatch SSE pipeline coverage.

The last_activity TypeError that blanked the dispatch page on 2026-04-20
(`datetime.fromisoformat(run["last_activity"])` called on a float)
shipped green because no test exercised the full path:

  dispatcher writes → dispatch_runs → _collect_dispatch_data →
  dispatch SSE topic → UI-consumed payload

These tests close that gap. They drive _collect_dispatch_data against
realistic dispatch_runs data (populated via the dispatcher's own write
helpers where feasible) and assert the emitted payload is:
  - shape-correct (active[] contains the running row with expected id)
  - consumable (last_activity is a finite number or null, never a raw
    string or a ValueError waiting to happen)

FAIL-REASON on master — mixed:
  - Test 6 (shape) currently PASSES if last_activity is absent and the
    row otherwise parses. Included as a regression guard, not RED today.
  - Test 7 (consumable last_activity) asserts invariants stricter than
    any existing test — passes under the 2026-04-20 dispatcher fix, but
    catches any revert.
  - Test 8 (float survival) plants a FLOAT directly in dispatch_runs,
    mirroring the exact pre-fix corrupted state. Today's
    _collect_dispatch_data raises TypeError; the watcher catches it and
    emits {"active":[]}. Test asserts the function either returns a
    valid payload OR dispatch_runs is normalised to string timestamps.
    RED on master.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import datetime, timezone

import pytest

from .conftest import (
    fetch_dispatch_row,
    insert_dispatch_run,
)

pytest.importorskip("pytest_asyncio")


def _populate_running_row(dispatch_db, *, run_id, bead_id, last_activity_value):
    """Insert a RUNNING dispatch_runs row with full realistic live-stats.

    The last_activity column is written with whatever Python value is passed,
    so tests can cover both ISO-string and float forms — triggering SQLite's
    numeric-affinity conversion for the float case (reproducing the
    2026-04-20 bug state precisely).
    """
    started_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(str(dispatch_db))
    conn.execute(
        "INSERT INTO dispatch_runs"
        " (id, bead_id, started_at, status, container_name,"
        "  branch, image, output_dir, token_count, turn_count,"
        "  cpu_pct, cpu_usec, mem_mb, last_activity, last_snippet, jsonl_offset)"
        " VALUES (?, ?, ?, 'RUNNING', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
        (
            run_id, bead_id, started_at, f"agent-{bead_id}-001",
            f"agent/{bead_id}", "autonomy-agent:dashboard",
            f"/tmp/agent-runs/{run_id}",
            60000, 20,
            10.5, 1000000, 128,
            last_activity_value,
            "last snippet placeholder",
        ),
    )
    conn.commit()
    conn.close()


class TestDispatchSSEPipeline:
    """Defect 3 — _collect_dispatch_data emits consumable payloads."""

    @pytest.mark.asyncio
    async def test_dispatch_sse_payload_includes_running_row(self, cleanup_env):
        """Planted RUNNING row must appear in active[] of the SSE payload."""
        srv = cleanup_env["server"]
        dispatch_db = cleanup_env["dispatch_db"]

        bead_id = "auto-sse-shape"
        run_id = f"{bead_id}-20260420-100000"

        # Realistic ISO-string last_activity (post-fix shape)
        iso_la = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        _populate_running_row(
            dispatch_db, run_id=run_id, bead_id=bead_id, last_activity_value=iso_la,
        )

        payload = await srv._collect_dispatch_data()

        assert isinstance(payload, dict), f"payload must be dict; got {type(payload)}"
        active = payload.get("active") or []
        ids = [a.get("id") for a in active]
        assert bead_id in ids, (
            f"Running bead {bead_id!r} missing from SSE dispatch payload. "
            f"active ids={ids!r}. _collect_dispatch_data likely threw and "
            "the watcher caught it — or the DAO is not returning the row."
        )

    @pytest.mark.asyncio
    async def test_dispatch_sse_payload_last_activity_is_consumable(self, cleanup_env):
        """Emitted last_activity must be numeric (unix timestamp) or None.

        The UI reads this as a number and renders relative time. A raw ISO
        string, a ValueError, or a missing key would break rendering.
        """
        srv = cleanup_env["server"]
        dispatch_db = cleanup_env["dispatch_db"]

        bead_id = "auto-sse-consume"
        run_id = f"{bead_id}-20260420-100000"
        iso_la = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        _populate_running_row(
            dispatch_db, run_id=run_id, bead_id=bead_id, last_activity_value=iso_la,
        )

        payload = await srv._collect_dispatch_data()
        active = payload.get("active") or []
        entry = next((a for a in active if a.get("id") == bead_id), None)
        assert entry is not None, f"active entry missing; active={active!r}"

        la = entry.get("last_activity")
        assert la is None or isinstance(la, (int, float)), (
            f"last_activity must be a number or None for UI consumption; "
            f"got type={type(la).__name__} value={la!r}. The server must "
            "convert dispatch_runs.last_activity (string or float) into a "
            "unix timestamp before emitting."
        )
        if isinstance(la, (int, float)):
            # Must be a plausible unix timestamp (post-2020)
            assert la > 1577836800, (
                f"last_activity unix timestamp is implausibly small: {la}. "
                "Likely a unit error (seconds vs milliseconds vs parsed int "
                "of digits from an ISO string)."
            )

    @pytest.mark.asyncio
    async def test_dispatch_sse_payload_survives_float_last_activity(self, cleanup_env):
        """Float last_activity in dispatch_runs must not crash the pipeline.

        Reproduces the exact pre-fix state from 2026-04-20: dispatcher's
        str(float) plus SQLite numeric-affinity round-tripped to a raw
        float in the column. Server's datetime.fromisoformat(float) threw
        TypeError, watcher fell back to empty active[], dispatch page
        rendered blank.

        Contract: the pipeline must either (a) tolerate the float and
        emit a valid payload, OR (b) normalise the column (coerce to
        ISO string at read time). Either way, active[] must contain the
        running row.
        """
        srv = cleanup_env["server"]
        dispatch_db = cleanup_env["dispatch_db"]

        bead_id = "auto-sse-float"
        run_id = f"{bead_id}-20260420-100000"
        # Plant a raw float — SQLite will store as REAL.
        float_ts = time.time()
        _populate_running_row(
            dispatch_db, run_id=run_id, bead_id=bead_id, last_activity_value=float_ts,
        )

        # Directly verify the column came back as float (reproducing bug state)
        row = fetch_dispatch_row(dispatch_db, run_id)
        assert row is not None
        raw_la = row["last_activity"]
        # If this is a str, the fixture didn't reproduce the bug state and
        # the test below is vacuous — skip meaningfully.
        if not isinstance(raw_la, (int, float)):
            pytest.skip(
                f"Could not reproduce float-affinity bug state; "
                f"got last_activity type={type(raw_la).__name__}. "
                "This test depends on SQLite numeric affinity for DATETIME."
            )

        # Run the pipeline — it must NOT raise, and active[] must contain the row
        payload = await srv._collect_dispatch_data()
        assert isinstance(payload, dict)
        active = payload.get("active") or []
        ids = [a.get("id") for a in active]
        assert bead_id in ids, (
            f"Running bead {bead_id!r} missing from active[]. Float-valued "
            f"last_activity crashed the pipeline. active={active!r}. "
            "The server must normalise dispatch_runs.last_activity regardless "
            "of whether it's stored as TEXT or REAL (defensive parse)."
        )
