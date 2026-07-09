"""auto-opbyh test #4 — _liveness_loop must skip non-tmux session types.

Dispatch, librarian, and agentic sessions are owned by their respective
dispatchers/watchers (death signal = deregister_session call when
decision.json lands, or container-exit collection for agentic runs). The
tmux-liveness poll is wrong for them — they never had a tmux session, so
`tmux has-session -t ...` always returns False and they get marked dead
within 10s of being registered.

This test runs ONE real liveness tick with UNPATCHED _check_tmux and
asserts dispatch/agentic/librarian rows survive. It also asserts container
rows WITHOUT a real tmux still get marked dead (regression guard — the
filter must be narrow, not "skip everything").

FAIL-REASON on master: session_monitor.py:_liveness_loop iterates all
is_live=1 rows, calls _check_tmux unconditionally, marks dead on False.
Dispatch/agentic rows is_live flip to 0 within the tick.
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
    """#4 — liveness loop skips type IN ('dispatch','librarian','agentic')."""

    async def test_liveness_loop_skips_dispatch_librarian_and_agentic(self, ipc_env):
        smmod = ipc_env["session_monitor"]
        db_path = ipc_env["db_path"]
        tmp_path = ipc_env["tmp_path"]

        # Use a random tmux_name that definitely doesn't exist as a real
        # tmux session — real _check_tmux will return False for both rows.
        fake_dispatch = f"auto-dispatch-fake-{uuid.uuid4().hex[:8]}"
        fake_container = f"auto-container-fake-{uuid.uuid4().hex[:8]}"
        fake_librarian = f"librarian-fake-{uuid.uuid4().hex[:8]}"
        fake_agentic = f"agentic-fake-{uuid.uuid4().hex[:8]}"

        # Plant rows directly — bypassing the (new, not-yet-existing) endpoint
        # because this test exercises the liveness loop, not the endpoint.
        sess_dir = tmp_path / "agent-runs" / "fakes" / "sessions" / "autonomy"
        sess_dir.mkdir(parents=True)
        for name in (fake_dispatch, fake_container, fake_librarian, fake_agentic):
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
        insert_row(
            db_path,
            tmux_name=fake_agentic,
            type_="agentic",
            jsonl_path=str(sess_dir / f"{fake_agentic}.jsonl"),
            is_live=1,
        )

        # Build a monitor — do NOT patch _check_tmux.
        mon = smmod.SessionMonitor()

        # Drive just the liveness sweep without starting the full monitor.
        # The fail-safe contract (2096a3f3) requires _LIVENESS_MISS_THRESHOLD
        # CONSECUTIVE authoritative "no such session" probes before a reap,
        # so run the extracted sweep helper that many times.
        if hasattr(mon, "_sweep_tmux_liveness"):
            rounds = getattr(mon, "_LIVENESS_MISS_THRESHOLD", 2)
            for _ in range(rounds):
                sessions = smmod.get_live_sessions()
                await mon._sweep_tmux_liveness(sessions, time.time())
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

        # Assertions — dispatch + librarian + agentic MUST still be live;
        # container dead.
        d_row = fetch_row(db_path, fake_dispatch)
        c_row = fetch_row(db_path, fake_container)
        l_row = fetch_row(db_path, fake_librarian)
        a_row = fetch_row(db_path, fake_agentic)

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
        assert a_row is not None and a_row["is_live"] == 1, (
            f"Agentic row {fake_agentic!r} got marked dead by liveness loop. "
            "Liveness must skip type='agentic' — their lifecycle is owned by "
            "the agentic container watcher (poll_and_collect_agentic), not "
            "tmux polling. Otherwise cleanup_session_worktrees yanks "
            "/workspace/repo out from under a still-running container. "
            f"row={a_row!r}"
        )
        # Regression guard — container rows without tmux ARE dead
        assert c_row is not None and c_row["is_live"] == 0, (
            f"Container row {fake_container!r} should have been marked dead "
            "(no real tmux session by that name). Filter is TOO broad — it "
            f"must only skip dispatch + librarian + agentic. row={c_row!r}"
        )
