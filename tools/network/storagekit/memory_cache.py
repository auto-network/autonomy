"""MEMORY-class home for the agent delegate signing key (``1e005d5c-c11``
§9, §12).

The delegate is MEMORY-class: it lives in a ramfs cache after sign-in,
is usable with no further human interaction, and is destroyed on host
reboot — re-provisioned only at the next unlock. Two properties make
that classification real rather than aspirational, and this module
enforces the first and models the second:

  1. **Never on disk, never swappable.** The backing directory must be a
     ramfs mount (``RAMFS_MAGIC``). tmpfs is REFUSED even though it looks
     like memory: tmpfs pages swap (§12 — the host has swap in use), so a
     tmpfs-resident key can hit the disk. Anything else (ext4, overlayfs)
     is refused outright. The check fails closed: an unprobeable or
     non-ramfs path never receives key bytes. Per §12 the class is
     re-checked on every write, not once at startup.

  2. **Gone after reboot, re-provisioned on unlock.** ramfs does not
     survive a host reboot, so a freshly constructed cache (a new process
     after reboot) reads empty and the caller must re-provision. This is
     behavioural, not enforced here: :meth:`load` returns ``None`` for a
     cache with no stored key, which is exactly the post-reboot state.

The probe is injectable (``magic_probe``) so the guard's logic is testable
headlessly without mounting ramfs: production passes the real
:func:`filesystem_magic`; a test may substitute a probe returning
``RAMFS_MAGIC`` to exercise the happy path on an ordinary filesystem.

Pure over the ramfs it is handed: no ledger, no network, no crypto beyond
serialising the key seed the caller already holds.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
from pathlib import Path
from typing import Callable, Optional

from tools.network.idkit import KeyPair

from .errors import StorageError

#: ``linux/magic.h`` filesystem type magics.
RAMFS_MAGIC = 0x858458F6
TMPFS_MAGIC = 0x01021994


class MemoryClassError(StorageError):
    """The backing store is not the required MEMORY class (ramfs)."""


class _Statfs(ctypes.Structure):
    # Layout of ``struct statfs`` on Linux x86-64 / aarch64 (LP64). Only
    # ``f_type`` is read; the remaining fields are declared so the offset
    # of the fields we do not use is correct and the buffer is large enough.
    _fields_ = [
        ("f_type", ctypes.c_long),
        ("f_bsize", ctypes.c_long),
        ("f_blocks", ctypes.c_ulong),
        ("f_bfree", ctypes.c_ulong),
        ("f_bavail", ctypes.c_ulong),
        ("f_files", ctypes.c_ulong),
        ("f_ffree", ctypes.c_ulong),
        ("f_fsid", ctypes.c_long * 2),
        ("f_namelen", ctypes.c_long),
        ("f_frsize", ctypes.c_long),
        ("f_flags", ctypes.c_long),
        ("f_spare", ctypes.c_long * 4),
    ]


def filesystem_magic(path) -> int:
    """The ``statfs.f_type`` magic of the filesystem backing *path*.

    Raises :class:`MemoryClassError` when the syscall fails (missing path,
    permission, unsupported platform) so the guard can fail closed rather
    than mis-classify.
    """
    libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
    buf = _Statfs()
    rc = libc.statfs(os.fsencode(str(path)), ctypes.byref(buf))
    if rc != 0:
        err = ctypes.get_errno()
        raise MemoryClassError(
            f"statfs({path!r}) failed: {os.strerror(err)} — cannot confirm the "
            "store is memory-backed"
        )
    return int(buf.f_type) & 0xFFFFFFFF


def assert_memory_backed(path, *, magic_probe: Callable[[object], int] = filesystem_magic) -> None:
    """Raise unless *path* is on a ramfs mount.

    tmpfs is refused with a message naming why (swappable), so a caller
    who mounted the wrong memory filesystem learns the distinction instead
    of a bare failure.
    """
    magic = magic_probe(path)
    if magic == RAMFS_MAGIC:
        return
    if magic == TMPFS_MAGIC:
        raise MemoryClassError(
            f"{path} is tmpfs (magic 0x{magic:08x}), which swaps to disk; the "
            "delegate signing key requires ramfs (MEMORY class)"
        )
    raise MemoryClassError(
        f"{path} is not ramfs (magic 0x{magic:08x}); the delegate signing key "
        "must never touch disk"
    )


class RamDelegateCache:
    """A ramfs-guarded home for one delegate signing key.

    Every write re-checks the class (§12: per write, not once), so a store
    whose mount changed under it stops accepting key bytes. The key is
    serialised as its 32-byte Ed25519 seed; nothing else is written. A
    cache constructed over a directory with no stored key — the post-reboot
    state — loads ``None``, signalling the caller to re-provision at unlock.
    """

    _FILENAME = "storage-delegate.key"

    def __init__(
        self,
        directory,
        *,
        magic_probe: Callable[[object], int] = filesystem_magic,
    ):
        self._dir = Path(directory)
        self._probe = magic_probe
        self._path = self._dir / self._FILENAME

    def _guard(self) -> None:
        assert_memory_backed(self._dir, magic_probe=self._probe)

    def store(self, delegate_signing_key: KeyPair) -> None:
        """Persist the delegate's private seed to the ramfs file (0600),
        after re-confirming the store is memory-backed."""
        self._guard()
        seed_hex = delegate_signing_key.private_hex
        fd = os.open(str(self._path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, seed_hex.encode("ascii"))
        finally:
            os.close(fd)

    def load(self) -> Optional[KeyPair]:
        """Return the stored delegate key, or ``None`` when the cache is
        empty (a fresh cache, i.e. after a host reboot)."""
        self._guard()
        try:
            data = self._path.read_bytes()
        except FileNotFoundError:
            return None
        return KeyPair.from_private_hex(data.decode("ascii").strip())

    def clear(self) -> None:
        """Drop the stored key. Used at sign-out and on revocation."""
        try:
            os.unlink(str(self._path))
        except FileNotFoundError:
            pass
