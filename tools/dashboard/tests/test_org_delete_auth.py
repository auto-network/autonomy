"""Authorization contract for destructive organization deletion."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from tools.dashboard import api_auth, server
from tools.graph import org_ops


COOKIE = "test_dashboard_session"


def _authenticate_bearer(request):
    identities = {
        "Bearer org-a": ("agent-a", "org-a"),
        "Bearer local": ("host-local", None),
    }
    identity = identities.get(request.headers.get("authorization", ""))
    if identity is not None:
        return identity, None
    return None, JSONResponse({"error": "invalid bearer"}, status_code=401)


def _verify_cookie(value):
    return {"sid": "browser-1"} if value == "valid-cookie" else None


@pytest.fixture
def delete_client(monkeypatch):
    calls = []

    def remove_org(slug, *, force=False):
        calls.append((slug, force))
        return SimpleNamespace(to_dict=lambda: {"deleted": slug, "force": force})

    monkeypatch.setattr(org_ops, "remove_org", remove_org)
    app = Starlette(
        routes=[
            Route(
                "/api/orgs/{slug}",
                server.api_orgs_delete,
                methods=["DELETE"],
            ),
        ],
        middleware=[
            Middleware(
                api_auth.ApiIdentityMiddleware,
                authenticate_bearer=_authenticate_bearer,
                verify_cookie=_verify_cookie,
                cookie_name=COOKIE,
            ),
        ],
    )
    with TestClient(app) as client:
        yield client, calls


@pytest.mark.parametrize(
    ("headers", "cookies", "status"),
    [
        ({}, {}, 401),
        ({"Authorization": "Bearer org-a"}, {}, 403),
        (
            {
                "Authorization": "Bearer org-a",
                "X-Graph-Org": "personal",
            },
            {COOKIE: "valid-cookie"},
            403,
        ),
    ],
)
def test_non_global_callers_are_refused_before_deletion(
    delete_client, headers, cookies, status,
):
    client, calls = delete_client
    response = client.delete(
        "/api/orgs/never-delete-this",
        headers=headers,
        cookies=cookies,
        params={"force": "1"},
    )

    assert response.status_code == status
    assert calls == []


@pytest.mark.parametrize(
    ("headers", "cookies"),
    [
        ({"X-Graph-Org": "another-org"}, {COOKIE: "valid-cookie"}),
        (
            {
                "Authorization": "Bearer local",
                "X-Graph-Org": "another-org",
            },
            {},
        ),
    ],
)
def test_global_callers_preserve_organization_deletion(
    delete_client, headers, cookies,
):
    client, calls = delete_client
    response = client.delete(
        "/api/orgs/test-org",
        headers=headers,
        cookies=cookies,
        params={"force": "1"},
    )

    assert response.status_code == 200
    assert response.json() == {"deleted": "test-org", "force": True}
    assert calls == [("test-org", True)]
