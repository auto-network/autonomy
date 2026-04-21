"""Consumer tests for ``autonomy.workspace.mount#1`` — composition + launch.

Covers:

* :func:`workspace_settings.load_mounts` returns a dict keyed by composite
  key and carrying :class:`WorkspaceMountV1` payloads.
* :func:`workspace_manager.prepare_session_mounts` appends
  ``host:container:mode`` for each mount whose host path exists.
* Missing required mount → :class:`WorkspaceMountMissingError` with
  provenance (``mount_key``, ``origin_org``, ``state``, paths).
* Missing optional mount → silently skipped.
* Host path that is not a directory → :class:`WorkspaceMountInvalidError`.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agents import workspace_manager as wm
from agents.workspace_settings import (
    WorkspaceMountInvalidError,
    WorkspaceMountMissingError,
    WorkspaceV1,
    load_mounts,
)
from tools.graph import ops, org_ops
from tools.graph.schemas.mount import (
    SET_ID as MOUNT_SET_ID,
    WorkspaceMountV1,
)
from tools.graph.settings_ops import ResolvedSetting


@pytest.fixture
def graph_db_env(tmp_path, monkeypatch):
    """Pin GRAPH_DB to an empty scratch DB for test-local writes."""
    db = tmp_path / "graph.db"
    monkeypatch.setenv("GRAPH_DB", str(db))
    monkeypatch.delenv("GRAPH_API", raising=False)
    yield db


def _mount_rs(
    *,
    key: str,
    host_path: str,
    container_path: str,
    mode: str = "ro",
    required: bool = True,
    state: str = "raw",
    org: str | None = "autonomy",
) -> ResolvedSetting:
    """Synthesize a ResolvedSetting[WorkspaceMountV1] without hitting the DB."""
    payload = WorkspaceMountV1(
        host_path=host_path,
        container_path=container_path,
        mode=mode,
        required=required,
    )
    return ResolvedSetting(
        id=f"mock-{key}",
        set_id=MOUNT_SET_ID,
        stored_revision=1,
        key=key,
        payload=payload,
        state=state,
        supersedes=None,
        excludes=None,
        deprecated=False,
        successor_id=None,
        created_at="2026-04-21T00:00:00Z",
        updated_at="2026-04-21T00:00:00Z",
        target_revision=None,
        org=org,
        upconverted=False,
    )


def _workspace(mounts: dict[str, ResolvedSetting]) -> WorkspaceV1:
    return WorkspaceV1(
        id="enterprise-ng",
        name="Enterprise NG",
        description="",
        image="autonomy-agent:enterprise-ng",
        graph_project="anchore",
        repos=(),
        mounts=mounts,
    )


# ── load_mounts (end-to-end through ops.read_set) ─────────────


def test_load_mounts_filters_by_workspace_prefix(graph_db_env):
    ops.add_setting(
        MOUNT_SET_ID, 1,
        key="enterprise-ng:vuln-diff",
        payload={
            "host_path": "/tmp/vuln-diff",
            "container_path": "/opt/vuln-diff",
            "mode": "ro",
            "required": True,
        },
        state="raw",
    )
    ops.add_setting(
        MOUNT_SET_ID, 1,
        key="enterprise-ng:harness",
        payload={
            "host_path": "/tmp/harness",
            "container_path": "/opt/harness",
            "mode": "rw",
            "required": False,
        },
        state="raw",
    )
    ops.add_setting(
        MOUNT_SET_ID, 1,
        key="other-workspace:fixture",
        payload={
            "host_path": "/tmp/other",
            "container_path": "/opt/other",
            "required": True,
        },
        state="raw",
    )

    mounts = load_mounts("enterprise-ng")
    assert set(mounts.keys()) == {
        "enterprise-ng:vuln-diff",
        "enterprise-ng:harness",
    }
    vd = mounts["enterprise-ng:vuln-diff"]
    assert isinstance(vd.payload, WorkspaceMountV1)
    assert vd.payload.container_path == "/opt/vuln-diff"
    assert vd.payload.mode == "ro"
    assert vd.payload.required is True


def test_load_mounts_empty_for_unknown_workspace(graph_db_env):
    assert load_mounts("nope") == {}


def test_load_mounts_returns_empty_when_schema_missing(graph_db_env, monkeypatch):
    """If the mount schema isn't registered, return empty (defensive)."""
    from tools.graph.schemas import registry

    key = f"{MOUNT_SET_ID}#1"
    stash = registry.SCHEMAS.pop(key)
    try:
        assert load_mounts("enterprise-ng") == {}
    finally:
        registry.SCHEMAS[key] = stash


# ── prepare_session_mounts wiring ────────────────────────────


def test_prepare_session_mounts_applies_existing_mount(tmp_path):
    host_dir = tmp_path / "vuln-diff"
    host_dir.mkdir()
    workspace = _workspace({
        "enterprise-ng:vuln-diff": _mount_rs(
            key="enterprise-ng:vuln-diff",
            host_path=str(host_dir),
            container_path="/opt/vuln-diff",
            mode="ro",
        ),
    })

    result = wm.prepare_session_mounts(
        workspace, "session-abc",
        repos_dir=tmp_path / "repos",
        worktrees_dir=tmp_path / "wt",
    )
    assert result[str(host_dir)] == "/opt/vuln-diff:ro"


def test_prepare_session_mounts_respects_rw_mode(tmp_path):
    host_dir = tmp_path / "harness"
    host_dir.mkdir()
    workspace = _workspace({
        "enterprise-ng:harness": _mount_rs(
            key="enterprise-ng:harness",
            host_path=str(host_dir),
            container_path="/opt/harness",
            mode="rw",
        ),
    })
    result = wm.prepare_session_mounts(
        workspace, "s",
        repos_dir=tmp_path / "repos",
        worktrees_dir=tmp_path / "wt",
    )
    assert result[str(host_dir)] == "/opt/harness:rw"


def test_missing_required_mount_raises(tmp_path):
    workspace = _workspace({
        "enterprise-ng:vuln-diff": _mount_rs(
            key="enterprise-ng:vuln-diff",
            host_path=str(tmp_path / "does-not-exist"),
            container_path="/opt/vuln-diff",
            required=True,
            state="raw",
            org="anchore",
        ),
    })
    with pytest.raises(WorkspaceMountMissingError) as ei:
        wm.prepare_session_mounts(
            workspace, "s",
            repos_dir=tmp_path / "repos",
            worktrees_dir=tmp_path / "wt",
        )
    err = ei.value
    assert err.mount_key == "enterprise-ng:vuln-diff"
    assert err.origin_org == "anchore"
    assert err.state == "raw"
    assert err.host_path == str(tmp_path / "does-not-exist")
    assert err.container_path == "/opt/vuln-diff"


def test_missing_optional_mount_silently_skipped(tmp_path):
    workspace = _workspace({
        "enterprise-ng:harness": _mount_rs(
            key="enterprise-ng:harness",
            host_path=str(tmp_path / "absent"),
            container_path="/opt/harness",
            required=False,
        ),
    })
    # Should not raise — the mount is optional.
    result = wm.prepare_session_mounts(
        workspace, "s",
        repos_dir=tmp_path / "repos",
        worktrees_dir=tmp_path / "wt",
    )
    # The absent mount produced no -v arg.
    assert not any("/opt/harness" in v for v in result.values())


def test_non_directory_host_path_raises(tmp_path):
    bad = tmp_path / "file.txt"
    bad.write_text("hi")  # file, not directory
    workspace = _workspace({
        "enterprise-ng:vuln-diff": _mount_rs(
            key="enterprise-ng:vuln-diff",
            host_path=str(bad),
            container_path="/opt/vuln-diff",
            required=True,
        ),
    })
    with pytest.raises(WorkspaceMountInvalidError) as ei:
        wm.prepare_session_mounts(
            workspace, "s",
            repos_dir=tmp_path / "repos",
            worktrees_dir=tmp_path / "wt",
        )
    assert "not a directory" in str(ei.value)
