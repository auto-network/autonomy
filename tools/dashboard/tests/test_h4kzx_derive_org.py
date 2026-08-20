"""auto-h4kzx, derive-when-present: the settings caller-org helpers take the org
from a valid session-token bearer when present (ignoring a spoofed X-Graph-Org),
and fall back to the header/cascade when there is no bearer — additive, so a
no-bearer caller is never refused (the no-token REFUSE is a later hardening).
"""

from __future__ import annotations

from unittest.mock import patch

from starlette.responses import JSONResponse

from tools.dashboard import server
from tools.graph import ops as graph_ops
from tools.graph import settings_ops


class _Req:
    def __init__(self, xorg=None):
        self.headers = {"X-Graph-Org": xorg} if xorg else {}


def _with_token(session, org):
    # authenticate_session_request -> ((session, org), None)
    return patch.object(
        server, "authenticate_session_request",
        lambda request: ((session, org), None),
    )


def _no_token():
    # authenticate_session_request -> (None, error_response)
    return patch.object(
        server, "authenticate_session_request",
        lambda request: (None, JSONResponse({"error": "no token"}, status_code=401)),
    )


def test_bearer_org_wins_over_spoofed_header():
    """A container bearer for 'beta' reading with X-Graph-Org: anchore resolves
    to beta — the header can no longer override the token (spoofing closed)."""
    with _with_token("auto-1", "beta"):
        assert server._caller_org(_Req(xorg="anchore")) == "beta"
        assert server._settings_caller_org(_Req(xorg="anchore")) == "beta"


def test_no_bearer_falls_back_to_header():
    """No bearer (old client / host): the header/cascade still applies —
    additive, never refused."""
    with _no_token():
        assert server._caller_org(_Req(xorg="anchore")) == "anchore"
        assert server._settings_caller_org(_Req(xorg="anchore")) == "anchore"


def test_no_bearer_no_header_settings_uses_caller_org_sentinel():
    with _no_token():
        assert server._caller_org(_Req()) is None
        assert server._settings_caller_org(_Req()) is graph_ops.CALLER_ORG


def test_local_token_org_none_falls_back_to_cascade():
    """A genuine local caller (valid token, org=None) keeps the header/cascade —
    the token does not force a scope it does not carry."""
    with _with_token("host-1", None):
        assert server._caller_org(_Req(xorg="anchore")) == "anchore"
        assert server._settings_caller_org(_Req()) is graph_ops.CALLER_ORG


# ── network_routes._scoped_org: same derive-when-present, the network-key path ──

from tools.dashboard import network_routes  # noqa: E402


def _scoped_token(org):
    return patch.object(server, "_token_org_or_none", lambda request: org)


def test_scoped_org_bearer_refuses_cross_org_request():
    """A bearer for 'beta' asking ?org=anchore is refused — the caller can no
    longer name its own org, closing the another-org's-network-key leak."""
    with _scoped_token("beta"):
        org, refused = network_routes._scoped_org("anchore", request=_Req())
    assert org is None
    assert refused is not None and refused.status_code == 403


def test_scoped_org_bearer_own_org_honored_and_returns_token_slug():
    with _scoped_token("beta"):
        org, refused = network_routes._scoped_org("beta", request=_Req())
        assert refused is None and org == "beta"
        org2, refused2 = network_routes._scoped_org(None, request=_Req())
        assert refused2 is None and org2 == "beta"


def test_scoped_org_no_bearer_explicit_org_is_a_selection():
    """No bearer means a LOCAL caller — the operator or a host process, whose
    authority already spans every org. An explicit ?org= is a selection of
    WHICH org's key, never an escalation, so it is honored, not compared
    against any ambient value (none exists). No org named -> the sentinel."""
    with _scoped_token(None):
        org, refused = network_routes._scoped_org("anchore", request=_Req())
        assert refused is None and org == "anchore"
        org2, refused2 = network_routes._scoped_org("beta", request=_Req())
        assert refused2 is None and org2 == "beta"
        org3, refused3 = network_routes._scoped_org(None, request=_Req())
        assert refused3 is None and org3 is settings_ops.CALLER_ORG
