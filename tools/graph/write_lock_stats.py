"""SQLite write-lock timing for every connection this process opens.

Operator requirement (2026-09-30): one central place that measures how long
write locks take, reported as a histogram over time, so a moment when "the
write locks go crazy" is visible and can be matched to the threads running
then.

Every file-backed connection already passes through
``tools.graph.sqlite_open_diag`` (it wraps ``sqlite3.connect`` for the whole
process), so its connection subclass calls into this module:

* **wait**: how long ``BEGIN IMMEDIATE`` / ``BEGIN EXCLUSIVE`` took to
  return, which is the time spent waiting for the database's write lock
  (another connection, thread or process held it);
* **hold**: from that ``BEGIN`` returning to ``COMMIT`` / ``ROLLBACK``
  (method or statement), which is how long this connection kept every
  other writer of that database waiting;
* **locked**: ``database is locked`` / ``busy`` errors raised to the caller.

Per database FILE NAME (bounded: one per org store plus the dashboard's own
stores), counts go into fixed-bucket histograms. A hold or wait longer than
``LONG_S`` is also kept, with the thread name and the repository frames that
committed, in a small ring the perf telemetry writes into its spike log.

Scope: only explicit ``BEGIN IMMEDIATE``/``EXCLUSIVE`` is timed (the 85
write sites that take the lock up front). A write that relies on Python's
implicit ``BEGIN`` takes the lock at its first DML statement and is not
timed here; its ``database is locked`` errors are still counted.
"""

from __future__ import annotations

import sys
import threading
import time
from collections import deque
from pathlib import Path

BUCKETS = (0.001, 0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)
LONG_S = 1.0
_REPO_ROOT = str(Path(__file__).resolve().parents[2]) + "/"

_lock = threading.Lock()
#: (kind, database) -> [bucket counts..., +Inf count], sum
_hist: dict[tuple[str, str], list[float]] = {}
_locked_errors: dict[str, int] = {}
#: Long waits/holds: (unix time, kind, database, seconds, thread, frames)
long_events: deque = deque(maxlen=200)


def _observe(kind: str, database: str, seconds: float, *, frames: str = "") -> None:
    seconds = max(0.0, seconds)
    with _lock:
        row = _hist.get((kind, database))
        if row is None:
            row = _hist[(kind, database)] = [0.0] * (len(BUCKETS) + 2)  # buckets, +Inf, sum
        for i, bound in enumerate(BUCKETS):
            if seconds <= bound:
                row[i] += 1
                break
        else:
            row[len(BUCKETS)] += 1
        row[-1] += seconds
    if seconds >= LONG_S:
        long_events.append((time.time(), kind, database, seconds,
                            threading.current_thread().name, frames or _frames()))


def note_locked(database: str) -> None:
    with _lock:
        _locked_errors[database] = _locked_errors.get(database, 0) + 1


def _frames(depth: int = 6) -> str:
    out = []
    frame = sys._getframe(2)
    while frame is not None and len(out) < depth:
        name = frame.f_code.co_filename
        if name.startswith(_REPO_ROOT) and "write_lock_stats" not in name and "sqlite_open_diag" not in name:
            out.append(f"{name[len(_REPO_ROOT):]}:{frame.f_lineno} {frame.f_code.co_name}")
        frame = frame.f_back
    return " < ".join(out)


def _begin_kind(sql) -> bool:
    if not isinstance(sql, str):
        return False
    head = sql.lstrip()[:24].upper()
    return head.startswith("BEGIN IMMEDIATE") or head.startswith("BEGIN EXCLUSIVE")


def _end_kind(sql) -> bool:
    if not isinstance(sql, str):
        return False
    head = sql.lstrip()[:10].upper()
    return head.startswith(("COMMIT", "END", "ROLLBACK")) and not head.startswith("ROLLBACK TO")


def _is_locked_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "database is locked" in text or "database table is locked" in text or "busy" in text


def timed_class(base: type, database_of) -> type:
    """A subclass of the connection class *base* that times write locks.
    *database_of(conn)* returns the database file name for labels."""

    class TimedConnection(base):
        __slots__ = () if "__weakref__" in getattr(base, "__slots__", ()) or base.__weakrefoffset__ else ("__weakref__",)

        def execute(self, sql, *args, **kwargs):
            if _begin_kind(sql):
                t0 = time.monotonic()
                try:
                    cur = super().execute(sql, *args, **kwargs)
                except Exception as exc:
                    if _is_locked_error(exc):
                        note_locked(database_of(self))
                    _observe("wait", database_of(self), time.monotonic() - t0)
                    raise
                now = time.monotonic()
                _observe("wait", database_of(self), now - t0)
                _hold_started[id(self)] = now
                return cur
            if _end_kind(sql):
                started = _hold_started.pop(id(self), None)
                try:
                    return super().execute(sql, *args, **kwargs)
                finally:
                    if started is not None:
                        _observe("hold", database_of(self), time.monotonic() - started)
            try:
                return super().execute(sql, *args, **kwargs)
            except Exception as exc:
                if _is_locked_error(exc):
                    note_locked(database_of(self))
                raise

        def commit(self):
            started = _hold_started.pop(id(self), None)
            try:
                return super().commit()
            finally:
                if started is not None:
                    _observe("hold", database_of(self), time.monotonic() - started)

        def close(self):
            _hold_started.pop(id(self), None)
            return super().close()

        def rollback(self):
            started = _hold_started.pop(id(self), None)
            try:
                return super().rollback()
            finally:
                if started is not None:
                    _observe("hold", database_of(self), time.monotonic() - started)

    TimedConnection.__name__ = base.__name__
    TimedConnection.__qualname__ = base.__qualname__
    return TimedConnection


#: id(connection) -> monotonic time its write lock was taken. A connection
#: holds at most one transaction; the entry is popped at COMMIT/ROLLBACK.
_hold_started: dict[int, float] = {}


def snapshot() -> dict:
    """Histograms, locked-error counts and recent long events, for export."""
    with _lock:
        hist = {k: list(v) for k, v in _hist.items()}
        locked = dict(_locked_errors)
    return {"buckets": BUCKETS, "hist": hist, "locked": locked,
            "long_events": list(long_events)}


def exposition(prefix: str = "dashboard_sqlite") -> list[str]:
    snap = snapshot()
    out: list[str] = []
    for kind, help_text in (("wait", "Seconds a BEGIN IMMEDIATE/EXCLUSIVE waited for the write lock."),
                            ("hold", "Seconds a write lock was held from BEGIN to COMMIT/ROLLBACK.")):
        name = f"{prefix}_write_{kind}_seconds"
        out.append(f"# HELP {name} {help_text}")
        out.append(f"# TYPE {name} histogram")
        for (k, database), row in sorted(snap["hist"].items()):
            if k != kind:
                continue
            db = database.replace('"', "")
            cumulative = 0.0
            for bound, count in zip(BUCKETS, row):
                cumulative += count
                out.append(f'{name}_bucket{{db="{db}",le="{bound}"}} {cumulative:.0f}')
            cumulative += row[len(BUCKETS)]
            out.append(f'{name}_bucket{{db="{db}",le="+Inf"}} {cumulative:.0f}')
            out.append(f'{name}_sum{{db="{db}"}} {row[-1]:.6g}')
            out.append(f'{name}_count{{db="{db}"}} {cumulative:.0f}')
    name = f"{prefix}_locked_errors_total"
    out.append(f"# HELP {name} 'database is locked' errors raised to callers.")
    out.append(f"# TYPE {name} counter")
    for database, count in sorted(snap["locked"].items()):
        out.append(f'{name}{{db="{database}"}} {count}')
    return out
