"""C2 sign-on ceremony — server-side routes (network_routes).

The browser does the actual ceremony (see the L2.B sweep in
``test_behavioral_sweep.py::TestNetworkSignOn``); these tests pin the
server half: the org-key route serves ONLY the encrypted armor (I1), the
binding route serves the registry coordinates the cert is pinned to, and
the revocation route forwards a root-signed record to the REAL B1
registry (httpx.ASGITransport) with end-to-end effect — a chain
containing the revoked session key stops verifying (spec §4.5, I7).
"""

from __future__ import annotations
from tools.network.idkit.root_factor_policy import mint_password_armor

import time

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard import network_routes
from tools.graph import settings_ops
from tools.graph.schemas.network_identity import (
    NETWORK_BINDING_SET_ID,
    NETWORK_BINDING_REVISION,
    NETWORK_ORG_KEY_SET_ID,
    NETWORK_ORG_KEY_REVISION,
)
from tools.network.idkit import KeyPair, Subject, issue_cert, issue_revocation
from tools.network.registry.app import create_app as create_registry_app
from tools.network.registry.signing import sign_request

ORG = "netorg"
ORG_UUID = "11111111-1111-4111-8111-111111111111"
REGISTRY_URL = "http://registry.test"
PASSPHRASE = "correct horse"

SESSION_SCOPE = ("delegate:agent", "link:publish", "link:revoke",
                 "tunnel:serve", "viewer:identify")


@pytest.fixture
def root():
    return KeyPair.generate()


@pytest.fixture
def registry_app(root):
    app = create_registry_app(":memory:", base_url=REGISTRY_URL,
                              secure_cookies=False)
    rc = TestClient(app)
    envelope = sign_request(
        root, "POST", "/v1/orgs",
        {"org_uuid": ORG_UUID, "root_pub": root.public_hex,
         "recovery_policy": "none"},
        ts=int(time.time()),
    )
    r = rc.post("/v1/orgs", json=envelope)
    assert r.status_code == 201, r.json()
    return app


@pytest.fixture
def env(tmp_path, monkeypatch, registry_app):
    from tools.graph.db import GraphDB

    GraphDB.close_all_pooled()
    # Orgs-tree hermeticity, no GRAPH_DB pin: the code under test
    # resolves explicit orgs, which a pin silently swallows and the
    # fail-loud resolver refuses. delenv guards ambient leaks.
    orgs_dir = tmp_path / "orgs"
    orgs_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    GraphDB.close_all_pooled()
    GraphDB.create_org_db(ORG, type_="shared", path=orgs_dir / f"{ORG}.db").close()
    monkeypatch.setenv("GRAPH_ORG", ORG)  # this dashboard IS this org — own-org caller
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)

    def fake_registry_client(base_url):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=registry_app), base_url=base_url)

    monkeypatch.setattr(network_routes, "_registry_client", fake_registry_client)

    with TestClient(Starlette(routes=network_routes.ROUTES)) as client:
        yield client
    GraphDB.close_all_pooled()


def _store_binding(root):
    settings_ops.add_setting(
        NETWORK_BINDING_SET_ID, NETWORK_BINDING_REVISION, "registry.test",
        {
            "org_uuid": ORG_UUID,
            "root_pub": root.public_hex,
            "registry_url": REGISTRY_URL,
            "recovery_policy": {"mode": "none"},
            "binding_expires_at": "2030-01-01T00:00:00Z",
        },
        org=ORG,
    )


#: The owner an org root is sealed to. An org root has no passphrase of its
#: own — one personal root opens every org it owns.
OWNER_SEED = bytes(range(32))


def _store_org_key(root, _unused=None):
    from tools.graph.schemas.network_identity import (
        NETWORK_ORG_KEY_REVISION_2, ORG_ROOT_ARMOR_PURPOSE,
    )
    from tools.network.idkit.sealing import derive_encapsulation_keypair, seal

    _, recipient_pub = derive_encapsulation_keypair(
        OWNER_SEED, ORG_ROOT_ARMOR_PURPOSE)
    sealed = seal(bytes.fromhex(root.private_hex), recipient_pub,
                  ORG_ROOT_ARMOR_PURPOSE)
    settings_ops.add_setting(
        NETWORK_ORG_KEY_SET_ID, NETWORK_ORG_KEY_REVISION_2, "default",
        {"root_pub": root.public_hex, "sealed_root_key": sealed.hex(),
         "owner_kem_pub": recipient_pub, "seal_purpose": ORG_ROOT_ARMOR_PURPOSE},
        org=ORG,
    )


def _session_cert(root, session_key, *, ttl=86400):
    now = int(time.time())
    return issue_cert(
        root, session_key.public_hex, scope=SESSION_SCOPE, org=ORG_UUID,
        subject=Subject("operator", "browser-deadbeef"),
        not_before=now - 60, not_after=now + ttl,
    )


# ── org-key + binding routes ─────────────────────────────────


def test_org_key_404_when_unset(env):
    r = env.get(f"/api/network/org-key?org={ORG}")
    assert r.status_code == 404
    error = r.json()["error"]
    assert "no signing key" in error
    # No internal codename or retired-ceremony language reaches the caller.
    assert "C1" not in error and "ceremony" not in error


def test_org_key_serves_sealed_revision_2(env, root):
    """A founding-ceremony org stores its root as an Option-B seal, not
    password armor — the route must serve that shape too (auto-05tom:
    refusing it left every ceremony-created org unable to sign on)."""
    from tools.graph.schemas.network_identity import (
        NETWORK_ORG_KEY_REVISION_2, ORG_ROOT_ARMOR_PURPOSE)
    from tools.network.idkit.sealing import derive_encapsulation_keypair, seal

    personal = KeyPair.generate()
    _, recipient_pub = derive_encapsulation_keypair(
        bytes.fromhex(personal.private_hex), ORG_ROOT_ARMOR_PURPOSE)
    sealed = seal(bytes.fromhex(root.private_hex), recipient_pub,
                  ORG_ROOT_ARMOR_PURPOSE).hex()
    settings_ops.add_setting(
        NETWORK_ORG_KEY_SET_ID, NETWORK_ORG_KEY_REVISION_2, "default",
        {"root_pub": root.public_hex, "sealed_root_key": sealed,
         "owner_kem_pub": recipient_pub,
         "seal_purpose": ORG_ROOT_ARMOR_PURPOSE},
        org=ORG,
    )
    r = env.get(f"/api/network/org-key?org={ORG}")
    assert r.status_code == 200
    body = r.json()
    assert body["sealed_root_key"] == sealed
    assert body["seal_purpose"] == ORG_ROOT_ARMOR_PURPOSE
    assert body["root_pub"] == root.public_hex
    assert "armored_private_key" not in body
    # I1: nothing in the response is usable without the personal password.
    assert root.private_hex not in r.text
    assert personal.private_hex not in r.text


def test_binding_404_when_unset(env):
    r = env.get(f"/api/network/binding?org={ORG}")
    assert r.status_code == 404


def test_binding_serves_registry_coordinates(env, root):
    _store_binding(root)
    r = env.get(f"/api/network/binding?org={ORG}")
    assert r.status_code == 200
    body = r.json()
    assert body["org_uuid"] == ORG_UUID
    assert body["root_pub"] == root.public_hex
    assert body["registry_url"] == REGISTRY_URL
    assert body["registry"] == "registry.test"


# ── revocation forwarding (§4.5) ─────────────────────────────


def test_revocation_end_to_end(env, root, registry_app):
    """Root-signed record → forwarded → the revoked session key's chain
    stops verifying at the registry."""
    _store_binding(root)
    session_key = KeyPair.generate()
    cert = _session_cert(root, session_key)
    now = int(time.time())

    # Before revocation the session key's chain passes the registry's I4
    # gate (renew is the weakest chain-verified mutation — link publish
    # rides the org tunnel and no longer exercises this gate).
    rc = TestClient(registry_app)
    renew = sign_request(
        session_key, "POST", f"/v1/orgs/{ORG_UUID}/renew", {},
        ts=now, cert=cert,
    )
    r = rc.post(f"/v1/orgs/{ORG_UUID}/renew", json=renew)
    assert r.status_code == 200, r.json()

    record = issue_revocation(
        root, session_key.public_hex, org=ORG_UUID,
        revoked_at=now, expires_at=cert.not_after,
        reason="operator revoked from key list",
        revoked_cert=cert,
    )
    resp = env.post("/api/network/revocations", json={
        "org": ORG,
        "record": record.to_json().decode(),
        "revoked_cert": cert.to_json().decode(),
    })
    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["ok"] is True
    assert body["revoked_key_id"] == session_key.public_hex

    # The revoked key's chain is now dead at the registry.
    renew2 = sign_request(
        session_key, "POST", f"/v1/orgs/{ORG_UUID}/renew", {},
        ts=int(time.time()), cert=cert,
    )
    r2 = rc.post(f"/v1/orgs/{ORG_UUID}/renew", json=renew2)
    assert r2.status_code == 403
    assert "Revoked" in r2.json()["detail"]


def test_revocation_rejects_malformed_body(env, root):
    _store_binding(root)
    r = env.post("/api/network/revocations", json={"org": ORG})
    assert r.status_code == 400
    r = env.post("/api/network/revocations", json={
        "org": ORG, "record": 42, "revoked_cert": "x"})
    assert r.status_code == 400


def test_revocation_404_without_binding(env):
    r = env.post("/api/network/revocations", json={
        "org": ORG, "record": "{}", "revoked_cert": "{}"})
    assert r.status_code == 404


def test_revocation_foreign_root_refused(env, root):
    """A record minted by a key that is NOT the bound root is the
    registry's 403, surfaced as our 502 with its detail."""
    _store_binding(root)
    mallory = KeyPair.generate()
    session_key = KeyPair.generate()
    cert = _session_cert(root, session_key)
    now = int(time.time())
    forged = issue_revocation(
        mallory, session_key.public_hex, org=ORG_UUID,
        revoked_at=now, expires_at=cert.not_after,
    )
    r = env.post("/api/network/revocations", json={
        "org": ORG,
        "record": forged.to_json().decode(),
        "revoked_cert": cert.to_json().decode(),
    })
    assert r.status_code == 502
    assert "registry refused the revocation" in r.json()["error"]


# ── registration persists the registry's authoritative 201, not the echo ──


class _CannedRegistry:
    """A registry client whose 201 asserts a specific authoritative binding,
    regardless of what the caller signed — to prove post_register persists the
    RESPONSE, not the request echo."""

    def __init__(self, org_uuid, root_pub, expires_at=1900000000):
        # The full authoritative-binding shape _validated_binding_response
        # requires (Central substrate): a short body is refused as malformed.
        self._body = {"org_uuid": org_uuid, "root_pub": root_pub,
                      "expires_at": expires_at,
                      "binding_generation": "ab" * 32,
                      "recovery_policy": {"mode": "none"},
                      "outcome": "claimed"}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def post(self, _path, json=None):
        class _Resp:
            status_code = 201
            _b = self._body

            def json(self_inner):
                return self_inner._b
        return _Resp()


def _registration_envelope(root, org_uuid):
    return sign_request(
        root, "POST", "/v1/orgs",
        {"org_uuid": org_uuid, "root_pub": root.public_hex,
         "recovery_policy": "none"},
        ts=int(time.time()),
    )


def test_register_persists_exact_confirmed_coordinates(env, root, monkeypatch):
    """Happy path: the registry's 201 confirms exactly the signed claim, and
    THAT authoritative response is what gets persisted."""
    _store_org_key(root, mint_password_armor(root, PASSPHRASE, iterations=10_000))
    monkeypatch.setattr(network_routes, "_registry_client",
                        lambda _base: _CannedRegistry(ORG_UUID, root.public_hex))
    r = env.post("/api/network/register", json={
        "org": ORG, "envelope": _registration_envelope(root, ORG_UUID)})
    assert r.status_code == 200, r.json()
    binding = r.json()["binding"]
    assert binding["org_uuid"] == ORG_UUID
    assert binding["root_pub"] == root.public_hex


def test_register_refuses_registry_uuid_differing_from_claim(env, root, monkeypatch):
    """A 201 binding a DIFFERENT uuid than the signed claim is refused — the
    dashboard never persists authority coordinates it did not prove."""
    _store_org_key(root, mint_password_armor(root, PASSPHRASE, iterations=10_000))
    caller_uuid = "11111111-1111-4111-8111-111111111111"
    registry_uuid = "99999999-9999-4999-8999-999999999999"
    monkeypatch.setattr(network_routes, "_registry_client",
                        lambda _base: _CannedRegistry(registry_uuid, root.public_hex))
    r = env.post("/api/network/register", json={
        "org": ORG, "envelope": _registration_envelope(root, caller_uuid)})
    assert r.status_code == 502
    assert "different UUID/root coordinates" in r.json()["error"]


def test_register_refuses_registry_binding_a_foreign_root(env, root, monkeypatch):
    """If the registry's 201 binds a DIFFERENT root than the one signed, refuse
    — never persist a binding for a key we did not prove control of."""
    _store_org_key(root, mint_password_armor(root, PASSPHRASE, iterations=10_000))
    foreign = KeyPair.generate()
    monkeypatch.setattr(network_routes, "_registry_client",
                        lambda _base: _CannedRegistry(ORG_UUID, foreign.public_hex))
    r = env.post("/api/network/register", json={
        "org": ORG, "envelope": _registration_envelope(root, ORG_UUID)})
    assert r.status_code == 502
    assert "different UUID/root coordinates" in r.json()["error"]
