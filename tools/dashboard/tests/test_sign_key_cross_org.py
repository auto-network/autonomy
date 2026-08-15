"""GET /api/sign-key serves the OPERATOR'S OWN signing key from personal.db.

auto-h4kzx (read-side leak fix, retained below at the primitive level): a
signing-key row that is ``canonical`` sits on an org's cross-org read-through
surface, so the old federated ``read_set`` served it to any subscribing org;
``read_owned_set`` excludes another org's row by construction.

auto-bsbaf (authority re-home): the commit-signing key is the operator's OWN
secret, so it lives in ``personal.db`` and get_sign_key now reads it pinned to
``personal`` (a legacy key still in the caller's own org DB is read owning-DB
only, transitionally, until moved). A personal secret is never in an org DB, so
it is structurally never on any org's cross-org read-through surface — the real
fix for the exposure, of which the owning-DB read was the symptom-level stop.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from tools.dashboard import api_auth
from tools.dashboard.approvals_routes import get_sign_key
from tools.graph import settings_ops
from tools.graph.db import GraphDB
from tools.graph.schemas.commit_signing_key import SIGN_KEY_SET_ID

ARMORED = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----"
)

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


def _global_request():
    return SimpleNamespace(
        state=SimpleNamespace(
            api_principal=api_auth.ApiPrincipal(
                api_auth.ApiPrincipalKind.OPERATOR_COOKIE,
                subject="browser-1",
            ),
        ),
        method="GET",
        url=SimpleNamespace(path="/api/sign-key"),
        query_params={},
    )


@pytest.fixture
def sign_key_client(monkeypatch):
    reads = []

    def read_owned_set(set_id, *, org):
        reads.append((set_id, org))
        return SimpleNamespace(
            members=[SimpleNamespace(payload={"armored_private_key": ARMORED})],
        )

    monkeypatch.setattr(settings_ops, "read_owned_set", read_owned_set)
    app = Starlette(
        routes=[Route("/api/sign-key", get_sign_key, methods=["GET"])],
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
        yield client, reads


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
def test_sign_key_refuses_non_global_callers_before_storage_read(
    sign_key_client, headers, cookies, status,
):
    client, reads = sign_key_client
    response = client.get("/api/sign-key", headers=headers, cookies=cookies)

    assert response.status_code == status
    assert "PRIVATE KEY" not in response.text
    assert reads == []


@pytest.mark.parametrize(
    ("headers", "cookies"),
    [
        ({"X-Graph-Org": "another-org"}, {COOKIE: "valid-cookie"}),
        ({"Authorization": "Bearer local"}, {}),
    ],
)
def test_sign_key_preserves_global_operator_read(
    sign_key_client, headers, cookies,
):
    client, reads = sign_key_client
    response = client.get("/api/sign-key", headers=headers, cookies=cookies)

    assert response.status_code == 200
    assert response.text == ARMORED
    assert reads == [(SIGN_KEY_SET_ID, "personal")]


@pytest.fixture
def orgs(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    GraphDB.create_org_db("anchore").close()
    GraphDB.create_org_db("beta").close()
    yield
    GraphDB.close_all_pooled()


def test_owned_set_excludes_another_orgs_canonical_sign_key(orgs):
    # anchore has a CANONICAL signing key — the federation-visible state the
    # live exposure was found in.
    settings_ops.upsert_by_key(
        SIGN_KEY_SET_ID, 1, "default",
        {"armored_private_key": ARMORED}, org="anchore", state="canonical",
    )
    # beta reading its OWN owned set sees nothing: anchore's canonical row lives
    # in anchore's DB and is never composed in. This is exactly what
    # get_sign_key now uses; the old federated read_set returned it cross-org.
    assert settings_ops.read_owned_set(SIGN_KEY_SET_ID, org="beta").members == []


def test_owned_set_still_serves_own_org_sign_key(orgs):
    settings_ops.upsert_by_key(
        SIGN_KEY_SET_ID, 1, "default",
        {"armored_private_key": ARMORED}, org="anchore", state="canonical",
    )
    own = settings_ops.read_owned_set(SIGN_KEY_SET_ID, org="anchore").members
    assert len(own) == 1
    assert own[0].payload["armored_private_key"] == ARMORED


def test_get_sign_key_serves_the_operators_personal_key(tmp_path, monkeypatch):
    """auto-bsbaf: the handler reads the operator's OWN key from personal.db,
    not from any org DB — a personal secret has no org to name."""
    import asyncio

    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    GraphDB.create_org_db("personal", type_="personal").close()
    settings_ops.upsert_by_key(
        SIGN_KEY_SET_ID, 1, "default",
        {"armored_private_key": ARMORED}, org="personal", state="raw",
    )
    from tools.dashboard.approvals_routes import get_sign_key

    resp = asyncio.run(get_sign_key(_global_request()))
    assert resp.status_code == 200
    assert "PRIVATE KEY" in resp.body.decode()
    GraphDB.close_all_pooled()


def test_get_sign_key_404_when_no_personal_key(tmp_path, monkeypatch):
    """No key anywhere -> 404, not an org-DB read."""
    import asyncio

    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    GraphDB.create_org_db("personal", type_="personal").close()
    from tools.dashboard.approvals_routes import get_sign_key

    resp = asyncio.run(get_sign_key(_global_request()))
    assert resp.status_code == 404
    GraphDB.close_all_pooled()
