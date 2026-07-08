"""Per-session resource metrics collector — CPU, RAM, disk.

Phase 1 of the session-card resource stats work (design d2250266): collect
the numbers cheaply; the UI consumes them in a later phase.

Design constraints (operator-set, 2026-07-08):

- **Live sessions only.** The poll set is ``get_live_sessions()``. A session
  that dies gets exactly one final disk measurement (persisted to the DB so
  ended cards can show a footprint) and is never polled again.
- **Two cadences.** CPU/RAM are cheap (a handful of file reads) and sample
  every ``cpu_interval`` (default 6s). Disk is the expensive one — walking
  directories counting bytes — so it runs at ``disk_interval`` (default 60s)
  per session, and at most ONE session's disk is measured per tick
  (round-robin stagger), never a burst.
- **The collector's own cost is a first-class output.** Every tick and every
  disk measure records its wall time into rolling windows exposed via
  :meth:`ResourceMonitor.get_health`; ticks over threshold log warnings.

Measurement backends, resolved once per session and cached:

- Container sessions (docker container named after the tmux row) → one
  ``docker inspect`` resolves the container id + overlay upper dir, then
  every CPU/RAM sample is direct cgroup file reads (v2 systemd scope,
  v2 cgroupfs, or v1 layouts — probed at resolve time).
- Host sessions → one ``tmux display-message`` resolves the pane PID, then
  samples walk the ``/proc`` process subtree summing utime+stime and RSS.

Disk components per session: docker overlay upper dir (direct ``du`` when
readable, else one amortized ``docker ps -s`` per disk interval shared by
all containers), the session run dir under ``data/agent-runs/``, and the
session's worktrees under ``data/worktrees/{name}/``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from tools.dashboard.dao.dashboard_db import (
    get_live_sessions,
    update_disk_usage,
)

logger = logging.getLogger(__name__)

_CLK_TCK = os.sysconf("SC_CLK_TCK")
_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")

# Warn when the collector itself gets expensive — the whole point of this
# module is to be invisible.
_TICK_WARN_MS = 100.0
_DISK_WARN_MS = 2000.0

# Sessions the liveness loop considers still-booting have no container yet;
# resolving them would burn a docker-inspect for nothing. Mirrors
# SessionMonitor._STARTUP_BOOTING_STATES.
_BOOTING_STATES = frozenset({
    "requesting", "preparing_workspace", "launching_container",
    "harness_starting",
})


def _read_first_int(path: Path) -> int:
    return int(path.read_text().split()[0])


def _read_stat_field(path: Path, key: str) -> int | None:
    for line in path.read_text().splitlines():
        k, _, v = line.partition(" ")
        if k == key:
            return int(v)
    return None


class _CgroupReader:
    """Direct cgroup reads for one container. Probed once, then O(file-read)."""

    #: Overridable in tests to point at a fake tree.
    ROOT = Path("/sys/fs/cgroup")

    #: (backend name, cpu-dir template, mem-dir template) — cpu/mem split
    #: matters only for cgroup v1 where controllers live in separate trees.
    _LAYOUTS = (
        ("v2-systemd", "system.slice/docker-{cid}.scope",
         "system.slice/docker-{cid}.scope"),
        ("v2-cgroupfs", "docker/{cid}",
         "docker/{cid}"),
        ("v1", "cpu,cpuacct/docker/{cid}",
         "memory/docker/{cid}"),
        ("v1-split", "cpuacct/docker/{cid}",
         "memory/docker/{cid}"),
    )

    def __init__(self, backend: str, cpu_dir: Path, mem_dir: Path) -> None:
        self.backend = backend
        self._v2 = backend.startswith("v2")
        self._cpu_dir = cpu_dir
        self._mem_dir = mem_dir

    @classmethod
    def probe(cls, container_id: str) -> "_CgroupReader | None":
        for backend, cpu_tpl, mem_tpl in cls._LAYOUTS:
            cpu_dir = cls.ROOT / cpu_tpl.format(cid=container_id)
            probe_file = cpu_dir / ("cpu.stat" if backend.startswith("v2")
                                    else "cpuacct.usage")
            try:
                if probe_file.is_file():
                    return cls(backend, cpu_dir,
                               cls.ROOT / mem_tpl.format(cid=container_id))
            except OSError:
                continue
        return None

    def read_cpu_ns(self) -> int:
        if self._v2:
            usec = _read_stat_field(self._cpu_dir / "cpu.stat", "usage_usec")
            return (usec or 0) * 1000
        return _read_first_int(self._cpu_dir / "cpuacct.usage")

    def read_mem_bytes(self) -> int:
        # Parity with `docker stats`: usage minus inactive file cache, so a
        # session doing heavy file IO doesn't read as a memory hog.
        if self._v2:
            usage = _read_first_int(self._mem_dir / "memory.current")
            inactive = _read_stat_field(
                self._mem_dir / "memory.stat", "inactive_file") or 0
        else:
            usage = _read_first_int(self._mem_dir / "memory.usage_in_bytes")
            inactive = _read_stat_field(
                self._mem_dir / "memory.stat", "total_inactive_file") or 0
        return max(0, usage - inactive)


def _proc_tree_sample(root_pid: int) -> tuple[int, int]:
    """Sum (cpu_ns, rss_bytes) over a process subtree via /proc.

    Children are discovered through ``/proc/<pid>/task/*/children`` — no
    full /proc scan. Processes that vanish mid-walk are skipped.
    """
    total_ticks = 0
    total_rss_pages = 0
    stack = [root_pid]
    seen: set[int] = set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        base = Path(f"/proc/{pid}")
        try:
            stat = (base / "stat").read_text()
            # utime/stime are fields 14/15 (1-indexed) AFTER the comm field,
            # which may itself contain spaces — split past the closing paren.
            fields = stat.rsplit(")", 1)[1].split()
            total_ticks += int(fields[11]) + int(fields[12])
            total_rss_pages += int((base / "statm").read_text().split()[1])
            for task in (base / "task").iterdir():
                children = task / "children"
                try:
                    stack.extend(int(c) for c in
                                 children.read_text().split())
                except OSError:
                    continue
        except (OSError, IndexError, ValueError):
            continue
    cpu_ns = int(total_ticks * (1_000_000_000 / _CLK_TCK))
    return cpu_ns, total_rss_pages * _PAGE_SIZE


def _du_bytes(path: Path) -> int:
    """Disk usage of a tree in bytes (st_blocks, matching ``du``)."""
    total = 0
    stack = [path]
    while stack:
        p = stack.pop()
        try:
            with os.scandir(p) as it:
                for entry in it:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    total += st.st_blocks * 512
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
        except (NotADirectoryError, FileNotFoundError):
            try:
                total += path.stat().st_blocks * 512
            except OSError:
                pass
        except OSError:
            continue
    return total


_SIZE_UNITS = {"B": 1, "kB": 10**3, "KB": 10**3, "MB": 10**6,
               "GB": 10**9, "TB": 10**12, "KiB": 2**10, "MiB": 2**20,
               "GiB": 2**30, "TiB": 2**40}


def _parse_docker_size(text: str) -> int | None:
    """Parse the SizeRw half of docker's ``12.3MB (virtual 4.5GB)``."""
    token = text.split("(", 1)[0].strip()
    for unit in sorted(_SIZE_UNITS, key=len, reverse=True):
        if token.endswith(unit):
            try:
                return int(float(token[: -len(unit)]) * _SIZE_UNITS[unit])
            except ValueError:
                return None
    return None


def _run_dir_for(row: dict) -> Path | None:
    """The session's run dir: the ancestor directly under ``agent-runs``."""
    raw = row.get("resolution_dir") or row.get("jsonl_path")
    if not raw:
        return None
    p = Path(raw)
    for anc in p.parents:
        if anc.parent.name == "agent-runs":
            return anc
    return None


@dataclass
class _SessionState:
    kind: str = "unresolved"            # container | host | unresolved
    backend: str = ""                   # cgroup layout / "proc"
    container_id: str = ""
    upper_dir: Path | None = None
    pane_pid: int = 0
    reader: _CgroupReader | None = None
    next_resolve_at: float = 0.0
    resolve_backoff: float = 5.0
    # cpu delta state
    last_cpu_ns: int = 0
    last_cpu_at: float = 0.0
    # latest samples
    cpu_pct: float | None = None
    mem_bytes: int | None = None
    sampled_at: float = 0.0
    disk: dict | None = None
    disk_sampled_at: float = 0.0
    next_disk_at: float = 0.0           # 0 → due immediately
    history: deque = field(default_factory=lambda: deque(maxlen=100))


class _RollingTimer:
    def __init__(self, maxlen: int = 240) -> None:
        self._samples: deque[float] = deque(maxlen=maxlen)
        self.count = 0

    def record(self, ms: float) -> None:
        self._samples.append(ms)
        self.count += 1

    def stats(self) -> dict:
        if not self._samples:
            return {"count": 0, "avg_ms": 0.0, "p95_ms": 0.0, "max_ms": 0.0}
        ordered = sorted(self._samples)
        return {
            "count": self.count,
            "avg_ms": round(sum(ordered) / len(ordered), 3),
            "p95_ms": round(ordered[int(len(ordered) * 0.95) - 1], 3),
            "max_ms": round(ordered[-1], 3),
        }


class ResourceMonitor:
    """Samples CPU/RAM/disk for live sessions on two cadences."""

    def __init__(
        self,
        cpu_interval: float | None = None,
        disk_interval: float | None = None,
        worktrees_dir: Path | None = None,
    ) -> None:
        self.cpu_interval = cpu_interval or float(
            os.environ.get("RESOURCE_CPU_INTERVAL", "6"))
        self.disk_interval = disk_interval or float(
            os.environ.get("RESOURCE_DISK_INTERVAL", "60"))
        if worktrees_dir is None:
            from agents.workspace_manager import WORKTREES_DIR
            worktrees_dir = WORKTREES_DIR
        self._worktrees_dir = worktrees_dir
        self._states: dict[str, _SessionState] = {}
        self._task: asyncio.Task | None = None
        self._tick_timer = _RollingTimer()
        self._disk_timer = _RollingTimer()
        self._last_tick_at = 0.0
        self._last_disk_session = ""
        # docker ps -s fallback cache (one subprocess per disk interval
        # shared across ALL containers, only used when the overlay upper
        # dir is unreadable, e.g. /var/lib/docker owned by root)
        self._docker_sizes: dict[str, int] = {}
        self._docker_sizes_at = 0.0
        self._upper_dir_readable: bool | None = None

    # ── lifecycle ────────────────────────────────────────────────

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())
            logger.info(
                "resource_monitor: started (cpu=%ss disk=%ss)",
                self.cpu_interval, self.disk_interval)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                rows = get_live_sessions()
                await asyncio.to_thread(self._tick, rows, time.time())
            except Exception:
                logger.exception("resource_monitor: tick error")
            await asyncio.sleep(self.cpu_interval)

    # ── death hook ───────────────────────────────────────────────

    async def on_session_dead(self, tmux_name: str, row: dict | None = None) -> None:
        """One final disk measurement, persist, then stop tracking forever.

        Called by the session monitor on every death path. Never raises —
        a metrics failure must not break the liveness loop.
        """
        try:
            await asyncio.to_thread(self._finalize, tmux_name, row)
        except Exception:
            logger.exception(
                "resource_monitor: final disk measure failed for %s", tmux_name)

    def _finalize(self, tmux_name: str, row: dict | None) -> None:
        if row is None:
            from tools.dashboard.dao.dashboard_db import get_session
            row = get_session(tmux_name)
        state = self._states.pop(tmux_name, None)
        if row is None:
            return
        started = time.monotonic()
        disk = self._measure_disk(tmux_name, row, state, final=True)
        elapsed_ms = (time.monotonic() - started) * 1000
        self._disk_timer.record(elapsed_ms)
        if disk is not None:
            update_disk_usage(
                tmux_name, disk["total"], json.dumps(disk), time.time())
            logger.info(
                "resource_monitor: final disk for %s = %d bytes (%.0fms)",
                tmux_name, disk["total"], elapsed_ms)

    # ── sampling (runs in a worker thread) ───────────────────────

    def _tick(self, rows: list[dict], now: float) -> None:
        started = time.monotonic()
        live_names = {r["tmux_name"] for r in rows}
        for gone in set(self._states) - live_names:
            self._states.pop(gone, None)

        disk_candidate: tuple[float, dict, _SessionState] | None = None
        for row in rows:
            name = row["tmux_name"]
            if row.get("startup_state") in _BOOTING_STATES:
                continue
            state = self._states.setdefault(name, _SessionState())
            if state.kind == "unresolved":
                if now < state.next_resolve_at:
                    continue
                self._resolve(name, state)
                if state.kind == "unresolved":
                    continue
            self._sample_cpu_mem(name, state, now)
            due = state.next_disk_at
            if due <= now and (disk_candidate is None or due < disk_candidate[0]):
                disk_candidate = (due, row, state)

        # Stagger: at most ONE session's disk per tick.
        if disk_candidate is not None:
            _, row, state = disk_candidate
            name = row["tmux_name"]
            disk_started = time.monotonic()
            disk = self._measure_disk(name, row, state, final=False)
            disk_ms = (time.monotonic() - disk_started) * 1000
            self._disk_timer.record(disk_ms)
            self._last_disk_session = name
            if disk_ms > _DISK_WARN_MS:
                logger.warning(
                    "resource_monitor: slow disk measure %s: %.0fms (%s)",
                    name, disk_ms, json.dumps(disk.get("timings_ms", {}))
                    if disk else "-")
            state.next_disk_at = now + self.disk_interval
            if disk is not None:
                state.disk = disk
                state.disk_sampled_at = now
                update_disk_usage(name, disk["total"], json.dumps(disk), now)

        tick_ms = (time.monotonic() - started) * 1000
        self._tick_timer.record(tick_ms)
        self._last_tick_at = now
        if tick_ms > _TICK_WARN_MS:
            logger.warning(
                "resource_monitor: slow tick %.0fms over %d sessions",
                tick_ms, len(rows))

    def _resolve(self, name: str, state: _SessionState) -> None:
        """Bind a session to its measurement backend. One-time subprocess."""
        try:
            out = subprocess.run(
                ["docker", "inspect", "-f",
                 "{{.Id}}\t{{.GraphDriver.Data.UpperDir}}", name],
                capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            out = None
        if out is not None and out.returncode == 0 and out.stdout.strip():
            cid, _, upper = out.stdout.strip().partition("\t")
            reader = _CgroupReader.probe(cid)
            if reader is not None:
                state.kind = "container"
                state.backend = reader.backend
                state.container_id = cid
                state.upper_dir = Path(upper) if upper else None
                state.reader = reader
                logger.info("resource_monitor: %s → container cgroup %s",
                            name, reader.backend)
                return
            logger.warning(
                "resource_monitor: no readable cgroup for %s (id=%.12s)",
                name, cid)
        else:
            try:
                out = subprocess.run(
                    ["tmux", "display-message", "-p", "-t", name,
                     "#{pane_pid}"],
                    capture_output=True, text=True, timeout=5)
                if out.returncode == 0 and out.stdout.strip().isdigit():
                    state.kind = "host"
                    state.backend = "proc"
                    state.pane_pid = int(out.stdout.strip())
                    logger.info("resource_monitor: %s → host pid %d",
                                name, state.pane_pid)
                    return
            except (OSError, subprocess.TimeoutExpired):
                pass
        state.next_resolve_at = time.time() + state.resolve_backoff
        state.resolve_backoff = min(state.resolve_backoff * 2, 120.0)

    def _sample_cpu_mem(self, name: str, state: _SessionState, now: float) -> None:
        try:
            if state.kind == "container":
                cpu_ns = state.reader.read_cpu_ns()
                mem = state.reader.read_mem_bytes()
            else:
                cpu_ns, mem = _proc_tree_sample(state.pane_pid)
        except OSError:
            # Container restarted / process gone — re-resolve next tick.
            self._states[name] = _SessionState()
            return
        if state.last_cpu_at > 0 and now > state.last_cpu_at:
            delta_ns = max(0, cpu_ns - state.last_cpu_ns)
            state.cpu_pct = round(
                delta_ns / ((now - state.last_cpu_at) * 1e9) * 100, 2)
        state.last_cpu_ns = cpu_ns
        state.last_cpu_at = now
        state.mem_bytes = mem
        state.sampled_at = now
        if state.cpu_pct is not None:
            state.history.append(
                (round(now, 1), state.cpu_pct, mem))

    # ── disk ─────────────────────────────────────────────────────

    def _measure_disk(self, name: str, row: dict,
                      state: _SessionState | None, *, final: bool) -> dict | None:
        components: dict[str, int] = {}
        timings: dict[str, float] = {}

        def timed(key: str, fn) -> None:
            t0 = time.monotonic()
            try:
                val = fn()
            except OSError:
                val = None
            timings[key] = round((time.monotonic() - t0) * 1000, 2)
            if val is not None:
                components[key] = val

        run_dir = _run_dir_for(row)
        if run_dir is not None and run_dir.is_dir():
            timed("run_dir", lambda: _du_bytes(run_dir))
        elif row.get("jsonl_path"):
            timed("jsonl", lambda: Path(row["jsonl_path"]).stat().st_size)

        wt_dir = self._worktrees_dir / name
        if wt_dir.is_dir():
            timed("worktrees", lambda: _du_bytes(wt_dir))

        # Container writable layer — live sessions only; a closed session
        # has no container storage (its container is gone).
        if state is not None and state.kind == "container" and not final:
            layer = self._container_layer_bytes(name, state)
            if layer is not None:
                key, val, ms = layer
                components[key] = val
                timings[key] = ms

        if not components:
            return None
        return {
            "total": sum(components.values()),
            "components": components,
            "timings_ms": timings,
        }

    def _container_layer_bytes(
            self, name: str, state: _SessionState) -> tuple[str, int, float] | None:
        t0 = time.monotonic()
        if state.upper_dir is not None and self._upper_dir_readable is not False:
            try:
                val = _du_bytes(state.upper_dir)
                # _du_bytes swallows per-entry errors; verify readability once
                # so an unreadable /var/lib/docker doesn't report 0 forever.
                if self._upper_dir_readable is None:
                    os.scandir(state.upper_dir).close()
                    self._upper_dir_readable = True
                return ("container_fs", val,
                        round((time.monotonic() - t0) * 1000, 2))
            except PermissionError:
                self._upper_dir_readable = False
                logger.info(
                    "resource_monitor: overlay upper dir unreadable — "
                    "falling back to docker ps -s")
            except OSError:
                return None
        # Amortized fallback: one `docker ps -s` per disk interval, shared.
        now = time.time()
        if now - self._docker_sizes_at > self.disk_interval:
            try:
                out = subprocess.run(
                    ["docker", "ps", "-s", "--format",
                     "{{.Names}}\t{{.Size}}"],
                    capture_output=True, text=True, timeout=30)
                if out.returncode == 0:
                    sizes = {}
                    for line in out.stdout.splitlines():
                        n, _, sz = line.partition("\t")
                        parsed = _parse_docker_size(sz)
                        if parsed is not None:
                            sizes[n] = parsed
                    self._docker_sizes = sizes
                    self._docker_sizes_at = now
            except (OSError, subprocess.TimeoutExpired):
                return None
        if name in self._docker_sizes:
            return ("container_fs", self._docker_sizes[name],
                    round((time.monotonic() - t0) * 1000, 2))
        return None

    # ── read side ────────────────────────────────────────────────

    def snapshot(self, include_history: bool = False) -> dict:
        sessions = {}
        for name, s in self._states.items():
            if s.kind == "unresolved":
                continue
            entry = {
                "kind": s.kind,
                "backend": s.backend,
                "cpu_pct": s.cpu_pct,
                "mem_bytes": s.mem_bytes,
                "sampled_at": s.sampled_at,
                "disk": s.disk,
                "disk_sampled_at": s.disk_sampled_at,
            }
            if include_history:
                entry["history"] = list(s.history)
            sessions[name] = entry
        return {"sessions": sessions, "health": self.get_health()}

    def get_health(self) -> dict:
        unresolved = [n for n, s in self._states.items()
                      if s.kind == "unresolved"]
        return {
            "cpu_interval_s": self.cpu_interval,
            "disk_interval_s": self.disk_interval,
            "ncpus": os.cpu_count(),
            "tracked": len(self._states) - len(unresolved),
            "unresolved": unresolved,
            "last_tick_at": self._last_tick_at,
            "last_disk_session": self._last_disk_session,
            "tick": self._tick_timer.stats(),
            "disk_measure": self._disk_timer.stats(),
            "overlay_du_readable": self._upper_dir_readable,
        }


resource_monitor = ResourceMonitor()
