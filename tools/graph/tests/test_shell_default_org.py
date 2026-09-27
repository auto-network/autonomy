"""``shell_default_org``: the shell's X-Graph-Org default (dashboard.shell.default-org).

There is no default first organization (graph://5f2f5a49-00d D7): a node
with no shared organization stamps no org, and a followed mirror is never
the answer, declared or derived. Before this, a fresh node that followed
Autonomy resolved to the mirror and every shell fetch was refused as a
write into it (compose simulation, 2026-09-27).
"""

from __future__ import annotations

import pytest

from tools.graph.db import _ORG_TYPE_CACHE, GraphDB
from tools.graph.schemas import dashboard_shell


@pytest.fixture
def orgs_env(tmp_path, monkeypatch):
    orgs_dir = tmp_path / "orgs"
    orgs_dir.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    GraphDB.close_all_pooled()
    _ORG_TYPE_CACHE.clear()
    yield orgs_dir
    GraphDB.close_all_pooled()
    _ORG_TYPE_CACHE.clear()


def _declare(monkeypatch, org: str | None):
    """Stand in for the machine-homed declaration (or its absence)."""
    from types import SimpleNamespace

    from tools.graph import settings_ops

    members = [] if org is None else [SimpleNamespace(
        key=dashboard_shell.SHELL_DEFAULT_ORG_KEY, payload={"org": org})]
    monkeypatch.setattr(settings_ops, "read_owned_set",
                        lambda *a, **k: SimpleNamespace(members=members))


def test_no_shared_organization_means_no_default(orgs_env, monkeypatch):
    _declare(monkeypatch, None)
    assert dashboard_shell.shell_default_org() == ""


def test_a_followed_mirror_alone_is_not_the_default(orgs_env, monkeypatch):
    _declare(monkeypatch, None)
    GraphDB.create_org_db("autonomy", type_="followed",
                          org_id="2d4b90cb-1e89-452b-82cb-68ca44fd8e52").close()
    assert dashboard_shell.shell_default_org() == ""


def test_first_shared_organization_is_the_derived_default(orgs_env, monkeypatch):
    _declare(monkeypatch, None)
    GraphDB.create_org_db("autonomy", type_="followed",
                          org_id="2d4b90cb-1e89-452b-82cb-68ca44fd8e52").close()
    GraphDB.create_org_db("localorg", type_="shared").close()
    assert dashboard_shell.shell_default_org() == "localorg"


def test_declared_default_wins_unless_it_names_a_mirror(orgs_env, monkeypatch):
    GraphDB.create_org_db("autonomy", type_="followed",
                          org_id="2d4b90cb-1e89-452b-82cb-68ca44fd8e52").close()
    GraphDB.create_org_db("localorg", type_="shared").close()
    _declare(monkeypatch, "localorg")
    assert dashboard_shell.shell_default_org() == "localorg"
    _declare(monkeypatch, "autonomy")
    assert dashboard_shell.shell_default_org() == "localorg"
