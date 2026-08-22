"""A caller-supplied ?peers= is honoured only for a global-authority caller.

Invariant 1: an org-bound caller does not choose which orgs compose into a
settings read — it gets its org's resolved peers. Only the operator (global
authority) may name a peer set explicitly.
"""
from __future__ import annotations

from starlette.requests import Request

from tools.dashboard import api_auth
from tools.dashboard.server import _client_peers_if_global


def _req(principal, peers=None):
    qs = f"peers={peers}".encode() if peers is not None else b""
    return Request({
        "type": "http", "method": "GET", "path": "/api/graph/settings/x",
        "query_string": qs, "headers": [],
        "state": {"api_principal": principal},
    })


ORG = api_auth.ApiPrincipal(api_auth.ApiPrincipalKind.ORG_SESSION, subject="a", org="autonomy")
LOCAL = api_auth.ApiPrincipal(api_auth.ApiPrincipalKind.LOCAL_SESSION, subject="h")
OPERATOR = api_auth.ApiPrincipal(api_auth.ApiPrincipalKind.OPERATOR_COOKIE, subject="op")


def test_org_bound_caller_cannot_name_peers():
    assert _client_peers_if_global(_req(ORG, peers="anchore,dynbench")) is None
    assert _client_peers_if_global(_req(ORG)) is None


def test_global_authority_may_name_peers():
    assert _client_peers_if_global(_req(OPERATOR, peers="anchore")) == ["anchore"]
    assert _client_peers_if_global(_req(LOCAL, peers="anchore,dynbench")) == ["anchore", "dynbench"]
    # global with no param -> default resolution
    assert _client_peers_if_global(_req(OPERATOR)) is None
