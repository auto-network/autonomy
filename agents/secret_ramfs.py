"""Automatic ramfs provisioning for the node's secret stores (auto-pw9bs.4).

One host ramfs mount the node needs, plus a per-container primitive:

  * KEY CACHE ``/run/autonomy-keycache`` — the dashboard's own memory-class
    home for opened key material (auto-a1pub). Bound into the dashboard with
    ``rslave`` propagation so this boot-time host mount reaches the running
    container; NEVER bound into a session — a different mount entirely, so a
    session cannot structurally reach it.
  * SESSION DELIVERY — a PRIVATE ramfs inside each session container's own
    mount namespace at ``/run/secrets``, created lazily at first delivery
    and written through one nsenter helper over stdin
    (:func:`deliver_secret_file`). Proven 2026-08-19 and adopted 2026-08-30
    after the shared-host-directory design's fourth incident: with no shared
    root there is no host-visible path for any other process to find or
    destroy, no per-uid subdirectory scheme, no sudo helper for sessions,
    and no sweeper reclaim — the kernel frees the private mount when the
    container dies. The old shared root (``/run/autonomy-secrets``) is
    retired; only legacy lease rows may still name paths under it.

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
  * Host-native unprivileged — refused with instructions for a one-time
    systemd mount unit (or the operator can run the root-owned
    ``agents/provision_ramfs.sh`` install by hand). This applies to the key
    cache only; session delivery needs no host mount at all.

Idempotent, every boot: an already-ramfs mount is left alone; ramfs is
ephemeral across a host reboot, so re-checking every boot is what self-heals.
Fail-closed: after mounting, ``RAMFS_MAGIC`` is the only success and
``TMPFS_MAGIC`` is refused by name — on the host side (the helper self-verifies)
and, for the container-bound cache, in the container too (``rslave`` propagation
is verified, not assumed).
"""
from __future__ import annotations

import base64
import os
import re
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

#: RETIRED shared delivery root. Kept only so legacy lease rows and the
#: transition sweeper can name it; nothing provisions or binds it anymore.
LEGACY_DELIVERY_MOUNT = "/run/autonomy-secrets"
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


def _run_in_host_mount_ns(script: str, *, what: str, timeout: int = 90) -> None:
    """Run *script* in PID 1's mount namespace through the Docker socket the node
    already has: a one-shot ``--privileged --pid=host`` helper that ``nsenter``s
    the host mount ns. No new capability on this container — the socket is the
    privilege. The script is expected to self-verify and exit non-zero (fail
    closed) on any problem."""
    cid = _own_container_id()
    if cid is None:
        raise ProvisionError(f"{what}: own container id is unknown")
    image = _own_image(cid)
    r = subprocess.run(
        ["docker", "run", "--rm", "--privileged", "--pid=host",
         "--entrypoint", "nsenter", image,
         "-t", "1", "-m", "--", "sh", "-c", script],
        capture_output=True, text=True, timeout=timeout,
    )
    if r.returncode != 0:
        raise ProvisionError(f"{what} (rc={r.returncode}): {(r.stderr or r.stdout).strip()}")


def _mount_via_socket_helper(path: str) -> None:
    """Provision the ramfs mount at *path* on the host — host-side + fail-closed."""
    _run_in_host_mount_ns(_host_helper_script(path), what=f"provision ramfs at {path}")


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


# ── Per-session secret delivery: a PRIVATE in-container ramfs ────────────────
# Each session that receives a vault release gets a ramfs mounted INSIDE its
# own mount namespace at ``SESSION_SECRET_DST`` — invisible in the host mount
# table and in every other container. The dashboard provisions and writes it
# in ONE nsenter helper run over the Docker socket it already holds (the
# socket is the privilege, host-native or containerized alike), with the
# plaintext transiting only the helper's stdin — never argv, env, or any
# on-disk docker config. A container that never receives a secret never
# mounts anything. When the container exits, the kernel frees the mount and
# its pages; nothing sweeps, so nothing can sweep wrongly.

#: Container-side mount point for a session's own private delivery ramfs.
SESSION_SECRET_DST = "/run/secrets"
#: All current session images run their unprivileged agent as uid 1000. Keep
#: the delivery helper and consumers on one value until per-session uids are
#: introduced.
SESSION_SECRET_UID = 1000

_SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _validated_name(label: str, value: str) -> str:
    if not isinstance(value, str) or not _SESSION_NAME_RE.match(value) \
            or value in (".", ".."):
        raise ProvisionError(f"unsafe {label} for secret delivery: {value!r}")
    return value


def _container_pid(container: str) -> int:
    out = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Pid}}", container],
        capture_output=True, text=True, timeout=15,
    )
    pid = out.stdout.strip()
    if out.returncode != 0 or not pid.isdigit() or int(pid) <= 0:
        raise ProvisionError(
            f"container {container!r} is not running (no pid): "
            f"{out.stderr.strip() or pid!r}"
        )
    return int(pid)


def _container_image(container: str) -> str:
    out = subprocess.run(
        ["docker", "inspect", "--format", "{{.Config.Image}}", container],
        capture_output=True, text=True, timeout=15,
    )
    if out.returncode != 0 or not out.stdout.strip():
        raise ProvisionError(
            f"could not resolve {container!r}'s image for the delivery "
            f"helper: {out.stderr.strip()}"
        )
    return out.stdout.strip()


def _deliver_script(filename: str, uid: int) -> str:
    """One shell script, run inside the TARGET container's mount namespace:
    ensure the private ramfs (mounting fresh, INCLUDING over an orphaned
    ``//deleted`` bind left by the retired shared-root design — the
    writability probe is what detects that state), verify it is ramfs and
    not swappable tmpfs, then write stdin to the named file atomically at
    0600. Fails closed at every step."""
    ramfs = f"{RAMFS_MAGIC:x}"
    tmpfs = f"{TMPFS_MAGIC:x}"
    p = SESSION_SECRET_DST
    return (
        f'set -e; mkdir -p "{p}" 2>/dev/null || true; '
        f'm=$(stat -f -c %t "{p}" 2>/dev/null || echo none); '
        # A healthy private ramfs is writable; an orphaned deleted-root bind
        # is ramfs by magic but every write fails — mount fresh over either
        # a non-ramfs or an unwritable one.
        f'if [ "$m" != "{ramfs}" ] || ! touch "{p}/.w" 2>/dev/null; '
        f'then mount -t ramfs ramfs "{p}"; else rm -f "{p}/.w"; fi; '
        f'm=$(stat -f -c %t "{p}"); '
        f'case "$m" in '
        f'{tmpfs}) echo "REFUSE: {p} is tmpfs, swappable" >&2; exit 4 ;; '
        f'{ramfs}) : ;; '
        f'*) echo "REFUSE: {p} is not ramfs (magic 0x$m)" >&2; exit 3 ;; '
        f'esac; '
        f'chown {int(uid)}:{int(uid)} "{p}"; chmod 0700 "{p}"; '
        f'umask 077; cat > "{p}/{filename}.tmp"; '
        f'chown {int(uid)}:{int(uid)} "{p}/{filename}.tmp"; '
        f'chmod 0600 "{p}/{filename}.tmp"; '
        f'mv -f "{p}/{filename}.tmp" "{p}/{filename}"'
    )


def deliver_secret_file(
    container: str,
    filename: str,
    data: bytes,
    *,
    uid: int = SESSION_SECRET_UID,
    timeout: int = 60,
) -> str:
    """Write *data* into *container*'s private delivery ramfs as *filename*.

    Provisions (or heals) the in-container ramfs and writes the file in ONE
    privileged helper run: ``nsenter`` into the target container's mount
    namespace via the Docker socket, plaintext piped over stdin only. The
    helper reuses the target's own image (every session image carries
    nsenter). Returns the container-visible path. Raises
    :class:`ProvisionError` on any failure — the caller decides policy.
    """
    _validated_name("container", container)
    _validated_name("filename", filename)
    pid = _container_pid(container)
    image = _container_image(container)
    r = subprocess.run(
        ["docker", "run", "--rm", "-i", "--privileged", "--pid=host",
         "--entrypoint", "nsenter", image,
         "-t", str(pid), "-m", "--", "sh", "-c",
         _deliver_script(filename, uid)],
        input=data, capture_output=True, timeout=timeout,
    )
    if r.returncode != 0:
        raise ProvisionError(
            f"secret delivery into {container!r} failed (rc={r.returncode}): "
            f"{(r.stderr or r.stdout).decode(errors='replace').strip()}"
        )
    return f"{SESSION_SECRET_DST}/{filename}"


def destroy_secret_file(
    container: str, filename: str, *, timeout: int = 30,
) -> None:
    """Remove ONE delivered file from *container*'s private ramfs.

    Addressed by exact (container, filename) from the durable lease — this
    never enumerates a directory, so it cannot reach any other session's
    secret (the shared-root failure it replaces). If the container is not
    running, the kernel already freed its private mount, so absence is
    success. Raises :class:`ProvisionError` only when the container IS
    running and the unlink itself fails.
    """
    _validated_name("container", container)
    _validated_name("filename", filename)
    try:
        pid = _container_pid(container)
    except ProvisionError:
        return  # container gone -> file gone with its mount
    image = _container_image(container)
    r = subprocess.run(
        ["docker", "run", "--rm", "--privileged", "--pid=host",
         "--entrypoint", "nsenter", image,
         "-t", str(pid), "-m", "--", "sh", "-c",
         f'rm -f "{SESSION_SECRET_DST}/{filename}"'],
        capture_output=True, timeout=timeout,
    )
    if r.returncode != 0:
        raise ProvisionError(
            f"secret destruction in {container!r} failed (rc={r.returncode}): "
            f"{(r.stderr or r.stdout).decode(errors='replace').strip()}"
        )


def daemon_missing(paths: list) -> "list | None":
    """The subset of *paths* that do NOT exist in the frame the Docker daemon
    binds a ``--mount`` source from — or ``None`` if the check could not be run,
    so the caller fails OPEN (docker itself stays the backstop) instead of
    refusing a launch it could not actually disprove.

    On a containerized node the daemon binds from PID 1's mount namespace, which
    this container does NOT share: a session's per-session ramfs subdir and a
    volume's ``/var/lib/docker/volumes`` mountpoint both exist there and not
    here, and a translated volume path exists here at a different path than the
    daemon uses — so a stat in our own frame is wrong in both directions. Stat
    THERE, through the socket the node already holds, the same privilege used to
    provision the ramfs above. Paths are passed base64-encoded so a source with
    shell metacharacters cannot break the probe. On a host-native node the
    process frame IS the daemon frame, so stat in-process.
    """
    uniq = [p for p in dict.fromkeys(paths) if p]
    if not uniq:
        return []
    cid = _own_container_id()
    if cid is None:
        return [p for p in uniq if not os.path.exists(p)]
    if not Path(_DOCKER_SOCKET).exists():
        return None  # containerized but no socket to reach the daemon frame
    try:
        image = _own_image(cid)
    except ProvisionError:
        return None
    enc = " ".join(base64.b64encode(p.encode()).decode() for p in uniq)
    script = (
        f'for b in {enc}; do '
        f'p=$(printf %s "$b" | base64 -d); '
        f'[ -e "$p" ] || printf "%s\\n" "$p"; '
        f'done'
    )
    try:
        r = subprocess.run(
            ["docker", "run", "--rm", "--privileged", "--pid=host",
             "--entrypoint", "nsenter", image,
             "-t", "1", "-m", "--", "sh", "-c", script],
            capture_output=True, text=True, timeout=60,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if r.returncode != 0:
        return None
    # Decode-then-stat can only report a path that was IN the probe set; trust
    # exact-match membership, not substring, so an odd path can't smuggle a line.
    absent = {ln for ln in r.stdout.splitlines() if ln}
    return [p for p in uniq if p in absent]


def _is_ramfs(path: str) -> bool:
    try:
        return filesystem_magic(path) == RAMFS_MAGIC
    except MemoryClassError:
        return False


def provision(path: str, *, container_bound: bool) -> None:
    """Ensure *path* is a ramfs mount, provisioning it if not. Idempotent.

    ``container_bound`` marks a path this container also binds (the key cache
    and delivery root, via ``rslave``): the in-container view is verified too,
    so a propagation failure is caught rather than assumed away.
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
    # The key cache is bound into the dashboard via rslave; session
    # delivery is per-container and needs no boot-time provisioning.
    targets = [(KEYCACHE_MOUNT, True)]
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
