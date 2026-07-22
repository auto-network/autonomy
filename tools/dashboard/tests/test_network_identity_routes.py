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
    NETWORK_BINDING_REVISION,
    NETWORK_BINDING_SET_ID,
    NETWORK_ORG_KEY_SET_ID,
    NETWORK_SERVE_CERT_SET_ID,
)
from tools.network.idkit import KeyPair, Subject, issue_cert
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
    monkeypatch.setenv("GRAPH_ORG", ORG)  # this dashboard IS this org — own-org caller
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


def test_cross_org_read_and_write_refused(env, root):
    """A caller must not read or write ANOTHER org's encrypted key /
    registry binding through a ``?org=`` / body ``org`` override — the
    org-key blob is offline-attackable, so a cross-org read is a real
    leak (Codex validation FAIL). The env caller IS ``netorg``; every
    request naming a foreign org is refused 403, while the caller's own
    org still resolves.
    """
    _store_key(env, root)                     # netorg's own key exists
    FOREIGN = "victimorg"

    # reads of a foreign org's key/binding → 403 (not 200-with-their-data)
    assert env.get(f"/api/network/org-key?org={FOREIGN}").status_code == 403
    assert env.get(f"/api/network/binding?org={FOREIGN}").status_code == 403

    # writes into a foreign org → 403 (must not plant a key/binding either)
    assert env.post(
        "/api/network/org-key",
        json={"org": FOREIGN, "armored_private_key": _armor(root)},
    ).status_code == 403
    assert env.post(
        "/api/network/register",
        json={"org": FOREIGN, "envelope": _registration_envelope(root)},
    ).status_code == 403
    assert env.post(
        "/api/network/revocations",
        json={"org": FOREIGN, "record": "{}", "revoked_cert": "{}"},
    ).status_code == 403

    # own-org access is unaffected: default (no override) and explicit
    # own-org both resolve the caller's own key.
    assert env.get("/api/network/org-key").status_code == 200
    assert env.get(f"/api/network/org-key?org={ORG}").status_code == 200


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


# ── smuggled-plaintext regression (Codex I1 finding) ──────────────────
#
# A decryptable armor whose base64 body carried an EXTRA
# {private_hex: <seed>} field used to be stored verbatim and served
# back — plaintext in graph.db, invisible to a raw-hex grep because it
# rode base64-encoded. The store must refuse it, and the pin must grep
# the DECODED body of whatever got persisted.

import base64
import json


def _smuggled_armor(root: KeyPair) -> str:
    """Codex's repro: valid armor + private_hex smuggled into the body."""
    import textwrap
    armor = _armor(root)
    lines = armor.strip().splitlines()
    body = json.loads(base64.b64decode("".join(lines[1:-1])))
    body["private_hex"] = root.private_hex
    b64 = base64.b64encode(json.dumps(body).encode()).decode()
    return "\n".join([lines[0], *textwrap.wrap(b64, 64), lines[-1]])


def _decoded_stored_armor_bodies() -> list[dict]:
    """Every persisted org-key armor, base64-DECODED back to its dict."""
    bodies = []
    for m in settings_ops.read_set(NETWORK_ORG_KEY_SET_ID, org=ORG).members:
        lines = m.payload["armored_private_key"].strip().splitlines()
        bodies.append(json.loads(base64.b64decode("".join(lines[1:-1]))))
    return bodies


def test_smuggled_plaintext_armor_refused(env, root):
    r = env.post("/api/network/org-key",
                 json={"org": ORG, "armored_private_key": _smuggled_armor(root)})
    assert r.status_code == 400
    assert "I1" in r.json()["error"]
    # Nothing persisted at all — served 404, zero rows, so the seed is
    # absent from graph.db in ANY encoding.
    assert env.get(f"/api/network/org-key?org={ORG}").status_code == 404
    assert settings_ops.read_set(NETWORK_ORG_KEY_SET_ID, org=ORG).members == []


def test_stored_armor_body_decodes_to_canonical_fields_only(env, root):
    """Decoded-payload pin: the persisted armor body carries EXACTLY the
    canonical fields and no trace of the seed in decoded form."""
    _store_key(env, root)
    bodies = _decoded_stored_armor_bodies()
    assert len(bodies) == 1
    body = bodies[0]
    assert set(body) == {"v", "kdf", "cipher", "root_pub", "ct"}
    assert set(body["kdf"]) == {"name", "hash", "iterations", "salt"}
    assert set(body["cipher"]) == {"name", "iv"}
    decoded_text = json.dumps(body)
    assert root.private_hex not in decoded_text
    assert "private_hex" not in decoded_text
    # The ct field is real ciphertext, not a disguised seed: GCM output
    # of the 32-byte seed is exactly 48 bytes and differs from the seed.
    ct = base64.b64decode(body["ct"])
    assert len(ct) == 48
    assert bytes.fromhex(root.private_hex) not in ct


def test_store_reserializes_to_canonical_form(env, root):
    """Belt-and-suspenders: even a cosmetically re-wrapped (but clean)
    armor is stored in the ONE canonical byte form."""
    from tools.network.idkit.armor import canonicalize_armor
    armor = _armor(root)
    lines = armor.strip().splitlines()
    rewrapped = "\n".join([lines[0], "".join(lines[1:-1]), lines[-1]])  # one long line
    r = env.post("/api/network/org-key",
                 json={"org": ORG, "armored_private_key": rewrapped})
    assert r.status_code == 200, r.text
    served = env.get(f"/api/network/org-key?org={ORG}").json()
    assert served["armored_private_key"] == canonicalize_armor(armor)


def test_smuggle_via_generic_settings_api_refused(test_app, tmp_path, monkeypatch, root):
    """Codex's second bypass: POST /api/graph/setting straight at the
    org-key set_id. The schema-layer gate must refuse it there too, with
    nothing — raw or base64-wrapped — reaching graph.db."""
    from tools.graph.db import GraphDB
    from starlette.testclient import TestClient as _TC

    GraphDB.close_all_pooled()
    monkeypatch.setenv("GRAPH_DB", str(tmp_path / "graph.db"))
    monkeypatch.setenv("GRAPH_ORG", ORG)  # this dashboard IS this org — own-org caller
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)

    forged = _smuggled_armor(root)
    with _TC(test_app) as client:
        r = client.post("/api/graph/setting", json={
            "set_id": NETWORK_ORG_KEY_SET_ID,
            "schema_revision": 1,
            "key": "default",
            "payload": {"armored_private_key": forged},
        })
    assert r.status_code == 400, r.text
    assert "I1" in json.dumps(r.json())

    GraphDB.close_all_pooled()
    blob = b"".join(
        p.read_bytes() for p in tmp_path.glob("graph.db*") if p.is_file()
    )
    assert root.private_hex.encode() not in blob
    assert bytes.fromhex(root.private_hex) not in blob
    # The forged base64 body (seed inside, encoded) must be absent too.
    for line in forged.splitlines()[1:-1]:
        assert line.encode() not in blob


# ── serve-cert provisioning (§5.1) ────────────────────────────────────


def _store_binding(root: KeyPair, *, org_uuid=ORG_UUID):
    settings_ops.add_setting(
        NETWORK_BINDING_SET_ID, NETWORK_BINDING_REVISION, "auto.network",
        {"org_uuid": org_uuid, "root_pub": root.public_hex,
         "registry_url": REGISTRY_URL, "recovery_policy": {"mode": "none"},
         "binding_expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                             time.gmtime(int(time.time()) + 30 * 86400))},
        org=ORG,
    )


def _mint_serve(root: KeyPair, *, scope=("tunnel:serve",), org_uuid=ORG_UUID):
    delegate = KeyPair.generate()
    now = int(time.time())
    cert = issue_cert(
        root, delegate.public_hex, scope=scope, org=org_uuid,
        subject=Subject("operator", "op-serve"),
        not_before=now - 300, not_after=now + 30 * 86400,
    )
    return delegate, cert


def _serve_key_dir(monkeypatch, tmp_path):
    d = tmp_path / "serve-keys"
    monkeypatch.setattr(network_routes, "SERVE_KEY_DIR", d)
    return d


def test_provision_serve_cert_happy_path(env, root, tmp_path, monkeypatch):
    _serve_key_dir(monkeypatch, tmp_path)
    _store_binding(root)
    delegate, cert = _mint_serve(root)

    r = env.post("/api/network/serve-cert", json={
        "org": ORG, "cert": cert.to_json().decode("ascii"),
        "private_key": delegate.private_hex,
    })
    assert r.status_code == 200, r.text
    assert r.json()["child_pub"] == delegate.public_hex

    # Key file exists, mode 0600, holds exactly the delegate key — and the
    # settings row points at it, never carrying the key itself.
    import os
    key_path = tmp_path / "serve-keys" / f"serve-{ORG_UUID}.key"
    assert key_path.is_file()
    assert (os.stat(key_path).st_mode & 0o777) == 0o600
    assert key_path.read_text().strip() == delegate.private_hex

    row = settings_ops.read_owned_set(NETWORK_SERVE_CERT_SET_ID, org=ORG).members[0].payload
    assert row["key_path"] == str(key_path)
    assert row["root_pub"] == root.public_hex
    assert row["not_after"] == cert.not_after
    assert "private_key" not in row  # the secret is NOT in the settings store


def test_provision_rejects_key_not_matching_cert(env, root, tmp_path, monkeypatch):
    _serve_key_dir(monkeypatch, tmp_path)
    _store_binding(root)
    _delegate, cert = _mint_serve(root)
    r = env.post("/api/network/serve-cert", json={
        "org": ORG, "cert": cert.to_json().decode("ascii"),
        "private_key": KeyPair.generate().private_hex,  # wrong key
    })
    assert r.status_code == 400
    assert "does not match" in r.json()["error"]
    assert not (tmp_path / "serve-keys").exists()  # nothing written on rejection


def test_provision_rejects_scope_without_tunnel_serve(env, root, tmp_path, monkeypatch):
    _serve_key_dir(monkeypatch, tmp_path)
    _store_binding(root)
    delegate, cert = _mint_serve(root, scope=("link:publish",))
    r = env.post("/api/network/serve-cert", json={
        "org": ORG, "cert": cert.to_json().decode("ascii"),
        "private_key": delegate.private_hex,
    })
    assert r.status_code == 400
    assert "tunnel:serve" in r.json()["error"]


def test_provision_rejects_cert_not_chaining_to_org_root(env, root, tmp_path, monkeypatch):
    _serve_key_dir(monkeypatch, tmp_path)
    _store_binding(root)
    # A cert signed by a DIFFERENT root — must not be accepted for this org.
    delegate, cert = _mint_serve(KeyPair.generate())
    r = env.post("/api/network/serve-cert", json={
        "org": ORG, "cert": cert.to_json().decode("ascii"),
        "private_key": delegate.private_hex,
    })
    assert r.status_code == 400
    assert "chain" in r.json()["error"]


def test_provision_requires_a_binding_first(env, root, tmp_path, monkeypatch):
    _serve_key_dir(monkeypatch, tmp_path)
    delegate, cert = _mint_serve(root)  # no binding stored
    r = env.post("/api/network/serve-cert", json={
        "org": ORG, "cert": cert.to_json().decode("ascii"),
        "private_key": delegate.private_hex,
    })
    assert r.status_code == 409
    assert "not registered" in r.json()["error"]


def test_provision_cross_org_refused(env, root, tmp_path, monkeypatch):
    _serve_key_dir(monkeypatch, tmp_path)
    _store_binding(root)
    delegate, cert = _mint_serve(root)
    r = env.post("/api/network/serve-cert", json={
        "org": "someone-else", "cert": cert.to_json().decode("ascii"),
        "private_key": delegate.private_hex,
    })
    assert r.status_code == 403
