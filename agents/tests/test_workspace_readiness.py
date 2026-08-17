"""What an organization's workspaces still need on the machine that just joined.

Joining brings the declarations; none of them say anything about this box.
This is the step between "you are a member" and "you can work" — and it has
to distinguish three things a single list of problems cannot: what stops a
workspace running, what it runs without, and what this process is in the
wrong place to answer at all.
"""
from __future__ import annotations

import pytest

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


def test_a_credential_nobody_provisioned_stops_it(org):
    _workspace("eng", repos=[{"host": "github.com", "repo": "anchore/enterprise",
                              "mount": "/workspace/repo"}])

    result = workspace_readiness("eng", org="anchore")

    assert not result.ready
    assert any("secure.setting" in f.address for f in result.blocking)


def test_an_unset_forwarded_variable_stops_it(org):
    _workspace("eng", env_from_host=["GH_TOKEN"])

    result = workspace_readiness("eng", org="anchore")

    assert [f.kind for f in result.blocking] == ["missing_env"]


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
