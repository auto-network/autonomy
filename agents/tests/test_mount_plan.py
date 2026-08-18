"""Unit tests for the mount_plan module (bead auto-vm8qh acceptance criteria).
Criterion 1 (byte-identical golden argv) lives in test_session_launcher.py."""
import os
import pytest
from agents import mount_plan as mp


def _spec(source, container_spec, origin=mp.Origin.HOST, required=True):
    return mp.MountSpec(source=source, container_spec=container_spec,
                        origin=origin, required=required)


# ── criterion 6: ONE mutator; override wins & moves to end; fill never overrides ──
def test_single_mutator_override_wins_and_moves_to_end():
    plan = mp.MountPlan()
    plan.set(_spec("/a", "/dest"))
    plan.set(_spec("/b/other", "/other"))
    plan.set(_spec("/b", "/dest:ro"))                       # replace=True (default)
    assert [s.dest for s in plan.specs()] == ["/other", "/dest"]
    d = [s for s in plan.specs() if s.dest == "/dest"][0]
    assert d.source == "/b" and d.container_spec == "/dest:ro"

def test_single_mutator_fill_if_absent_does_not_override_caller():
    plan = mp.MountPlan()
    plan.set(_spec("/caller", "/dest"))                     # caller override
    plan.set(_spec("/capability", "/dest"), replace=False)  # fill -> skipped
    assert [s.source for s in plan.specs()] == ["/caller"]

def test_only_one_mutation_api():
    assert not hasattr(mp.MountPlan, "setdefault")


# ── finding 1: origin is DERIVED from the source path, never hand-labelled ──
def test_classify_origin_is_path_derived():
    assert mp._DATA_ROOT is not None
    # a platform worktree/clone under DATA_ROOT must be NODE (else it fabricates)
    assert mp.classify_origin(os.path.join(str(mp._DATA_ROOT), "worktrees", "s", "r")) is mp.Origin.NODE
    assert mp.classify_origin(os.path.join(str(mp._REPO_ROOT), "agents", "x")) is mp.Origin.NODE
    assert mp.classify_origin("/dev/null") is mp.Origin.DEVICE
    assert mp.classify_origin("/home/jeremy/workspace/vuln-diff") is mp.Origin.HOST

def test_mount_spec_factory_derives_origin():
    s = mp.mount_spec(os.path.join(str(mp._DATA_ROOT), "worktrees", "x"), "/workspace/x")
    assert s.origin is mp.Origin.NODE


# ── socket refusal over the FULL plan (incl. appended specs) ──
def test_socket_refused_over_full_plan():
    topo = mp.NodeTopology(is_host_process=True)
    plan = mp.MountPlan()
    plan.set(_spec("/ok", "/data/ok"))
    plan.set(_spec("/var/run/docker.sock", "/startup.sock"))
    with pytest.raises(mp.SocketMountRefused):
        mp.mount_args(plan, topo)


# ── criterion 4: resolve() never touches the filesystem for NODE-origin ──
def test_resolve_node_origin_never_touches_filesystem(monkeypatch):
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("resolve touched the fs"))
    monkeypatch.setattr(os.path, "exists", boom)
    monkeypatch.setattr(os, "stat", boom)
    monkeypatch.setattr(os.path, "isdir", boom)
    topo = mp.NodeTopology(
        is_host_process=False,
        volumes=(mp.NodeVolume(name="autonomy-state", mount_point="/app/data"),),
    )
    r = mp.resolve(_spec("/app/data/.beads", "/data/.beads", origin=mp.Origin.NODE), topo)
    assert r.volume == "autonomy-state" and r.subpath == ".beads"


# ── deepest node root wins (/app/data before /app) ──
def test_resolve_picks_deepest_node_root():
    topo = mp.NodeTopology(
        is_host_process=False,
        volumes=(mp.NodeVolume(name="autonomy-code", mount_point="/app"),
                 mp.NodeVolume(name="autonomy-state", mount_point="/app/data")),
    )
    r = mp.resolve(_spec("/app/data/agent-runs/x", "/workspace/output", origin=mp.Origin.NODE), topo)
    assert r.volume == "autonomy-state" and r.subpath == "agent-runs/x"


# ── criterion 5 (rewritten per ruling B): Docker 25/API 1.45 floor, NO fallback ──
def test_emit_volume_subpath_when_supported():
    topo = mp.NodeTopology(
        is_host_process=False, volume_subpath=True,
        volumes=(mp.NodeVolume(name="autonomy-state", mount_point="/app/data"),),
    )
    plan = mp.MountPlan()
    plan.set(_spec("/app/data/agent-runs/x", "/workspace/output", origin=mp.Origin.NODE))
    assert mp.mount_args(plan, topo) == [
        "--mount",
        "type=volume,src=autonomy-state,dst=/workspace/output,volume-subpath=agent-runs/x",
    ]

def test_pre_1_45_host_path_bind_fallback():
    """Pre-1.45: bind the SPECIFIC subpath by the volume's cached host path —
    exposes only that subdir, never the whole volume (and no --mount type=volume)."""
    topo = mp.NodeTopology(
        is_host_process=False, volume_subpath=False,
        volumes=(mp.NodeVolume(
            name="autonomy-state", mount_point="/app/data",
            host_source="/var/lib/docker/volumes/autonomy-state/_data"),),
    )
    plan = mp.MountPlan()
    plan.set(_spec("/app/data/.beads", "/data/.beads:ro", origin=mp.Origin.NODE))
    args = mp.mount_args(plan, topo)
    # --mount type=bind, NOT -v: the daemon REFUSES a missing source (verified on
    # sjc-2), so the fallback cannot fabricate an empty dir the way -v would — the
    # no-fabrication contract that makes it equivalent to volume-subpath.
    assert args == [
        "--mount",
        "type=bind,src=/var/lib/docker/volumes/autonomy-state/_data/.beads,"
        "dst=/data/.beads,readonly",
    ]
    assert "-v" not in args                       # never the fabricating -v
    assert "type=volume" not in " ".join(args)    # never a whole-volume mount

def test_pre_1_45_refuses_only_when_host_path_unknown():
    """Refusal is the EDGE (no host Source to build the safe fallback from), never
    a whole-volume mount. No MountArgs/whole-volume code path exists."""
    topo = mp.NodeTopology(
        is_host_process=False, volume_subpath=False,
        volumes=(mp.NodeVolume(name="autonomy-state", mount_point="/app/data"),),  # host_source=""
    )
    plan = mp.MountPlan()
    plan.set(_spec("/app/data/.beads", "/data/.beads", origin=mp.Origin.NODE))
    with pytest.raises(mp.VolumeSubpathUnsupported):
        mp.mount_args(plan, topo)
    assert not hasattr(mp, "MountArgs")


# ── criterion 7: Path.home() is a NODE-scoped root ──
def test_classify_origin_home_is_node():
    import pathlib
    assert mp.classify_origin(str(pathlib.Path.home() / ".codex" / "skills")) is mp.Origin.NODE


# ── topology discovered once, cached (a live node's mounts can't change) ──
def test_topology_discovered_once_and_cached(monkeypatch):
    mp.reset_topology_cache()
    calls = []
    def fake():
        calls.append(1)
        return mp.NodeTopology(is_host_process=True)
    monkeypatch.setattr(mp, "_discover_topology_uncached", fake)
    mp.discover_topology(); mp.discover_topology(); mp.discover_topology()
    assert len(calls) == 1
    mp.reset_topology_cache()


def test_optional_tool_home_mount_resolves_to_volume_on_containerized_node():
    """auto-vm8qh criterion 7: a Codex skills mount under Path.home() classifies
    NODE and resolves to the home volume on a containerized node — NOT raw -v that
    would fabricate an empty host path."""
    import pathlib
    home = str(pathlib.Path.home())
    topo = mp.NodeTopology(
        is_host_process=False,
        volumes=(mp.NodeVolume("autonomy-home", home,
                               host_source="/var/lib/docker/volumes/autonomy-home/_data"),),
    )
    s = mp.mount_spec(home + "/.codex/skills", "/home/agent/.codex/skills:ro")
    assert s.origin is mp.Origin.NODE
    r = mp.resolve(s, topo)
    assert r.volume == "autonomy-home" and r.host_source is None and r.subpath == ".codex/skills"
