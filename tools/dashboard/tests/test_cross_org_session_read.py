"""The cross-org guard on the session-content read routes.

A bearer stamped for one org must not read another org's session — transcript,
metadata, output files, or startup trace. The guard is a single predicate the
four read handlers share; these tests pin its authorization logic directly
(the org-derivation it calls, ``session_org_slug``, is tested elsewhere and is
stubbed here so only the comparison is under test).

The predicate returns True to mean "hidden", and each handler then returns its
own route-native not-found, so a cross-org session is byte-indistinguishable
from a nonexistent one — never a 403, which would confirm the session exists in
another org.
"""

from __future__ import annotations

import pytest
from starlette.requests import Request

from tools.dashboard import api_auth
from tools.dashboard import org_identity
from tools.dashboard import server

ORG = api_auth.ApiPrincipal(
    api_auth.ApiPrincipalKind.ORG_SESSION, subject="auto-1", org="autonomy")
ORG_NO_SLUG = api_auth.ApiPrincipal(
    api_auth.ApiPrincipalKind.ORG_SESSION, subject="auto-2", org=None)
LOCAL = api_auth.ApiPrincipal(api_auth.ApiPrincipalKind.LOCAL_SESSION, subject="host-1")
OPERATOR = api_auth.ApiPrincipal(api_auth.ApiPrincipalKind.OPERATOR_COOKIE, subject="op")


def _request(principal) -> Request:
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/api/session/x",
        "query_string": b"",
        "headers": [],
        "state": {"api_principal": principal},
    })


def test_org_caller_is_hidden_from_another_orgs_session(monkeypatch):
    monkeypatch.setattr(org_identity, "session_org_slug", lambda s: "personal")
    assert server._session_hidden_cross_org(_request(ORG), {"row": 1}) is True


def test_org_caller_sees_its_own_orgs_session(monkeypatch):
    monkeypatch.setattr(org_identity, "session_org_slug", lambda s: "autonomy")
    assert server._session_hidden_cross_org(_request(ORG), {"row": 1}) is False


def test_unresolvable_session_is_hidden_from_an_org_caller():
    # No session row → org cannot be attributed → an org caller may not
    # distinguish it from cross-org, so it is hidden.
    assert server._session_hidden_cross_org(_request(ORG), None) is True


def test_org_caller_without_an_org_slug_is_hidden(monkeypatch):
    monkeypatch.setattr(org_identity, "session_org_slug", lambda s: "autonomy")
    assert server._session_hidden_cross_org(_request(ORG_NO_SLUG), {"row": 1}) is True


def test_global_authority_sees_every_org(monkeypatch):
    monkeypatch.setattr(org_identity, "session_org_slug", lambda s: "personal")
    assert server._session_hidden_cross_org(_request(LOCAL), {"row": 1}) is False
    assert server._session_hidden_cross_org(_request(OPERATOR), {"row": 1}) is False


def test_compatibility_traffic_is_not_judged_here(monkeypatch):
    # The default-deny gate governs compatibility traffic; this guard never
    # turns it into a cross-org refusal.
    monkeypatch.setattr(org_identity, "session_org_slug", lambda s: "personal")
    assert server._session_hidden_cross_org(
        _request(api_auth.COMPATIBILITY_PRINCIPAL), {"row": 1}) is False
