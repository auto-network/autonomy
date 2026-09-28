"""What this machine has free for another session (bead auto-mje3g).

The machine chooser shows, per fleet machine: live sessions, free RAM, free
disk and load. Each machine answers for itself through session-control's
``status`` op; nothing here is written anywhere, because the figures change
constantly and presence rows must never become a heartbeat.

* RAM: ``MemAvailable`` / ``MemTotal`` from /proc/meminfo, unless this
  dashboard runs under a cgroup memory limit, in which case the limit is the
  total and ``limit - usage`` is what is free (``ram_limited`` says which).
* Disk: free space on the volume holding the data root, where sessions'
  worktrees and run directories live -- the disk a new session consumes.
* Load: the 1-minute load average and the CPU count; the chooser shows
  load / cpus, so 1.0 means every core busy on any size of machine.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

_GIB = 1024 ** 3


def _meminfo(path: Path = Path("/proc/meminfo")) -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        for line in path.read_text().splitlines():
            key, _, rest = line.partition(":")
            parts = rest.split()
            if parts and parts[0].isdigit():
                out[key] = int(parts[0]) * 1024          # kB -> bytes
    except OSError:
        pass
    return out


def _cgroup_memory(root: Path = Path("/sys/fs/cgroup")) -> tuple[int, int] | None:
    """(limit, usage) in bytes under a cgroup v2 memory limit, else None."""
    try:
        limit = (root / "memory.max").read_text().strip()
        usage = (root / "memory.current").read_text().strip()
    except OSError:
        return None
    if limit == "max" or not limit.isdigit() or not usage.isdigit():
        return None
    return int(limit), int(usage)


def sample(data_root: str | os.PathLike | None = None, *,
           meminfo: Path = Path("/proc/meminfo"),
           cgroup: Path = Path("/sys/fs/cgroup")) -> dict:
    """The machine's free resources, rounded for display."""
    if data_root is None:
        from tools.data_paths import DATA_ROOT
        data_root = DATA_ROOT
    info = _meminfo(meminfo)
    total = info.get("MemTotal", 0)
    free = info.get("MemAvailable", 0)
    limited = False
    cg = _cgroup_memory(cgroup)
    if cg is not None and (not total or cg[0] < total):
        total, free, limited = cg[0], max(0, cg[0] - cg[1]), True
    try:
        disk = shutil.disk_usage(data_root)
        disk_free, disk_total = disk.free, disk.total
    except OSError:
        disk_free = disk_total = 0
    try:
        load_1m = os.getloadavg()[0]
    except OSError:
        load_1m = 0.0
    return {
        "ram_free_gb": round(free / _GIB, 1),
        "ram_total_gb": round(total / _GIB, 1),
        "ram_limited": limited,
        "disk_free_gb": round(disk_free / _GIB, 1),
        "disk_total_gb": round(disk_total / _GIB, 1),
        "load_1m": round(load_1m, 2),
        "cpus": os.cpu_count() or 1,
    }
