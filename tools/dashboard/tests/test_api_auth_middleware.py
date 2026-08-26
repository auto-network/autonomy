"""Global API identity and trusted-scope contract tests.

The middleware is deliberately in compatibility mode while route policies are
migrated: unauthenticated and delegated requests still reach their handlers,
but they never acquire authenticated authority.  These tests pin the final
credential classification and scope rules independently of that rollout gate.
"""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from tools.dashboard import api_auth
from tools.graph import ops as graph_ops


COOKIE = "test_dashboard_session"


async def _whoami(request):
    principal = api_auth.principal_from_request(request)
    body = await request.body()
    return JSONResponse({
        "kind": principal.kind.value,
        "subject": principal.subject,
        "org": principal.org,
        "authenticated": principal.authenticated,
        "global_authority": principal.global_authority,
        "org_bound": principal.org_bound,
        "auth_error_status": principal.auth_error_status,
        "effective_org": graph_ops._caller_org_var.get(),
        "body": body.decode(),
        "existing_state": getattr(request.state, "existing", None),
    })


def _authenticate_bearer(request):
    value = request.headers.get("authorization", "")
    identities = {
        "Bearer org-a": ("agent-a", "org-a"),
        "Bearer org-b": ("agent-b", "org-b"),
        "Bearer local": ("host-local", None),
    }
    identity = identities.get(value)
    if identity is not None:
        return identity, None
    return None, JSONResponse({"error": "invalid bearer"}, status_code=401)


def _verify_cookie(value):
    if value == "valid-cookie":
        return {"sid": "browser-1", "method": "password"}
    return None


def _app(*, authenticate_bearer=_authenticate_bearer, verify_cookie=_verify_cookie):
    async def seed_state(scope, receive, send):
        scope.setdefault("state", {})["existing"] = "preserved"
        await inner(scope, receive, send)

    inner = Starlette(
        routes=[
            Route("/api/probe", _whoami, methods=["GET", "POST"]),
            Route("/page", _whoami),
        ],
        middleware=[Middleware(
            api_auth.ApiIdentityMiddleware,
            authenticate_bearer=authenticate_bearer,
            verify_cookie=verify_cookie,
            cookie_name=COOKIE,
        )],
    )
    return seed_state


def _get(*, headers=None, cookies=None, path="/api/probe"):
    with TestClient(_app()) as client:
        if cookies:
            client.cookies.update(cookies)
        return client.get(path, headers=headers or {}).json()


def test_valid_dashboard_cookie_is_global_operator_and_may_select_org():
    result = _get(
        headers={"X-Graph-Org": "org-a"},
        cookies={COOKIE: "valid-cookie"},
    )
    assert result == {
        "kind": "operator_cookie",
        "subject": "browser-1",
        "org": None,
        "authenticated": True,
        "global_authority": True,
        "org_bound": False,
        "auth_error_status": None,
        "effective_org": "org-a",
        "body": "",
        "existing_state": "preserved",
    }


def test_valid_dashboard_cookie_needs_no_org_selector():
    result = _get(cookies={COOKIE: "valid-cookie"})
    assert result["kind"] == "operator_cookie"
    assert result["effective_org"] is None


def test_org_session_conflicting_header_is_refused():
    """auto-lbwzr: an org bearer for org-a WITH ``X-Graph-Org: org-b`` names two
    different orgs. The token is authoritative and un-widenable, and an org
    caller has no reason to request another org, so the conflict is a spoof or a
    client bug — refused loudly (403), not silently scoped to org-a."""
    with TestClient(_app()) as client:
        resp = client.get("/api/probe", headers={
            "Authorization": "Bearer org-a",
            "X-Graph-Org": "org-b",
        })
    assert resp.status_code == 403
    assert "mismatch" in resp.json()["error"].lower()


def test_org_session_matching_header_is_served():
    """A redundant but MATCHING ``X-Graph-Org`` is not a conflict — served,
    scoped to the bearer's org."""
    result = _get(headers={
        "Authorization": "Bearer org-a",
        "X-Graph-Org": "org-a",
    })
    assert result["kind"] == "org_session"
    assert result["subject"] == "agent-a"
    assert result["org"] == "org-a"
    assert result["effective_org"] == "org-a"
    assert result["org_bound"] is True
    assert result["global_authority"] is False


def test_org_session_token_supplies_scope_when_header_is_absent():
    result = _get(headers={"Authorization": "Bearer org-b"})
    assert result["org"] == "org-b"
    assert result["effective_org"] == "org-b"


def test_valid_bearer_takes_narrower_precedence_over_valid_cookie():
    # No conflicting X-Graph-Org: the point is bearer-vs-cookie precedence, not
    # the mismatch refuse (test_org_session_conflicting_header_is_refused covers
    # that). A conflicting header would 403 before precedence even mattered.
    result = _get(
        headers={"Authorization": "Bearer org-a"},
        cookies={COOKIE: "valid-cookie"},
    )
    assert result["kind"] == "org_session"
    assert result["effective_org"] == "org-a"


def test_local_host_session_token_has_global_authority():
    result = _get(headers={
        "Authorization": "Bearer local",
        "X-Graph-Org": "org-b",
    })
    assert result["kind"] == "local_session"
    assert result["subject"] == "host-local"
    assert result["global_authority"] is True
    assert result["effective_org"] == "org-b"


def test_invalid_bearer_falls_back_to_valid_cookie_during_rollout():
    result = _get(
        headers={
            "Authorization": "Bearer revoked",
            "X-Graph-Org": "org-a",
        },
        cookies={COOKIE: "valid-cookie"},
    )
    assert result["kind"] == "operator_cookie"
    assert result["effective_org"] == "org-a"


def test_missing_credentials_remain_open_without_gaining_authority():
    result = _get(headers={"X-Graph-Org": "legacy-org"})
    assert result["kind"] == "compatibility"
    assert result["authenticated"] is False
    assert result["global_authority"] is False
    assert result["effective_org"] == "legacy-org"
    assert result["auth_error_status"] is None


def test_invalid_or_delegated_bearer_remains_open_for_endpoint_verifier():
    result = _get(headers={
        "Authorization": "Bearer delegated-service-token",
        "X-Graph-Org": "legacy-org",
    })
    assert result["kind"] == "compatibility"
    assert result["auth_error_status"] == 401
    assert result["effective_org"] == "legacy-org"


def test_invalid_cookie_remains_compatibility_not_operator():
    result = _get(cookies={COOKIE: "expired-or-revoked"})
    assert result["kind"] == "compatibility"
    assert result["authenticated"] is False


def test_bearer_store_failure_does_not_mask_a_valid_cookie():
    def broken_bearer(_request):
        raise RuntimeError("auth DB unavailable")

    with TestClient(_app(authenticate_bearer=broken_bearer)) as client:
        result = client.get(
            "/api/probe",
            headers={
                "Authorization": "Bearer unknown",
                "X-Graph-Org": "operator-selected",
                "Cookie": f"{COOKIE}=valid-cookie",
            },
        ).json()
    assert result["kind"] == "operator_cookie"
    assert result["effective_org"] == "operator-selected"


def test_cookie_store_failure_stays_open_without_operator_authority():
    def broken_cookie(_value):
        raise RuntimeError("identity session DB unavailable")

    with TestClient(_app(verify_cookie=broken_cookie)) as client:
        result = client.get(
            "/api/probe",
            headers={"Cookie": f"{COOKIE}=otherwise-valid"},
        ).json()
    assert result["kind"] == "compatibility"
    assert result["authenticated"] is False


def test_non_api_requests_keep_header_scope_for_human_gate_path():
    result = _get(path="/page", headers={"X-Graph-Org": "page-org"})
    assert result["kind"] == "compatibility"
    assert result["effective_org"] == "page-org"


def test_request_body_is_not_consumed_by_authentication():
    with TestClient(_app()) as client:
        result = client.post(
            "/api/probe",
            content=b"payload survives",
            headers={"Authorization": "Bearer org-a"},
        ).json()
    assert result["body"] == "payload survives"


def test_scope_and_principal_do_not_leak_between_requests():
    with TestClient(_app()) as client:
        first = client.get(
            "/api/probe",
            headers={"Authorization": "Bearer org-a"},
        ).json()
        second = client.get("/api/probe").json()
    assert first["effective_org"] == "org-a"
    assert second["effective_org"] is None
    assert second["kind"] == "compatibility"


def test_global_middleware_covers_the_live_api_route_table():
    from tools.dashboard import server

    middleware_classes = [entry.cls for entry in server.app.user_middleware]
    assert middleware_classes.count(api_auth.ApiIdentityMiddleware) == 1

    api_routes = [
        route for route in server.app.routes
        if getattr(route, "path", "").startswith("/api/")
    ]
    # Dynamic inventory: core and plugin routes share the same app boundary.
    assert len(api_routes) >= 250
