"""Consumer tests for ``autonomy.workspace.mount#2`` — the hybrid resolver.

Covers (auto-fteke):

* :func:`workspace_settings.load_mounts` reads rev 2 and returns
  :class:`WorkspaceMountV2` payloads keyed by composite key.
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
    WorkspaceMountV2,
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
    payload = WorkspaceMountV2(
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


def _legacy_mount_rs(*, key, host_path, container_path, mode="ro",
                     required=True, state="raw", org="autonomy") -> ResolvedSetting:
    """A rev-1-shaped (deprecated host_path) mount, valid as rev 2 by identity."""
    payload = WorkspaceMountV2(
        host_path=host_path, container_path=container_path, mode=mode, required=required,
    )
    return ResolvedSetting(
        id=f"mock-{key}", set_id=MOUNT_SET_ID, stored_revision=1, key=key,
        payload=payload, state=state, supersedes=None, excludes=None,
        deprecated=False, successor_id=None,
        created_at="2026-08-19T00:00:00Z", updated_at="2026-08-19T00:00:00Z",
        target_revision=None, org=org, upconverted=True,
    )


def _workspace(mounts) -> WorkspaceV1:
    return WorkspaceV1(
        id="enterprise-ng", name="Enterprise NG", description="",
        image="autonomy-agent:enterprise-ng", graph_project=ORG,
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
        state="raw", org=ops.CALLER_ORG)
    ops.add_setting(
        MOUNT_SET_ID, MOUNT_SCHEMA_REVISION_2, key="other-workspace:fixture",
        payload={"subpath": "x", "container_path": "/opt/other", "kind": "dir",
                 "required": True},
        state="raw", org=ops.CALLER_ORG)
    mounts = load_mounts("enterprise-ng")
    assert set(mounts.keys()) == {"enterprise-ng:vuln-diff"}
    vd = mounts["enterprise-ng:vuln-diff"]
    assert isinstance(vd.payload, WorkspaceMountV2)
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


# ── deprecated fallback: rev-1 rows SURVIVE a rev-2 read via identity upconvert ─
def test_load_mounts_keeps_rev1_rows_via_identity_upconverter(graph_db_env):
    # The regression the first merge caused, inverted: with host_path kept in rev 2
    # and the identity 1->2 upconverter, a rev-1 row is READ (not dropped) at rev 2
    # as a host_path payload, alongside a native rev-2 subpath row. Nothing drops.
    ops.add_setting(
        MOUNT_SET_ID, 1, key="w:old",
        payload={"host_path": "/abs/x", "container_path": "/opt/x", "required": True},
        state="raw", org=ops.CALLER_ORG)
    ops.add_setting(
        MOUNT_SET_ID, MOUNT_SCHEMA_REVISION_2, key="w:new",
        payload={"subpath": "x", "container_path": "/opt/y", "kind": "dir", "required": True},
        state="raw", org=ops.CALLER_ORG)
    mounts = load_mounts("w")
    assert set(mounts) == {"w:old", "w:new"}
    assert mounts["w:old"].payload.host_path == "/abs/x"
    assert mounts["w:old"].payload.subpath is None
    assert mounts["w:new"].payload.subpath == "x"


# ── dual-dispatch: a legacy host_path row applies via the old HOST behavior ────
def test_legacy_host_path_row_applied_via_old_host_behavior(tmp_path):
    host_dir = tmp_path / "legacy-host-dir"
    host_dir.mkdir()
    ws = _workspace({"w:legacy": _legacy_mount_rs(
        key="w:legacy", host_path=str(host_dir), container_path="/opt/legacy", mode="ro")})
    result = _prepare(ws, tmp_path)
    assert result[str(host_dir)] == "/opt/legacy:ro"
    # NOT the guarded path's strict-bind marker — legacy rows keep the old -v shape.
    assert getattr(result[str(host_dir)], "bind_refuse_missing", False) is False


def test_rev1_trailing_slash_row_read_at_rev2_and_applied_across_the_seam(graph_db_env, tmp_path):
    # Coordinator caveat 2 + the reviewer's exact-shape find, in ONE regression
    # that crosses the converter/model SEAM where the defect lived: the EXACT live
    # scale-harness:artifacts-dir shape (trailing-slash container_path, valid V1)
    # stored at rev 1, READ at rev 2 (real ops.read_set identity upconvert — not
    # upconvert_chain in isolation, not a directly-constructed model), and APPLIED
    # through the old HOST path with its mount spec byte-identical (slash intact).
    host_dir = tmp_path / "artifacts"
    host_dir.mkdir()
    ops.add_setting(
        MOUNT_SET_ID, 1, key="scale-harness:artifacts-dir",
        payload={"host_path": str(host_dir),
                 "container_path": "/etc/autonomy/artifacts/scale-harness/",
                 "required": True},
        state="raw", org=ops.CALLER_ORG)
    mounts_rs = load_mounts("scale-harness")            # real rev1 -> rev2 read
    assert set(mounts_rs) == {"scale-harness:artifacts-dir"}
    payload = mounts_rs["scale-harness:artifacts-dir"].payload
    assert payload.host_path == str(host_dir) and payload.subpath is None
    result = _prepare(_workspace(mounts_rs), tmp_path)  # applied via old HOST path
    assert result[str(host_dir)] == "/etc/autonomy/artifacts/scale-harness/:ro"


def test_legacy_host_path_required_missing_raises(tmp_path):
    ws = _workspace({"w:legacy": _legacy_mount_rs(
        key="w:legacy", host_path=str(tmp_path / "absent"),
        container_path="/opt/x", required=True)})
    with pytest.raises(WorkspaceMountMissingError):
        _prepare(ws, tmp_path)


def test_legacy_and_subpath_rows_coexist_in_one_workspace(orgs_root, tmp_path):
    # Dual-dispatch end to end: one workspace with both a legacy host_path row and
    # a guarded subpath row — each takes its own path.
    legacy_dir = tmp_path / "legacy"; legacy_dir.mkdir()
    (orgs_root / "guarded").mkdir()
    ws = _workspace({
        "w:legacy": _legacy_mount_rs(key="w:legacy", host_path=str(legacy_dir),
                                     container_path="/opt/legacy"),
        "w:guarded": _mount_rs(key="w:guarded", subpath="guarded",
                               container_path="/opt/guarded", kind="dir"),
    })
    result = _prepare(ws, tmp_path)
    assert result[str(legacy_dir)] == "/opt/legacy:ro"                     # old path
    guarded = os.path.realpath(orgs_root / "guarded")
    assert getattr(result[guarded], "bind_refuse_missing", False) is True  # guarded path


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
        "--mount", "type=bind,src=/tmp/daemon-host-source/anchore/x,dst=/opt/x,readonly"]
    assert "-v" not in args
