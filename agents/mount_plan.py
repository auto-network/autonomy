"""Session mount handling: declare -> resolve -> emit, over one node topology.

One place each for the invariants the launcher used to copy-paste: destination
uniqueness (the plan is dest-keyed), caller-over-capability precedence (set vs
setdefault), socket refusal, and the frame question (resolve consults the node's
own mount table, never a wrong-frame ``.exists()``).

Bead auto-vm8qh (epic auto-nj2kd). Scope: NODE and DEVICE origins are resolved
here; workspace-declared mounts arrive as HOST-origin caller mounts and pass
through unchanged (the pre-refactor stub) until the hybrid resolver (auto-fteke).

A ``container_spec`` is ``"<dest>"`` or ``"<dest>:<mode>"`` (e.g.
``"/workspace/repo:ro"``); ``dest`` (the uniqueness key) is everything before the
mode suffix. Specs retain the original ``source`` and ``container_spec`` strings
so emission is byte-identical to the hand-rolled ``-v`` it replaces.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

try:  # the platform's own roots — a source under them is NODE (platform-managed)
    from tools.data_paths import DATA_ROOT as _DATA_ROOT
except Exception:  # pragma: no cover - keep the module importable in isolation
    _DATA_ROOT = None
_REPO_ROOT = Path(__file__).resolve().parent.parent


class Origin(Enum):
    NODE = "node"        # a path under the platform's own roots; resolved against node volumes/binds
    HOST = "host"        # a genuine external host path (a workspace-declared mount); pass-through stub
    DEVICE = "device"    # /dev/null and friends — always a plain bind, never volume-mapped


def _dest_of(container_spec: str) -> str:
    """The container path a spec mounts at — the plan's uniqueness key.

    Mirrors the pre-refactor ``container_spec.split(":")[0]``: dests carry no
    colon, the optional ``:ro``/``:rw`` mode is the only thing after one.
    """
    return container_spec.split(":", 1)[0]


def classify_origin(source) -> "Origin":
    """Derive a mount's origin from its SOURCE path.

    Origin is derived, never hand-labelled, so platform-managed paths (worktrees,
    managed clones, run-dir material, the startup script — all under the
    platform's own REPO_ROOT/DATA_ROOT) can never be accidentally marked HOST and
    left to fabricate raw ``-v /app/...`` on a containerized node. A device node
    is DEVICE; anything outside the platform roots (a workspace-declared host
    path) is HOST.
    """
    s = str(source)
    if s == "/dev/null" or s.startswith("/dev/"):
        return Origin.DEVICE
    src = os.path.normpath(s)
    # Path.home() is a third NODE-scoped root: _resolve_optional_tool_mounts
    # sources Codex config/skills/rules and the agents home from the node's own
    # home dir, which is platform-managed like REPO_ROOT/DATA_ROOT. (Whether it's
    # a bind or its own volume on a containerized node is m7vh7's compose call.)
    roots = [_REPO_ROOT, Path.home()] + ([_DATA_ROOT] if _DATA_ROOT is not None else [])
    for root in roots:
        r = os.path.normpath(str(root))
        if src == r or src.startswith(r + os.sep):
            return Origin.NODE
    return Origin.HOST


@dataclass(frozen=True)
class MountSpec:
    source: str                 # host/launcher path OR device node
    container_spec: str         # "<dest>" or "<dest>:<mode>" — emitted verbatim
    origin: Origin = Origin.HOST
    required: bool = True
    #: Strict-bind provenance carried from the resolver: emit `--mount type=bind`
    #: (refuse-missing), never `-v`. Set by build_mount_plan from a
    #: BindRefuseMissing container_spec — never inferred from the source path.
    bind_refuse_missing: bool = False

    @property
    def dest(self) -> str:
        return _dest_of(self.container_spec)


def mount_spec(source, container_spec: str, required: bool = True,
               bind_refuse_missing: bool = False) -> MountSpec:
    """Build a MountSpec with its origin DERIVED from the source path
    (classify_origin), so no call site can mislabel a platform path as HOST.

    EXCEPT a resolved workspace bind (``bind_refuse_missing``, declared via a
    BindRefuseMissing container_spec): its source is already a concrete
    DAEMON-HOST path the resolver translated, so it is forced Origin.HOST and
    bound literally. It must NOT be lexically classified against node roots — its
    STRING may coincide with DATA_ROOT/REPO_ROOT/home (e.g. a custom docker
    data-root under /app/data), which would wrongly map it to a node volume and
    drop the strict bind. Origin AND refusal mode both stay explicit after
    translation."""
    refuse = bind_refuse_missing or getattr(container_spec, "bind_refuse_missing", False)
    origin = Origin.HOST if refuse else classify_origin(source)
    return MountSpec(str(source), container_spec, origin, required, refuse)


class MountPlan:
    """Ordered, dest-keyed mount table. ONE mutator, ``set(spec, replace=...)``,
    IS the dedup logic the launcher copy-pastes four times today: ``replace=True``
    (default) is a caller override — delete any spec already at the dest and
    reinsert at the end (the caller phase; update-in-place would keep the old slot
    and break byte-identity); ``replace=False`` is fill-if-absent — skip when the
    dest is claimed, so a later capability/tool mount never overrides a caller.

    ``specs()`` returns INSERTION order. There is no global depth sort: the
    launcher already inserts parents before children for the real nested cases
    (.beads before its credential mask; /workspace/repo before its data/uploads),
    and a global sort would reorder unrelated defaults.
    """

    def __init__(self) -> None:
        self._by_dest: "dict[str, MountSpec]" = {}

    def set(self, spec: MountSpec, *, replace: bool = True) -> None:
        """The one mutator; the four copy-pasted dedup sites collapse to it.

        replace=True (default): caller override — delete any spec already at this
        dest and reinsert at the end (the caller phase). replace=False:
        fill-if-absent — skip when the dest is already claimed, so a later
        capability/tool mount never overrides a caller mount.
        """
        if replace:
            self._by_dest.pop(spec.dest, None)   # del-then-insert -> moves to end
            self._by_dest[spec.dest] = spec
        elif spec.dest not in self._by_dest:
            self._by_dest[spec.dest] = spec

    def has_dest(self, dest: str) -> bool:
        return dest in self._by_dest

    def specs(self) -> "list[MountSpec]":
        return list(self._by_dest.values())


# ── Layer 0: the node's own topology, discovered once ────────────────────────

@dataclass(frozen=True)
class NodeVolume:
    name: str
    mount_point: str        # where the node mounts it (container side)
    host_source: str = ""   # host path (/var/lib/docker/volumes/<name>/_data), from
                            # the node's own docker inspect — used by the pre-1.45
                            # fallback to bind a specific subpath by host path.

@dataclass(frozen=True)
class NodeBind:
    host_source: str
    mount_point: str

@dataclass(frozen=True)
class NodeTopology:
    is_host_process: bool
    volumes: tuple = ()          # tuple[NodeVolume, ...]
    binds: tuple = ()            # tuple[NodeBind, ...]
    volume_subpath: bool = True  # daemon supports --mount ...,volume-subpath (API 1.45+)


def _own_container_id() -> Optional[str]:
    """This process's container id, or None when running as a host process."""
    if os.environ.get("AUTONOMY_CONTAINER") == "0":
        return None
    if not os.path.exists("/.dockerenv"):
        # Best-effort: /.dockerenv absent almost always means host process.
        try:
            with open("/proc/1/cgroup", encoding="utf-8", errors="replace") as fh:
                if "docker" not in fh.read():
                    return None
        except OSError:
            return None
    hostname = os.environ.get("HOSTNAME")
    return hostname or None


def _deepest_containing(
    mounts: "list", source: str,
):
    """The most-specific node mount whose mount_point contains ``source``
    (so /app/data wins over /app). None if none contain it."""
    src = os.path.normpath(source)
    best = None
    best_len = -1
    for m in mounts:
        mp = m.mount_point.rstrip("/") or "/"
        if src == mp or src.startswith(mp + "/"):
            if len(mp) > best_len:
                best, best_len = m, len(mp)
    return best


# ── Layer 2: resolution ──────────────────────────────────────────────────────

class MountUnresolvable(RuntimeError):
    """A required NODE-origin source resolves to no node volume or bind — the
    fabrication case, refused loudly instead of letting docker invent an empty dir."""
    def __init__(self, source: str, dest: str) -> None:
        super().__init__(
            f"required mount source {source!r} (-> {dest}) is under a node root but "
            f"resolves to no mounted volume/bind; refusing rather than fabricating"
        )
        self.source, self.dest = source, dest


class SocketMountRefused(RuntimeError):
    """A mount references the docker socket as source or dest — never given to a session."""


class BindRefuseMissing(str):
    """A ``container_spec`` that MUST emit ``--mount type=bind`` (which refuses a
    nonexistent source), never ``-v`` (which fabricates one) — so a resolved
    workspace source that vanishes before docker run fails the launch instead of
    silently mounting an empty dir (the epic's core bug).

    A ``str`` subclass so it rides the ``{host_path: container_spec}`` dict and
    ``dict.update()`` transparently and reads as a plain spec everywhere; only
    ``build_mount_plan`` inspects ``.bind_refuse_missing`` and stamps it onto the
    MountSpec. Provenance thus FOLLOWS the resolver's declaration through the plan
    — it is never rediscovered by comparing paths, which is unsound across the
    node/daemon-host frame boundary (a host-frame Source is not a node-frame path
    and must not be filesystem-resolved in the node namespace)."""
    bind_refuse_missing = True


@dataclass(frozen=True)
class ResolvedMount:
    container_spec: str
    host_source: Optional[str] = None    # emit as a bind (-v, or --mount if bind_refuse_missing)
    volume: Optional[str] = None         # emit as a --mount type=volume
    subpath: Optional[str] = None
    #: A workspace bind: emit `--mount type=bind` (refuse-missing), never `-v`.
    bind_refuse_missing: bool = False


def _deeper(a, b):
    """The mount with the longer (more specific) mount_point, or whichever is set."""
    if a is None:
        return b
    if b is None:
        return a
    return a if len(a.mount_point) >= len(b.mount_point) else b


def resolve(spec: MountSpec, topo: NodeTopology) -> "Optional[ResolvedMount]":
    """Turn one MountSpec into a ResolvedMount, or None to skip (optional +
    unresolvable). Never touches the filesystem for a NODE-origin spec — the node
    topology is the authority, not a wrong-frame ``.exists()``."""
    # Host process, or an origin that always binds by its literal source path:
    # byte-identical to today's ``-v source:spec`` — EXCEPT a spec the resolver
    # explicitly declared as a strict workspace bind (bind_refuse_missing, carried
    # from a BindRefuseMissing container_spec), which emits `--mount type=bind` so
    # a source that vanished after the resolver's check refuses the launch instead
    # of fabricating an empty mount. The property is declared, never inferred from
    # the path (which is unsound across the node/daemon-host frame boundary).
    if topo.is_host_process or spec.origin in (Origin.DEVICE, Origin.HOST):
        return ResolvedMount(
            container_spec=spec.container_spec, host_source=spec.source,
            bind_refuse_missing=spec.bind_refuse_missing,
        )

    # NODE origin on a containerized node: map the source to the deepest node
    # mount that contains it (/app/data before /app).
    chosen = _deeper(
        _deepest_containing(list(topo.volumes), spec.source),
        _deepest_containing(list(topo.binds), spec.source),
    )
    if isinstance(chosen, NodeVolume):
        rel = os.path.relpath(spec.source, chosen.mount_point)
        return ResolvedMount(
            container_spec=spec.container_spec, volume=chosen.name,
            subpath=None if rel == "." else rel,
        )
    if isinstance(chosen, NodeBind):
        rel = os.path.relpath(spec.source, chosen.mount_point)
        host = chosen.host_source if rel == "." else os.path.join(chosen.host_source, rel)
        return ResolvedMount(container_spec=spec.container_spec, host_source=host)

    # Under a node root but nothing resolves it -> the fabrication case.
    if spec.required:
        raise MountUnresolvable(spec.source, spec.dest)
    return None


# ── Layer 3: emission ────────────────────────────────────────────────────────

class VolumeSubpathUnsupported(RuntimeError):
    """A subpath mount is needed on a pre-1.45 daemon (no --mount volume-subpath)
    AND the fallback can't be built because the volume's host mountpoint is
    unknown (no Source in the node's self-inspect). The normal pre-1.45 path is
    the host-path-bind fallback in emit(), which exposes only the subpath; this
    is raised only when even that is impossible, so we refuse rather than mount
    the whole volume (which would disclose org DBs / other sessions' worktrees)."""
    def __init__(self, dest: str, volume: str) -> None:
        super().__init__(
            f"mount {dest!r} needs a subpath of volume {volume!r} on a pre-1.45 "
            f"daemon, but that volume's host path is unknown, so the safe "
            f"host-path-bind fallback can't be built. Refusing rather than "
            f"mounting the whole volume."
        )
        self.dest, self.volume = dest, volume


def _spec_dest_mode(container_spec: str):
    dest, sep, mode = container_spec.partition(":")
    readonly = bool(sep) and "ro" in mode.split(",")
    return dest, readonly


def _volume_host_source(topo: NodeTopology, name: str) -> str:
    for v in topo.volumes:
        if v.name == name:
            return v.host_source
    return ""


def emit(r: ResolvedMount, topo: NodeTopology) -> list:
    dest, readonly = _spec_dest_mode(r.container_spec)
    if r.volume is not None:
        if r.subpath and not topo.volume_subpath:
            # Pre-1.45 daemon: no --mount volume-subpath. Fall back to a bind of the
            # SPECIFIC subpath, from the volume's host mountpoint (captured once at
            # topology discovery). Exposes ONLY that subdirectory — never the whole
            # volume — so it has the same isolation as volume-subpath on any Docker
            # version. Emitted as `--mount type=bind`, NOT `-v`: --mount REFUSES a
            # nonexistent source, matching volume-subpath's refuse-missing behavior,
            # whereas -v would fabricate an empty source dir and report success (the
            # very bug this epic exists to close). The subpath is a NODE-origin
            # input the platform trusts — that trust is the premise; a path
            # component could still be a symlink, so this does not rely on the bind
            # being symlink-proof.
            host = _volume_host_source(topo, r.volume)
            if not host:
                # Can't locate the volume's host path (no Source in self-inspect)
                # -> no safe fallback possible; refuse rather than over-expose.
                raise VolumeSubpathUnsupported(dest, r.volume)
            src = os.path.join(host, r.subpath)
            parts = ["type=bind", f"src={src}", f"dst={dest}"]
            if readonly:
                parts.append("readonly")
            return ["--mount", ",".join(parts)]
        parts = ["type=volume", f"src={r.volume}", f"dst={dest}"]
        if r.subpath:
            parts.append(f"volume-subpath={r.subpath}")
        if readonly:
            parts.append("readonly")
        return ["--mount", ",".join(parts)]
    if r.bind_refuse_missing:
        # A resolved workspace bind: `--mount type=bind` REFUSES a nonexistent
        # source, so a source that vanished between the resolver's check and now
        # fails the launch instead of `-v` fabricating an empty dir at it.
        parts = ["type=bind", f"src={r.host_source}", f"dst={dest}"]
        if readonly:
            parts.append("readonly")
        return ["--mount", ",".join(parts)]
    # Bind: byte-identical to the hand-rolled ``-v host:spec``.
    return ["-v", f"{r.host_source}:{r.container_spec}"]


_DOCKER_SOCKET = "/var/run/docker.sock"


def mount_args(plan: MountPlan, topo: NodeTopology) -> list:
    """The one emission path both entry points call. Refuses the docker socket
    over the WHOLE plan (closing the bypass where startup_script/global_claude_md
    skipped the check), then resolves+emits each spec in insertion order."""
    for spec in plan.specs():
        if spec.source.rstrip("/") == _DOCKER_SOCKET or spec.dest.rstrip("/") == _DOCKER_SOCKET:
            raise SocketMountRefused(
                f"refusing docker socket mount ({spec.source} -> {spec.dest})"
            )
    out: list = []
    for spec in plan.specs():
        r = resolve(spec, topo)
        if r is not None:
            out += emit(r, topo)
    return out


def preflight_sources(plan: "MountPlan", topo: "NodeTopology") -> list:
    """``(host_path, dest)`` for every mount whose SOURCE is a concrete host path
    that must already exist for ``docker run`` to succeed — so a missing one can
    be NAMED before the run instead of failing namelessly (``docker run`` with a
    missing source, bind OR volume-subpath, creates NO container and reports
    nothing that identifies the path — the wjzh4/qk4ip class of failure).

    Resolves the plan exactly as ``mount_args`` does and covers the two source
    kinds a missing path can hide in:

      * host BINDS (``bind_refuse_missing`` and plain ``-v``): the ``host_source``.
      * VOLUME-SUBPATH mounts: the subpath must already exist INSIDE the volume,
        whether the daemon takes it natively (``volume-subpath=``) or via the
        pre-1.45 bind fallback. Its host path is ``<volume mountpoint>/<subpath>``,
        the same derivation ``emit`` uses for the fallback.

    Skipped (nothing the launcher should stat): a WHOLE-volume mount (the daemon
    names a missing named volume itself), a ``/dev/null`` device bind, and a
    volume whose host mountpoint can't be located (no safe path to check —
    ``emit`` refuses that case at run time anyway). Every returned path is in the
    frame the DAEMON binds from, so the caller must stat it there, not locally."""
    out: list = []
    for spec in plan.specs():
        r = resolve(spec, topo)
        if r is None:
            continue
        dest, _ = _spec_dest_mode(r.container_spec)
        if r.volume is not None:
            if r.subpath:
                host = _volume_host_source(topo, r.volume)
                if host:
                    out.append((os.path.join(host, r.subpath), dest))
            continue  # whole-volume: daemon owns its existence
        src = r.host_source
        if src and src != "/dev/null":
            out.append((src, dest))
    return out


# ── Topology discovery ───────────────────────────────────────────────────────

def _daemon_supports_volume_subpath() -> bool:
    """volume-subpath needs Docker API >= 1.45 (Docker 25+)."""
    try:
        out = subprocess.run(
            ["docker", "version", "--format", "{{.Server.APIVersion}}"],
            capture_output=True, text=True, timeout=15,
        )
        if out.returncode != 0:
            return False
        major, _, minor = out.stdout.strip().partition(".")
        return (int(major), int(minor or 0)) >= (1, 45)
    except Exception:
        return False


_TOPOLOGY_CACHE: "NodeTopology | None" = None


def reset_topology_cache() -> None:
    """Drop the cached topology (tests only — a live node's mounts never change)."""
    global _TOPOLOGY_CACHE
    _TOPOLOGY_CACHE = None


def discover_topology() -> NodeTopology:
    """The node's own mount topology, discovered ONCE and cached. A live
    container's mounts cannot change (you can't add/remove a mount from a running
    container, and a volume's Mountpoint doesn't move), so one boot-time inspect
    serves every launch — zero docker calls per launch."""
    global _TOPOLOGY_CACHE
    if _TOPOLOGY_CACHE is None:
        _TOPOLOGY_CACHE = _discover_topology_uncached()
    return _TOPOLOGY_CACHE


def _discover_topology_uncached() -> NodeTopology:
    cid = _own_container_id()
    if cid is None:
        return NodeTopology(is_host_process=True)
    mounts = []
    try:
        # One inspect of the node's own container: it returns, per mount, the
        # volume Name, the container-side Destination, AND the host-side Source
        # (for a volume, Source is its /var/lib/docker/volumes/<name>/_data
        # mountpoint) — everything the resolver and the fallback need, no separate
        # `docker volume inspect` required.
        out = subprocess.run(
            ["docker", "inspect", "--format", "{{json .Mounts}}", cid],
            capture_output=True, text=True, timeout=15,
        )
        if out.returncode == 0 and out.stdout.strip():
            mounts = json.loads(out.stdout)
    except Exception:
        mounts = []
    volumes, binds = [], []
    for m in mounts or []:
        dest = (m.get("Destination") or "").rstrip("/") or "/"
        if m.get("Type") == "volume":
            volumes.append(NodeVolume(
                name=m.get("Name") or "", mount_point=dest,
                host_source=m.get("Source") or "",
            ))
        elif m.get("Type") == "bind":
            binds.append(NodeBind(host_source=m.get("Source") or "", mount_point=dest))
    return NodeTopology(
        is_host_process=False, volumes=tuple(volumes), binds=tuple(binds),
        volume_subpath=_daemon_supports_volume_subpath(),
    )
