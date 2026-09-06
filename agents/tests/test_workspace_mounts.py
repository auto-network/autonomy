"""Consumer tests for ``autonomy.workspace.mount#2`` — the hybrid resolver.

Covers (auto-fteke):

* :func:`workspace_settings.load_mounts` reads rev 2 and returns
  :class:`WorkspaceMountV3` payloads keyed by composite key.
* :func:`workspace_manager.prepare_session_mounts` resolves each ``subpath``
  under ``orgs/<workspace-org>/`` in the autonomy-orgs pool, realpath-refuses
  an escape to a sibling org, type-checks against ``kind``, and translates to a
  host path for the bind.
* Missing required → :class:`WorkspaceMountMissingError`; missing optional →
  skipped; present-but-wrong-type or escaping → :class:`WorkspaceMountInvalidError`.
* On a containerized node the node-frame path is translated onto the volume's
  host source; without the autonomy-orgs volume the resolver refuses rather
  than fabricate.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agents import workspace_manager as wm
from agents import mount_plan
from agents.workspace_settings import (
    WorkspaceMountInvalidError,
    WorkspaceMountMissingError,
    WorkspaceV1,
    load_mounts,
)
from tools.graph import ops
from tools.graph.schemas.mount import (
    SET_ID as MOUNT_SET_ID,
    MOUNT_SCHEMA_REVISION_2,
    MOUNT_SCHEMA_REVISION_3,
    WorkspaceMountV3,
)
from tools.graph.settings_ops import ResolvedSetting


ORG = "anchore"


@pytest.fixture
def orgs_root(tmp_path, monkeypatch):
    """Host-process orgs pool at <tmp>/orgs, with <org>/ pre-created.

    The autouse conftest fixture pins host-process topology, so the resolver
    reads DATA_DIR/orgs; point DATA_DIR at the tmp tree for hermeticity.
    """
    monkeypatch.setattr(wm, "DATA_DIR", tmp_path)
    root = tmp_path / "org-mounts" / ORG   # distinct from tmp_path/orgs (the org DBs)
    root.mkdir(parents=True)
    return root


@pytest.fixture
def graph_db_env(tmp_path, monkeypatch):
    from tools.graph.db import GraphDB
    orgs = tmp_path / "graphorgs"
    orgs.mkdir()
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    GraphDB.close_all_pooled()
    for slug, kind in (("autonomy", "shared"), ("personal", "personal")):
        GraphDB.create_org_db(slug, type_=kind, path=orgs / f"{slug}.db").close()
    GraphDB.close_all_pooled()
    yield orgs / "personal.db"
    GraphDB.close_all_pooled()


def _mount_rs(*, key, subpath, container_path, kind, mode="ro",
              required=True, state="raw", org="autonomy") -> ResolvedSetting:
    payload = WorkspaceMountV3(
        subpath=subpath, container_path=container_path, kind=kind,
        mode=mode, required=required,
    )
    return ResolvedSetting(
        id=f"mock-{key}", set_id=MOUNT_SET_ID, stored_revision=2, key=key,
        payload=payload, state=state, supersedes=None, excludes=None,
        deprecated=False, successor_id=None,
        created_at="2026-08-19T00:00:00Z", updated_at="2026-08-19T00:00:00Z",
        target_revision=None, org=org, upconverted=False,
    )



def _workspace(mounts) -> WorkspaceV1:
    return WorkspaceV1(
        id="enterprise-ng", name="Enterprise NG", description="",
        image="session-enterprise-ng", graph_project=ORG,
        repos=(), mounts=mounts,
    )


def _prepare(workspace, tmp_path):
    return wm.prepare_session_mounts(
        workspace, "s", repos_dir=tmp_path / "repos", worktrees_dir=tmp_path / "wt",
    )


# ── load_mounts reads rev 2 ─────────────────────────────────────────────────
def test_load_mounts_reads_rev2_and_filters_by_prefix(graph_db_env):
    ops.add_setting(
        MOUNT_SET_ID, MOUNT_SCHEMA_REVISION_2, key="enterprise-ng:vuln-diff",
        payload={"subpath": "vuln-diff-validation", "container_path": "/opt/vuln-diff",
                 "kind": "dir", "mode": "ro", "required": True},
        state="raw", org="personal")
    ops.add_setting(
        MOUNT_SET_ID, MOUNT_SCHEMA_REVISION_2, key="other-workspace:fixture",
        payload={"subpath": "x", "container_path": "/opt/other", "kind": "dir",
                 "required": True},
        state="raw", org="personal")
    mounts = load_mounts("enterprise-ng")
    assert set(mounts.keys()) == {"enterprise-ng:vuln-diff"}
    vd = mounts["enterprise-ng:vuln-diff"]
    assert isinstance(vd.payload, WorkspaceMountV3)
    assert vd.payload.subpath == "vuln-diff-validation" and vd.payload.kind == "dir"


# ── happy path: dir + rw + file, translated to host path ────────────────────
def test_existing_dir_mount_is_resolved_and_bound(orgs_root, tmp_path):
    (orgs_root / "vuln-diff-validation").mkdir()
    ws = _workspace({"enterprise-ng:vuln-diff": _mount_rs(
        key="enterprise-ng:vuln-diff", subpath="vuln-diff-validation",
        container_path="/opt/vuln-diff", kind="dir", mode="ro")})
    result = _prepare(ws, tmp_path)
    host = os.path.realpath(orgs_root / "vuln-diff-validation")
    assert result[host] == "/opt/vuln-diff:ro"


def test_rw_mode_is_preserved(orgs_root, tmp_path):
    (orgs_root / "harness").mkdir()
    ws = _workspace({"enterprise-ng:harness": _mount_rs(
        key="enterprise-ng:harness", subpath="harness",
        container_path="/opt/harness", kind="dir", mode="rw")})
    result = _prepare(ws, tmp_path)
    assert result[os.path.realpath(orgs_root / "harness")] == "/opt/harness:rw"


def test_file_subpath_is_mounted(orgs_root, tmp_path):
    # A subpath may point at a single FILE (the narrowest credential mount, e.g.
    # a license.yaml or an encrypted key) — the collapsed artifact capability.
    (orgs_root / "personal" / "scale-harness").mkdir(parents=True)
    keyfile = orgs_root / "personal" / "scale-harness" / "license.yaml"
    keyfile.write_text("secret")
    ws = _workspace({"scale-harness:license": _mount_rs(
        key="scale-harness:license", subpath="personal/scale-harness/license.yaml",
        container_path="/etc/autonomy/artifacts/license.yaml", kind="file")})
    result = _prepare(ws, tmp_path)
    assert result[os.path.realpath(keyfile)].startswith("/etc/autonomy/artifacts/license.yaml")


# ── absence ─────────────────────────────────────────────────────────────────
def test_missing_required_mount_raises_with_org_scoped_descriptor(orgs_root, tmp_path):
    ws = _workspace({"enterprise-ng:vuln-diff": _mount_rs(
        key="enterprise-ng:vuln-diff", subpath="absent", container_path="/opt/vuln-diff",
        kind="dir", required=True, state="raw", org=ORG)})
    with pytest.raises(WorkspaceMountMissingError) as ei:
        _prepare(ws, tmp_path)
    err = ei.value
    assert err.mount_key == "enterprise-ng:vuln-diff"
    assert err.origin_org == ORG and err.state == "raw"
    assert err.host_path == f"orgs/{ORG}/absent"      # frame-neutral, no machine path
    assert err.container_path == "/opt/vuln-diff"


def test_missing_optional_mount_skipped(orgs_root, tmp_path):
    ws = _workspace({"enterprise-ng:harness": _mount_rs(
        key="enterprise-ng:harness", subpath="absent", container_path="/opt/harness",
        kind="dir", required=False)})
    result = _prepare(ws, tmp_path)
    assert not any("/opt/harness" in v for v in result.values())


# ── node-provided storage: a missing rw dir is created, not refused ─────────
def test_missing_required_rw_dir_is_created_and_bound(orgs_root, tmp_path):
    ws = _workspace({"enterprise-ng:storage": _mount_rs(
        key="enterprise-ng:storage", subpath="workspace-storage",
        container_path="/opt/storage", kind="dir", mode="rw", required=True)})
    result = _prepare(ws, tmp_path)
    created = orgs_root / "workspace-storage"
    assert created.is_dir()
    assert result[os.path.realpath(created)] == "/opt/storage:rw"


def test_missing_optional_rw_dir_is_created_too(orgs_root, tmp_path):
    # The storage rule keys on kind+mode, not required: an empty writable
    # directory is its own correct provisioning either way.
    ws = _workspace({"enterprise-ng:cache": _mount_rs(
        key="enterprise-ng:cache", subpath="cache", container_path="/opt/cache",
        kind="dir", mode="rw", required=False)})
    result = _prepare(ws, tmp_path)
    assert (orgs_root / "cache").is_dir()
    assert result[os.path.realpath(orgs_root / "cache")] == "/opt/cache:rw"


def test_missing_rw_file_still_refused(orgs_root, tmp_path):
    # Only directories are node-provided storage; an empty stand-in FILE
    # (a key, a license) would launch successfully and fail wrongly later.
    ws = _workspace({"w:key": _mount_rs(
        key="w:key", subpath="bridge.key", container_path="/etc/bridge.key",
        kind="file", mode="rw", required=True)})
    with pytest.raises(WorkspaceMountMissingError):
        _prepare(ws, tmp_path)
    assert not (orgs_root / "bridge.key").exists()


def test_missing_ro_dir_still_refused(orgs_root, tmp_path):
    # Read-only means operator-supplied content; creating it empty would
    # hide the provisioning gap instead of reporting it.
    ws = _workspace({"w:data": _mount_rs(
        key="w:data", subpath="dataset", container_path="/opt/dataset",
        kind="dir", mode="ro", required=True)})
    with pytest.raises(WorkspaceMountMissingError):
        _prepare(ws, tmp_path)
    assert not (orgs_root / "dataset").exists()


def test_readiness_reports_missing_rw_dir_advisory_without_creating(
    orgs_root, tmp_path,
):
    findings = wm.check_org_mount_readiness(
        key="enterprise-ng:storage",
        payload={"subpath": "workspace-storage", "container_path": "/opt/storage",
                 "kind": "dir", "mode": "rw", "required": True},
        org=ORG,
    )
    assert len(findings) == 1
    finding = findings[0]
    assert finding.kind == "missing_path"
    assert finding.severity == "advisory"
    assert "created at first launch" in finding.detail
    # Readiness is a pure read — the launch path does the creating.
    assert not (orgs_root / "workspace-storage").exists()


def test_readiness_empty_dir_is_ready_not_a_finding(orgs_root):
    # A present but EMPTY directory is a valid mount target — it binds and
    # works. The doctor must not complain merely because an org mount dir is
    # empty (operator, screenshot 49d1ef4e: seven empty org dirs each listed
    # as "present but empty · still runs without it").
    (orgs_root / "anchorectl").mkdir()
    findings = wm.check_org_mount_readiness(
        key="anchore:anchorectl",
        payload={"subpath": "anchorectl", "container_path": "/opt/anchorectl",
                 "kind": "dir"},
        org=ORG,
    )
    assert findings == ()


def test_readiness_empty_file_is_advisory(orgs_root):
    # A present but empty FILE is still worth an advisory — a zero-byte file is
    # an unprovisioned stub, not real content.
    (orgs_root / "license.yaml").write_bytes(b"")
    findings = wm.check_org_mount_readiness(
        key="anchore:license",
        payload={"subpath": "license.yaml", "container_path": "/etc/x",
                 "kind": "file"},
        org=ORG,
    )
    assert len(findings) == 1
    assert findings[0].kind == "unpopulated_path"
    assert findings[0].severity == "advisory"


def test_readiness_populated_file_is_ready(orgs_root):
    (orgs_root / "license.yaml").write_bytes(b"key: value\n")
    findings = wm.check_org_mount_readiness(
        key="anchore:license",
        payload={"subpath": "license.yaml", "container_path": "/etc/x",
                 "kind": "file"},
        org=ORG,
    )
    assert findings == ()


# ── machine-located mounts: the org row declares, artifact-path locates ─────
def _machine_mount_rs(*, key, container_path, kind, mode="rw",
                      required=False, org="dynbench") -> ResolvedSetting:
    """An org mount row with NO source at all — location comes from this
    machine's own autonomy.artifact-path row."""
    payload = WorkspaceMountV3(
        container_path=container_path, kind=kind, mode=mode, required=required,
    )
    return ResolvedSetting(
        id=f"mock-{key}", set_id=MOUNT_SET_ID, stored_revision=2, key=key,
        payload=payload, state="raw", supersedes=None, excludes=None,
        deprecated=False, successor_id=None,
        created_at="2026-08-30T00:00:00Z", updated_at="2026-08-30T00:00:00Z",
        target_revision=None, org=org, upconverted=False,
    )


def _dynbench_ws(mounts) -> WorkspaceV1:
    return WorkspaceV1(
        id="dynbench-backend", name="Backend", description="",
        image="img", graph_project="dynbench", repos=(), mounts=mounts,
    )


def test_machine_located_mount_binds_the_machine_declared_path(
    tmp_path, monkeypatch,
):
    content = tmp_path / "win-agent"
    content.mkdir()
    monkeypatch.setattr(
        wm, "_machine_located_mount_source",
        lambda key, org: str(content) if (key, org) ==
        ("dynbench-backend:win-build-dir", "dynbench") else None,
    )
    ws = _dynbench_ws({"dynbench-backend:win-build-dir": _machine_mount_rs(
        key="dynbench-backend:win-build-dir", container_path="/windows",
        kind="dir", mode="rw")})
    result = _prepare(ws, tmp_path)
    assert str(result[str(content)]) == "/windows:rw"


def test_machine_located_mount_absent_row_skips_when_optional(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        wm, "_machine_located_mount_source", lambda key, org: None,
    )
    ws = _dynbench_ws({"dynbench-backend:win-build-dir": _machine_mount_rs(
        key="dynbench-backend:win-build-dir", container_path="/windows",
        kind="dir", required=False)})
    result = _prepare(ws, tmp_path)
    assert not any("/windows" in str(v) for v in result.values())


def test_machine_located_mount_absent_row_refuses_when_required(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        wm, "_machine_located_mount_source", lambda key, org: None,
    )
    ws = _dynbench_ws({"dynbench-backend:win-build-dir": _machine_mount_rs(
        key="dynbench-backend:win-build-dir", container_path="/windows",
        kind="dir", required=True)})
    with pytest.raises(WorkspaceMountMissingError) as ei:
        _prepare(ws, tmp_path)
    # The refusal names the exact machine row to write.
    assert "autonomy.artifact-path dynbench:win-build-dir" in str(ei.value)


def test_machine_located_mount_wrong_type_refused(tmp_path, monkeypatch):
    a_file = tmp_path / "not-a-dir"
    a_file.write_text("x")
    monkeypatch.setattr(
        wm, "_machine_located_mount_source", lambda key, org: str(a_file),
    )
    ws = _dynbench_ws({"dynbench-backend:win-build-dir": _machine_mount_rs(
        key="dynbench-backend:win-build-dir", container_path="/windows",
        kind="dir")})
    with pytest.raises(WorkspaceMountInvalidError, match="kind=dir"):
        _prepare(ws, tmp_path)


def test_machine_located_mount_on_containerized_node_binds_unverified(
    tmp_path, monkeypatch,
):
    """Compose shape: the declared path lives in the DAEMON's frame, which a
    containerized node cannot stat — so it emits the strict refuse-missing
    bind without a wrong-frame exists() check, and the launch preflight (or
    docker itself) is the existence guard."""
    monkeypatch.setattr(
        wm.mount_plan, "discover_topology",
        lambda: _topo(is_host_process=False),
    )
    monkeypatch.setattr(
        wm, "_machine_located_mount_source",
        lambda key, org: "/mnt/c/Agent",  # absent in THIS filesystem
    )
    ws = _dynbench_ws({"dynbench-backend:win-build-dir": _machine_mount_rs(
        key="dynbench-backend:win-build-dir", container_path="/windows",
        kind="dir", mode="rw")})
    result = _prepare(ws, tmp_path)
    spec = result["/mnt/c/Agent"]
    assert isinstance(spec, mount_plan.BindRefuseMissing)
    assert str(spec) == "/windows:rw"


def test_machine_located_readiness_unanswerable_on_containerized_node(
    monkeypatch,
):
    monkeypatch.setattr(
        wm.mount_plan, "discover_topology",
        lambda: _topo(is_host_process=False),
    )
    monkeypatch.setattr(
        wm, "_machine_located_mount_source", lambda key, org: "/mnt/c/Agent",
    )
    findings = wm.check_org_mount_readiness(
        key="dynbench-backend:win-build-dir",
        payload={"container_path": "/windows", "kind": "dir", "mode": "rw",
                 "required": False},
        org="dynbench",
    )
    assert len(findings) == 1
    assert findings[0].kind == "unanswerable_here"


def test_machine_located_source_absent_store_means_not_declared(monkeypatch):
    from tools.graph.db import GraphDBMissing

    def raise_missing(*_a, **_kw):
        raise GraphDBMissing("no machine store here")

    monkeypatch.setattr("tools.graph.ops.read_set_key", raise_missing)
    assert wm._machine_located_mount_source(
        "dynbench-backend:win-build-dir", "dynbench",
    ) is None


def test_machine_located_readiness_names_the_row_to_write(monkeypatch):
    monkeypatch.setattr(
        wm, "_machine_located_mount_source", lambda key, org: None,
    )
    findings = wm.check_org_mount_readiness(
        key="dynbench-backend:win-build-dir",
        payload={"container_path": "/windows", "kind": "dir", "mode": "rw",
                 "required": False},
        org="dynbench",
    )
    assert len(findings) == 1
    assert findings[0].severity == "advisory"
    assert "autonomy.artifact-path dynbench:win-build-dir" in findings[0].detail


# ── kind: present-but-wrong-type refused (the anti-fabrication guard) ────────
def test_kind_file_but_dir_present_refused(orgs_root, tmp_path):
    (orgs_root / "license.yaml").mkdir()  # a DIR where a file is declared
    ws = _workspace({"w:m": _mount_rs(
        key="w:m", subpath="license.yaml", container_path="/etc/x", kind="file")})
    with pytest.raises(WorkspaceMountInvalidError, match="kind=file"):
        _prepare(ws, tmp_path)


def test_kind_dir_but_file_present_refused(orgs_root, tmp_path):
    (orgs_root / "thing").write_text("x")  # a FILE where a dir is declared
    ws = _workspace({"w:m": _mount_rs(
        key="w:m", subpath="thing", container_path="/opt/thing", kind="dir")})
    with pytest.raises(WorkspaceMountInvalidError, match="kind=dir"):
        _prepare(ws, tmp_path)


# ── THE security property: a planted symlink escaping the org tree is refused ─
def test_symlink_escape_to_sibling_org_refused(orgs_root, tmp_path):
    # A sibling org in the SAME pool with a secret, and a symlink planted inside
    # THIS org that points at it. realpath resolves the link; the resolver refuses.
    sibling = tmp_path / "org-mounts" / "other-org"
    sibling.mkdir(parents=True)
    (sibling / "secret.pem").write_text("sibling org secret")
    (orgs_root / "escape").symlink_to(sibling)
    ws = _workspace({"w:m": _mount_rs(
        key="w:m", subpath="escape/secret.pem", container_path="/etc/x", kind="file")})
    with pytest.raises(WorkspaceMountInvalidError, match="resolves outside"):
        _prepare(ws, tmp_path)


def test_symlink_within_org_is_allowed(orgs_root, tmp_path):
    # A symlink that stays inside the org tree is fine — bind the resolved target.
    (orgs_root / "real").mkdir()
    (orgs_root / "real" / "f").write_text("x")
    (orgs_root / "link").symlink_to(orgs_root / "real")
    ws = _workspace({"w:m": _mount_rs(
        key="w:m", subpath="link/f", container_path="/etc/f", kind="file")})
    result = _prepare(ws, tmp_path)
    assert result[os.path.realpath(orgs_root / "real" / "f")] == "/etc/f:ro"


# ── containerized frame: translate onto the volume host source; refuse if absent ─
def _topo(*, is_host_process, volumes=()):
    return mount_plan.NodeTopology(is_host_process=is_host_process, volumes=tuple(volumes))


def test_containerized_translate_rebases_onto_host_source(monkeypatch, tmp_path):
    # The node's /app/orgs view maps to the volume's host source; resolution
    # runs in the node frame (here tmp) and the bound path is the host path.
    node_view = tmp_path / "app-orgs"
    (node_view / ORG / "vuln-diff").mkdir(parents=True)
    topo = _topo(is_host_process=False, volumes=[mount_plan.NodeVolume(
        name="autonomy-orgs", mount_point=str(node_view),
        host_source="/var/lib/docker/volumes/autonomy-orgs/_data")])
    monkeypatch.setattr(mount_plan, "discover_topology", lambda: topo)
    ws = _workspace({"w:m": _mount_rs(
        key="w:m", subpath="vuln-diff", container_path="/opt/vuln-diff", kind="dir")})
    result = _prepare(ws, tmp_path)
    assert result["/var/lib/docker/volumes/autonomy-orgs/_data/anchore/vuln-diff"] == "/opt/vuln-diff:ro"


def test_containerized_without_orgs_volume_refuses(monkeypatch, tmp_path):
    monkeypatch.setattr(mount_plan, "discover_topology", lambda: _topo(is_host_process=False))
    ws = _workspace({"w:m": _mount_rs(
        key="w:m", subpath="x", container_path="/opt/x", kind="dir")})
    with pytest.raises(WorkspaceMountInvalidError, match="autonomy-orgs volume is not mounted"):
        _prepare(ws, tmp_path)


# ── blocker 1: containerized, volume present but host_source missing -> refuse ─
def test_containerized_volume_present_but_no_host_source_refuses(monkeypatch, tmp_path):
    topo = _topo(is_host_process=False, volumes=[
        mount_plan.NodeVolume(name="autonomy-orgs", mount_point="/app/orgs", host_source="")])
    monkeypatch.setattr(mount_plan, "discover_topology", lambda: topo)
    ws = _workspace({"w:m": _mount_rs(
        key="w:m", subpath="x", container_path="/opt/x", kind="dir")})
    with pytest.raises(WorkspaceMountInvalidError, match="host source is unknown"):
        _prepare(ws, tmp_path)


def test_containerized_relative_host_source_refuses(monkeypatch, tmp_path):
    topo = _topo(is_host_process=False, volumes=[
        mount_plan.NodeVolume(name="autonomy-orgs", mount_point="/app/orgs",
                              host_source="relative/not/absolute")])
    monkeypatch.setattr(mount_plan, "discover_topology", lambda: topo)
    ws = _workspace({"w:m": _mount_rs(
        key="w:m", subpath="x", container_path="/opt/x", kind="dir")})
    with pytest.raises(WorkspaceMountInvalidError, match="host source is unknown or not absolute"):
        _prepare(ws, tmp_path)


# ── blocker 2: the org ROOT itself is a symlink -> refuse ─────────────────────
def test_org_root_itself_a_symlink_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(wm, "DATA_DIR", tmp_path)
    pool = tmp_path / "org-mounts"
    (pool / "other-org").mkdir(parents=True)
    (pool / "other-org" / "secret").write_text("sibling secret")
    (pool / ORG).symlink_to(pool / "other-org")   # anchore -> other-org
    ws = _workspace({"w:m": _mount_rs(
        key="w:m", subpath="secret", container_path="/etc/x", kind="file")})
    with pytest.raises(WorkspaceMountInvalidError, match="redirected"):
        _prepare(ws, tmp_path)


# ── blocker 3: kind=file rejects a special node (FIFO/socket/device) ──────────
def test_kind_file_rejects_fifo(orgs_root, tmp_path):
    os.mkfifo(orgs_root / "pipe")
    ws = _workspace({"w:m": _mount_rs(
        key="w:m", subpath="pipe", container_path="/etc/x", kind="file")})
    with pytest.raises(WorkspaceMountInvalidError, match="not a regular file"):
        _prepare(ws, tmp_path)


# ── blocker 5: translate reaches REAL host content, not the node-view path ────
def test_containerized_translate_reaches_real_host_content(monkeypatch, tmp_path):
    node_view = tmp_path / "app-orgs"
    host_src = tmp_path / "hostsrc"
    for base in (node_view, host_src):       # same volume seen at two mount points
        d = base / ORG / "vuln-diff"
        d.mkdir(parents=True)
        (d / "sentinel").write_text("REAL-DATA")
    topo = _topo(is_host_process=False, volumes=[mount_plan.NodeVolume(
        name="autonomy-orgs", mount_point=str(node_view), host_source=str(host_src))])
    monkeypatch.setattr(mount_plan, "discover_topology", lambda: topo)
    ws = _workspace({"w:m": _mount_rs(
        key="w:m", subpath="vuln-diff", container_path="/opt/vuln-diff", kind="dir")})
    result = _prepare(ws, tmp_path)
    bound = next(iter(result))
    # fabrication-tell: bound is under the HOST source, never the node-view path.
    assert bound.startswith(str(host_src)) and not bound.startswith(str(node_view))
    # and the translated path reaches real, non-empty content.
    assert (Path(bound) / "sentinel").read_text() == "REAL-DATA"


# ── blocker 5 / AC6: a symlink escaping the volume ENTIRELY is refused ────────
def test_symlink_escape_outside_volume_refused(orgs_root, tmp_path):
    outside = tmp_path / "outside-the-pool"
    outside.mkdir()
    (outside / "secret").write_text("host secret")
    (orgs_root / "esc").symlink_to(outside)
    ws = _workspace({"w:m": _mount_rs(
        key="w:m", subpath="esc/secret", container_path="/etc/x", kind="file")})
    with pytest.raises(WorkspaceMountInvalidError, match="resolves outside"):
        _prepare(ws, tmp_path)







# ── blocker 4 (explicit provenance): real resolver -> emission, BOTH frames ────
def _emit_result_through_plan(result, topo):
    plan = mount_plan.MountPlan()
    for host, spec in result.items():
        plan.set(mount_plan.mount_spec(host, spec))
    return mount_plan.mount_args(plan, topo)


def test_host_process_workspace_bind_emits_mount_not_v(orgs_root, tmp_path):
    (orgs_root / "x").mkdir()
    ws = _workspace({"w:m": _mount_rs(
        key="w:m", subpath="x", container_path="/opt/x", kind="dir")})
    result = _prepare(ws, tmp_path)   # host-process (conftest)
    # the resolved container_spec carries the strict-bind marker explicitly
    bind_src = os.path.realpath(orgs_root / "x")
    assert getattr(result[bind_src], "bind_refuse_missing", False) is True
    args = _emit_result_through_plan(result, mount_plan.NodeTopology(is_host_process=True))
    assert args[0] == "--mount" and args[1].startswith("type=bind,")
    assert "-v" not in args


def test_containerized_workspace_bind_emits_mount_not_v(monkeypatch, tmp_path):
    node_view = tmp_path / "app-orgs"
    (node_view / ORG / "x").mkdir(parents=True)
    topo = _topo(is_host_process=False, volumes=[mount_plan.NodeVolume(
        name="autonomy-orgs", mount_point=str(node_view),
        host_source="/tmp/daemon-host-source")])
    monkeypatch.setattr(mount_plan, "discover_topology", lambda: topo)
    ws = _workspace({"w:m": _mount_rs(
        key="w:m", subpath="x", container_path="/opt/x", kind="dir")})
    result = _prepare(ws, tmp_path)
    # translated onto the DAEMON host source, and marked strict-bind
    assert "/tmp/daemon-host-source/anchore/x" in result
    assert getattr(result["/tmp/daemon-host-source/anchore/x"], "bind_refuse_missing", False)
    args = _emit_result_through_plan(result, topo)
    assert args == [
        "--mount",
        "type=bind,src=/tmp/daemon-host-source/anchore/x,dst=/opt/x,"
        "bind-propagation=rslave,readonly"]
    assert "-v" not in args
