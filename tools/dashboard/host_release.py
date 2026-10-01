"""Release vault values to host processes that cannot open the vault.

The dashboard holds the vault key; a host cron job or a sidecar container
does not. Those processes read values the dashboard RELEASES into the host's
ramfs key cache (``/run/autonomy-keycache/<subdir>/``) -- the carrier the
serving connector's key already uses (link_serving_supervisor.
_release_serving_key). One value per file, 0600, temp-then-rename, refused
unless the directory is ramfs. A reboot empties it: until the vault is
unlocked again, those processes have nothing, which is the operator's ruling
("we don't need to be running backups if we're not live"). auto-5gdao.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

#: One release at a time per process: unlock, a hot-reload restore and a
#: config change can each start one, and two writers must not interleave.
RELEASE_LOCK = threading.Lock()


def keycache_dir() -> Path:
    from agents.secret_ramfs import KEYCACHE_MOUNT

    return Path(os.environ.get("AUTONOMY_KEYCACHE_MOUNT") or KEYCACHE_MOUNT)


def release_dir(subdir: str) -> Path:
    return keycache_dir() / subdir


def write_files(directory: Path, files: dict[str, bytes], *, memory_check=None) -> None:
    """Write each ``{name: bytes}`` into *directory* (0700), 0600 each, after
    proving the directory is ramfs. Raises on any failure; never logs a value."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if memory_check is None:
        from tools.network.storagekit.memory_cache import assert_memory_backed

        memory_check = assert_memory_backed
    memory_check(directory)          # ramfs only: never tmpfs, never disk
    for name, data in files.items():
        tmp = directory / f".{name}.{os.getpid()}.{threading.get_ident()}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(tmp, flags, 0o600)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
        finally:
            os.close(fd)
        os.replace(tmp, directory / name)


def clear_files(directory: Path, names) -> None:
    for name in names:
        try:
            (directory / name).unlink()
        except FileNotFoundError:
            pass


def read_file(directory: Path, name: str) -> str:
    """A released value, or "" when it has not been released."""
    try:
        return (directory / name).read_text().strip()
    except OSError:
        return ""
