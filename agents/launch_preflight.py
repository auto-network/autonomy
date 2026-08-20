"""Validate every input a ``docker run`` launch depends on BEFORE running it,
and report ALL of them together — an image that was never built, a runtime the
daemon doesn't have, a mount source that doesn't exist — instead of handing
docker a doomed command and reading back a nameless "produced no container".

An hour was lost to one of these (a `:dashboard` image orphaned by a rebuild):
docker's real error existed but was thrown away by a launcher that only learned
"no container appeared". The answer is not to catch that failure and re-raise it
more loudly — it is to discover what is missing FIRST, by name, and never issue
the doomed command at all.

Every check queries the DAEMON (image, runtime) or the frame the daemon binds
from (mount sources), so each is correct whether the node is containerized and
talking to the host socket or running host-native. A check that cannot run is
reported as UNKNOWN, never as a failure: the launch proceeds and docker stays
the backstop. Preflight only ever ADDS naming; it never blocks a launch it could
not actually disprove.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class LaunchProblem:
    """One missing launch input, named. ``kind`` is ``image`` / ``runtime`` /
    ``mount``; ``name`` is the tag / runtime / host source path; ``detail`` is a
    plain sentence saying what is wrong and, where it is knowable, the fix."""

    kind: str
    name: str
    detail: str

    def line(self) -> str:
        return f"    [{self.kind}] {self.name} — {self.detail}"


def _docker(args: list, timeout: int = 15):
    """Run a ``docker`` query, or return None if docker itself can't be reached
    (so the caller treats the check as UNKNOWN, not failed)."""
    try:
        return subprocess.run(
            ["docker", *args], capture_output=True, text=True, timeout=timeout
        )
    except (subprocess.SubprocessError, OSError):
        return None


def image_present(image: str):
    """True/False if *image* is/ isn't present locally, or None if unknowable.

    The launcher only ever runs locally-built ``autonomy-agent:*`` images —
    never pushed to a registry, so a tag absent locally is absent, full stop
    (docker cannot pull it). Absence is therefore a real, fatal, nameable
    problem, not a "docker will fetch it" situation."""
    r = _docker(["image", "inspect", image])
    if r is None:
        return None
    return r.returncode == 0


def runtime_name(runtime_args) -> "str | None":
    """The daemon runtime name inside an emitted ``--runtime=<name>`` arg (e.g.
    ``sysbox-runc``), or None when there is nothing to check — the default
    runtime (no ``--runtime``) and ``--privileged`` need no registered runtime."""
    for a in runtime_args or ():
        if isinstance(a, str) and a.startswith("--runtime="):
            return a.split("=", 1)[1] or None
    return None


def runtime_available(runtime: str):
    """True/False if the daemon has *runtime* registered, or None if unknowable.

    A dind/sysbox session emits ``--runtime=sysbox-runc``; if that runtime is
    not installed or not wired into ``/etc/docker/daemon.json`` the daemon
    refuses the container as namelessly as a missing image."""
    r = _docker(["info", "--format", "{{json .Runtimes}}"])
    if r is None or r.returncode != 0:
        return None
    try:
        runtimes = json.loads(r.stdout or "{}")
    except ValueError:
        return None
    return runtime in (runtimes or {})


def preflight(*, image: str, runtime_args, plan, topo) -> list:
    """Every missing launch input, gathered into one list so the caller can
    report the whole picture and refuse once — not fail on the first and hide
    the rest. Empty list means every input the daemon needs is present (or was
    genuinely unknowable, which is deliberately not a failure)."""
    from agents.mount_plan import preflight_sources
    from agents import secret_ramfs

    problems: list = []

    if image_present(image) is False:
        problems.append(LaunchProblem(
            "image", image,
            "not built on this host — docker has no such image and it is "
            "local-only (never pushed to a registry, so it cannot be pulled). "
            "Build it with `agents/build.sh`.",
        ))

    rt = runtime_name(runtime_args)
    if rt is not None and runtime_available(rt) is False:
        problems.append(LaunchProblem(
            "runtime", rt,
            "not registered with the docker daemon — the container runtime is "
            "not installed or not configured in /etc/docker/daemon.json.",
        ))

    pairs = preflight_sources(plan, topo)
    missing = secret_ramfs.daemon_missing([s for s, _ in pairs])
    if missing:
        absent = set(missing)
        for src, dest in pairs:
            if src in absent:
                problems.append(LaunchProblem(
                    "mount", src,
                    f"source does not exist in the frame the daemon binds from "
                    f"(would mount at {dest}).",
                ))

    return problems
