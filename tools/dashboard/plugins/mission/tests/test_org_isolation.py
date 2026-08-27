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


def test_session_contributions_link_the_new_app(monkeypatch):
    """A pillar coordinator gets one /mission/<uuid> badge; completed
    missions and unrelated sessions contribute nothing."""
    from collections import namedtuple
    Member = namedtuple("Member", "key payload")
    rows = {
        "mission.registry": [
            Member("mid-1", {"name": "Multi-User Autonomy",
                             "status": "active"}),
            Member("mid-2", {"name": "Old Push", "status": "complete"}),
        ],
        "mission.pillar": [
            Member("mid-1:relay", {"name": "Relay",
                                   "coordinator_session": "auto-relay",
                                   "color": "#3987e5"}),
            Member("mid-2:legacy", {"name": "Legacy",
                                    "coordinator_session": "auto-old"}),
        ],
    }
    from tools.graph import ops as graph_ops
    monkeypatch.setattr(
        graph_ops, "read_set",
        lambda set_id, org=None, peers=None: rows.get(set_id, []))
    monkeypatch.setattr(api, "_org_scopes", lambda request: ["autonomy"])
    out = api.session_contributions(
        ["auto-relay", "auto-old", "auto-none"], request=None)
    assert [c["href"] for c in out["auto-relay"]] == ["/mission/mid-1"]
    assert out["auto-relay"][0]["accent"] == "#3987e5"
    assert "Relay" in out["auto-relay"][0]["title"]
    assert out["auto-old"] == []      # completed mission: no badge
    assert out["auto-none"] == []


def test_mission_coordinator_badges_too_deduped(monkeypatch):
    """The registry row's own coordinator_session gets a badge (it is
    what mission-surface relays target); a session already badged via a
    pillar of the same mission keeps only its pillar entry."""
    from collections import namedtuple
    Member = namedtuple("Member", "key payload")
    rows = {
        "mission.registry": [
            Member("mid-1", {"name": "Vault", "status": "active",
                             "coordinator_session": "auto-mc"}),
            Member("mid-2", {"name": "Both", "status": "active",
                             "coordinator_session": "auto-dual"}),
            Member("mid-3", {"name": "Done", "status": "complete",
                             "coordinator_session": "auto-mc"}),
        ],
        "mission.pillar": [
            Member("mid-2:relay", {"name": "Relay",
                                   "coordinator_session": "auto-dual",
                                   "color": "#3987e5"}),
        ],
    }
    from tools.graph import ops as graph_ops
    monkeypatch.setattr(
        graph_ops, "read_set",
        lambda set_id, org=None, peers=None: rows.get(set_id, []))
    monkeypatch.setattr(api, "_org_scopes", lambda request: ["autonomy"])
    out = api.session_contributions(["auto-mc", "auto-dual"], request=None)
    assert [c["id"] for c in out["auto-mc"]] == ["mission-coordinator:mid-1"]
    assert "mission coordinator" in out["auto-mc"][0]["title"]
    # dual: pillar badge only — deduped against the registry badge
    assert [c["id"] for c in out["auto-dual"]] == ["mission-pillar:mid-2:relay"]
