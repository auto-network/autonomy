"""Perf telemetry: one-second CPU/lag sampling, spike dumps, Prometheus
exposition, and SQLite write-lock timing (operator requirement 2026-09-30)."""

from __future__ import annotations

import sqlite3
import sys
import threading
import time
from pathlib import Path

from tools.dashboard import perf_telemetry as pt
from tools.graph import write_lock_stats


def _stat(ticks_u: int, ticks_s: int, comm: str = "python3") -> str:
    # Fields after "(comm)": state is field 3; utime is field 14, stime 15.
    rest = ["S"] + ["0"] * 10 + [str(ticks_u), str(ticks_s)] + ["0"] * 30
    return f"1 ({comm}) " + " ".join(rest)


class FakeProc:
    def __init__(self, root: Path, self_pid: int):
        self.root, self.self_pid = root, self_pid

    def set(self, pid: int, ticks: int, cmd: str, threads: dict[int, int] | None = None):
        d = self.root / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "stat").write_text(_stat(ticks, 0))
        (d / "cmdline").write_bytes(cmd.replace(" ", "\0").encode())
        for tid, t in (threads or {}).items():
            td = d / "task" / str(tid)
            td.mkdir(parents=True, exist_ok=True)
            (td / "stat").write_text(_stat(t, 0))


def test_sampler_turns_tick_deltas_into_core_ratios(tmp_path):
    clock = [100.0]
    fake = FakeProc(tmp_path, self_pid=10)
    fake.set(10, 0, "python3 -c from multiprocessing.spawn import spawn_main", {10: 0, 11: 0})
    fake.set(7, 0, "python3 -m tools.dashboard.reload_with_notice tools.dashboard.server:app")
    fake.set(20, 0, "python3 -m tools.dashboard.link_serving --relay wss://x --org 2d4b90cb-1e89")
    s = pt.Sampler(proc_root=str(tmp_path), self_pid=10, clock=lambda: clock[0])
    assert s.sample() is None  # first read is the baseline
    hz = pt._CLK_TCK
    clock[0] += 2.0
    fake.set(10, 5 * hz, "python3 -c from multiprocessing.spawn import spawn_main", {10: hz, 11: 4 * hz})
    fake.set(7, hz // 2, "python3 -m tools.dashboard.reload_with_notice tools.dashboard.server:app")
    fake.set(20, hz, "python3 -m tools.dashboard.link_serving --relay wss://x --org 2d4b90cb-1e89")
    out = s.sample(thread_names={10: "MainThread", 11: "ThreadPoolExecutor-0_3"})
    roles = out.by_role()
    assert abs(roles["worker"] - 2.5) < 1e-6          # 5 core-seconds over 2 s
    assert abs(roles["reloader"] - 0.25) < 1e-6
    assert abs(roles["connector:2d4b90cb"] - 0.5) < 1e-6
    assert abs(out.container - 3.25) < 1e-6
    assert out.by_thread_label() == {"MainThread": 0.5, "ThreadPoolExecutor": 2.0}


def test_labels_are_bounded():
    assert pt.thread_label("ThreadPoolExecutor-0_12") == "ThreadPoolExecutor"
    assert pt.thread_label("asyncio_7") == "asyncio"
    assert pt.thread_label("perf-telemetry") == "perf-telemetry"
    assert pt.process_role("docker ps -s", 5, 1) == "docker-cli"
    assert pt.process_role("/usr/local/bin/tailwindcss --watch", 5, 1) == "tailwind"
    assert pt.process_role("anything", 1, 1) == "worker"


def _sample(container_worker: float, lag: float = 0.0) -> pt.Sample:
    s = pt.Sample(at=time.time(), elapsed=1.0)
    s.procs = {1: ("worker", "spawn_main", container_worker), 7: ("reloader", "reload", 0.1)}
    s.threads = {threading.get_native_id(): ("MainThread", container_worker)}
    s.lag_max_s = lag
    return s


class StubSampler:
    def __init__(self, samples):
        self.samples = list(samples)

    def sample(self, thread_names=None):
        return self.samples.pop(0) if self.samples else None


def test_a_spike_writes_one_episode_naming_process_thread_and_stack(tmp_path):
    dump = tmp_path / "perf-spikes.log"
    t = pt.Telemetry(sampler=StubSampler([_sample(2.8), _sample(2.9), _sample(0.2)]), dump_path=dump,
                     loop_thread_ident=lambda: threading.get_ident())
    for _ in range(3):
        t.tick()
    text = dump.read_text()
    assert t.spike_episodes == 1 and t.spike_dumps == 2
    assert "SPIKE" in text and "hottest process worker 280%" in text
    assert "hottest worker thread MainThread 280%" in text
    assert "stack: event-loop thread" in text and "thread tid" in text and "test_perf_telemetry.py" in text
    assert "busy threads by pool" in text and "MainThread: 1 threads, 280%" in text
    assert "=== episode end" in text


def test_loop_lag_alone_triggers_a_dump(tmp_path):
    dump = tmp_path / "perf-spikes.log"
    t = pt.Telemetry(sampler=StubSampler([_sample(0.1)]), dump_path=dump,
                     heartbeat_age=lambda: 3.0, loop_thread_ident=lambda: threading.get_ident())
    t.tick()
    text = dump.read_text()
    assert "loop lag 3.00s" in text and "stack: event-loop thread" in text


def test_exposition_is_prometheus_text_with_lag_histogram(tmp_path):
    t = pt.Telemetry(sampler=StubSampler([_sample(0.4)]), dump_path=tmp_path / "x.log")
    for lag in (0.01, 0.3, 4.0):
        t.note_lag(lag)
    t.tick()
    text = t.exposition()
    assert 'dashboard_process_cpu_ratio{role="worker"} 0.4' in text
    assert "dashboard_loop_lag_max_seconds 4" in text
    assert 'dashboard_loop_lag_seconds_bucket{le="0.05"} 1' in text
    assert 'dashboard_loop_lag_seconds_bucket{le="0.5"} 2' in text
    assert 'dashboard_loop_lag_seconds_bucket{le="+Inf"} 3' in text
    assert "dashboard_loop_lag_seconds_count 3" in text
    for line in text.splitlines():
        assert line.startswith("#") or " " in line, line


def test_write_lock_wait_and_hold_are_timed_per_database(tmp_path):
    db = tmp_path / "locks.db"
    base = write_lock_stats.timed_class(sqlite3.Connection, lambda conn: "locks.db")
    holder = sqlite3.connect(db, factory=base, isolation_level=None, check_same_thread=False, timeout=5)
    waiter = sqlite3.connect(db, factory=base, isolation_level=None, check_same_thread=False, timeout=5)
    holder.execute("CREATE TABLE t (x)")
    before = write_lock_stats.snapshot()["hist"].get(("hold", "locks.db"), [0] * 20)[-1]
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO t VALUES (1)")
    released = threading.Event()

    def release():
        time.sleep(0.3)
        holder.execute("COMMIT")
        released.set()

    threading.Thread(target=release).start()
    waiter.execute("BEGIN IMMEDIATE")   # blocks until the holder commits
    waiter.rollback()
    released.wait(2)
    snap = write_lock_stats.snapshot()["hist"]
    assert snap[("hold", "locks.db")][-1] - before >= 0.25
    assert max(snap[("wait", "locks.db")][-1], 0) >= 0.2
    text = "\n".join(write_lock_stats.exposition())
    assert 'dashboard_sqlite_write_wait_seconds_bucket{db="locks.db",le="+Inf"}' in text
    assert 'dashboard_sqlite_write_hold_seconds_count{db="locks.db"}' in text


def test_the_process_wrapper_times_locks_on_every_open(tmp_path):
    from tools.graph import sqlite_open_diag
    sqlite_open_diag.install()
    conn = sqlite3.connect(tmp_path / "wrapped.db", isolation_level=None)
    conn.execute("CREATE TABLE t (x)")
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.execute("COMMIT")
    assert ("hold", "wrapped.db") in write_lock_stats.snapshot()["hist"]
    conn.close()


def test_a_spike_spread_over_a_pool_is_summarised_by_function(tmp_path):
    """Twelve pool threads at 15% each: every one gets a stack, and one line
    counts which repository function the pool's threads are in."""
    s = pt.Sample(at=time.time(), elapsed=1.0)
    s.procs = {1: ("worker", "spawn_main", 1.8)}
    s.threads = {100 + i: (f"asyncio_{i}", 0.15) for i in range(12)}

    def busy():
        return sys._getframe()

    frame = busy()
    lines = pt.render_dump(s, ["worker CPU 180%"], {100 + i: 5000 + i for i in range(12)}, None,
                           frames={5000 + i: frame for i in range(12)})
    text = "\n".join(lines)
    assert "asyncio: 12 threads, 180%" in text
    assert "test_perf_telemetry.py" in text and " busy x12" in text
    assert text.count("stack: thread tid") == pt.MAX_STACKS


def test_a_long_write_lock_logs_lock_hold(tmp_path, caplog):
    db = tmp_path / "slow.db"
    base = write_lock_stats.timed_class(sqlite3.Connection, lambda conn: "slow.db")
    conn = sqlite3.connect(db, factory=base, isolation_level=None)
    conn.execute("CREATE TABLE t (x)")
    old = write_lock_stats.LONG_S
    write_lock_stats.LONG_S = 0.05
    try:
        with caplog.at_level("WARNING", logger="tools.graph.write_lock_stats"):
            conn.execute("BEGIN IMMEDIATE")
            time.sleep(0.1)
            conn.execute("COMMIT")
    finally:
        write_lock_stats.LONG_S = old
    assert any("LOCK-HOLD store=slow.db" in r.getMessage() and "thread=" in r.getMessage()
               for r in caplog.records)


def test_cpu_is_attributed_to_the_paths_each_thread_was_sampled_in():
    """A thread measured at 80% that was sampled 3 times in hot() and once in
    warm() gets 0.6 and 0.2 CPU-seconds there; a sleeping thread whose stack
    looks busy but used no CPU attributes nothing."""
    def hot():
        return sys._getframe()

    def warm():
        return sys._getframe()

    hot_f, warm_f = hot(), warm()
    seq = [hot_f, hot_f, hot_f, warm_f]
    st = pt.StackSampler(hz=4, frames_fn=lambda: {1: seq.pop(0) if seq else hot_f, 2: hot_f})
    for _ in range(4):
        st.sample_once(threads=[(1, 101, "asyncio_3"), (2, 102, "sleeper")])
    st.attribute({101: 0.8, 102: 0.0}, 1.0, now=1000.0)
    window = st.window(5, now=1000.5)
    by_leaf = {path.split(" < ")[0].rsplit(" ", 1)[-1]: round(v, 3) for (pool, path), v in window.items()}
    assert by_leaf == {"hot": 0.6, "warm": 0.2}
    assert all(pool == "asyncio" for pool, _ in window)
    text = "\n".join(pt.render_window(window, 5))
    assert "0.80 CPU-seconds attributed" in text and " 75%  asyncio" in text


def test_a_spike_dump_reports_where_the_cpu_went(tmp_path):
    def busy():
        return sys._getframe()

    frame = busy()
    st = pt.StackSampler(hz=10, frames_fn=lambda: {threading.get_ident(): frame})
    tid = threading.get_native_id()
    for _ in range(10):
        st.sample_once(threads=[(threading.get_ident(), tid, "ThreadPoolExecutor-0_1")])
    sample = _sample(2.5)
    sample.threads = {tid: ("ThreadPoolExecutor-0_1", 2.5)}
    dump = tmp_path / "perf-spikes.log"
    t = pt.Telemetry(sampler=StubSampler([sample]), dump_path=dump, stacks=st)
    t.tick()
    text = dump.read_text()
    assert "where the worker's CPU went over the last 5s (2.50 CPU-seconds attributed)" in text
    assert "ThreadPoolExecutor" in text and "busy" in text


def test_large_gauges_keep_full_precision(tmp_path):
    t = pt.Telemetry(sampler=StubSampler([_sample(0.1)]), dump_path=tmp_path / "x.log")
    t.tick()
    line = next(l for l in t.exposition().splitlines() if l.startswith("dashboard_worker_start_time_seconds "))
    assert abs(float(line.split()[1]) - pt._WORKER_STARTED) < 0.01


def _spin(seconds):
    end = time.thread_time() + seconds
    while time.thread_time() < end:
        pass
    return "done"


def test_every_to_thread_job_is_timed_by_function():
    import asyncio
    stats = pt.JobStats()

    async def main():
        asyncio.get_running_loop().set_default_executor(pt.instrumented_executor(stats, max_workers=4))
        results = await asyncio.gather(*[asyncio.to_thread(_spin, 0.02) for _ in range(5)],
                                       asyncio.to_thread(time.sleep, 0.05))
        return results

    results = asyncio.run(main())
    assert results[:5] == ["done"] * 5
    key = "tests.test_perf_telemetry._spin" if "tests.test_perf_telemetry._spin" in stats.calls else \
        next(k for k in stats.calls if k.endswith("test_perf_telemetry._spin"))
    assert stats.calls[key] == 5
    assert stats.cpu[key] >= 0.09
    sleep_key = next(k for k in stats.calls if k.endswith("sleep"))
    assert stats.cpu[sleep_key] < 0.02 and stats.wall[sleep_key] >= 0.04
    text = "\n".join(pt.render_jobs(stats, 5))
    assert "6 jobs" in text and "_spin" in text
    expo = "\n".join(stats.exposition())
    assert "dashboard_executor_job_cpu_seconds_total{fn=" in expo and "_spin" in expo


def test_job_key_unwraps_partials():
    import contextvars
    import functools
    ctx = contextvars.copy_context()
    assert pt.job_key(functools.partial(ctx.run, _spin, 1)).endswith("test_perf_telemetry._spin")
    assert pt.job_key(functools.partial(_spin, 1)).endswith("test_perf_telemetry._spin")


def test_section_times_a_block_into_the_job_table(monkeypatch, caplog):
    """2026-10-01: named sections measure suspected hot paths (the sender-href
    refresh in transcript parsing) beside the executor jobs."""
    import threading
    from tools.dashboard import perf_telemetry as pt

    class _T:
        jobs = pt.JobStats()
    monkeypatch.setattr(pt, "_telemetry", _T())
    started, release = threading.Event(), threading.Event()

    def slow():
        with pt.section("demo", slow_log_s=0.0):
            started.set()
            release.wait(2)
    other = threading.Thread(target=slow, name="other")
    other.start()
    started.wait(2)
    with caplog.at_level("WARNING", logger="tools.dashboard.perf_telemetry"):
        with pt.section("demo", slow_log_s=0.0):
            assert _T.jobs.in_flight["section:demo"] == 2
        release.set()
        other.join(2)
    assert _T.jobs.calls["section:demo"] == 2
    assert _T.jobs.in_flight["section:demo"] == 0
    assert "section:demo" in _T.jobs.cpu and "section:demo" in _T.jobs.max_wall
    lines = [r.getMessage() for r in caplog.records if "SLOW-SECTION demo" in r.getMessage()]
    assert any("thread=MainThread in_flight=2" in line for line in lines)


def test_section_is_a_no_op_without_telemetry(monkeypatch):
    from tools.dashboard import perf_telemetry as pt
    monkeypatch.setattr(pt, "_telemetry", None)
    with pt.section("demo", slow_log_s=0.0):
        value = 1
    assert value == 1


def test_gc_timing_records_collections_by_generation_and_thread(monkeypatch, caplog):
    """2026-10-01: collections are recorded per generation and paying thread,
    so a cheap job charged seconds of CPU can be checked against the
    collector's share. The callback only queues; the sampler drains."""
    import gc
    import threading
    from tools.dashboard import perf_telemetry as pt

    monkeypatch.setattr(pt, "GC_SLOW_LOG_S", 0.0)
    pt._gc_events.clear()
    pt._gc_callback("start", {"generation": 2})
    pt._gc_callback("stop", {"generation": 2, "collected": 5, "uncollectable": 0})
    pt._gc_callback("stop", {"generation": 1})            # stop without a start: ignored
    assert len(pt._gc_events) == 1

    def in_pool_thread():
        pt._gc_callback("start", {"generation": 0})
        pt._gc_callback("stop", {"generation": 0, "collected": 0, "uncollectable": 0})
    worker = threading.Thread(target=in_pool_thread, name="asyncio_7")
    worker.start()
    worker.join()

    jobs = pt.JobStats()
    with caplog.at_level("WARNING", logger="tools.dashboard.perf_telemetry"):
        pt.drain_gc_timing(jobs)
    assert jobs.calls == {"gc:gen2:MainThread": 1, "gc:gen0:asyncio": 1}
    assert jobs.in_flight.get("gc:gen2:MainThread", 0) == 0
    assert not pt._gc_events
    assert any("SLOW-GC gen=2" in r.getMessage() and "thread=MainThread" in r.getMessage()
               and "collected=5" in r.getMessage() for r in caplog.records)

    before = len(gc.callbacks)
    pt.install_gc_timing()
    pt.install_gc_timing()
    assert len(gc.callbacks) - before <= 1
    if pt._gc_callback in gc.callbacks:
        gc.callbacks.remove(pt._gc_callback)
    monkeypatch.setattr(pt, "_gc_installed", False)
