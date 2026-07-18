"""C1 create-org-identity ceremony — server-side routes (network_routes).

The browser does the actual ceremony (see the L2.B sweep in
``test_behavioral_sweep.py::TestNetworkIdentityCeremony``); these tests
pin the server half against the REAL B1 registry (httpx.ASGITransport):

* the org-key write path stores ONLY the canonical passphrase-encrypted
  armor — anything plaintext-shaped is refused, and a raw-bytes scan of
  the settings DB proves the seed never touched disk (I1 grep-pin);
* registration forwards a root-direct envelope to the SERVER-configured
  registry (frozen destination, C3 discipline) and persists the binding
  row on 201;
* tested rejections: unsigned/foreign-signed envelopes, destination
  override attempts, registering a root whose armor was never stored.
"""

from __future__ import annotations

import time

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard import network_routes
from tools.graph import settings_ops
from tools.graph.schemas.network_identity import (
    NETWORK_BINDING_SET_ID,
    NETWORK_ORG_KEY_SET_ID,
)
from tools.network.idkit import KeyPair
from tools.network.idkit.armor import decrypt_root_key, encrypt_root_key
from tools.network.registry.app import create_app as create_registry_app
from tools.network.registry.signing import sign_request

ORG = "netorg"
ORG_UUID = "33333333-3333-4333-8333-333333333333"
REGISTRY_URL = "http://registry.test"
PASSPHRASE = "correct horse battery"


@pytest.fixture
def root():
    return KeyPair.generate()


@pytest.fixture
def registry_app():
    """A fresh registry with NO orgs bound — C1 is the genesis ceremony."""
    return create_registry_app(":memory:", base_url=REGISTRY_URL,
                               secure_cookies=False)


@pytest.fixture
def env(tmp_path, monkeypatch, registry_app):
    from tools.graph.db import GraphDB

    GraphDB.close_all_pooled()
    monkeypatch.setenv("GRAPH_DB", str(tmp_path / "graph.db"))
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    monkeypatch.setenv("AUTONOMY_NETWORK_REGISTRY_URL", REGISTRY_URL)

    def fake_registry_client(base_url):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=registry_app), base_url=base_url)

    monkeypatch.setattr(network_routes, "_registry_client", fake_registry_client)

    with TestClient(Starlette(routes=network_routes.ROUTES)) as client:
        yield client
    GraphDB.close_all_pooled()


def _armor(root: KeyPair) -> str:
    # Floor-of-range iterations keep the suite fast; shape is identical.
    return encrypt_root_key(root, PASSPHRASE, iterations=10_000)


def _store_key(client, root: KeyPair):
    r = client.post("/api/network/org-key",
                    json={"org": ORG, "armored_private_key": _armor(root)})
    assert r.status_code == 200, r.text
    return r.json()


def _registration_envelope(root: KeyPair, recovery="none", recovery_pub=None,
                           org_uuid=ORG_UUID):
    payload = {"org_uuid": org_uuid, "root_pub": root.public_hex,
               "recovery_policy": recovery}
    if recovery_pub is not None:
        payload["recovery_pub"] = recovery_pub
    return sign_request(root, "POST", "/v1/orgs", payload, ts=int(time.time()))


# ── registry destination (frozen server-side) ─────────────────────────


def test_registry_url_is_server_configured(env):
    r = env.get("/api/network/registry")
    assert r.status_code == 200
    assert r.json() == {"registry_url": REGISTRY_URL}


def test_registry_url_defaults_to_production(env, monkeypatch):
    monkeypatch.delenv("AUTONOMY_NETWORK_REGISTRY_URL")
    r = env.get("/api/network/registry")
    assert r.json() == {"registry_url": "https://registry.auto.network"}


# ── org-key storage (I1) ──────────────────────────────────────────────


def test_store_org_key_roundtrip(env, root):
    body = _store_key(env, root)
    assert body["root_pub"] == root.public_hex
    served = env.get(f"/api/network/org-key?org={ORG}").json()
    # The served blob opens with the canonical decrypt — what C2's
    # sign-on mirrors in WebCrypto.
    opened = decrypt_root_key(served["armored_private_key"], PASSPHRASE)
    assert opened.public_hex == root.public_hex
    assert opened.private_hex == root.private_hex


def test_stored_payload_contains_only_armor_fields(env, root):
    _store_key(env, root)
    members = settings_ops.read_set(NETWORK_ORG_KEY_SET_ID, org=ORG).members
    assert len(members) == 1
    payload = members[0].payload
    assert set(payload) == {"armored_private_key", "root_pub"}
    assert payload["armored_private_key"].startswith("-----BEGIN")


def test_i1_grep_pin_seed_never_touches_disk(env, root, tmp_path):
    """The raw private seed hex must not appear ANYWHERE in the settings
    DB bytes after a store — the definitive I1 pin."""
    _store_key(env, root)
    from tools.graph.db import GraphDB
    GraphDB.close_all_pooled()   # flush WAL into the main file
    blob = b"".join(
        p.read_bytes() for p in tmp_path.glob("graph.db*") if p.is_file()
    )
    assert blob, "settings DB was never written"
    assert root.private_hex.encode() not in blob
    assert bytes.fromhex(root.private_hex) not in blob
    assert root.public_hex.encode() in blob   # sanity: the row IS there


def test_store_plaintext_seed_refused(env, root):
    r = env.post("/api/network/org-key",
                 json={"org": ORG, "armored_private_key": root.private_hex})
    assert r.status_code == 400
    assert "I1" in r.json()["error"]
    assert env.get(f"/api/network/org-key?org={ORG}").status_code == 404


def test_store_non_armor_refused(env):
    r = env.post("/api/network/org-key",
                 json={"org": ORG, "armored_private_key": "not an armor at all"})
    assert r.status_code == 400
    assert env.get(f"/api/network/org-key?org={ORG}").status_code == 404


def test_store_root_pub_mismatch_refused(env, root):
    other = KeyPair.generate()
    r = env.post("/api/network/org-key",
                 json={"org": ORG, "armored_private_key": _armor(root),
                       "root_pub": other.public_hex})
    assert r.status_code == 400
    assert "does not match" in r.json()["error"]


def test_store_never_overwrites_existing_identity(env, root):
    _store_key(env, root)
    r = env.post("/api/network/org-key",
                 json={"org": ORG,
                       "armored_private_key": _armor(KeyPair.generate())})
    assert r.status_code == 409
    served = env.get(f"/api/network/org-key?org={ORG}").json()
    assert served["root_pub"] == root.public_hex   # original untouched


# ── registration (B1 forward + binding persist) ───────────────────────


def test_register_end_to_end(env, root, registry_app):
    _store_key(env, root)
    r = env.post("/api/network/register",
                 json={"org": ORG, "envelope": _registration_envelope(root)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True

    # The registry — the actual security boundary — verified and bound it.
    bound = registry_app.state.store.get_org(ORG_UUID)
    assert bound is not None
    assert bound.root_pub == root.public_hex

    # The local binding row matches what was registered.
    binding = body["binding"]
    assert binding["org_uuid"] == ORG_UUID
    assert binding["root_pub"] == root.public_hex
    assert binding["registry_url"] == REGISTRY_URL
    assert binding["recovery_policy"] == {"mode": "none"}
    members = settings_ops.read_set(NETWORK_BINDING_SET_ID, org=ORG).members
    assert [m.payload["org_uuid"] for m in members] == [ORG_UUID]
    served = env.get(f"/api/network/binding?org={ORG}").json()
    assert served["binding_expires_at"] == binding["binding_expires_at"]


def test_register_with_recovery_key_policy(env, root, registry_app):
    _store_key(env, root)
    recovery = KeyPair.generate()
    env_resp = env.post("/api/network/register", json={
        "org": ORG,
        "envelope": _registration_envelope(root, recovery="recovery-key",
                                           recovery_pub=recovery.public_hex),
    })
    assert env_resp.status_code == 200, env_resp.text
    binding = env_resp.json()["binding"]
    assert binding["recovery_policy"] == {
        "mode": "recovery-key", "recovery_pub": recovery.public_hex}
    assert registry_app.state.store.get_org(ORG_UUID).recovery_pub \
        == recovery.public_hex


def test_register_without_stored_key_refused(env, root, registry_app):
    r = env.post("/api/network/register",
                 json={"org": ORG, "envelope": _registration_envelope(root)})
    assert r.status_code == 409
    assert "store the encrypted armor first" in r.json()["error"]
    assert registry_app.state.store.get_org(ORG_UUID) is None


def test_register_root_mismatch_with_stored_key_refused(env, root, registry_app):
    _store_key(env, root)
    other = KeyPair.generate()   # self-consistent envelope, wrong identity
    r = env.post("/api/network/register",
                 json={"org": ORG, "envelope": _registration_envelope(other)})
    assert r.status_code == 409
    assert registry_app.state.store.get_org(ORG_UUID) is None


def test_register_foreign_signer_refused(env, root, registry_app):
    _store_key(env, root)
    attacker = KeyPair.generate()
    envelope = sign_request(
        attacker, "POST", "/v1/orgs",
        {"org_uuid": ORG_UUID, "root_pub": root.public_hex,
         "recovery_policy": "none"},
        ts=int(time.time()),
    )
    r = env.post("/api/network/register", json={"org": ORG, "envelope": envelope})
    assert r.status_code == 403
    assert "self-signed" in r.json()["error"]
    assert registry_app.state.store.get_org(ORG_UUID) is None


def test_register_tampered_signature_refused_by_registry(env, root, registry_app):
    _store_key(env, root)
    envelope = _registration_envelope(root)
    envelope["sig"] = "0" * 128
    r = env.post("/api/network/register", json={"org": ORG, "envelope": envelope})
    assert r.status_code == 502
    assert "registry refused" in r.json()["error"]
    assert registry_app.state.store.get_org(ORG_UUID) is None
    # Nothing half-registered locally either.
    assert env.get(f"/api/network/binding?org={ORG}").status_code == 404


def test_register_destination_override_refused(env, root, registry_app):
    _store_key(env, root)
    r = env.post("/api/network/register", json={
        "org": ORG, "envelope": _registration_envelope(root),
        "registry_url": "http://evil.example",
    })
    assert r.status_code == 400
    assert "fixed server-side" in r.json()["error"]
    assert registry_app.state.store.get_org(ORG_UUID) is None


def test_register_with_cert_refused(env, root):
    _store_key(env, root)
    envelope = _registration_envelope(root)
    envelope["cert"] = "{}"
    r = env.post("/api/network/register", json={"org": ORG, "envelope": envelope})
    assert r.status_code == 400
    assert "root-direct" in r.json()["error"]


def test_mock_mode_stores_nothing(env, monkeypatch, root):
    monkeypatch.setenv("DASHBOARD_MOCK", "1")
    assert env.post("/api/network/org-key",
                    json={"armored_private_key": _armor(root)}).status_code == 502
    assert env.post("/api/network/register",
                    json={"envelope": {}}).status_code == 502
