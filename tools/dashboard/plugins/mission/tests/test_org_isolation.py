"""Org isolation at the API boundary — the security property, pinned.

Plugin code runs in the dashboard process and could read any org
database; the enforcement point is the middleware-established
principal. These tests pin the three legs: an org-bound session reads
exactly its own org (and cannot probe another org's missions, not even
their existence); a global-authority caller (operator cookie, local
host session) aggregates all orgs; anything else fails closed to an
empty scope. Anything less would be a security bug.
"""
from __future__ import annotations

import pytest

from tools.dashboard.api_auth import ApiPrincipal, ApiPrincipalKind
from tools.dashboard.plugins.mission import compose
from tools.dashboard.plugins.mission.entrypoints import api


class _Req:
    def __init__(self, kind, subject=None, org=None):
        class _State:
            pass
        self.state = _State()
        self.state.api_principal = ApiPrincipal(kind, subject=subject, org=org)
        self.state.api_organization = org


@pytest.fixture()
def two_orgs(monkeypatch):
    """Two org databases, one mission each."""
    missions = {"orga": {"m-a": {"name": "A"}}, "orgb": {"m-b": {"name": "B"}}}
    monkeypatch.setattr(
        compose, "load_mission",
        lambda org, mid: missions.get(org, {}).get(mid))
    import tools.graph.cross_org as cross_org
    monkeypatch.setattr(cross_org, "list_org_slugs",
                        lambda **kw: ["orga", "orgb"])
    return missions


def test_org_session_scoped_to_its_own_org(two_orgs):
    req = _Req(ApiPrincipalKind.ORG_SESSION, subject="auto-x", org="orga")
    assert api._org_scopes(req) == ["orga"]
    assert api._owning_org(req, "m-a") == "orga"
    # another org's mission is indistinguishable from a nonexistent one
    assert api._owning_org(req, "m-b") is None


def test_operator_cookie_aggregates_all_orgs(two_orgs):
    req = _Req(ApiPrincipalKind.OPERATOR_COOKIE)
    assert api._org_scopes(req) == ["orga", "orgb"]
    assert api._owning_org(req, "m-a") == "orga"
    assert api._owning_org(req, "m-b") == "orgb"


def test_local_host_session_has_global_authority(two_orgs):
    req = _Req(ApiPrincipalKind.LOCAL_SESSION)
    assert api._org_scopes(req) == ["orga", "orgb"]


def test_unscoped_caller_fails_closed(two_orgs):
    req = _Req(ApiPrincipalKind.COMPATIBILITY)
    assert api._org_scopes(req) == []
    assert api._owning_org(req, "m-a") is None


def test_org_session_with_lost_scope_fails_closed(two_orgs):
    """An org-bound principal whose org somehow resolves empty must see
    nothing — never fall through to aggregation or to personal."""
    req = _Req(ApiPrincipalKind.ORG_SESSION, subject="auto-x", org=None)
    assert api._org_scopes(req) == []
