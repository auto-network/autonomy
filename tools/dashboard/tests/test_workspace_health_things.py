"""What the workspace-health route reports, once, as data.

Two things this route got wrong, both of which reached a screen.

The report was PER WORKSPACE, so a fact several workspaces share was drawn
once per workspace: on real anchore data, one unprovisioned credential
rendered as seven separate problems and two host variables as six each --
twenty-three rows for six facts. A reader could not see that setting one
variable clears six workspaces, which is the only thing they wanted.

And every finding travelled as an English sentence. A UI built against it had
to regex the quoted path back out of the prose to put it in a heading, which
is the tell that the checker held the value and threw it away.
"""
from __future__ import annotations

import pytest

from agents.workspace_readiness import org_readiness
from tools.dashboard.server import _things_missing
from tools.graph import settings_ops
from tools.graph.db import GraphDB


@pytest.fixture
def org(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    # Answer in the host frame, so the path checks resolve rather than
    # refusing -- this suite is about the SHAPE of the report, and the frame
    # guard has its own tests.
    monkeypatch.setenv("AUTONOMY_CONTAINER", "0")
    GraphDB.create_org_db("anchore").close()
    yield tmp_path
    GraphDB.close_all_pooled()


def _workspace(key, **extra):
    payload = {"name": key.title(), "image": "img", "harness": "claude"}
    payload.update(extra)
    settings_ops.add_setting(
        "autonomy.workspace", 1, key, payload, org="anchore", state="raw")


def _mount(workspace, name, host_path, *, required=True, description=None):
    payload = {
        "host_path": host_path,
        "container_path": f"/etc/autonomy/artifacts/{name}",
        "mode": "ro",
        "required": required,
    }
    if description is not None:
        payload["description"] = description
    settings_ops.add_setting(
        "autonomy.workspace.mount", 1, f"{workspace}:{name}", payload,
        org="anchore", state="raw")


def _things(kind=None):
    out = _things_missing(org_readiness("anchore"))
    return [t for t in out if kind is None or t["kind"] == kind]


# ── one fact, however many workspaces want it ────────────────


def test_one_missing_file_wanted_by_three_workspaces_is_one_thing(org):
    for ws in ("alpha", "beta", "gamma"):
        _workspace(ws)
        _mount(ws, "license.yaml", "/nonexistent/license.yaml")

    things = _things("missing_path")

    assert len(things) == 1, (
        f"the same missing file was reported {len(things)} times because three "
        f"workspaces declare it; a reader sees three problems and one fix"
    )
    assert sorted(things[0]["needed_by"]) == ["Alpha", "Beta", "Gamma"]


def test_one_missing_variable_wanted_by_two_workspaces_is_one_thing(org):
    for ws in ("alpha", "beta"):
        _workspace(ws, env_from_host=["GH_TOKEN"])

    things = _things("missing_env")

    assert len(things) == 1
    assert things[0]["subject"] == "GH_TOKEN"
    assert sorted(things[0]["needed_by"]) == ["Alpha", "Beta"]


def test_different_things_do_not_collapse_into_each_other(org):
    _workspace("alpha", env_from_host=["GH_TOKEN", "OTHER_TOKEN"])

    subjects = sorted(t["subject"] for t in _things("missing_env"))

    assert subjects == ["GH_TOKEN", "OTHER_TOKEN"]


def test_the_worst_severity_any_declaration_gave_it_wins(org):
    """One workspace calling it optional does not make it optional.

    Reporting the softer answer says a launch will work when it will not, and
    it is the direction nobody re-examines.
    """
    _workspace("alpha")
    _mount("alpha", "license.yaml", "/nonexistent/license.yaml", required=False)
    _workspace("beta")
    _mount("beta", "license.yaml", "/nonexistent/license.yaml", required=True)

    things = _things("missing_path")

    assert len(things) == 1
    assert things[0]["severity"] == "blocking"


# ── the finding as data, not as a sentence ───────────────────


def test_a_finding_carries_the_facts_without_parsing_its_own_prose(org):
    _workspace("alpha")
    _mount("alpha", "license.yaml", "/nonexistent/license.yaml",
           description="Anchore Enterprise license — RSA-verified at login.")

    thing = _things("missing_path")[0]

    assert thing["subject"] == "/nonexistent/license.yaml"
    assert thing["field"] == "host_path"
    assert thing["set_id"] == "autonomy.workspace.mount"
    assert thing["key"] == "alpha:license.yaml"
    assert thing["org"] == "anchore"
    assert thing["frame"] == "platform-host"
    assert thing["description"].startswith("Anchore Enterprise license")
    # What this KIND of thing is, as opposed to what THIS one is.
    assert thing["field_description"] == (
        "Absolute host path to the directory to mount")


def test_a_description_travels_from_whichever_declaration_has_one(org):
    """Two rows can name the same file and only one describe it."""
    _workspace("bare")
    _mount("bare", "license.yaml", "/nonexistent/license.yaml")
    _workspace("described")
    _mount("described", "license.yaml", "/nonexistent/license.yaml",
           description="Anchore Enterprise license — RSA-verified at login.")

    thing = _things("missing_path")[0]

    assert thing["description"].startswith("Anchore Enterprise license")


# ── the load-bearing one ─────────────────────────────────────


def test_a_row_that_only_mentions_a_thing_does_not_get_to_name_it(org):
    """THE one worth more than the rest.

    A row's ``name`` and ``description`` describe the thing THAT ROW IS
    ABOUT. A mount row is about the one path it declares. A workspace row
    listing six names in ``env_from_host`` is about none of them.

    Attaching the workspace's own name to a variable produces a confident
    label that is simply wrong -- a missing GH_TOKEN rendered as "Alpha",
    because a field existed and meant something else. Nothing about that
    looks broken from outside: it reads as a real name, on a real tile, and
    the reader has no way to tell.

    Blank is correct here. Nothing in the data describes a host variable, so
    a UI showing nothing is telling the truth.
    """
    _workspace("alpha", env_from_host=["GH_TOKEN"])

    thing = _things("missing_env")[0]

    assert thing["subject"] == "GH_TOKEN"
    assert thing["name"] == "", (
        f"the variable was labelled {thing['name']!r}, which is the "
        f"WORKSPACE's name -- the row mentions the variable, it does not "
        f"describe it"
    )
    assert thing["description"] == ""


def test_a_row_that_is_about_the_thing_does_get_to_describe_it(org):
    """The other half: without this the gate above could just blank everything."""
    _workspace("alpha")
    _mount("alpha", "license.yaml", "/nonexistent/license.yaml",
           description="Anchore Enterprise license — RSA-verified at login.")

    thing = _things("missing_path")[0]

    assert thing["description"].startswith("Anchore Enterprise license")
