"""Automatic ramfs provisioning for the node's secret stores (auto-pw9bs.4).

Two host ramfs mounts a node needs before it can hold secrets in memory:

  * DELIVERY  ``/run/autonomy-secrets``  — per-session secret delivery. Each
    session gets its OWN writable subdirectory bind-mounted in at launch; the
    session writes and reads, the dashboard never binds this. Unlinking the
    subdirectory at session end returns the memory (ramfs pages belong to the
    file, not the writer). The per-session subdir wiring is auto-f51kg.
  * KEY CACHE ``/run/autonomy-keycache`` — the dashboard's own memory-class
    home for opened key material (auto-a1pub). Bound into the dashboard with
    ``rslave`` propagation so this boot-time host mount reaches the running
    container; NEVER bound into a session — a different mount entirely, so a
    session cannot structurally reach it.

Both MUST be ramfs, never tmpfs: tmpfs pages swap, and a swap slot cannot be
wiped from userspace, so a secret on tmpfs can reach disk. ``RAMFS_MAGIC`` /
``TMPFS_MAGIC`` come from :mod:`tools.network.storagekit` — one source of truth
for the numbers whose whole job is to be exactly right.

Automatic, no operator step:

  * Containerized node — it holds the Docker socket, which is effective
    host-root (it can launch a privileged container), so it asks the daemon to
    run a one-shot privileged helper that enters PID 1's mount namespace and
    mounts ramfs. No ``CAP_SYS_ADMIN`` on the dashboard container itself; the
    socket already is the privilege.
  * Host-native root — mounts directly.
  * Host-native unprivileged — cannot self-provision; raises with what to
    install once (a systemd ramfs mount unit). The one unavoidable human step,
    and only off the containerized path.

Idempotent, every boot: an already-ramfs mount is left alone; ramfs is
ephemeral across a host reboot, so re-checking every boot is what self-heals.
Fail-closed: after mounting, ``RAMFS_MAGIC`` is the only success and
``TMPFS_MAGIC`` is refused by name — on the host side (the helper self-verifies)
and, for the container-bound cache, in the container too (``rslave`` propagation
is verified, not assumed).
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from agents.mount_plan import _own_container_id
from tools.network.storagekit import (
    RAMFS_MAGIC,
    TMPFS_MAGIC,
    MemoryClassError,
    assert_memory_backed,
    filesystem_magic,
)

#: Per-session secret delivery (sessions bind their own subdir; dashboard never binds).
DELIVERY_MOUNT = "/run/autonomy-secrets"
#: The dashboard's own key cache (bound into the dashboard via rslave; never a session).
KEYCACHE_MOUNT = "/run/autonomy-keycache"

_DOCKER_SOCKET = "/var/run/docker.sock"


class ProvisionError(Exception):
    """The secret ramfs could not be provisioned or did not verify as ramfs."""


def _host_helper_script(path: str) -> str:
    """A shell snippet, run in PID 1's mount namespace, that idempotently
    mounts ramfs at *path* and FAILS CLOSED unless it is ramfs afterwards.

    The magic values are injected from storagekit so the helper never carries
    a second, driftable copy of them.
    """
    ramfs = f"{RAMFS_MAGIC:x}"
    tmpfs = f"{TMPFS_MAGIC:x}"
    return (
        f'set -e; mkdir -p "{path}"; '
        f'mountpoint -q "{path}" || mount -t ramfs ramfs "{path}"; '
        f'chmod 0700 "{path}"; '
        f'm=$(stat -f -c %t "{path}"); '
        f'case "$m" in '
        f'{ramfs}) : ;; '
        f'{tmpfs}) echo "REFUSE: {path} is tmpfs (0x{tmpfs}), swappable — secrets must not swap" >&2; exit 4 ;; '
        f'*) echo "REFUSE: {path} is not ramfs (magic 0x$m)" >&2; exit 3 ;; '
        f'esac'
    )


def _own_image(cid: str) -> str:
    out = subprocess.run(
        ["docker", "inspect", "--format", "{{.Config.Image}}", cid],
        capture_output=True, text=True, timeout=15,
    )
    if out.returncode != 0 or not out.stdout.strip():
        raise ProvisionError(
            f"could not resolve own image to reuse as the mount helper: {out.stderr.strip()}"
        )
    return out.stdout.strip()


def _mount_via_socket_helper(path: str) -> None:
    """Provision on the host through the Docker socket the node already has: a
    one-shot ``--privileged --pid=host`` helper that ``nsenter``s PID 1's mount
    namespace and mounts ramfs there. No new capability on this container. The
    helper self-verifies ramfs and exits non-zero (fail closed) otherwise."""
    cid = _own_container_id()
    if cid is None:
        raise ProvisionError(
            "containerized provisioning requested but own container id is unknown"
        )
    image = _own_image(cid)
    r = subprocess.run(
        ["docker", "run", "--rm", "--privileged", "--pid=host",
         "--entrypoint", "nsenter", image,
         "-t", "1", "-m", "--", "sh", "-c", _host_helper_script(path)],
        capture_output=True, text=True, timeout=90,
    )
    if r.returncode != 0:
        raise ProvisionError(
            f"socket helper failed to provision ramfs at {path} "
            f"(rc={r.returncode}): {(r.stderr or r.stdout).strip()}"
        )


def _mount_host_native(path: str) -> None:
    if os.geteuid() != 0:
        raise ProvisionError(
            f"cannot mount ramfs at {path}: host-native node running unprivileged with no "
            f"Docker socket to borrow root from. Install a ramfs mount unit once — name it "
            f"with `systemd-escape -p --suffix=mount {path}`, Type=ramfs, Options=mode=0700, "
            f"then `systemctl enable --now` it — and this check passes."
        )
    Path(path).mkdir(parents=True, exist_ok=True)
    if subprocess.run(["mountpoint", "-q", path]).returncode != 0:
        subprocess.run(["mount", "-t", "ramfs", "ramfs", path], check=True)
    os.chmod(path, 0o700)


def _is_ramfs(path: str) -> bool:
    try:
        return filesystem_magic(path) == RAMFS_MAGIC
    except MemoryClassError:
        return False


def provision(path: str, *, container_bound: bool) -> None:
    """Ensure *path* is a ramfs mount, provisioning it if not. Idempotent.

    ``container_bound`` marks a path this container also binds (the key cache,
    via ``rslave``): for those the in-container view is verified too, so a
    propagation failure is caught rather than assumed away. The delivery mount
    is not bound by this container, so only the helper's host-side self-verify
    applies here — the launcher and the session-side consumer verify it per
    session.
    """
    if container_bound and _is_ramfs(path):
        return  # already ramfs in our own view; nothing to do

    if _own_container_id() is not None and Path(_DOCKER_SOCKET).exists():
        _mount_via_socket_helper(path)   # host-side, self-verifying, fail closed
    else:
        _mount_host_native(path)

    if container_bound:
        # Confirm rslave actually delivered the host mount into this container;
        # do not trust that it did.
        assert_memory_backed(path)


def main(argv: list[str] | None = None) -> int:
    # (path, container_bound): the cache is bound into us via rslave; delivery is not.
    targets = [(DELIVERY_MOUNT, False), (KEYCACHE_MOUNT, True)]
    for path, bound in targets:
        try:
            provision(path, container_bound=bound)
        except (ProvisionError, MemoryClassError) as exc:
            print(f"ramfs-provision: FATAL for {path}: {exc}", flush=True)
            return 1
        print(f"ramfs-provision: ready {path} (ramfs)", flush=True)
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:] or None))
