"""The machine-scoped, route-scoped MCP relay service token.

Proves the credential does exactly what it is allowed to and nothing more:

* it is stored hashed in auth.db, carries no organization, and is invisible to
  the ordinary session-token path (``resolve_token``);
* presented on an ``/api/mcp/*`` route it classifies as an ``MCP_SERVICE``
  principal — authenticated, but with no organization and no dashboard
  authority;
* presented on any other route it does not classify here at all, and because it
  is not a session token the ordinary bearer path cannot authenticate it either.
"""

from __future__ import annotations

import hashlib

import pytest
from starlette.requests import Request

from tools.dashboard import api_auth
from tools.dashboard.dao import auth_db
from tools.dashboard.server import authenticate_mcp_service

RAW = "relay-service-secret-value"
HASH = hashlib.sha256(RAW.encode()).hexdigest()


@pytest.fixture
def service_token_db(tmp_path):
    """Bind auth_db to a fresh temp DB holding one registered service token,
    and restore the real connection afterwards so no other test is disturbed."""
    saved_conn = auth_db._conn
    auth_db.init_db(tmp_path / "auth.db")
    auth_db.insert_service_token(HASH, "mcp-relay-service")
    try:
        yield
    finally:
        auth_db._conn = saved_conn


def _request(path: str, *, bearer: str | None) -> Request:
    headers = []
    if bearer is not None:
        headers.append((b"authorization", f"Bearer {bearer}".encode()))
    return Request({
        "type": "http",
        "method": "POST",
        "path": path,
        "query_string": b"",
        "headers": headers,
    })


def test_service_token_is_invisible_to_the_session_path(service_token_db):
    # resolve_service_token sees it; resolve_token (session tokens) never does.
    assert auth_db.resolve_service_token(HASH) == "mcp-relay-service"
    assert auth_db.resolve_token(HASH) is None


def test_a_session_token_is_invisible_to_the_service_path(service_token_db):
    other_raw = "an-ordinary-session-token"
    other_hash = hashlib.sha256(other_raw.encode()).hexdigest()
    auth_db.insert_token(other_hash, "auto-1234-5678", org="autonomy")
    assert auth_db.resolve_service_token(other_hash) is None


def test_mcp_service_principal_has_no_authority_beyond_being_authenticated():
    p = api_auth.ApiPrincipal(
        api_auth.ApiPrincipalKind.MCP_SERVICE, subject="mcp-relay-service")
    assert p.authenticated is True
    assert p.global_authority is False
    assert p.org_bound is False
    assert p.org is None


def test_valid_token_classifies_on_an_mcp_route(service_token_db):
    principal = authenticate_mcp_service(
        _request("/api/mcp/session/resolve", bearer=RAW))
    assert principal is not None
    assert principal.kind is api_auth.ApiPrincipalKind.MCP_SERVICE
    assert principal.subject == "mcp-relay-service"


def test_valid_token_does_not_classify_off_its_routes(service_token_db):
    # The same valid token on a non-MCP route: the service verifier declines,
    # and the ordinary session path cannot see it either (asserted above), so it
    # authenticates nowhere but the MCP routes.
    assert authenticate_mcp_service(
        _request("/api/graph/settings/autonomy.workspace", bearer=RAW)) is None


def test_absent_or_wrong_token_on_an_mcp_route_does_not_classify(service_token_db):
    assert authenticate_mcp_service(
        _request("/api/mcp/session/resolve", bearer=None)) is None
    assert authenticate_mcp_service(
        _request("/api/mcp/session/resolve", bearer="not-the-token")) is None
