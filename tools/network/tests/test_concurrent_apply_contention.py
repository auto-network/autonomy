"""Two processes applying to one personal.db must both complete (auto-ew9wf).

The relay fallback creates a condition that did not exist before: the
CONNECTOR applies a whole scope (a delegated relay pull) while the DASHBOARD
applies another (its direct pulls). Two processes, one file.

This is the honest remainder of a claim I retracted. I reported the apply path
had no busy timeout; it has one — `sqlite3.connect(..., timeout=30.0)` at
fleet_sync_scheduler._open, whose comment records that 5 s "surfaced as
OperationalError at N=50". So the hazard is not new and the mitigation is
present. What nothing proved is whether 30 s covers TWO WHOLE-SCOPE APPLIES
overlapping, which is longer than the "several pulls" the comment describes.

These use the apply path's own connection settings rather than a convenient
approximation, because the number under test is exactly that setting.
"""

from __future__ import annotations

import multiprocessing as mp
import sqlite3
import time
from pathlib import Path

import pytest

from tools.graph.db import GraphDB
from tools.network.fleet_sync_connection import FleetSyncConnection
from tools.network.idkit import KeyPair

APPLY_TIMEOUT_S = 30.0


def _connect(path: Path, timeout: float = APPLY_TIMEOUT_S):
    """Exactly what fleet_sync_scheduler._open uses."""
    conn = sqlite3.connect(str(path), factory=FleetSyncConnection, timeout=timeout)
    conn.row_factory = sqlite3.Row
    return conn


def _hold_writer(path: str, hold_s: float, ready, done):
    """Take the write lock and keep it, as a long apply would."""
    conn = _connect(Path(path))
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("CREATE TABLE IF NOT EXISTS contention (n INTEGER)")
        conn.execute("INSERT INTO contention VALUES (1)")
        ready.set()
        time.sleep(hold_s)
        conn.commit()
    finally:
        conn.close()
        done.set()


def _second_writer(path: str, timeout: float, result):
    """The other process's apply: must WAIT for the lock, not fail instantly."""
    conn = _connect(Path(path), timeout=timeout)
    started = time.monotonic()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("CREATE TABLE IF NOT EXISTS contention (n INTEGER)")
        conn.execute("INSERT INTO contention VALUES (2)")
        conn.commit()
        result["outcome"] = "committed"
    except sqlite3.OperationalError as exc:
        result["outcome"] = f"OperationalError: {exc}"
    finally:
        result["waited_s"] = time.monotonic() - started
        conn.close()


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "personal.db"
    db = GraphDB(path)
    try:
        db.activate_fleet_sync_writers(KeyPair.generate().public_hex)
    finally:
        db.close()
    return path


def test_a_second_process_waits_for_the_lock_instead_of_failing(store):
    """The property the relay fallback newly depends on: one process holding
    the write lock for a whole-scope apply must not make another process's
    apply fail immediately."""
    ctx = mp.get_context("spawn")
    with ctx.Manager() as manager:
        ready, done = ctx.Event(), ctx.Event()
        result = manager.dict()
        holder = ctx.Process(target=_hold_writer, args=(str(store), 2.0, ready, done))
        holder.start()
        assert ready.wait(15), "the first writer never took the lock"
        second = ctx.Process(
            target=_second_writer, args=(str(store), APPLY_TIMEOUT_S, result),
        )
        second.start()
        second.join(60)
        holder.join(60)
        assert result["outcome"] == "committed", (
            f"the second apply did not complete: {result['outcome']}"
        )
        assert result["waited_s"] >= 1.0, (
            "it returned too fast to have waited for the lock at all"
        )


def test_without_the_timeout_it_fails_immediately(store):
    """The control. This is what the apply path would do if the timeout were
    absent — which is what I wrongly reported it as. It fails in
    milliseconds, so the 30 s is doing real work rather than being decorative."""
    ctx = mp.get_context("spawn")
    with ctx.Manager() as manager:
        ready, done = ctx.Event(), ctx.Event()
        result = manager.dict()
        holder = ctx.Process(target=_hold_writer, args=(str(store), 2.0, ready, done))
        holder.start()
        assert ready.wait(15)
        second = ctx.Process(target=_second_writer, args=(str(store), 0.0, result))
        second.start()
        second.join(60)
        holder.join(60)
        assert "OperationalError" in result["outcome"]
        assert result["waited_s"] < 1.0, "a zero timeout should not have waited"
