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
  * Host-native unprivileged — hands off to a narrow, root-owned sudo helper
    (``/usr/local/sbin/provision_ramfs``, NOPASSWD-scoped to that exact path;
    source in ``agents/provision_ramfs.sh``) instead of the containerized
    path's docker-socket/nsenter dance, which needs a socket this node doesn't
    have. The helper self-heals the base delivery mount too (mkdir/mount if
    missing), so no separate systemd unit is needed on this path.

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


# ── Per-session secret delivery subdir (auto-f51kg) ──────────────────────────
# Each session gets its OWN subdirectory under the delivery ramfs, owned by the
# session's uid and readable by nobody else, bound into the container at
# ``SESSION_SECRET_DST``. In ``delivered`` mode the SESSION (not the dashboard)
# opens the sealed content key, decrypts the body, and writes the plaintext file
# here; only the path enters the tool result. The guard exists for three real
# reasons — state them right, because a guard with a wrong stated purpose gets
# deleted by whoever notices it is not real:
#   1. No dashboard variable ever holds the plaintext — the session materialises
#      it, so a traceback or logging middleware cannot serialise a secret.
#   2. Plaintext never reaches disk — the subdir is ramfs, never swappable tmpfs
#      (a tmpfs page can reach a swap slot, and a swap slot cannot be wiped).
#   3. Cross-session isolation — 0700 + per-uid ownership, so session A cannot
#      read session B's secrets.
# Provisioning is host-side (the launcher creates the subdir before the container
# starts; the container binds it with refuse-missing, so a failed mkdir fails the
# launch rather than yielding a look-alike on-disk directory). Teardown is the
# launcher's — the component that created it removes it. Per auto-f51kg.

#: Container-side mount point for a session's own delivery subdir (f51kg spec).
SESSION_SECRET_DST = "/run/secrets"

_SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _session_dir(session_name: str, base: str) -> str:
    if not _SESSION_NAME_RE.match(session_name) or session_name in (".", ".."):
        raise ProvisionError(f"unsafe session name for a secret subdir: {session_name!r}")
    return f"{base.rstrip('/')}/{session_name}"


def _session_dir_script(path: str, uid: int) -> str:
    ramfs = f"{RAMFS_MAGIC:x}"
    tmpfs = f"{TMPFS_MAGIC:x}"
    return (
        f'set -e; mkdir -p "{path}"; chown {int(uid)}:{int(uid)} "{path}"; chmod 0700 "{path}"; '
        f'm=$(stat -f -c %t "{path}"); '
        f'case "$m" in '
        f'{ramfs}) : ;; '
        f'{tmpfs}) echo "REFUSE: {path} is tmpfs (0x{tmpfs}), swappable — a secret here can reach disk" >&2; exit 4 ;; '
        f'*) echo "REFUSE: {path} is not ramfs (magic 0x$m)" >&2; exit 3 ;; '
        f'esac'
    )


#: Root-owned, non-writable-by-us copy of agents/provision_ramfs.sh — sudo is
#: only a real privilege boundary if the calling user can't also edit the
#: script it grants root on (see NOPASSWD line in /etc/sudoers.d/provision-ramfs).
_SUDO_HELPER = "/usr/local/sbin/provision_ramfs"


def _provision_via_sudo_helper(path: str, uid: int) -> None:
    """Host-native, unprivileged, no docker socket to borrow root from: hand off
    to the narrow root helper instead of failing outright. Still fails closed —
    a helper error still raises — but the caller (session_launcher) treats that
    as best-effort and launches the session anyway, without the secret mount."""
    r = subprocess.run(
        ["sudo", "-n", _SUDO_HELPER, path, str(int(uid))],
        capture_output=True, text=True, timeout=15,
    )
    if r.returncode != 0:
        raise ProvisionError(
            f"cannot create {path} via {_SUDO_HELPER}: {(r.stderr or r.stdout).strip()}"
        )


def provision_session_dir(session_name: str, uid: int, *, base: str = DELIVERY_MOUNT) -> str:
    """Create *session_name*'s own writable ramfs subdir under the delivery mount,
    owned by *uid* (mode 0700), and return its host path. Fails closed: the subdir
    must be ramfs afterward. The caller binds this ``src`` with refuse-missing, so
    a helper failure fails the launch rather than yielding a writable on-disk
    directory that looks identical and is not memory-backed. Note this function
    still raises on failure — it is the CALLER's job (session_launcher) to treat
    that as best-effort and launch without secrets rather than fail the session;
    this function has no business deciding that policy for every caller."""
    path = _session_dir(session_name, base)
    if _own_container_id() is not None and Path(_DOCKER_SOCKET).exists():
        _run_in_host_mount_ns(
            _session_dir_script(path, uid), what=f"provision session secret dir {path}"
        )
    elif os.geteuid() == 0:
        Path(path).mkdir(parents=True, exist_ok=True)
        os.chown(path, int(uid), int(uid))
        os.chmod(path, 0o700)
        assert_memory_backed(path)  # ramfs only; tmpfs refused by name
    else:
        _provision_via_sudo_helper(path, uid)
    return path


def teardown_session_dir(session_name: str, *, base: str = DELIVERY_MOUNT) -> None:
    """Unlink a session's secret subdir to return its ramfs memory. The LAUNCHER's
    job: the component that created it removes it (a sweeper is only a
    died-launcher backstop, since ramfs pages belong to the file, not the writer).
    Best-effort — teardown must never raise into session cleanup."""
    try:
        path = _session_dir(session_name, base)
    except ProvisionError:
        return
    try:
        if _own_container_id() is not None and Path(_DOCKER_SOCKET).exists():
            _run_in_host_mount_ns(f'rm -rf "{path}"', what=f"teardown session secret dir {path}", timeout=30)
        elif os.geteuid() == 0:
            subprocess.run(["rm", "-rf", path], timeout=15)
    except (ProvisionError, subprocess.SubprocessError, OSError):
        pass


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
