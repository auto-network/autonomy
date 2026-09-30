"""Dashboard performance telemetry: loop lag and CPU, sampled every second,
exported to Prometheus, with an automatic stack dump on every spike.

Operator requirement (2026-09-30, verbatim in intent): lag and CPU of the
dashboard sampled every second into Prometheus; when the system lags or CPU
spikes, dump the frames automatically and name the process and thread that
is burning the CPU. The operator watches container CPU sit at 27-50% and
spike to 200-300% every minute or two; nothing recorded where that went.

What this module does, all from one daemon thread in the dashboard worker:

* **Every second** it reads ``/proc`` for every process in the container
  (worker, reloader, one serving connector per org, tailwind, children) and
  every thread of this worker, and turns tick deltas into CPU ratios
  (1.0 = one full core). It also takes the event-loop lag: the largest
  watchdog lag reported in the last second, and the heartbeat age right now
  (which is what shows a loop that is blocked so hard the watchdog cannot
  report at all).
* **On a spike** (container CPU, worker CPU or loop lag over its threshold)
  it writes one episode to ``<log dir>/perf-spikes.log``: the per-process
  table, the per-thread table, and the Python stack of every hot worker
  thread plus the loop thread, re-sampled every second while the spike
  lasts. The first lines name the hottest process and thread.
* **Prometheus**: a text exposition served on its own listener
  (``DASHBOARD_METRICS_PORT``, default 9464, bound on the container's
  interfaces and not published to the host), so a Prometheus on the compose
  network scrapes ``dashboard:9464/metrics`` every second without touching
  the authenticated app. The exposition is hand-rolled like
  ``tools/network/registry/metrics.py``: no new dependency, and every label
  value comes from a bounded vocabulary (process role, org prefix, thread
  pool name).
"""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

logger = logging.getLogger("tools.dashboard.perf_telemetry")

SAMPLE_S = 1.0
#: Spike thresholds, as CPU ratios (1.0 = one core) and seconds of loop lag.
CONTAINER_SPIKE = float(os.environ.get("DASHBOARD_PERF_CONTAINER_SPIKE", "1.5"))
WORKER_SPIKE = float(os.environ.get("DASHBOARD_PERF_WORKER_SPIKE", "1.0"))
LAG_SPIKE_S = float(os.environ.get("DASHBOARD_PERF_LAG_SPIKE_S", "0.5"))
#: A thread gets its stack in a dump when it used at least this ratio. Low on
#: purpose: a CPU spike is often spread over a whole to_thread pool (a dozen
#: asyncio_N threads at 15% each), and every one of them is the evidence.
HOT_RATIO = 0.05
#: At most this many thread stacks per dump (hottest first).
MAX_STACKS = 12
#: At most this many dumped samples per episode, then one line per second.
MAX_DUMPS_PER_EPISODE = 20
#: Lag histogram bucket bounds, seconds.
LAG_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0)

_CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
#: Continuous stack sampling rate. A one-shot stack at the end of a second
#: misses the burst that used the CPU (observed 2026-09-30 16:36Z: ten pool
#: threads at 56% all caught idle), so every thread is sampled this often and
#: the samples of the last seconds are what a spike dump attributes.
STACK_HZ = float(os.environ.get("DASHBOARD_PERF_STACK_HZ", "20"))
STACK_WINDOW_S = 10


# ── /proc readers ────────────────────────────────────────────────────────


def _ticks(stat_path: str) -> int | None:
    """utime+stime ticks from a /proc stat file, or None when it vanished."""
    try:
        with open(stat_path, "rb") as fh:
            raw = fh.read().decode("utf-8", "replace")
    except OSError:
        return None
    # comm may contain spaces and parentheses: split after the LAST ')'.
    rest = raw.rsplit(")", 1)[-1].split()
    try:
        return int(rest[11]) + int(rest[12])
    except (IndexError, ValueError):
        return None


def _cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return fh.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
    except OSError:
        return ""


_ORG_RE = re.compile(r"--org\s+([0-9a-f]{8})")


def process_role(cmdline: str, pid: int, self_pid: int) -> str:
    """A bounded label for one container process."""
    if pid == self_pid or "multiprocessing.spawn" in cmdline and "spawn_main" in cmdline:
        return "worker"
    if "reload_with_notice" in cmdline or "uvicorn" in cmdline:
        return "reloader"
    if "tools.dashboard.link_serving" in cmdline:
        m = _ORG_RE.search(cmdline)
        return "connector:" + (m.group(1) if m else "unknown")
    if "tailwindcss" in cmdline:
        return "tailwind"
    if cmdline.startswith("docker ") or "/docker " in cmdline:
        return "docker-cli"
    if "tmux" in cmdline:
        return "tmux"
    if "git " in cmdline or cmdline.startswith("git"):
        return "git"
    if cmdline.startswith(("bd ", "/usr/local/bin/bd")) or " bd " in cmdline:
        return "bd"
    if "tini" in cmdline:
        return "init"
    return "other"


_POOL_RE = re.compile(r"(_\d+|-\d+|\s+\(\d+\))+$")


def thread_label(name: str) -> str:
    """A bounded label for one worker thread: pool numbering stripped, so
    ``ThreadPoolExecutor-0_12`` and ``asyncio_7`` collapse to their pool."""
    base = _POOL_RE.sub("", name or "unnamed").strip() or "unnamed"
    return base[:48]


# ── sampling ─────────────────────────────────────────────────────────────


@dataclass
class Sample:
    at: float
    elapsed: float
    procs: dict[int, tuple[str, str, float]] = field(default_factory=dict)  # pid -> (role, cmd, ratio)
    threads: dict[int, tuple[str, float]] = field(default_factory=dict)     # native tid -> (name, ratio)
    lag_max_s: float = 0.0
    heartbeat_age_s: float = 0.0

    @property
    def container(self) -> float:
        return sum(r for _, _, r in self.procs.values())

    @property
    def worker(self) -> float:
        return sum(r for role, _, r in self.procs.values() if role == "worker")

    def by_role(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for role, _, ratio in self.procs.values():
            out[role] = out.get(role, 0.0) + ratio
        return out

    def by_thread_label(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for name, ratio in self.threads.values():
            label = thread_label(name)
            out[label] = out.get(label, 0.0) + ratio
        return out


class Sampler:
    """Reads /proc into :class:`Sample` s; ``proc_root`` is injectable for tests."""

    def __init__(self, *, proc_root: str = "/proc", self_pid: int | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.proc_root = proc_root
        self.self_pid = self_pid if self_pid is not None else os.getpid()
        self.clock = clock
        self._last_at: float | None = None
        self._last_proc: dict[int, int] = {}
        self._last_thread: dict[int, int] = {}

    def _pids(self) -> list[int]:
        try:
            return [int(p) for p in os.listdir(self.proc_root) if p.isdigit()]
        except OSError:
            return []

    def _tids(self) -> list[int]:
        try:
            return [int(t) for t in os.listdir(f"{self.proc_root}/{self.self_pid}/task") if t.isdigit()]
        except OSError:
            return []

    def sample(self, *, thread_names: dict[int, str] | None = None) -> Sample | None:
        now = self.clock()
        procs = {}
        for pid in self._pids():
            t = _ticks(f"{self.proc_root}/{pid}/stat")
            if t is not None:
                procs[pid] = t
        threads = {}
        for tid in self._tids():
            t = _ticks(f"{self.proc_root}/{self.self_pid}/task/{tid}/stat")
            if t is not None:
                threads[tid] = t
        last_at, last_proc, last_thread = self._last_at, self._last_proc, self._last_thread
        self._last_at, self._last_proc, self._last_thread = now, procs, threads
        if last_at is None or now <= last_at:
            return None
        elapsed = now - last_at
        out = Sample(at=time.time(), elapsed=elapsed)
        for pid, ticks in procs.items():
            if pid not in last_proc:
                continue
            ratio = max(0, ticks - last_proc[pid]) / _CLK_TCK / elapsed
            cmd = _cmdline(pid) if self.proc_root == "/proc" else _read_text(f"{self.proc_root}/{pid}/cmdline")
            out.procs[pid] = (process_role(cmd, pid, self.self_pid), cmd[:160], ratio)
        names = thread_names or {}
        for tid, ticks in threads.items():
            if tid not in last_thread:
                continue
            ratio = max(0, ticks - last_thread[tid]) / _CLK_TCK / elapsed
            out.threads[tid] = (names.get(tid, f"tid-{tid}"), ratio)
        return out


def _read_text(path: str) -> str:
    try:
        return Path(path).read_bytes().replace(b"\0", b" ").decode("utf-8", "replace").strip()
    except OSError:
        return ""


# ── continuous stack sampling ───────────────────────────────────────────


class StackSampler:
    """Samples every thread's stack ``STACK_HZ`` times a second, keyed by the
    thread's native id, and turns each second's samples into CPU attribution
    once that second's per-thread CPU is known (:meth:`attribute`): a
    thread's measured CPU-seconds are split across the code paths it was
    sampled in. Sleeping or waiting threads therefore attribute nothing,
    whatever their stack looks like."""

    def __init__(self, *, hz: float = STACK_HZ, window_s: int = STACK_WINDOW_S,
                 frames_fn: Callable[[], dict] = sys._current_frames) -> None:
        from collections import Counter, deque
        self.hz = max(1.0, hz)
        self.frames_fn = frames_fn
        self._Counter = Counter
        self._pending: dict[int, "Counter"] = {}          # native id -> Counter[path] since last attribute()
        self._names: dict[int, str] = {}
        self.attributed = deque(maxlen=window_s)          # (unix second, Counter[(pool, path)] cpu-seconds)
        self._lock = threading.Lock()
        self.samples = 0

    def sample_once(self, *, threads: list | None = None, skip: set[int] | None = None) -> None:
        """One sample of every thread. *threads* is ``[(ident, native_id, name)]``
        (tests inject it); default from :func:`threading.enumerate`."""
        if threads is None:
            threads = [(t.ident, t.native_id, t.name) for t in threading.enumerate()]
        frames = self.frames_fn()
        skip = skip or set()
        with self._lock:
            for ident, native_id, name in threads:
                if ident in skip or native_id is None:
                    continue
                frame = frames.get(ident)
                if frame is None:
                    continue
                self._pending.setdefault(native_id, self._Counter())[repo_path(frame)] += 1
                self._names[native_id] = name
            self.samples += 1

    def attribute(self, thread_cpu: dict[int, float], elapsed: float, *, now: float | None = None) -> None:
        """Split each thread's CPU-seconds over the paths it was sampled in
        since the last call. *thread_cpu* maps native id -> CPU ratio."""
        with self._lock:
            pending, self._pending = self._pending, {}
            names = dict(self._names)
        out = self._Counter()
        for native_id, ratio in thread_cpu.items():
            counts = pending.get(native_id)
            if not counts or ratio <= 0:
                continue
            total = sum(counts.values())
            cpu_s = ratio * elapsed
            pool = thread_label(names.get(native_id, f"tid-{native_id}"))
            for path, n in counts.items():
                out[(pool, path)] += cpu_s * n / total
        with self._lock:
            self.attributed.append((int(now if now is not None else time.time()), out))

    def window(self, seconds: int, *, now: float | None = None) -> "Counter":
        cutoff = int(now if now is not None else time.time()) - seconds
        total = self._Counter()
        with self._lock:
            for second, counts in self.attributed:
                if second > cutoff:
                    total.update(counts)
        return total

    def run(self) -> None:
        me = threading.get_ident()
        period = 1.0 / self.hz
        while True:
            try:
                self.sample_once(skip={me})
            except Exception:
                logger.debug("stack sample failed", exc_info=True)
            time.sleep(period)


def render_window(counts, seconds: int, limit: int = 15) -> list[str]:
    """Where the worker's CPU went over the last *seconds*: CPU-seconds per
    thread pool and repository call path (stack samples weighted by each
    thread's measured CPU)."""
    if not counts:
        return [f"  where the worker's CPU went over the last {seconds}s: no attributed CPU"]
    total = sum(counts.values())
    lines = [f"  where the worker's CPU went over the last {seconds}s "
             f"({total:.2f} CPU-seconds attributed):"]
    for (pool, path), cpu_s in counts.most_common(limit):
        lines.append(f"    {cpu_s:6.2f}s {cpu_s / total * 100:4.0f}%  {pool:<22} {path}")
    return lines


# ── the live state: metrics + spike dumps ────────────────────────────────


class Telemetry:
    def __init__(self, *, sampler: Sampler, dump_path: Path | None,
                 heartbeat_age: Callable[[], float] | None = None,
                 loop_thread_ident: Callable[[], int | None] | None = None,
                 stacks: "StackSampler | None" = None) -> None:
        self.sampler = sampler
        self.stacks = stacks
        self.dump_path = dump_path
        self.heartbeat_age = heartbeat_age or (lambda: 0.0)
        self.loop_thread_ident = loop_thread_ident or (lambda: None)
        self._lock = threading.Lock()
        self._lag_window_max = 0.0
        self._lag_bucket_counts = [0] * (len(LAG_BUCKETS) + 1)
        self._lag_count = 0
        self._lag_sum = 0.0
        self.last: Sample | None = None
        self.cpu_seconds: dict[str, float] = {}       # role -> cumulative core-seconds
        self.thread_cpu_seconds: dict[str, float] = {}
        self.spike_episodes = 0
        self.spike_dumps = 0
        self.samples = 0
        self._episode_dumps = 0
        self._in_episode = False
        self._episode_started = 0.0

    # Called from the event-loop watchdog on every tick (cheap: a lock + adds).
    def note_lag(self, lag_s: float) -> None:
        lag = max(0.0, float(lag_s))
        with self._lock:
            if lag > self._lag_window_max:
                self._lag_window_max = lag
            self._lag_count += 1
            self._lag_sum += lag
            for i, bound in enumerate(LAG_BUCKETS):
                if lag <= bound:
                    self._lag_bucket_counts[i] += 1
                    break
            else:
                self._lag_bucket_counts[-1] += 1

    def tick(self) -> Sample | None:
        names = {}
        idents = {}
        for t in threading.enumerate():
            if t.native_id is not None:
                names[t.native_id] = t.name
                idents[t.native_id] = t.ident
        sample = self.sampler.sample(thread_names=names)
        with self._lock:
            lag_max = self._lag_window_max
            self._lag_window_max = 0.0
        if sample is None:
            return None
        sample.lag_max_s = lag_max
        if self.stacks is not None:
            self.stacks.attribute({tid: ratio for tid, (_, ratio) in sample.threads.items()},
                                  sample.elapsed)
        try:
            sample.heartbeat_age_s = max(0.0, float(self.heartbeat_age()))
        except Exception:
            sample.heartbeat_age_s = 0.0
        with self._lock:
            self.last = sample
            self.samples += 1
            for role, ratio in sample.by_role().items():
                self.cpu_seconds[role] = self.cpu_seconds.get(role, 0.0) + ratio * sample.elapsed
            for label, ratio in sample.by_thread_label().items():
                self.thread_cpu_seconds[label] = self.thread_cpu_seconds.get(label, 0.0) + ratio * sample.elapsed
        self._maybe_dump(sample, idents)
        return sample

    # ── spikes ──

    def spike_reasons(self, s: Sample) -> list[str]:
        reasons = []
        if s.container >= CONTAINER_SPIKE:
            reasons.append(f"container CPU {s.container * 100:.0f}% >= {CONTAINER_SPIKE * 100:.0f}%")
        if s.worker >= WORKER_SPIKE:
            reasons.append(f"worker CPU {s.worker * 100:.0f}% >= {WORKER_SPIKE * 100:.0f}%")
        lag = max(s.lag_max_s, s.heartbeat_age_s)
        if lag >= LAG_SPIKE_S:
            reasons.append(f"loop lag {lag:.2f}s >= {LAG_SPIKE_S:.2f}s")
        return reasons

    def _maybe_dump(self, s: Sample, idents: dict[int, int]) -> None:
        reasons = self.spike_reasons(s)
        if not reasons:
            if self._in_episode:
                self._write([f"=== episode end after {time.time() - self._episode_started:.0f}s "
                             f"({self._episode_dumps} samples) ==="])
            self._in_episode = False
            self._episode_dumps = 0
            return
        if not self._in_episode:
            self._in_episode = True
            self._episode_started = time.time()
            self.spike_episodes += 1
        self._episode_dumps += 1
        if self._episode_dumps > MAX_DUMPS_PER_EPISODE:
            self._write([_headline(s, reasons, short=True)])
            return
        self.spike_dumps += 1
        lines = render_dump(s, reasons, idents, self.loop_thread_ident())
        if self.stacks is not None:
            # First dump of an episode covers the lead-in too; later ones the last 2 s.
            span = 5 if self._episode_dumps == 1 else 2
            lines[2:2] = render_window(self.stacks.window(span), span)
        lines.extend(self._recent_lock_events())
        self._write(lines)

    _lock_events_seen = 0.0

    def _recent_lock_events(self) -> list[str]:
        """Write-lock waits/holds over a second since the last dump, with the
        thread and the frames that committed: the lock side of the spike."""
        try:
            from tools.graph import write_lock_stats
            events = [e for e in list(write_lock_stats.long_events) if e[0] > self._lock_events_seen]
        except Exception:
            return []
        if not events:
            return []
        self._lock_events_seen = max(e[0] for e in events)
        lines = ["  long SQLite write locks since the last dump:"]
        for at, kind, database, seconds, thread, frames in events[-10:]:
            stamp = time.strftime("%H:%M:%S", time.gmtime(at))
            lines.append(f"    {stamp} {kind} {seconds:.2f}s {database} thread={thread} at {frames}")
        return lines

    def _write(self, lines: list[str]) -> None:
        text = "\n".join(lines) + "\n"
        if self.dump_path is None:
            logger.warning("%s", text.rstrip())
            return
        try:
            self.dump_path.parent.mkdir(parents=True, exist_ok=True)
            _rotate(self.dump_path)
            with open(self.dump_path, "a", encoding="utf-8") as fh:
                fh.write(text)
        except OSError:
            logger.warning("perf spike dump could not be written", exc_info=True)

    # ── Prometheus exposition ──

    def exposition(self) -> str:
        with self._lock:
            s = self.last
            cpu_seconds = dict(self.cpu_seconds)
            thread_seconds = dict(self.thread_cpu_seconds)
            buckets = list(self._lag_bucket_counts)
            lag_count, lag_sum = self._lag_count, self._lag_sum
            episodes, dumps, samples = self.spike_episodes, self.spike_dumps, self.samples
        out: list[str] = []

        def metric(name, kind, help_text, rows):
            out.append(f"# HELP {name} {help_text}")
            out.append(f"# TYPE {name} {kind}")
            for labels, value in rows:
                lbl = ",".join(f'{k}="{_escape(v)}"' for k, v in labels.items())
                text = repr(float(value)) if abs(value) >= 1e5 else f"{value:.6g}"
                out.append(f"{name}{{{lbl}}} {text}" if lbl else f"{name} {text}")

        if s is not None:
            metric("dashboard_container_cpu_ratio", "gauge",
                   "CPU used by every process in the dashboard container over the last second (1 = one core).",
                   [({}, s.container)])
            metric("dashboard_process_cpu_ratio", "gauge",
                   "CPU by process role over the last second (1 = one core).",
                   [({"role": r}, v) for r, v in sorted(s.by_role().items())])
            metric("dashboard_thread_cpu_ratio", "gauge",
                   "CPU by worker thread (pool numbering stripped) over the last second.",
                   [({"thread": t}, v) for t, v in sorted(s.by_thread_label().items())])
            metric("dashboard_loop_lag_max_seconds", "gauge",
                   "Largest event-loop lag the watchdog saw in the last second.",
                   [({}, s.lag_max_s)])
            metric("dashboard_loop_heartbeat_age_seconds", "gauge",
                   "Seconds since the event loop last ran its watchdog (grows while the loop is blocked).",
                   [({}, s.heartbeat_age_s)])
        metric("dashboard_process_cpu_seconds_total", "counter",
               "Cumulative CPU core-seconds by process role since this worker started.",
               [({"role": r}, v) for r, v in sorted(cpu_seconds.items())])
        metric("dashboard_thread_cpu_seconds_total", "counter",
               "Cumulative CPU core-seconds by worker thread since this worker started.",
               [({"thread": t}, v) for t, v in sorted(thread_seconds.items())])
        out.append("# HELP dashboard_loop_lag_seconds Event-loop lag per watchdog tick.")
        out.append("# TYPE dashboard_loop_lag_seconds histogram")
        cumulative = 0
        for bound, count in zip(LAG_BUCKETS, buckets):
            cumulative += count
            out.append(f'dashboard_loop_lag_seconds_bucket{{le="{bound}"}} {cumulative}')
        cumulative += buckets[-1]
        out.append(f'dashboard_loop_lag_seconds_bucket{{le="+Inf"}} {cumulative}')
        out.append(f"dashboard_loop_lag_seconds_sum {lag_sum:.6g}")
        out.append(f"dashboard_loop_lag_seconds_count {lag_count}")
        metric("dashboard_perf_spike_episodes_total", "counter",
               "Spike episodes (CPU or lag over threshold) since this worker started.", [({}, episodes)])
        metric("dashboard_perf_spike_dumps_total", "counter",
               "Stack dumps written to perf-spikes.log since this worker started.", [({}, dumps)])
        metric("dashboard_perf_samples_total", "counter",
               "One-second samples taken since this worker started.", [({}, samples)])
        metric("dashboard_worker_start_time_seconds", "gauge",
               "Unix time this dashboard worker started (a change marks a restart or hot reload).",
               [({}, _WORKER_STARTED)])
        try:
            from tools.graph import write_lock_stats
            out.extend(write_lock_stats.exposition())
        except Exception:
            logger.debug("write-lock exposition failed", exc_info=True)
        return "\n".join(out) + "\n"


_WORKER_STARTED = time.time()


def _escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _headline(s: Sample, reasons: list[str], *, short: bool = False) -> str:
    procs = sorted(s.procs.values(), key=lambda p: p[2], reverse=True)
    threads = sorted(s.threads.values(), key=lambda t: t[1], reverse=True)
    top_proc = f"{procs[0][0]} {procs[0][2] * 100:.0f}%" if procs else "none"
    top_thread = f"{threads[0][0]} {threads[0][1] * 100:.0f}%" if threads else "none"
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(s.at))
    head = f"{stamp} SPIKE {'; '.join(reasons)} | hottest process {top_proc} | hottest worker thread {top_thread}"
    return head if not short else head + " (episode continues; dump cap reached)"


def render_dump(s: Sample, reasons: list[str], idents: dict[int, int],
                loop_ident: int | None, *, frames: dict | None = None) -> list[str]:
    lines = ["", _headline(s, reasons)]
    lines.append("  processes (CPU over the last second):")
    for pid, (role, cmd, ratio) in sorted(s.procs.items(), key=lambda kv: kv[1][2], reverse=True)[:8]:
        if ratio < 0.01:
            break
        lines.append(f"    {ratio * 100:6.1f}%  pid {pid:<8} {role:<22} {cmd[:110]}")
    lines.append("  worker threads (CPU over the last second):")
    hot: list[int] = []
    for tid, (name, ratio) in sorted(s.threads.items(), key=lambda kv: kv[1][1], reverse=True)[:8]:
        if ratio < 0.01:
            break
        lines.append(f"    {ratio * 100:6.1f}%  tid {tid:<8} {name}")
    for tid, (name, ratio) in sorted(s.threads.items(), key=lambda kv: kv[1][1], reverse=True)[:MAX_STACKS]:
        if ratio >= HOT_RATIO:
            hot.append(tid)
    current = frames if frames is not None else sys._current_frames()
    # Per pool: how many busy threads, their total CPU, and which repository
    # function each one is in right now. This is the line that names a spike
    # spread over a to_thread pool.
    pools: dict[str, list] = {}
    for tid in hot:
        name, ratio = s.threads[tid]
        frame = current.get(idents.get(tid)) if idents.get(tid) is not None else None
        pools.setdefault(thread_label(name), []).append((ratio, repo_leaf(frame)))
    if pools:
        lines.append("  busy threads by pool, and the repository function each is in:")
        for pool, rows in sorted(pools.items(), key=lambda kv: -sum(r for r, _ in kv[1])):
            leaves: dict[str, int] = {}
            for _, leaf in rows:
                leaves[leaf] = leaves.get(leaf, 0) + 1
            top = ", ".join(f"{leaf} x{n}" for leaf, n in sorted(leaves.items(), key=lambda kv: -kv[1])[:5])
            lines.append(f"    {pool}: {len(rows)} threads, {sum(r for r, _ in rows) * 100:.0f}% | {top}")
    loop_title = (f"event-loop thread (lag max {s.lag_max_s:.2f}s, heartbeat age "
                  f"{s.heartbeat_age_s:.2f}s)")
    wanted: list[tuple[str, int | None]] = [
        ((loop_title + ", " if loop_ident is not None and idents.get(t) == loop_ident else "")
         + f"thread tid {t} ({s.threads[t][0]}, {s.threads[t][1] * 100:.0f}%)", idents.get(t))
        for t in hot]
    if loop_ident is not None and loop_ident not in [i for _, i in wanted]:
        wanted.append((loop_title, loop_ident))
    for title, ident in wanted:
        frame = current.get(ident) if ident is not None else None
        lines.append(f"  stack: {title}")
        if frame is None:
            lines.append("    (no Python frame: native code or the thread exited)")
            continue
        for entry in traceback.format_stack(frame)[-12:]:
            for part in entry.rstrip().splitlines():
                lines.append("    " + part)
    return lines


_APP_ROOT = str(Path(__file__).resolve().parents[2]) + "/"
#: A thread whose innermost frame is one of these is waiting, not working.
_IDLE_NAMES = frozenset({"wait", "get", "_worker", "select", "poll", "accept", "recv", "recv_into",
                         "read", "readline", "sleep", "acquire", "join", "_wait_for_tstate_lock"})
_IDLE_FILES = ("threading.py", "queue.py", "concurrent/futures/thread.py", "selectors.py",
               "socket.py", "ssl.py", "subprocess.py", "inotify_simple.py", "socketserver.py")


def repo_path(frame, depth: int = 4) -> str:
    """Up to *depth* repository frames of *frame*, innermost first
    (``a.py:10 f < b.py:20 g``), ``idle`` for a thread waiting for work, or
    the innermost frame when no repository frame is on the stack."""
    leaf = repo_leaf(frame)
    if leaf in ("idle", "native") or ".py:" not in leaf or leaf.startswith(("threading.py", "queue.py", "selectors.py")):
        return leaf
    out = []
    f = frame
    while f is not None and len(out) < depth:
        name = f.f_code.co_filename
        if name.startswith(_APP_ROOT) and "perf_telemetry" not in name:
            out.append(f"{name[len(_APP_ROOT):]}:{f.f_lineno} {f.f_code.co_name}")
        f = f.f_back
    return " < ".join(out) if out else leaf


def repo_leaf(frame) -> str:
    """The innermost repository frame of *frame* as ``path:line function``,
    or ``idle`` for a pool thread waiting for work, or ``native`` when no
    repository frame is on the stack."""
    if frame is None:
        return "native"
    top = frame.f_code
    if top.co_name in _IDLE_NAMES and any(k in top.co_filename for k in _IDLE_FILES):
        return "idle"
    f = frame
    while f is not None:
        name = f.f_code.co_filename
        if name.startswith(_APP_ROOT) and "perf_telemetry" not in name:
            return f"{name[len(_APP_ROOT):]}:{f.f_lineno} {f.f_code.co_name}"
        f = f.f_back
    top = frame.f_code
    if top.co_name in ("wait", "get", "_worker", "select") and ("threading" in top.co_filename
                                                                  or "queue" in top.co_filename
                                                                  or "thread.py" in top.co_filename
                                                                  or "selectors" in top.co_filename):
        return "idle"
    return f"{Path(top.co_filename).name}:{frame.f_lineno} {top.co_name}"


def _rotate(path: Path, max_bytes: int = 20 * 1024 * 1024, backups: int = 3) -> None:
    try:
        if path.stat().st_size < max_bytes:
            return
    except OSError:
        return
    for i in range(backups - 1, 0, -1):
        src = path.with_name(f"{path.name}.{i}")
        if src.exists():
            src.replace(path.with_name(f"{path.name}.{i + 1}"))
    path.replace(path.with_name(f"{path.name}.1"))


# ── process wiring ───────────────────────────────────────────────────────

_telemetry: Telemetry | None = None
_started = False


def note_lag(lag_s: float) -> None:
    t = _telemetry
    if t is not None:
        t.note_lag(lag_s)


def telemetry() -> Telemetry | None:
    return _telemetry


def start(*, heartbeat_age: Callable[[], float], loop_thread_ident: Callable[[], int | None],
          log_dir: Path) -> Telemetry | None:
    """Start the one-second sampler and the metrics listener, once per worker.
    ``DASHBOARD_PERF_TELEMETRY=off`` disables both."""
    global _telemetry, _started
    if _started or os.environ.get("DASHBOARD_PERF_TELEMETRY", "on").lower() in ("0", "off", "false"):
        return _telemetry
    _started = True
    stacks = StackSampler() if STACK_HZ > 0 else None
    _telemetry = Telemetry(sampler=Sampler(), dump_path=Path(log_dir) / "perf-spikes.log",
                           heartbeat_age=heartbeat_age, loop_thread_ident=loop_thread_ident,
                           stacks=stacks)
    if stacks is not None:
        threading.Thread(target=stacks.run, name="perf-stack-sampler", daemon=True).start()
    threading.Thread(target=_run, args=(_telemetry,), name="perf-telemetry", daemon=True).start()
    port = int(os.environ.get("DASHBOARD_METRICS_PORT", "9464"))
    if port > 0:
        threading.Thread(target=_serve, args=(_telemetry, port), name="perf-metrics", daemon=True).start()
    return _telemetry


def _run(t: Telemetry) -> None:
    next_at = time.monotonic()
    while True:
        next_at += SAMPLE_S
        try:
            t.tick()
        except Exception:
            logger.debug("perf telemetry sample failed", exc_info=True)
        delay = next_at - time.monotonic()
        if delay < 0:
            next_at = time.monotonic()
            delay = 0
        time.sleep(delay)


def _serve(t: Telemetry, port: int) -> None:
    """Serve /metrics. During a hot reload the outgoing worker may still hold
    the port; retry until it is released rather than giving up."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.split("?", 1)[0] != "/metrics":
                self.send_error(404)
                return
            body = t.exposition().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # one scrape per second: never log them
            return

    ThreadingHTTPServer.allow_reuse_address = True
    while True:
        try:
            server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        except OSError:
            time.sleep(2.0)
            continue
        server.daemon_threads = True
        server.serve_forever()
