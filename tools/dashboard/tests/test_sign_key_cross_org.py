"""GET /api/sign-key serves the operator's own signing key, keyed by organization.

The key is the operator's own secret: decrypted only in their browser, with
their passphrase, to sign their commits. It lives in their own database, and
they hold one per organization they sign for.

That home is the structural fix for the cross-org exposure. A row in an org's
database can be published onto that org's read-through surface, where any
subscribing org composes it in; a row in the operator's own database cannot be
reached that way at all, by anyone. The schema declares the home, so putting
one in an org's database is refused rather than merely discouraged.

Keying by organization slug is what lets a reader ASK for the right row. A
single row under a fixed label cannot hold a second organization's key, and
leaves a reader with nothing to name -- so it has to find the row it wants by
looking inside the stored values until one appears to be a private key, and
takes whichever comes first.
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
from tools.graph.schemas.registry import SchemaValidationError
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

    def read_set_key(set_id, key, *, org, peers=None):
        reads.append((set_id, key, org, peers))
        return {"payload": {"armored_private_key": ARMORED}}

    monkeypatch.setattr(settings_ops, "_resolve_settings_caller", lambda _: "anchore")
    monkeypatch.setattr(settings_ops, "read_set_key", read_set_key)
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
    # The caller's organization names the row; the operator's own database
    # holds it; no peer database is consulted for a signing key.
    assert reads == [(SIGN_KEY_SET_ID, "anchore", "personal", [])]


@pytest.fixture
def orgs(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    GraphDB.create_org_db("anchore").close()
    GraphDB.create_org_db("beta").close()
    yield
    GraphDB.close_all_pooled()


def test_a_signing_key_cannot_be_put_in_an_organizations_database(orgs):
    """The exposure, closed at its source.

    A row in an org's database can be promoted onto that org's read-through
    surface, and every subscribing org then composes it in. Refusing the write
    means there is no such row to promote -- which is a different and stronger
    thing than reading it carefully.
    """
    with pytest.raises(SchemaValidationError, match="operator's own database"):
        settings_ops.upsert_by_key(
            SIGN_KEY_SET_ID, 1, "anchore",
            {"armored_private_key": ARMORED}, org="anchore", state="canonical",
        )


def test_one_key_per_organization_in_the_operators_own_database(orgs):
    """Two organizations, two keys, each found by naming its organization."""
    settings_ops.upsert_by_key(
        SIGN_KEY_SET_ID, 1, "anchore",
        {"armored_private_key": ARMORED + "-anchore"}, org="personal", state="raw",
    )
    settings_ops.upsert_by_key(
        SIGN_KEY_SET_ID, 1, "beta",
        {"armored_private_key": ARMORED + "-beta"}, org="personal", state="raw",
    )

    for slug in ("anchore", "beta"):
        row = settings_ops.read_set_key(
            SIGN_KEY_SET_ID, slug, org="personal", peers=[],
        )
        assert row is not None, f"no signing key found for {slug}"
        assert row["payload"]["armored_private_key"].endswith(slug)


def test_get_sign_key_serves_the_operators_personal_key(tmp_path, monkeypatch):
    """auto-bsbaf: the handler reads the operator's OWN key from personal.db,
    not from any org DB — a personal secret has no org to name."""
    import asyncio

    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    GraphDB.create_org_db("personal", type_="personal").close()
    settings_ops.upsert_by_key(
        SIGN_KEY_SET_ID, 1, "anchore",
        {"armored_private_key": ARMORED}, org="personal", state="raw",
    )
    monkeypatch.setattr(settings_ops, "_resolve_settings_caller", lambda _: "anchore")
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
    monkeypatch.setattr(settings_ops, "_resolve_settings_caller", lambda _: "anchore")
    from tools.dashboard.approvals_routes import get_sign_key

    resp = asyncio.run(get_sign_key(_global_request()))
    assert resp.status_code == 404
    GraphDB.close_all_pooled()
