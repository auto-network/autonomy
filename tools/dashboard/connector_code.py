"""Which repository code a serving connector runs, and whether it changed.

A serving connector used to be replaced whenever the checked-out commit moved,
because the supervisor compared its boot commit with the disk HEAD. Most
commits touch nothing a connector runs, and each restart took an
organization's public serving down for tens of seconds (auto-j6ssc). Instead,
the connector reports the repository source files it has loaded, each with
the SHA-256 of its content, and the supervisor replaces it only when one of
those files differs on disk or is gone.

Both sides use this module, so the paths and the digest rule match.

Only imported ``.py`` files are fingerprinted. A repository data or
configuration file the connector reads at runtime is not, so changing one
does not restart it.
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def process_start_time() -> float:
    """This process's start as epoch seconds (Linux ``/proc``), so modules
    imported before any Python code ran are covered; now, elsewhere."""
    import time

    try:
        with open("/proc/self/stat") as fh:
            ticks = int(fh.read().rsplit(")", 1)[1].split()[19])
        with open("/proc/stat") as fh:
            boot = next(int(line.split()[1]) for line in fh if line.startswith("btime "))
        return boot + ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration):
        return time.time()


def _digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def loaded_files(started_at: float, known: dict[str, str | None]) -> list[list]:
    """``[[repo-relative path, sha256 | None], ...]`` for every repository
    source file this process has imported, sorted by path.

    *known* carries earlier results between calls, so a module imported
    lazily after startup is added the first time it is seen, and a file is
    hashed once. A file modified after *started_at* (the process start) is
    reported with ``None``: the process may have imported it before that
    change, so its digest cannot vouch for the loaded code, and the
    supervisor treats ``None`` as changed."""
    root = str(REPO_ROOT) + os.sep
    for module in list(sys.modules.values()):
        filename = getattr(module, "__file__", None)
        if not filename or not filename.endswith(".py"):
            continue
        path = os.path.realpath(filename)
        if not path.startswith(root):
            continue
        relative = path[len(root):]
        if relative in known:
            continue
        try:
            modified_after_start = os.stat(path).st_mtime > started_at
        except OSError:
            modified_after_start = True
        known[relative] = None if modified_after_start else _digest(Path(path))
    return [[relative, digest] for relative, digest in sorted(known.items())]


#: (path, inode, mtime_ns, ctime_ns, size) -> digest, so each supervisor
#: check rehashes only files that changed on disk.
_DIGEST_CACHE: dict[tuple, str | None] = {}
#: A file touched this recently is always rehashed: on a filesystem with
#: coarse timestamps, a same-size rewrite within one tick keeps every stat
#: field, and the cache would hide the change.
_FRESH_S = 2.0


def current_digest(relative: str) -> str | None:
    import time

    path = REPO_ROOT / relative
    try:
        st = path.stat()
    except OSError:
        return None
    if time.time() - max(st.st_mtime, st.st_ctime) < _FRESH_S:
        return _digest(path)
    key = (relative, st.st_ino, st.st_mtime_ns, st.st_ctime_ns, st.st_size)
    if key not in _DIGEST_CACHE:
        if len(_DIGEST_CACHE) > 20_000:
            _DIGEST_CACHE.clear()
        _DIGEST_CACHE[key] = _digest(path)
    return _DIGEST_CACHE[key]


def changed_files(files) -> list[str] | None:
    """The reported files whose content no longer matches, or ``None`` when
    the report is missing or malformed (a connector that reports no
    fingerprint is stale by definition). An empty list means unchanged."""
    if not isinstance(files, list) or not files:
        return None
    changed = []
    for entry in files:
        if not (isinstance(entry, (list, tuple)) and len(entry) == 2
                and isinstance(entry[0], str)):
            return None
        relative, digest = entry
        if relative.startswith(("/", "..")) or ".." in Path(relative).parts:
            return None
        if digest is None or current_digest(relative) != digest:
            changed.append(relative)
    return changed
