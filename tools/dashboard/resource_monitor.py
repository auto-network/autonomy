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

# Disk components fall into two cost classes, measured on separate clocks:
# - fast: the session run dir (JSONLs, attachments) — sub-ms to ~40ms walks,
#   and the component that actually grows with session activity.
# - heavy: worktrees (a full repo checkout walks in 0.25s warm / 7.5s cold
#   cache — cost tracks inode count, not bytes) and the container overlay
#   layer (the docker ps -s fallback costs ~1.4s warm at the daemon).
# Cadence adapts to session age: young sessions (first 30min) are building
# up their footprint, so measure more often; old sessions settle and slow
# down. The force-refresh API bypasses all of this on demand, which is what
# lets the baseline be slow.
_FAST, _HEAVY = "fast", "heavy"
_DISK_CADENCE: dict[str, tuple[float, float]] = {
    # class: (young interval, old interval) in seconds
    _FAST: (60.0, 300.0),
    _HEAVY: (300.0, 1800.0),
}
_YOUNG_AGE_S = 30 * 60.0
_DOCKER_SIZES_TTL = 240.0       # docker ps -s fallback cache lifetime
_DOCKER_SIZES_FORCE_TTL = 5.0   # …when a force refresh asks for fresh data

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
    # per-class disk clocks; missing key → due immediately
    next_disk_at: dict[str, float] = field(default_factory=dict)
    # entry_count at each class's last scan — unchanged count means an idle
    # session, and idle sessions don't get rescanned at all
    disk_entry_count: dict[str, int] = field(default_factory=dict)
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
        disk_cadence: dict[str, tuple[float, float]] | None = None,
        worktrees_dir: Path | None = None,
    ) -> None:
        self.cpu_interval = cpu_interval or float(
            os.environ.get("RESOURCE_CPU_INTERVAL", "6"))
        self.disk_cadence = disk_cadence or _DISK_CADENCE
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
        self._event_bus = None

    # ── lifecycle ────────────────────────────────────────────────

    async def start(self, event_bus=None) -> None:
        if self._task is None:
            self._event_bus = event_bus
            self._task = asyncio.create_task(self._loop())
            logger.info(
                "resource_monitor: started (cpu=%ss disk=%s)",
                self.cpu_interval, self.disk_cadence)

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
                # Push the latest samples (no history — the frontend keeps
                # its own ring buffer by appending these) over the shared
                # SSE bus. Dedup means an all-idle fleet broadcasts nothing;
                # the bus's last-value cache replays state to newly opened
                # pages on handler registration.
                if self._event_bus is not None:
                    await self._event_bus.broadcast(
                        "resources",
                        self.snapshot(include_history=False)["sessions"])
            except Exception:
                logger.exception("resource_monitor: tick error")
            await asyncio.sleep(self.cpu_interval)

    # ── snapshot / restore (hot-reload continuity) ───────────────

    _SNAPSHOT_VERSION = 1
    # Sessions whose last sample is older than this are not restored —
    # a snapshot from a long-dead process would splice stale points into
    # the sparkline as if they were adjacent to fresh ones.
    _SNAPSHOT_MAX_AGE_S = 900.0

    def save_state(self, path) -> None:
        """Persist ring buffers + sampling state to ``path`` atomically.

        Called from the server's shutdown handler (same contract as
        EventBus.snapshot) so a hot reload doesn't blow away ~10 minutes
        of sparkline history. Measurement handles (cgroup dirs, pane
        PIDs) are deliberately NOT persisted — they re-resolve with one
        docker inspect / tmux call per session on the first tick.
        Failures are logged, never raised.
        """
        try:
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            sessions = {}
            for name, s in self._states.items():
                if s.kind == "unresolved" and not s.history:
                    continue
                sessions[name] = {
                    "history": list(s.history),
                    "last_cpu_ns": s.last_cpu_ns,
                    "last_cpu_at": s.last_cpu_at,
                    "cpu_pct": s.cpu_pct,
                    "mem_bytes": s.mem_bytes,
                    "sampled_at": s.sampled_at,
                    "disk": s.disk,
                    "disk_sampled_at": s.disk_sampled_at,
                    "next_disk_at": dict(s.next_disk_at),
                    "disk_entry_count": dict(s.disk_entry_count),
                }
            state = {"version": self._SNAPSHOT_VERSION, "sessions": sessions}
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_text(json.dumps(state))
            tmp.replace(target)
        except Exception:
            logger.exception("resource_monitor.save_state(%s) failed", path)

    def load_state(self, path) -> bool:
        """Load a prior process's snapshot. Returns True if state was loaded.

        Restored sessions come back as ``unresolved`` — the next tick
        re-resolves their measurement handle and keeps appending to the
        carried-over history. ``last_cpu_ns`` survives so the first
        post-restart CPU delta is computed across the reload gap instead
        of being lost. Missing/corrupt/stale snapshots restore nothing.
        """
        try:
            target = Path(path)
            if not target.is_file():
                return False
            state = json.loads(target.read_text())
            if (not isinstance(state, dict)
                    or state.get("version") != self._SNAPSHOT_VERSION):
                return False
            now = time.time()
            restored = 0
            for name, row in (state.get("sessions") or {}).items():
                if (now - (row.get("sampled_at") or 0)) > self._SNAPSHOT_MAX_AGE_S:
                    continue
                s = _SessionState()
                s.history = deque(
                    (tuple(p) for p in row.get("history") or []), maxlen=100)
                s.last_cpu_ns = int(row.get("last_cpu_ns") or 0)
                s.last_cpu_at = float(row.get("last_cpu_at") or 0)
                s.cpu_pct = row.get("cpu_pct")
                s.mem_bytes = row.get("mem_bytes")
                s.sampled_at = float(row.get("sampled_at") or 0)
                s.disk = row.get("disk")
                s.disk_sampled_at = float(row.get("disk_sampled_at") or 0)
                s.next_disk_at = dict(row.get("next_disk_at") or {})
                s.disk_entry_count = dict(row.get("disk_entry_count") or {})
                self._states[name] = s
                restored += 1
            if restored:
                logger.info(
                    "resource_monitor: restored %d session buffer(s) from %s",
                    restored, target)
            return restored > 0
        except Exception:
            logger.exception("resource_monitor.load_state(%s) failed", path)
            return False

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
        disk = self._measure_disk(tmux_name, row, state,
                                  classes=(_FAST, _HEAVY), final=True)
        elapsed_ms = (time.monotonic() - started) * 1000
        self._disk_timer.record(elapsed_ms)
        if disk is not None:
            disk["total"] = sum(disk["components"].values())
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
            due = min((state.next_disk_at.get(c, 0.0)
                       for c in self.disk_cadence), default=0.0)
            if due <= now and (disk_candidate is None or due < disk_candidate[0]):
                disk_candidate = (due, row, state)

        # Stagger: at most ONE session's disk per tick, and only its due
        # component classes.
        if disk_candidate is not None:
            _, row, state = disk_candidate
            self._scan_disk(row, state, now)

        tick_ms = (time.monotonic() - started) * 1000
        self._tick_timer.record(tick_ms)
        self._last_tick_at = now
        if tick_ms > _TICK_WARN_MS:
            logger.warning(
                "resource_monitor: slow tick %.0fms over %d sessions",
                tick_ms, len(rows))

    _COMPONENT_CLASS = {"run_dir": _FAST, "jsonl": _FAST,
                        "worktrees": _HEAVY, "container_fs": _HEAVY}

    def _next_interval(self, row: dict, cls: str, now: float) -> float:
        young, old = self.disk_cadence[cls]
        age = now - (row.get("created_at") or now)
        return young if age < _YOUNG_AGE_S else old

    def _scan_disk(self, row: dict, state: _SessionState, now: float,
                   *, force: bool = False) -> dict | None:
        """Measure the session's due disk component classes and persist.

        Idle skip: a class whose ``entry_count`` hasn't moved since its last
        scan is rescheduled without walking anything — a completely idle
        session stops scanning disk entirely. ``force`` (the refresh API)
        bypasses both the clocks and the idle skip.
        """
        name = row["tmux_name"]
        due = [c for c in self.disk_cadence
               if force or state.next_disk_at.get(c, 0.0) <= now]
        for c in due:
            state.next_disk_at[c] = now + self._next_interval(row, c, now)
        entry_count = row.get("entry_count") or 0
        if not force:
            due = [c for c in due
                   if state.disk_entry_count.get(c) != entry_count]
        if not due:
            return None
        disk_started = time.monotonic()
        partial = self._measure_disk(name, row, state,
                                     classes=due, final=False, force=force)
        disk_ms = (time.monotonic() - disk_started) * 1000
        self._disk_timer.record(disk_ms)
        self._last_disk_session = name
        if disk_ms > _DISK_WARN_MS:
            logger.warning(
                "resource_monitor: slow disk measure %s (%s): %.0fms (%s)",
                name, ",".join(due), disk_ms,
                json.dumps(partial.get("timings_ms", {})) if partial else "-")
        for c in due:
            state.disk_entry_count[c] = entry_count
        if partial is None:
            return None
        merged = state.disk or {"components": {}, "timings_ms": {}}
        # A rescanned class fully replaces its components, so anything that
        # vanished (e.g. a cleaned-up worktree) doesn't linger in the total.
        for comp, cls in self._COMPONENT_CLASS.items():
            if cls in due:
                merged["components"].pop(comp, None)
                merged["timings_ms"].pop(comp, None)
        merged["components"].update(partial["components"])
        merged["timings_ms"].update(partial["timings_ms"])
        merged["total"] = sum(merged["components"].values())
        state.disk = merged
        state.disk_sampled_at = now
        update_disk_usage(name, merged["total"], json.dumps(merged), now)
        return merged

    async def refresh_disk(self, tmux_name: str) -> dict | None:
        """Force-refresh one session's full disk footprint (UI affordance).

        Bypasses the cadence clocks and the idle skip; the docker size
        fallback cache is also refreshed if stale. Returns the merged disk
        dict, or None for unknown/dead sessions.
        """
        from tools.dashboard.dao.dashboard_db import get_session
        from tools.dashboard.session_lifecycle_worker import derive_lifecycle_state
        row = get_session(tmux_name)
        if row is None or derive_lifecycle_state(row) in ("ENDED", "FAILED"):
            return None
        state = self._states.setdefault(tmux_name, _SessionState())
        return await asyncio.to_thread(
            self._scan_disk, row, state, time.time(), force=True)

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
                      state: _SessionState | None, *,
                      classes: list[str] | tuple[str, ...],
                      final: bool, force: bool = False) -> dict | None:
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

        if _FAST in classes:
            run_dir = _run_dir_for(row)
            if run_dir is not None and run_dir.is_dir():
                timed("run_dir", lambda: _du_bytes(run_dir))
            elif row.get("jsonl_path"):
                timed("jsonl", lambda: Path(row["jsonl_path"]).stat().st_size)

        if _HEAVY in classes:
            wt_dir = self._worktrees_dir / name
            if wt_dir.is_dir():
                timed("worktrees", lambda: _du_bytes(wt_dir))

            # Container writable layer — live sessions only; a closed
            # session has no container storage (its container is gone).
            if state is not None and state.kind == "container" and not final:
                layer = self._container_layer_bytes(name, state, force=force)
                if layer is not None:
                    key, val, ms = layer
                    components[key] = val
                    timings[key] = ms

        if not components:
            return None
        return {"components": components, "timings_ms": timings}

    def _container_layer_bytes(
            self, name: str, state: _SessionState,
            *, force: bool = False) -> tuple[str, int, float] | None:
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
        # Amortized fallback: one `docker ps -s` per cache lifetime, shared
        # by all containers. Force refreshes accept a much shorter TTL.
        now = time.time()
        ttl = _DOCKER_SIZES_FORCE_TTL if force else _DOCKER_SIZES_TTL
        if now - self._docker_sizes_at > ttl:
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
            "disk_cadence_s": {c: {"young": y, "old": o}
                               for c, (y, o) in self.disk_cadence.items()},
            "young_age_s": _YOUNG_AGE_S,
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
