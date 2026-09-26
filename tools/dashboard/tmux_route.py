"""Which tmux server holds a session's pane.

New sessions are created on this process's own default server — the node's
``tmux`` sidecar, named by TMUX_TMPDIR (deploy/entrypoint.sh). Panes that
were already running on the HOST's tmux server when a node moved to the
sidecar stay there, visible and drivable, until the operator restarts or
closes them (operator direction 2026-09-26, epic auto-e9mpm). The host server
is reached through the ``/tmp:/host-tmp`` bind; nothing is ever created on it.

``tmux_sessions.tmux_socket`` records the server socket a session lives on:
set at creation, and resolved once by probing for rows that predate it. When
no host server socket exists (every fresh node) there is one server and every
command is plain ``tmux``, exactly as without this module.
"""

from __future__ import annotations

import logging
import os
import subprocess

logger = logging.getLogger(__name__)

#: Where the host's tmux server socket appears in the dashboard container.
LEGACY_HOST_TMPDIR = "/host-tmp"

_cache: dict[str, str] = {}


def _socket_in(tmpdir: str) -> str:
    return os.path.join(tmpdir, f"tmux-{os.getuid()}", "default")


def creation_socket() -> str:
    """The server new sessions are created on: this process's default."""
    return _socket_in(os.environ.get("TMUX_TMPDIR") or "/tmp")


def legacy_socket() -> str | None:
    """The host server's socket, when it exists and is not the default."""
    sock = _socket_in(LEGACY_HOST_TMPDIR)
    if sock == creation_socket() or not os.path.exists(sock):
        return None
    return sock


def _session_name(target: str) -> str:
    return str(target).split(":", 1)[0]


def _has_session(sock: str, name: str) -> bool:
    try:
        return subprocess.run(
            ["tmux", "-S", sock, "has-session", "-t", f"={name}"],
            capture_output=True, timeout=5,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def socket_for(target: str) -> str:
    """The socket of the server holding *target*'s session."""
    legacy = legacy_socket()
    if legacy is None:
        return creation_socket()
    name = _session_name(target)
    cached = _cache.get(name)
    if cached:
        return cached
    stored = None
    try:
        from tools.dashboard.dao import dashboard_db
        stored = dashboard_db.get_tmux_socket(name)
    except Exception:
        logger.debug("tmux_route: socket lookup failed for %s", name, exc_info=True)
    if not stored:
        for sock in (creation_socket(), legacy):
            if _has_session(sock, name):
                stored = sock
                break
        if stored:
            record(name, stored)
    if not stored:
        return creation_socket()
    _cache[name] = stored
    return stored


def argv(target: str, *args: str) -> list[str]:
    """``tmux`` argv for a command aimed at *target*'s session."""
    sock = socket_for(target)
    if sock == creation_socket():
        return ["tmux", *args]
    return ["tmux", "-S", sock, *args]


def record(name: str, sock: str) -> None:
    """Remember which server *name* lives on (at creation, or once probed)."""
    _cache[name] = sock
    try:
        from tools.dashboard.dao import dashboard_db
        dashboard_db.set_tmux_socket(name, sock)
    except Exception:
        logger.debug("tmux_route: could not record socket for %s", name, exc_info=True)


def record_created(name: str) -> None:
    record(name, creation_socket())


def list_sessions_result(**kwargs) -> subprocess.CompletedProcess:
    """``tmux list-sessions -F #{session_name}`` across every server.

    A drop-in for the single-server call: with no host server it IS that
    call. Otherwise the two servers' names are merged, and the result fails
    only when neither server answered."""
    cmd = ["tmux", "list-sessions", "-F", "#{session_name}"]
    base = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    legacy = legacy_socket()
    if legacy is None:
        return base
    extra = subprocess.run(
        ["tmux", "-S", legacy, *cmd[1:]], capture_output=True, text=True, **kwargs,
    )
    if extra.returncode != 0:
        return base
    if base.returncode != 0:
        return extra
    names: list[str] = []
    for out in (base.stdout, extra.stdout):
        for line in (out or "").splitlines():
            if line and line not in names:
                names.append(line)
    return subprocess.CompletedProcess(base.args, 0, "\n".join(names) + "\n", base.stderr)
