"""Persistent browser profile storage (auto-0skxh, design graph://c330323d-986).

A persistent profile keeps a site's trusted-device state between leases. Its
path is built from the caller's AUTHENTICATED organization and workspace plus
a short name the caller supplies, so a path into another workspace cannot be
expressed at all: the caller never supplies a path, and every segment is
validated before it is joined.

Callers pass ``org`` and ``workspace`` from the resolved ``CallerScope``
(tools/dashboard/capability_gate.py), never from request fields.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from agents.mount_plan import MountSpec, mount_spec
from tools.data_paths import resolve_store

#: The caller-supplied profile name. The leading character must be a letter or
#: digit, which also rules out ".", ".." and every other all-dots name.
PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

#: Where a lease container sees its one profile.
PROFILE_MOUNT_DEST = "/profile"


def _check_segment(value: str, what: str) -> None:
    # org and workspace come from the authenticated identity, but each must
    # still be exactly one path segment: "a/b" + "c" and "a" + "b/c" would
    # otherwise share a path and a container name.
    if not value or value in {".", ".."} or "/" in value or "\\" in value or "\0" in value:
        raise ValueError(f"invalid {what} for a browser profile: {value!r}")


def _check(org: str, workspace: str, name: str) -> None:
    _check_segment(org, "organization")
    _check_segment(workspace, "workspace")
    if not PROFILE_NAME_RE.fullmatch(name):
        raise ValueError(f"invalid browser profile name: {name!r}")


def profile_path(org: str, workspace: str, name: str) -> Path:
    """``<browser_profiles root>/<org>/<workspace>/<name>``.

    Raises ValueError for an invalid segment, or when any level below the
    store root resolves (through a symlink) somewhere other than
    ``<root>/<org>/<workspace>/<name>``.
    """
    _check(org, workspace, name)
    root = resolve_store("browser_profiles").resolve()
    base = root / org / workspace
    if base.resolve() != base:
        raise ValueError(f"browser profile workspace {org}/{workspace} resolves outside the store")
    path = (base / name).resolve()
    if path.parent != base:
        raise ValueError(f"browser profile {name!r} resolves outside its workspace")
    return path


def profile_container_name(org: str, workspace: str, name: str) -> str:
    """``brw-p-`` + the first 16 hex characters of SHA-256(``<org>/<workspace>/<name>``).

    Docker refuses a second container with the same name, which is what gives
    a persistent profile a single owner.
    """
    _check(org, workspace, name)
    digest = hashlib.sha256(f"{org}/{workspace}/{name}".encode()).hexdigest()
    return f"brw-p-{digest[:16]}"


def profile_mount(org: str, workspace: str, name: str) -> MountSpec:
    """The mount of ``profile_path`` at ``/profile`` in the lease container.

    Creating the directory (owner and mode) belongs to the lease launch
    (auto-czoc0); this only describes the mount.
    """
    return mount_spec(profile_path(org, workspace, name), PROFILE_MOUNT_DEST)
