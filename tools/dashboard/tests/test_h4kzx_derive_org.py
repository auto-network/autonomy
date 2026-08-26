"""The common-core org helpers (api_auth) that replaced the per-module
``_caller_org`` / ``_settings_caller_org`` / ``_scoped_org`` resolvers.

The derive-from-token / spoof-closed behavior — a valid org bearer's org wins
over a spoofed ``X-Graph-Org`` — is proven at the middleware in
``test_api_auth_middleware.py``, the ONE place that logic lives now. This file
proves the helpers that READ the middleware's result: the settings-scope
sentinel default, and the network cross-org REFUSE (``resolve_scoped_org``).
"""

from __future__ import annotations

from starlette.requests import Request

from tools.dashboard import api_auth
from tools.dashboard.api_auth import ApiPrincipal, ApiPrincipalKind
from tools.graph import settings_ops


def _req(*, principal: ApiPrincipal, organization=None) -> Request:
    """A request carrying the identity the middleware would have bound —
    handlers and the helpers read only this, never the raw header."""
    return Request(
        {
            "type": "http",
            "headers": [],
            "state": {
                "api_principal": principal,
                "api_organization": organization,
            },
        }
    )


def _org_session(org: str) -> ApiPrincipal:
    return ApiPrincipal(ApiPrincipalKind.ORG_SESSION, subject="auto-1", org=org)


def _operator() -> ApiPrincipal:
    return ApiPrincipal(ApiPrincipalKind.OPERATOR_COOKIE, subject="op")


# ── settings_scope_from_request: the CALLER_ORG sentinel default ──────────


def test_settings_scope_uses_the_selected_org():
    r = _req(principal=_org_session("beta"), organization="beta")
    assert api_auth.settings_scope_from_request(r) == "beta"


def test_settings_scope_falls_back_to_the_caller_org_sentinel():
    """No org selected → the env-cascade sentinel, never a scopeless None that
    would silently land a Settings write in the scopeless DB."""
    r = _req(principal=_operator(), organization=None)
    assert api_auth.settings_scope_from_request(r) is settings_ops.CALLER_ORG


# ── resolve_scoped_org: the network cross-org refuse ──────────────────────


def test_scoped_org_bearer_refuses_cross_org_request():
    """An org bearer for 'beta' asking for ?org=anchore is refused — the caller
    cannot name another org, closing the another-org's-network-key leak."""
    r = _req(principal=_org_session("beta"), organization="beta")
    org, refused = api_auth.resolve_scoped_org("anchore", request=r)
    assert org is None
    assert refused is not None and refused.status_code == 403


def test_scoped_org_bearer_own_org_honored_and_returns_token_slug():
    r = _req(principal=_org_session("beta"), organization="beta")
    org, refused = api_auth.resolve_scoped_org("beta", request=r)
    assert refused is None and org == "beta"
    org2, refused2 = api_auth.resolve_scoped_org(None, request=r)
    assert refused2 is None and org2 == "beta"


def test_scoped_org_local_caller_selects_freely():
    """No org-bound token means a LOCAL caller (operator / host) whose authority
    already spans every org. An explicit selection is honored, not compared
    against any ambient value; no selection → the sentinel."""
    r = _req(principal=_operator(), organization=None)
    org, refused = api_auth.resolve_scoped_org("anchore", request=r)
    assert refused is None and org == "anchore"
    org2, refused2 = api_auth.resolve_scoped_org("beta", request=r)
    assert refused2 is None and org2 == "beta"
    org3, refused3 = api_auth.resolve_scoped_org(None, request=r)
    assert refused3 is None and org3 is settings_ops.CALLER_ORG
