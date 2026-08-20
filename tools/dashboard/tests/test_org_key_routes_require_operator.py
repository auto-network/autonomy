"""The org-key routes are operator-only (auto-6ff9b).

``_scoped_org`` already refuses a caller that NAMES another org. That is a
different question from whether the caller may read AT ALL, and the gap was
real: an unauthenticated request for its OWN org passed the cross-org check
and received the key. Measured from an agent container with no token.

The material is passphrase-encrypted or sealed rather than plaintext, so this
is not immediate compromise — it is unauthenticated disclosure of
offline-attackable material, the severity argument that closed
``auto-1wwpf.2``.

Every assertion about a refusal checks that THE KEY FIELDS ARE ABSENT, not
merely that a status code changed. A guard that returns 403 while still
serialising the armor into the body is the failure this class of test exists
to catch.

The gate state is pinned explicitly in every test. ``require_global_api_authority``
stands down for compatibility traffic while the human gate is not enforced,
and ``human_auth_enrolled`` returns True when it cannot read its store — so a
test that left it implicit would pass through a path nobody chose, and would
silently swap paths the day that fallback changed.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from tools.dashboard import api_auth, network_routes, unlock_routes
from tools.graph import org_ops, settings_ops
from tools.graph.schemas.network_identity import (
    NETWORK_ORG_KEY_SET_ID,
    ORG_ROOT_ARMOR_PURPOSE,
)
from tools.network.idkit import KeyPair
from tools.network.idkit.sealing import derive_encapsulation_keypair, seal

COOKIE = "test_dashboard_session"
PERSONAL_SEED = bytes(range(32))

#: Anything here appearing in a refused response body is the bug.
KEY_FIELDS = ("armored_private_key", "sealed_root_key", "owner_kem_pub")


def _authenticate_bearer(request):
    identities = {
        "Bearer org-a": ("agent-a", "acme"),   # ORG_SESSION, its OWN org
        "Bearer local": ("host-local", None),  # LOCAL_SESSION
    }
    identity = identities.get(request.headers.get("authorization", ""))
    if identity is not None:
        return identity, None
    return None, JSONResponse({"error": "invalid bearer"}, status_code=401)


def _verify_cookie(value):
    return {"sid": "browser-1"} if value == "valid-cookie" else None


def _sealed_payload(org_root: KeyPair | None = None) -> dict:
    org_root = org_root or KeyPair.generate()
    _, recipient_pub = derive_encapsulation_keypair(
        PERSONAL_SEED, ORG_ROOT_ARMOR_PURPOSE
    )
    record = seal(
        bytes.fromhex(org_root.private_hex), recipient_pub, ORG_ROOT_ARMOR_PURPOSE
    )
    return {
        "root_pub": org_root.public_hex,
        "sealed_root_key": record.hex(),
        "owner_kem_pub": recipient_pub,
        "seal_purpose": ORG_ROOT_ARMOR_PURPOSE,
    }


@pytest.fixture
def enforced(monkeypatch):
    """The ordinary deployment: a dashboard with the human gate enrolled."""
    monkeypatch.setattr(unlock_routes, "gate_enforced", lambda: True)


@pytest.fixture
def client(tmp_path, monkeypatch):
    from tools.graph.db import GraphDB

    GraphDB.close_all_pooled()
    orgs = tmp_path / "orgs"
    orgs.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.setenv("GRAPH_ORG", "acme")
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    GraphDB.close_all_pooled()
    GraphDB.create_org_db(
        "personal", type_="personal", path=orgs / "personal.db"
    ).close()
    app = Starlette(
        routes=[
            Route("/api/network/org-key", network_routes.get_org_key,
                  methods=["GET"]),
            Route("/api/network/org-key/sealed",
                  network_routes.post_sealed_org_key, methods=["POST"]),
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
    with TestClient(app) as c:
        yield c
    GraphDB.close_all_pooled()


@pytest.fixture
def founded_key(client):
    """An org whose sealed root is already stored, so a read has something
    to leak. A guard tested against a 404 proves nothing."""
    org_ops.create_org_shell("acme")
    org_ops.store_sealed_org_key("acme", _sealed_payload())
    return client


# ── reading the key ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("label", "headers", "cookies", "status"),
    [
        ("no credential at all", {}, {}, 401),
        ("an agent's own-org bearer", {"Authorization": "Bearer org-a"}, {}, 403),
        (
            "an agent bearer alongside a browser cookie",
            {"Authorization": "Bearer org-a"},
            {COOKIE: "valid-cookie"},
            403,
        ),
        (
            "an agent forging the org header",
            {"Authorization": "Bearer org-a", "X-Graph-Org": "acme"},
            {},
            403,
        ),
    ],
)
def test_a_caller_without_operator_authority_gets_no_key_material(
    founded_key, enforced, label, headers, cookies, status,
):
    """THE ONE THAT MATTERS. The container measurement that opened the bead is
    the first row: no credential, own org, HTTP 200 with the key in it."""
    response = founded_key.get(
        "/api/network/org-key", params={"org": "acme"},
        headers=headers, cookies=cookies,
    )

    assert response.status_code == status, label
    body = response.text
    for field in KEY_FIELDS:
        assert field not in body, f"{label}: {field} served to a refused caller"


@pytest.mark.parametrize(
    ("headers", "cookies"),
    [
        ({}, {COOKIE: "valid-cookie"}),          # OPERATOR_COOKIE
        ({"Authorization": "Bearer local"}, {}),  # LOCAL_SESSION
    ],
)
def test_operator_authority_still_reads_the_key(
    founded_key, enforced, headers, cookies,
):
    """The guard must not cost the callers it was never aimed at. Both browser
    screens and unlock's serving-credential maintenance arrive here holding
    the session cookie."""
    response = founded_key.get(
        "/api/network/org-key", params={"org": "acme"},
        headers=headers, cookies=cookies,
    )

    assert response.status_code == 200, response.text
    assert response.json()["sealed_root_key"]


def test_refusal_precedes_the_cross_org_check(founded_key, enforced):
    """An unauthenticated caller naming ANOTHER org is refused for want of
    authority (401), not for the cross-org attempt (403). Ordering matters:
    the authority answer must not depend on what the caller claimed."""
    response = founded_key.get(
        "/api/network/org-key", params={"org": "somewhere-else"},
    )

    assert response.status_code == 401


# ── the founding write ───────────────────────────────────────


def test_an_org_session_cannot_seal_a_root(client, enforced):
    """Founding an organization is an operator act. An org-bound agent is
    positively identified and deliberately too narrow for it, even naming its
    own org."""
    org_ops.create_org_shell("acme")

    response = client.post(
        "/api/network/org-key/sealed",
        json=_sealed_payload(),
        headers={"Authorization": "Bearer org-a"},
    )

    assert response.status_code == 403
    assert settings_ops.read_owned_set(
        NETWORK_ORG_KEY_SET_ID, org="acme").members == []


def test_an_unauthenticated_caller_cannot_seal_a_root(client, enforced):
    org_ops.create_org_shell("acme")

    response = client.post("/api/network/org-key/sealed", json=_sealed_payload())

    assert response.status_code == 401
    assert settings_ops.read_owned_set(
        NETWORK_ORG_KEY_SET_ID, org="acme").members == []


def test_a_malformed_body_is_never_read_from_a_refused_caller(client, enforced):
    """Refusal precedes the body read, so a refused caller's payload is never
    parsed. A 400 here would mean the route processed foreign input first."""
    org_ops.create_org_shell("acme")

    response = client.post(
        "/api/network/org-key/sealed",
        content=b"not json at all",
        headers={"Authorization": "Bearer org-a",
                 "Content-Type": "application/json"},
    )

    assert response.status_code == 403


def test_the_operator_still_seals_the_founding_root(client, enforced):
    org_ops.create_org_shell("acme")
    payload = _sealed_payload()

    response = client.post(
        "/api/network/org-key/sealed", json={**payload, "org": "acme"},
        cookies={COOKIE: "valid-cookie"},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "root_pub": payload["root_pub"]}


# ── the deliberate stand-down ────────────────────────────────


def test_an_unenrolled_dashboard_still_serves_the_operator(
    founded_key, monkeypatch,
):
    """Not a weakening, and it is a state real deployments sit in. With the
    human gate unenforced the gate admits the browser WITHOUT a cookie, so the
    operator's own requests arrive as compatibility traffic; refusing them
    here would contradict the gate that just let them in, and would protect
    nothing, because the same browser reaches the same stores through every
    ungated route."""
    monkeypatch.setattr(unlock_routes, "gate_enforced", lambda: False)

    response = founded_key.get("/api/network/org-key", params={"org": "acme"})

    assert response.status_code == 200


def test_the_stand_down_does_not_extend_to_an_agent(founded_key, monkeypatch):
    """An org-bound agent is positively identified and deliberately narrow.
    The state of the HUMAN gate says nothing about it, so its refusal stands
    even on an unenrolled dashboard."""
    monkeypatch.setattr(unlock_routes, "gate_enforced", lambda: False)

    response = founded_key.get(
        "/api/network/org-key", headers={"Authorization": "Bearer org-a"},
    )

    assert response.status_code == 403
    for field in KEY_FIELDS:
        assert field not in response.text
