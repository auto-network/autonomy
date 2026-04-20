"""auto-opbyh test #4 — _liveness_loop must skip non-tmux session types.

Dispatch and librarian sessions are owned by their respective dispatchers
(death signal = deregister_session call when decision.json lands). The
tmux-liveness poll is wrong for them — they never had a tmux session, so
`tmux has-session -t ...` always returns False and they get marked dead
within 10s of being registered.

This test runs ONE real liveness tick with UNPATCHED _check_tmux and
asserts dispatch rows survive. It also asserts container rows WITHOUT
a real tmux still get marked dead (regression guard — the filter must be
narrow, not "skip everything").

FAIL-REASON on master: session_monitor.py:_liveness_loop iterates all
is_live=1 rows, calls _check_tmux unconditionally, marks dead on False.
Dispatch row is_live flips to 0 within the tick.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path

import pytest

from .conftest import fetch_row, insert_row

pytest.importorskip("pytest_asyncio")


@pytest.mark.asyncio
class TestLivenessTypeFilter:
    """#4 — liveness loop skips type IN ('dispatch','librarian')."""

    async def test_liveness_loop_skips_dispatch_and_librarian(self, ipc_env):
        smmod = ipc_env["session_monitor"]
        db_path = ipc_env["db_path"]
        tmp_path = ipc_env["tmp_path"]

        # Use a random tmux_name that definitely doesn't exist as a real
        # tmux session — real _check_tmux will return False for both rows.
        fake_dispatch = f"auto-dispatch-fake-{uuid.uuid4().hex[:8]}"
        fake_container = f"auto-container-fake-{uuid.uuid4().hex[:8]}"
        fake_librarian = f"librarian-fake-{uuid.uuid4().hex[:8]}"

        # Plant rows directly — bypassing the (new, not-yet-existing) endpoint
        # because this test exercises the liveness loop, not the endpoint.
        sess_dir = tmp_path / "agent-runs" / "fakes" / "sessions" / "autonomy"
        sess_dir.mkdir(parents=True)
        for name in (fake_dispatch, fake_container, fake_librarian):
            jsonl = sess_dir / f"{name}.jsonl"
            jsonl.write_text("")

        insert_row(
            db_path,
            tmux_name=fake_dispatch,
            type_="dispatch",
            jsonl_path=str(sess_dir / f"{fake_dispatch}.jsonl"),
            bead_id="auto-fake",
            is_live=1,
        )
        insert_row(
            db_path,
            tmux_name=fake_container,
            type_="container",
            jsonl_path=str(sess_dir / f"{fake_container}.jsonl"),
            is_live=1,
        )
        insert_row(
            db_path,
            tmux_name=fake_librarian,
            type_="librarian",
            jsonl_path=str(sess_dir / f"{fake_librarian}.jsonl"),
            is_live=1,
        )

        # Build a monitor — do NOT patch _check_tmux. Run one liveness tick.
        mon = smmod.SessionMonitor()

        # Drive just the liveness cycle without starting the full monitor.
        # _liveness_loop is an infinite loop; we want one iteration, so we
        # call its body. The implementation under test may factor this into
        # a helper `_liveness_tick()` — try that first, fall back to manual.
        if hasattr(mon, "_liveness_tick"):
            await mon._liveness_tick()
        else:
            # Fallback: invoke the loop and cancel after one sleep.
            task = asyncio.create_task(mon._liveness_loop())
            # Let it run one iteration — the loop body runs then sleeps 10s.
            await asyncio.sleep(0.5)
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        # Assertions — dispatch + librarian MUST still be live; container dead.
        d_row = fetch_row(db_path, fake_dispatch)
        c_row = fetch_row(db_path, fake_container)
        l_row = fetch_row(db_path, fake_librarian)

        assert d_row is not None and d_row["is_live"] == 1, (
            f"Dispatch row {fake_dispatch!r} got marked dead by liveness loop. "
            "Liveness must skip type='dispatch' — their death signal is "
            "explicit deregister_session() from dispatcher, not tmux polling. "
            f"row={d_row!r}"
        )
        assert l_row is not None and l_row["is_live"] == 1, (
            f"Librarian row {fake_librarian!r} got marked dead by liveness loop. "
            "Liveness must skip type='librarian'. "
            f"row={l_row!r}"
        )
        # Regression guard — container rows without tmux ARE dead
        assert c_row is not None and c_row["is_live"] == 0, (
            f"Container row {fake_container!r} should have been marked dead "
            "(no real tmux session by that name). Filter is TOO broad — it "
            f"must only skip dispatch + librarian. row={c_row!r}"
        )
