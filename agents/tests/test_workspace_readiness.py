"""What an organization's workspaces still need on the machine that just joined.

Joining brings the declarations; none of them say anything about this box.
This is the step between "you are a member" and "you can work" — and it has
to distinguish three things a single list of problems cannot: what stops a
workspace running, what it runs without, and what this process is in the
wrong place to answer at all.
"""
from __future__ import annotations

import pytest

from agents import mount_plan
from agents import workspace_manager
from agents.workspace_readiness import org_readiness, workspace_readiness
from tools.graph import settings_ops
from tools.graph.db import GraphDB


@pytest.fixture
def org(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("AUTONOMY_CONTAINER", "0")
    GraphDB.create_org_db("anchore").close()
    yield tmp_path
    GraphDB.close_all_pooled()


def _workspace(key, **extra):
    payload = {"name": key.title(), "image": "img", "harness": "claude"}
    payload.update(extra)
    return settings_ops.add_setting(
        "autonomy.workspace", 1, key, payload, org="anchore", state="raw")


def test_a_workspace_needing_nothing_local_is_ready(org):
    _workspace("docs")

    assert workspace_readiness("docs", org="anchore").ready


def test_a_repo_host_does_not_invent_a_secure_setting_dependency(org):
    """Repository preparation consumes host SSH configuration, not a sealed
    connector setting whose coincidental key resembles the remote host."""
    _workspace("eng", repos=[{"host": "github.com", "repo": "anchore/enterprise",
                              "mount": "/workspace/repo"}])

    result = workspace_readiness("eng", org="anchore")

    assert result.ready
    assert not any("secure.setting" in f.address for f in result.blocking)


def test_an_unset_forwarded_variable_stops_it(org):
    _workspace("eng", env_from_host=["GH_TOKEN"])

    result = workspace_readiness("eng", org="anchore")

    assert [f.kind for f in result.blocking] == ["missing_env"]


def test_a_fixed_workspace_value_satisfies_a_forwarded_variable(org):
    """Launch applies fixed env first and host env as an optional override."""
    _workspace("eng", env={"GH_TOKEN": "configured"},
               env_from_host=["GH_TOKEN"])

    result = workspace_readiness("eng", org="anchore")

    assert result.ready
    assert any(
        item.kind == "available_env" and item.subject == "GH_TOKEN"
        for item in result.satisfied
    )


def test_a_local_clone_source_that_is_absent_does_not(org):
    """It degrades to a network fetch. Grouped with the blockers, it teaches
    the reader that the list is not worth reading."""
    _workspace("eng", repos=[{"host": "github.com", "repo": "anchore/enterprise",
                              "mount": "/workspace/repo",
                              "base_source": "/not/here"}])

    result = workspace_readiness("eng", org="anchore")

    assert [f.kind for f in result.advisory] == ["missing_path"]
    assert not any(f.kind == "missing_path" for f in result.blocking)


def test_a_required_mount_stops_it_and_an_optional_one_does_not(org):
    _workspace("eng")
    settings_ops.add_setting("autonomy.workspace.mount", 1, "eng:vuln", {
        "host_path": "/not/here/vuln", "container_path": "/w/v",
        "required": True}, org="anchore")
    settings_ops.add_setting("autonomy.workspace.mount", 1, "eng:mig", {
        "host_path": "/not/here/mig", "container_path": "/w/m",
        "required": False}, org="anchore")

    result = workspace_readiness("eng", org="anchore")

    assert [f.address.split("key=")[1] for f in result.blocking] == ["'eng:vuln' in 'anchore'"]
    assert [f.address.split("key=")[1] for f in result.advisory] == ["'eng:mig' in 'anchore'"]


def test_a_question_asked_in_the_wrong_place_is_not_ready(org, monkeypatch):
    """The one that matters most for a report somebody reads.

    Asked from a container, a bind-mount source on the host cannot be
    answered — and counting that as satisfied is how a workspace reads as set
    up when nobody has looked at the filesystem it actually uses.
    """
    monkeypatch.setenv("AUTONOMY_CONTAINER", "1")
    _workspace("eng")
    settings_ops.add_setting("autonomy.workspace.mount", 1, "eng:vuln", {
        "host_path": "/not/here/vuln", "container_path": "/w/v",
        "required": True}, org="anchore")

    result = workspace_readiness("eng", org="anchore")

    assert [f.kind for f in result.unanswerable] == ["unanswerable_here"]
    assert not result.blocking
    assert not result.ready, "an unasked question reported a workspace as ready"


def test_every_workspace_the_org_declares_is_reported(org):
    _workspace("beta")
    _workspace("alpha", env_from_host=["GH_TOKEN"])

    rows = org_readiness("anchore")

    assert [r.workspace_id for r in rows] == ["alpha", "beta"], "stable key order"
    assert [r.ready for r in rows] == [False, True]


def test_an_org_with_no_workspaces_reports_nothing_rather_than_failing(org):
    assert org_readiness("anchore") == []


def test_absent_pinned_capability_implementation_version_blocks_workspace(org):
    """An enabled install pinned to a vanished version is not launch-ready."""
    _workspace("eng")
    settings_ops.add_setting(
        "autonomy.capability.contract", 1, "test_execution",
        {
            "name": "test_execution",
            "version": 1,
            "summary": "Run tests",
            "ops": [{
                "name": "run",
                "summary": "Run selected tests",
                "input_schema": {},
                "output_schema": {},
            }],
        },
        org="anchore",
    )
    settings_ops.add_setting(
        "autonomy.capability.impl", 1, "autonomy/agent-test",
        {
            "name": "autonomy/agent-test",
            "version": 5,
            "implements": [{"contract": "test_execution", "version": 1}],
            "delivery_mode": "mounted_tools",
            "package_root": "agents/capabilities/agent-test",
            "probe": {"kind": "command", "entrypoint": "agent-test doctor"},
        },
        org="anchore",
    )
    settings_ops.add_setting(
        "autonomy.org.capability.install", 1, "test_execution",
        {
            "contract": "test_execution",
            "contract_version": 1,
            "implementation": "autonomy/agent-test",
            "implementation_version": 3,
        },
        org="anchore",
    )
    settings_ops.add_setting(
        "autonomy.workspace.capability.enable", 1,
        "eng:test_execution",
        {"contract": "test_execution", "contract_version": 1},
        org="anchore",
    )

    result = workspace_readiness("eng", org="anchore")

    assert not result.ready
    broken = [
        finding for finding in result.blocking
        if finding.kind == "missing_capability_implementation_version"
    ]
    assert [finding.subject for finding in broken] == ["autonomy/agent-test@3"]


def test_missing_capability_contract_is_one_specialized_factor(org):
    _workspace("eng")
    settings_ops.add_setting(
        "autonomy.workspace.capability.enable", 1,
        "eng:video_tooling",
        {"contract": "video_tooling", "contract_version": 1},
        org="anchore",
    )

    result = workspace_readiness("eng", org="anchore")

    assert not result.ready
    assert not any(f.kind == "missing_reference" for f in result.blocking)
    assert [f.kind for f in result.blocking] == [
        "missing_capability_install",
        "missing_capability_contract_version",
    ]


def _volume_mount(*, required=True):
    return {
        "subpath": "fixtures",
        "container_path": "/opt/fixtures",
        "kind": "dir",
        "required": required,
    }


def test_volume_mount_presence_is_blocking_and_empty_is_advisory(
    org, monkeypatch,
):
    _workspace("eng")
    monkeypatch.setattr(workspace_manager, "DATA_DIR", org)
    monkeypatch.setattr(
        mount_plan,
        "discover_topology",
        lambda: mount_plan.NodeTopology(is_host_process=True, volumes=()),
    )
    settings_ops.add_setting(
        "autonomy.workspace.mount", 2, "eng:fixtures", _volume_mount(),
        org="anchore",
    )

    absent = workspace_readiness("eng", org="anchore")
    assert [f.kind for f in absent.blocking] == ["missing_path"]

    target = org / "org-mounts" / "anchore" / "fixtures"
    target.mkdir(parents=True)
    empty = workspace_readiness("eng", org="anchore")
    assert not empty.blocking
    assert [f.kind for f in empty.advisory] == ["unpopulated_path"]

    (target / "sentinel").write_text("ready")
    assert workspace_readiness("eng", org="anchore").ready


def test_volume_mount_is_unanswerable_without_volume_view(org, monkeypatch):
    _workspace("eng")
    monkeypatch.setattr(
        mount_plan,
        "discover_topology",
        lambda: mount_plan.NodeTopology(is_host_process=False, volumes=()),
    )
    settings_ops.add_setting(
        "autonomy.workspace.mount", 2, "eng:fixtures", _volume_mount(),
        org="anchore",
    )

    result = workspace_readiness("eng", org="anchore")

    assert not result.ready
    assert [f.kind for f in result.unanswerable] == ["unanswerable_here"]
    assert result.unanswerable[0].frame == "container-fs"


def test_a_peers_workspace_is_not_this_operators_to_provision(org):
    """Read the org's OWN rows. A workspace visible because a peer publishes
    it belongs to that peer, and listing it asks the operator to provision
    something they do not own."""
    GraphDB.create_org_db("partner").close()
    settings_ops.add_setting(
        "autonomy.workspace", 1, "theirs",
        {"name": "Theirs", "image": "img", "harness": "claude",
         "env_from_host": ["GH_TOKEN"]},
        org="partner", state="published")

    assert [r.workspace_id for r in org_readiness("anchore")] == []
