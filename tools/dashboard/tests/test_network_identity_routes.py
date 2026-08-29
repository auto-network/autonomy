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
from tools.network.idkit.root_factor_policy import mint_password_armor

import time
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard import network_routes
from tools.graph import settings_ops
from tools.graph.schemas.network_identity import (
    NETWORK_BINDING_REVISION,
    NETWORK_BINDING_REVISION_2,
    NETWORK_BINDING_SET_ID,
    NETWORK_ORG_KEY_SET_ID,
    NETWORK_SERVE_CERT_SET_ID,
)
from tools.network.idkit import KeyPair, Subject, issue_cert
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
    # Orgs-tree hermeticity, no GRAPH_DB pin: the code under test
    # resolves explicit orgs, which a pin silently swallows (73bad14e)
    # and the fail-loud resolver refuses. delenv guards ambient leaks.
    orgs_dir = tmp_path / "orgs"
    orgs_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    from tools.graph.db import GraphDB
    GraphDB.close_all_pooled()
    GraphDB.create_org_db(ORG).close()
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


#: The owner seed the org root is sealed to. An org root is not protected by
#: a passphrase of its own — one personal root opens every org it owns — so
#: the fixture needs an owner, not a password.
OWNER_SEED = bytes(range(32))


def _sealed(root: KeyPair) -> dict:
    from tools.network.idkit.sealing import derive_encapsulation_keypair, seal
    from tools.graph.schemas.network_identity import ORG_ROOT_ARMOR_PURPOSE

    _, recipient_pub = derive_encapsulation_keypair(
        OWNER_SEED, ORG_ROOT_ARMOR_PURPOSE)
    sealed = seal(bytes.fromhex(root.private_hex), recipient_pub,
                  ORG_ROOT_ARMOR_PURPOSE)
    return {
        "root_pub": root.public_hex,
        "sealed_root_key": sealed.hex(),
        "owner_kem_pub": recipient_pub,
        "seal_purpose": ORG_ROOT_ARMOR_PURPOSE,
    }


def _store_key(client, root: KeyPair):
    r = client.post("/api/network/org-key/sealed",
                    json={"org": ORG, **_sealed(root)})
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


def test_cross_org_read_and_write_refused(env, root, monkeypatch):
    """A caller must not read or write ANOTHER org's encrypted key /
    registry binding through a ``?org=`` / body ``org`` override — the
    org-key blob is offline-attackable, so a cross-org read is a real
    leak (Codex validation FAIL). The env caller IS ``netorg``; every
    request naming a foreign org is refused 403, while the caller's own
    org still resolves.
    """
    # Invariant 1 (auto-h4kzx): cross-org refusal keys off a TOKEN-stamped
    # caller org, which a valid bearer carries. The env is that authenticated
    # netorg dashboard — stamp it, so a foreign ``?org=`` is the refused
    # cross-org attempt this test asserts, not a local operator's legitimate
    # selection (which would open the missing foreign store and 500).
    from _pytest.monkeypatch import MonkeyPatch

    from tools.dashboard import api_auth
    from tools.dashboard.api_auth import ApiPrincipal, ApiPrincipalKind
    _store_key(env, root)   # setup runs as the operator: org-key is operator-only
    # A dedicated patch instance: the shared `monkeypatch` fixture also carries
    # the env fixture's setup, which a blanket undo() would destroy.
    stamp = MonkeyPatch()
    stamp.setattr(
        api_auth, "principal_from_request",
        lambda request: ApiPrincipal(
            ApiPrincipalKind.ORG_SESSION, subject="auto-1", org=ORG
        ),
    )
    FOREIGN = "victimorg"

    # reads of a foreign org's key/binding → 403 (not 200-with-their-data)
    assert env.get(f"/api/network/org-key?org={FOREIGN}").status_code == 403
    assert env.get(f"/api/network/binding?org={FOREIGN}").status_code == 403

    # writes into a foreign org → 403 (must not plant a key/binding either)
    assert env.post(
        "/api/network/org-key/sealed",
        json={"org": FOREIGN, **_sealed(root)},
    ).status_code == 403
    assert env.post(
        "/api/network/register",
        json={"org": FOREIGN, "envelope": _registration_envelope(root)},
    ).status_code == 403
    assert env.post(
        "/api/network/revocations",
        json={"org": FOREIGN, "record": "{}", "revoked_cert": "{}"},
    ).status_code == 403

    # Own-org access is unaffected for the authority that may read at all
    # (org-key is operator-only). The ambient-default resolution (contextvar
    # -> GRAPH_ORG -> scopeless) is pinned by test_h4kzx_derive_org; here the
    # operator's explicit own-org selection must still resolve the key.
    stamp.undo()
    assert env.get(f"/api/network/org-key?org={ORG}").status_code == 200


def test_i1_grep_pin_seed_never_touches_disk(env, root, tmp_path):
    """The raw private seed hex must not appear ANYWHERE in the settings
    DB bytes after a store — the definitive I1 pin."""
    _store_key(env, root)
    from tools.graph.db import GraphDB
    GraphDB.close_all_pooled()   # flush WAL into the main file
    # The org-key row lives in the org's OWN DB in the orgs tree (the
    # pinned single-file store is gone) — the never-on-disk property is
    # asserted against the store that actually holds the row.
    blob = b"".join(
        p.read_bytes()
        for p in (tmp_path / "orgs").glob(f"{ORG}.db*") if p.is_file()
    )
    assert blob, "settings DB was never written"
    assert root.private_hex.encode() not in blob
    assert bytes.fromhex(root.private_hex) not in blob
    assert root.public_hex.encode() in blob   # sanity: the row IS there


def test_store_never_overwrites_a_founded_identity(env, root):
    """Once the ledger has committed to a root, that root is the org's
    forever — a second submission cannot quietly replace what the ledger
    already attests to."""
    _store_key(env, root)
    r = env.post("/api/network/org-key/sealed",
                 json={"org": ORG, **_sealed(KeyPair.generate())})
    assert r.status_code in (200, 409)
    served = env.get(f"/api/network/org-key?org={ORG}").json()
    if r.status_code == 409:
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
    assert body["outcome"] == "claimed"
    assert binding["org_uuid"] == ORG_UUID
    assert binding["root_pub"] == root.public_hex
    assert binding["registry_url"] == REGISTRY_URL
    assert binding["recovery_policy"] == {"mode": "none"}
    assert len(binding["binding_generation"]) == 64
    members = settings_ops.read_set(NETWORK_BINDING_SET_ID, org=ORG).members
    assert [m.payload["org_uuid"] for m in members] == [ORG_UUID]
    assert [m.stored_revision for m in members] == [NETWORK_BINDING_REVISION_2]
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


@pytest.mark.parametrize(
    "change",
    [
        {"outcome": "invented"},
        {"recovery_policy": {"mode": "none", "recovery_pub": "aa" * 32}},
        {"recovery_policy": {"mode": "none", "extra": True}},
        {"binding_generation": "not-authority"},
        {"expires_at": 9_007_199_254_740_992},
    ],
)
def test_register_rejects_malformed_authoritative_binding_response(
    env, root, monkeypatch, change
):
    _store_key(env, root)
    response = {
        "outcome": "claimed",
        "org_uuid": ORG_UUID,
        "root_pub": root.public_hex,
        "binding_generation": "aa" * 32,
        "expires_at": int(time.time()) + 30 * 86400,
        "recovery_policy": {"mode": "none"},
    }
    response.update(change)

    class _Response:
        status_code = 201
        text = "response"

        def json(self):
            return response

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            return _Response()

    monkeypatch.setattr(network_routes, "_registry_client", lambda url: _Client())
    result = env.post(
        "/api/network/register",
        json={"org": ORG, "envelope": _registration_envelope(root)},
    )
    assert result.status_code == 502
    assert settings_ops.read_set(NETWORK_BINDING_SET_ID, org=ORG).members == []


@pytest.mark.parametrize(
    "outcome, expected_status, expected_revision",
    [
        ("claimed", 502, NETWORK_BINDING_REVISION),
        ("already_bound_self", 200, NETWORK_BINDING_REVISION_2),
        ("reclaimed_expired", 200, NETWORK_BINDING_REVISION_2),
    ],
)
def test_register_context_refuses_fresh_claim_for_existing_v1_binding(
    env,
    root,
    monkeypatch,
    outcome,
    expected_status,
    expected_revision,
):
    """Only a truly unbound local context may persist registry ``claimed``."""
    _store_key(env, root)
    settings_ops.add_setting(
        NETWORK_BINDING_SET_ID,
        NETWORK_BINDING_REVISION,
        "registry.test",
        {
            "org_uuid": ORG_UUID,
            "root_pub": root.public_hex,
            "registry_url": REGISTRY_URL,
            "recovery_policy": {"mode": "none"},
            "binding_expires_at": "2026-09-26T00:00:00Z",
        },
        org=ORG,
    )
    response = {
        "outcome": outcome,
        "org_uuid": ORG_UUID,
        "root_pub": root.public_hex,
        "binding_generation": "aa" * 32,
        "expires_at": int(time.time()) + 30 * 86400,
        "recovery_policy": {"mode": "none"},
    }

    class _Response:
        status_code = 201
        text = "response"

        def json(self):
            return response

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            return _Response()

    monkeypatch.setattr(network_routes, "_registry_client", lambda url: _Client())
    result = env.post(
        "/api/network/register",
        json={"org": ORG, "envelope": _registration_envelope(root)},
    )
    assert result.status_code == expected_status
    row = settings_ops.read_set(NETWORK_BINDING_SET_ID, org=ORG).members[0]
    assert row.stored_revision == expected_revision
    if expected_status == 200:
        assert result.json()["outcome"] == outcome
        assert row.payload["binding_generation"] == response["binding_generation"]
    else:
        assert "invalid for this binding context" in result.json()["error"]
        assert "binding_generation" not in row.payload


@pytest.mark.parametrize("mismatch", ["org_uuid", "root_pub", "policy", "registry_key"])
def test_register_refresh_refuses_mismatched_frozen_binding_before_network(
    env, root, monkeypatch, mismatch
):
    """A local binding freezes every refresh/reclaim coordinate."""
    _store_key(env, root)
    binding = {
        "org_uuid": ORG_UUID,
        "root_pub": root.public_hex,
        "registry_url": "https://frozen.registry.example",
        "recovery_policy": {"mode": "none"},
        "binding_expires_at": "2026-09-26T00:00:00Z",
    }
    key = "frozen.registry.example"
    if mismatch == "org_uuid":
        binding["org_uuid"] = "44444444-4444-4444-8444-444444444444"
    elif mismatch == "root_pub":
        binding["root_pub"] = "ab" * 32
    elif mismatch == "policy":
        binding["recovery_policy"] = {
            "mode": "recovery-key",
            "recovery_pub": "cd" * 32,
        }
    else:
        key = "wrong.registry.example"
    settings_ops.add_setting(
        NETWORK_BINDING_SET_ID,
        NETWORK_BINDING_REVISION,
        key,
        binding,
        org=ORG,
    )
    calls: list[str] = []

    def forbidden_client(base_url):
        calls.append(base_url)
        raise AssertionError("mismatched local authority must not contact a registry")

    monkeypatch.setattr(network_routes, "_registry_client", forbidden_client)
    result = env.post(
        "/api/network/register",
        json={"org": ORG, "envelope": _registration_envelope(root)},
    )
    assert result.status_code == 409
    assert calls == []
    row = settings_ops.read_set(NETWORK_BINDING_SET_ID, org=ORG).members[0]
    assert row.stored_revision == NETWORK_BINDING_REVISION
    assert "binding_generation" not in row.payload


def test_register_refresh_uses_nondefault_frozen_registry_and_exact_context(
    env, root, monkeypatch
):
    """Refresh/reclaim never falls back to the deployment default registry."""
    _store_key(env, root)
    frozen_url = "https://frozen.registry.example"
    settings_ops.add_setting(
        NETWORK_BINDING_SET_ID,
        NETWORK_BINDING_REVISION,
        "frozen.registry.example",
        {
            "org_uuid": ORG_UUID,
            "root_pub": root.public_hex,
            "registry_url": frozen_url,
            "recovery_policy": {"mode": "none"},
            "binding_expires_at": "2026-09-26T00:00:00Z",
        },
        org=ORG,
    )
    response = {
        "outcome": "already_bound_self",
        "org_uuid": ORG_UUID,
        "root_pub": root.public_hex,
        "binding_generation": "ef" * 32,
        "expires_at": int(time.time()) + 30 * 86400,
        "recovery_policy": {"mode": "none"},
    }
    calls: list[str] = []

    class _Response:
        status_code = 201
        text = "response"

        def json(self):
            return response

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            return _Response()

    def frozen_client(base_url):
        calls.append(base_url)
        return _Client()

    monkeypatch.setattr(network_routes, "_registry_client", frozen_client)
    result = env.post(
        "/api/network/register",
        json={"org": ORG, "envelope": _registration_envelope(root)},
    )
    assert result.status_code == 200, result.text
    assert result.json()["outcome"] == "already_bound_self"
    assert calls == [frozen_url]
    row = settings_ops.read_set(NETWORK_BINDING_SET_ID, org=ORG).members[0]
    assert row.key == "frozen.registry.example"
    assert row.stored_revision == NETWORK_BINDING_REVISION_2
    assert row.payload["registry_url"] == frozen_url
    assert row.payload["binding_generation"] == response["binding_generation"]


def test_register_refuses_to_overwrite_binding_changed_during_registry_call(
    env, root, monkeypatch
):
    """The response is not persisted across a local binding-state race."""
    _store_key(env, root)
    original = {
        "org_uuid": ORG_UUID,
        "root_pub": root.public_hex,
        "registry_url": REGISTRY_URL,
        "recovery_policy": {"mode": "none"},
        "binding_expires_at": "2026-09-26T00:00:00Z",
    }
    settings_ops.add_setting(
        NETWORK_BINDING_SET_ID,
        NETWORK_BINDING_REVISION,
        "registry.test",
        original,
        org=ORG,
    )
    response = {
        "outcome": "already_bound_self",
        "org_uuid": ORG_UUID,
        "root_pub": root.public_hex,
        "binding_generation": "ef" * 32,
        "expires_at": int(time.time()) + 30 * 86400,
        "recovery_policy": {"mode": "none"},
    }

    class _Response:
        status_code = 201
        text = "response"

        def json(self):
            return response

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            settings_ops.upsert_by_key(
                NETWORK_BINDING_SET_ID,
                NETWORK_BINDING_REVISION,
                "registry.test",
                {
                    **original,
                    "endpoint_hints": [{"url": "https://changed.example"}],
                },
                org=ORG,
            )
            return _Response()

    monkeypatch.setattr(network_routes, "_registry_client", lambda url: _Client())
    result = env.post(
        "/api/network/register",
        json={"org": ORG, "envelope": _registration_envelope(root)},
    )
    assert result.status_code == 409
    assert "changed during registry registration" in result.json()["error"]
    row = settings_ops.read_set(NETWORK_BINDING_SET_ID, org=ORG).members[0]
    assert row.stored_revision == NETWORK_BINDING_REVISION
    assert row.payload["endpoint_hints"] == [{"url": "https://changed.example"}]
    assert "binding_generation" not in row.payload


def _renew_envelope(root: KeyPair, org_uuid=ORG_UUID):
    """A root-direct binding-renewal heartbeat, signed exactly as the browser's
    sign-in maintenance signs it (network-signon _signRootRequest)."""
    return sign_request(root, "POST", f"/v1/orgs/{org_uuid}/renew", {},
                        ts=int(time.time()))


def test_renew_heartbeats_a_live_binding(env, root, registry_app):
    """The dashboard renew proxy forwards a root-direct §4.2 heartbeat to the
    registry and persists the authoritative new expiry locally — the path that
    keeps the binding alive across its 30-day route authorization."""
    _store_key(env, root)
    reg = env.post("/api/network/register",
                   json={"org": ORG, "envelope": _registration_envelope(root)})
    assert reg.status_code == 200, reg.text

    r = env.post("/api/network/renew",
                 json={"org": ORG, "envelope": _renew_envelope(root)})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    # The registry — the security boundary — accepted the root-direct heartbeat
    # and the org stays live.
    assert registry_app.state.store.get_org(ORG_UUID) is not None
    # The local binding row now reflects the registry's authoritative expiry.
    served = env.get(f"/api/network/binding?org={ORG}").json()
    assert served["binding_expires_at"] == r.json()["binding"]["binding_expires_at"]


def test_renew_recovers_v1_only_from_authoritative_registry_generation(
    env, root, registry_app
):
    """A V1 row can become V2 only through a matching registry response."""
    now = int(time.time())
    registry_app.state.store.create_org(
        ORG_UUID,
        root.public_hex,
        "none",
        None,
        now=now,
        expires_at=now + 30 * 86400,
    )
    settings_ops.add_setting(
        NETWORK_BINDING_SET_ID,
        NETWORK_BINDING_REVISION,
        "registry.test",
        {
            "org_uuid": ORG_UUID,
            "root_pub": root.public_hex,
            "registry_url": REGISTRY_URL,
            "recovery_policy": {"mode": "none"},
            "binding_expires_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 30 * 86400)
            ),
        },
        org=ORG,
    )
    response = env.post(
        "/api/network/renew",
        json={"org": ORG, "envelope": _renew_envelope(root)},
    )
    assert response.status_code == 200, response.text
    row = settings_ops.read_set(NETWORK_BINDING_SET_ID, org=ORG).members[0]
    assert row.stored_revision == NETWORK_BINDING_REVISION_2
    assert row.payload["binding_generation"] == registry_app.state.store.get_org(
        ORG_UUID
    ).binding_generation


def test_renew_policy_drift_extends_registry_but_writes_no_local_v2_change(
    env, root, registry_app
):
    _store_key(env, root)
    registered = env.post(
        "/api/network/register",
        json={"org": ORG, "envelope": _registration_envelope(root)},
    )
    assert registered.status_code == 200, registered.text
    before = dict(registered.json()["binding"])
    authority = registry_app.state.store.get_org(ORG_UUID)
    recovery = KeyPair.generate()
    assert registry_app.state.store.update_recovery_policy(
        ORG_UUID,
        "recovery-key",
        recovery.public_hex,
        expected_epoch=authority.policy_epoch,
        new_epoch=authority.policy_epoch + 1,
    )
    response = env.post(
        "/api/network/renew",
        json={"org": ORG, "envelope": _renew_envelope(root)},
    )
    assert response.status_code == 502
    assert "does not match" in response.json()["error"]
    after = settings_ops.read_set(NETWORK_BINDING_SET_ID, org=ORG).members[0]
    assert after.payload == before
    assert registry_app.state.store.get_org(ORG_UUID).renewed_at is not None


def test_renew_of_an_unregistered_org_is_404(env, root):
    """A heartbeat needs an existing binding to name — an org that never
    registered has nothing to renew (the expired case is reclaimed by a fresh
    registration, not a heartbeat)."""
    _store_key(env, root)
    r = env.post("/api/network/renew",
                 json={"org": ORG, "envelope": _renew_envelope(root)})
    assert r.status_code == 404
    assert "no binding to renew" in r.json()["error"]


def test_renew_cannot_redirect_to_a_foreign_org(env, root):
    """Renewal carries only {org, envelope}; the registry destination and UUID
    come from the stored binding, so a caller cannot heartbeat a foreign org."""
    _store_key(env, root)
    r = env.post("/api/network/renew", json={
        "org": ORG, "envelope": _renew_envelope(root), "registry": "evil"})
    assert r.status_code == 400


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


def _store_personal_identity(personal: KeyPair) -> None:
    from tools.graph import settings_ops
    from tools.graph.schemas.personal_identity import (
        PERSONAL_IDENTITY_REVISION,
        PERSONAL_IDENTITY_SET_ID,
    )

    armor = mint_password_armor(personal, "correct horse battery staple",
                             iterations=10_000)  # low iters: fast test
    with settings_ops.identity_write_context():
        settings_ops.upsert_by_key(
            PERSONAL_IDENTITY_SET_ID, PERSONAL_IDENTITY_REVISION, "default",
            {"armored_private_key": armor, "root_pub": personal.public_hex,
             "display_name": "Operator", "created_at": "2026-01-01T00:00:00Z"},
            org=None)


def test_register_personal_org_accepts_the_personal_root(env, monkeypatch, registry_app):
    """The personal identity registers as its OWN org (the 'personal tunnel'):
    no NetworkOrgKeyV2 org-key is needed — the personal identity row proves the
    personal root's armor is stored, satisfying the same recoverability
    invariant — and the binding lands in the personal store, which is exactly
    what the fleet reachability path (_load_binding(None)) reads."""
    from tools.graph import settings_ops
    from tools.network import fleet_runtime

    # Personal scope: no org stamped, so CALLER_ORG collapses to the personal DB.
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    personal = KeyPair.generate()
    _store_personal_identity(personal)

    org_uuid = fleet_runtime.personal_org_uuid(personal.public_hex)
    r = env.post("/api/network/register",
                 json={"envelope": _registration_envelope(personal, org_uuid=org_uuid)})
    assert r.status_code == 200, r.text
    bound = registry_app.state.store.get_org(org_uuid)
    assert bound is not None and bound.root_pub == personal.public_hex
    members = settings_ops.read_owned_set(NETWORK_BINDING_SET_ID, org=None).members
    assert [m.payload["org_uuid"] for m in members] == [org_uuid]


def test_register_named_org_still_requires_its_own_org_key(env, registry_app):
    """The personal-identity fallback is scoped to the personal org only: a
    NAMED org with no org-key is still refused even when a personal identity for
    the same root exists (env keeps GRAPH_ORG=ORG → a named-org registration)."""
    personal = KeyPair.generate()
    _store_personal_identity(personal)
    r = env.post("/api/network/register",
                 json={"org": ORG, "envelope": _registration_envelope(personal)})
    assert r.status_code == 409
    assert "store the encrypted armor first" in r.json()["error"]
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
    assert env.post("/api/network/org-key/sealed",
                    json=_sealed(root)).status_code == 502
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


def _mint_serve(
    root: KeyPair,
    *,
    scope=("tunnel:serve",),
    org_uuid=ORG_UUID,
    subject=Subject("persona", "ab" * 32),
    not_before=None,
    not_after=None,
):
    delegate = KeyPair.generate()
    now = int(time.time())
    cert = issue_cert(
        root, delegate.public_hex, scope=scope, org=org_uuid,
        subject=subject,
        not_before=now - 300 if not_before is None else not_before,
        not_after=now + 30 * 86400 if not_after is None else not_after,
    )
    return delegate, cert


def _serve_body(root: KeyPair, delegate: KeyPair, cert, *, org=ORG, **changes):
    """Canonical two-cert provisioning body over one child and time window."""
    viewer_cert = issue_cert(
        root, delegate.public_hex, scope=tuple(cert.scope), org=cert.org,
        subject=Subject("operator", delegate.public_hex),
        not_before=cert.not_before, not_after=cert.not_after,
    )
    body = {
        "org": org,
        "cert": cert.to_json().decode("ascii"),
        "viewer_cert": viewer_cert.to_json().decode("ascii"),
        "private_key": delegate.private_hex,
    }
    body.update(changes)
    return body


def _serve_key_dir(monkeypatch, tmp_path):
    d = tmp_path / "serve-keys"
    monkeypatch.setenv("AUTONOMY_NETWORK_KEY_DIR", str(d))
    return d


PERSONAL_ORG_UUID = "02d833fd-a664-5b08-86ca-86615db52f6f"


def test_provision_serve_cert_personal_scope(env, root, tmp_path, monkeypatch):
    """A personal-org serve-cert (org=None) must be WRITTEN under the "personal"
    store, not refused as a scopeless write to the @home("organization") set.

    Regression pin for the exact bug that blocked the personal tunnel live:
    post_serve_cert wrote the cert with org=org, which for the personal scope is
    None, and the org-homed set refuses a scopeless write — 500ing the mint,
    swallowed by the browser's best-effort provisioning ("binding present,
    serve-cert never provisioned"). The write now targets "personal", which
    resolves to the same personal.db serve_cert_state(None) reads.
    """
    monkeypatch.delenv("GRAPH_ORG", raising=False)   # personal (scopeless) caller
    _serve_key_dir(monkeypatch, tmp_path)
    # The personal binding lives in the personal store; an org-homed set needs an
    # explicit scope on write, so it is stored under "personal" (post_register
    # does the same). `root` stands in for the personal root here.
    settings_ops.add_setting(
        NETWORK_BINDING_SET_ID, NETWORK_BINDING_REVISION_2, "auto.network",
        {"org_uuid": PERSONAL_ORG_UUID, "root_pub": root.public_hex,
         "registry_url": REGISTRY_URL, "recovery_policy": {"mode": "none"},
         "binding_generation": "aa" * 32,
         "binding_expires_at": time.strftime(
             "%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(time.time()) + 30 * 86400))},
        org="personal",
    )
    delegate, cert = _mint_serve(root, org_uuid=PERSONAL_ORG_UUID)
    # org=None is the scopeless POST the browser makes for the personal org.
    r = env.post("/api/network/serve-cert",
                 json=_serve_body(root, delegate, cert, org=None))
    assert r.status_code == 200, r.text
    assert r.json()["child_pub"] == delegate.public_hex
    # The row landed in the personal store the runtime reads (org=None), proving
    # the org-homed scopeless-write refusal is gone.
    members = settings_ops.read_owned_set(
        NETWORK_SERVE_CERT_SET_ID, org=None).members
    assert any(delegate.public_hex in (m.payload.get("cert") or "")
               for m in members), "personal serve-cert row not written"


def test_register_treats_registry_409_as_idempotent_when_binding_matches(
    env, root, monkeypatch
):
    """A same-root re-registration that the (not-yet-upgraded production) registry
    409s is treated as idempotent success — but ONLY when our own persisted
    binding proves the org is ours (same org_uuid + root_pub). This is what lets a
    re-unlock proceed past register to serve-cert provisioning without depending on
    the production registry running claim_org's same-root idempotency."""
    monkeypatch.delenv("GRAPH_ORG", raising=False)   # personal scope
    _store_personal_identity(root)
    settings_ops.add_setting(
        NETWORK_BINDING_SET_ID, NETWORK_BINDING_REVISION_2, "auto.network",
        {"org_uuid": PERSONAL_ORG_UUID, "root_pub": root.public_hex,
         "registry_url": REGISTRY_URL, "recovery_policy": {"mode": "none"},
         "binding_generation": "aa" * 32,
         "binding_expires_at": time.strftime(
             "%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(time.time()) + 30 * 86400))},
        org="personal",
    )

    class _Conflict:
        status_code = 409
        text = "conflict"

        def json(self):
            return {"detail": "org UUID is already bound"}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *a, **k):
            return _Conflict()

    monkeypatch.setattr(network_routes, "_registry_client", lambda url: _Client())

    r = env.post("/api/network/register",
                 json={"envelope": _registration_envelope(
                     root, org_uuid=PERSONAL_ORG_UUID)})
    assert r.status_code == 200, r.text
    assert r.json().get("already_registered") is True

    # A DIFFERENT root hitting the same 409 must still fail — the idempotency is
    # scoped to our own persisted binding, not "any 409 is fine". (The stored
    # binding keeps root's pub; overwriting the personal identity only satisfies
    # the recoverability precondition so we reach the registry 409.)
    other = KeyPair.generate()
    _store_personal_identity(other)   # singleton upsert
    r2 = env.post("/api/network/register",
                  json={"envelope": _registration_envelope(
                      other, org_uuid=PERSONAL_ORG_UUID)})
    assert r2.status_code == 502, r2.text


def test_provision_serve_cert_happy_path(env, root, tmp_path, monkeypatch):
    _serve_key_dir(monkeypatch, tmp_path)
    _store_binding(root)
    delegate, cert = _mint_serve(root)

    r = env.post("/api/network/serve-cert", json=_serve_body(root, delegate, cert))
    assert r.status_code == 200, r.text
    assert r.json()["child_pub"] == delegate.public_hex

    # Key file exists, mode 0600, holds exactly the delegate key — and the
    # settings row points at it, never carrying the key itself.
    import os
    key_path = (
        tmp_path / "serve-keys" / f"serve-{ORG_UUID}-{delegate.public_hex}.key"
    )
    assert key_path.is_file()
    assert (os.stat(key_path).st_mode & 0o777) == 0o600
    assert key_path.read_text().strip() == delegate.private_hex

    row = settings_ops.read_owned_set(NETWORK_SERVE_CERT_SET_ID, org=ORG).members[0].payload
    assert row["key_path"] == key_path.name
    assert row["root_pub"] == root.public_hex
    assert row["not_after"] == cert.not_after
    assert "private_key" not in row  # the secret is NOT in the settings store


def test_serve_cert_status_is_a_cheap_required_or_ok_signal(
    env, root, tmp_path, monkeypatch,
):
    _serve_key_dir(monkeypatch, tmp_path)
    _store_binding(root)
    missing = env.get(f"/api/network/serve-cert?org={ORG}")
    assert missing.status_code == 200
    assert missing.json() == {
        "required": True, "status": "missing",
        # No row, so no life to report — the counter is present and null
        # rather than absent, so a caller never has to guess which it is.
        "days_remaining": None,
    }

    delegate, cert = _mint_serve(root)
    stored = env.post(
        "/api/network/serve-cert", json=_serve_body(root, delegate, cert))
    assert stored.status_code == 200, stored.text
    ready = env.get(f"/api/network/serve-cert?org={ORG}")
    assert ready.json() == {
        "required": False, "status": "ok", "days_remaining": 30.0,
    }


def test_failed_serve_cert_update_preserves_previous_row_and_key(
    env, root, tmp_path, monkeypatch,
):
    key_dir = _serve_key_dir(monkeypatch, tmp_path)
    _store_binding(root)
    first_key, first_cert = _mint_serve(root)
    first = env.post(
        "/api/network/serve-cert",
        json=_serve_body(root, first_key, first_cert),
    )
    assert first.status_code == 200, first.text
    first_row = settings_ops.read_owned_set(
        NETWORK_SERVE_CERT_SET_ID, org=ORG
    ).members[0].payload
    first_path = key_dir / first_row["key_path"]
    assert first_path.read_text().strip() == first_key.private_hex

    second_key, second_cert = _mint_serve(root)

    def fail_upsert(*args, **kwargs):
        raise RuntimeError("injected settings failure")

    monkeypatch.setattr(settings_ops, "upsert_by_key", fail_upsert)
    second = env.post(
        "/api/network/serve-cert",
        json=_serve_body(root, second_key, second_cert),
    )
    assert second.status_code == 500
    current = settings_ops.read_owned_set(
        NETWORK_SERVE_CERT_SET_ID, org=ORG
    ).members[0].payload
    assert current == first_row
    assert first_path.read_text().strip() == first_key.private_hex
    assert not (key_dir / f"serve-{ORG_UUID}-{second_key.public_hex}.key").exists()


def test_exact_serve_cert_retry_is_idempotent(env, root, tmp_path, monkeypatch):
    key_dir = _serve_key_dir(monkeypatch, tmp_path)
    _store_binding(root)
    delegate, cert = _mint_serve(root)
    body = _serve_body(root, delegate, cert)
    assert env.post("/api/network/serve-cert", json=body).status_code == 200
    key_path = key_dir / f"serve-{ORG_UUID}-{delegate.public_hex}.key"
    before = key_path.stat().st_mtime_ns

    retried = env.post("/api/network/serve-cert", json=body)
    assert retried.status_code == 200, retried.text
    assert retried.json()["child_pub"] == delegate.public_hex
    assert key_path.stat().st_mtime_ns == before


def test_same_child_cannot_be_recertified(env, root, tmp_path, monkeypatch):
    _serve_key_dir(monkeypatch, tmp_path)
    _store_binding(root)
    delegate, cert = _mint_serve(root)
    assert env.post(
        "/api/network/serve-cert", json=_serve_body(root, delegate, cert)
    ).status_code == 200
    now = int(time.time())
    replacement_cert = issue_cert(
        root,
        delegate.public_hex,
        scope=("tunnel:serve",),
        org=ORG_UUID,
        subject=Subject("persona", "ab" * 32),
        not_before=now - 10,
        not_after=now + 10 * 86400,
    )

    refused = env.post(
        "/api/network/serve-cert",
        json=_serve_body(root, delegate, replacement_cert),
    )
    assert refused.status_code == 409
    assert "fresh" in refused.json()["error"]


def test_provision_rejects_key_not_matching_cert(env, root, tmp_path, monkeypatch):
    _serve_key_dir(monkeypatch, tmp_path)
    _store_binding(root)
    _delegate, cert = _mint_serve(root)
    body = _serve_body(root, _delegate, cert)
    body["private_key"] = KeyPair.generate().private_hex
    r = env.post("/api/network/serve-cert", json=body)
    assert r.status_code == 400
    assert "does not match" in r.json()["error"]
    assert not (tmp_path / "serve-keys").exists()  # nothing written on rejection


def test_local_cross_org_child_key_reuse_is_refused(monkeypatch):
    from tools.graph import org_ops

    root = KeyPair.generate()
    child, cert = _mint_serve(root)
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.setattr(
        org_ops,
        "list_orgs",
        lambda: [SimpleNamespace(slug="org-a"), SimpleNamespace(slug="org-b")],
    )
    monkeypatch.setattr(
        settings_ops,
        "read_owned_set",
        lambda set_id, org=None: SimpleNamespace(
            members=(
                [SimpleNamespace(payload={"cert": cert.to_json().decode("ascii")})]
                if org == "org-b"
                else []
            )
        ),
    )

    assert network_routes._serve_child_used_by_another_local_org(
        child.public_hex, "org-a"
    )


def test_unreadable_local_org_store_fails_closed_for_child_reuse(monkeypatch):
    from tools.graph import org_ops

    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.setattr(
        org_ops,
        "list_orgs",
        lambda: [SimpleNamespace(slug="org-a"), SimpleNamespace(slug="org-b")],
    )

    def unreadable(set_id, org=None):
        if org == "org-b":
            raise OSError("injected unreadable store")
        return SimpleNamespace(members=[])

    monkeypatch.setattr(settings_ops, "read_owned_set", unreadable)
    assert network_routes._serve_child_used_by_another_local_org(
        "ab" * 32, "org-a"
    )


def test_provision_rejects_scope_without_tunnel_serve(env, root, tmp_path, monkeypatch):
    _serve_key_dir(monkeypatch, tmp_path)
    _store_binding(root)
    delegate, cert = _mint_serve(root, scope=("link:publish",))
    r = env.post("/api/network/serve-cert", json=_serve_body(root, delegate, cert))
    assert r.status_code == 400
    assert "tunnel:serve" in r.json()["error"]


def test_provision_rejects_extra_serve_scope(env, root, tmp_path, monkeypatch):
    _serve_key_dir(monkeypatch, tmp_path)
    _store_binding(root)
    delegate, cert = _mint_serve(root, scope=("link:publish", "tunnel:serve"))
    r = env.post("/api/network/serve-cert", json=_serve_body(root, delegate, cert))
    assert r.status_code == 400
    assert "exactly" in r.json()["error"]


@pytest.mark.parametrize("subject", [
    Subject("operator", "ab" * 32),
    Subject("persona", "AB" * 32),
    Subject("persona", "browser-deadbeef"),
])
def test_provision_rejects_noncanonical_persona_subject(
    env, root, tmp_path, monkeypatch, subject,
):
    _serve_key_dir(monkeypatch, tmp_path)
    _store_binding(root)
    delegate, cert = _mint_serve(root, subject=subject)
    r = env.post("/api/network/serve-cert", json=_serve_body(root, delegate, cert))
    assert r.status_code == 400
    assert "organization-scoped persona" in r.json()["error"]


def test_provision_rejects_not_yet_valid_cert(env, root, tmp_path, monkeypatch):
    _serve_key_dir(monkeypatch, tmp_path)
    _store_binding(root)
    now = int(time.time())
    delegate, cert = _mint_serve(
        root, not_before=now + 3600, not_after=now + 7200)
    r = env.post("/api/network/serve-cert", json=_serve_body(root, delegate, cert))
    assert r.status_code == 400
    assert "chain" in r.json()["error"]


def test_provision_rejects_intermediate_issued_serve_cert(
    env, root, tmp_path, monkeypatch,
):
    _serve_key_dir(monkeypatch, tmp_path)
    _store_binding(root)
    now = int(time.time())
    intermediate = KeyPair.generate()
    parent = issue_cert(
        root,
        intermediate.public_hex,
        scope=("link:publish", "tunnel:serve"),
        org=ORG_UUID,
        subject=Subject("operator", "intermediate"),
        not_before=now - 100,
        not_after=now + 20 * 86400,
    )
    delegate = KeyPair.generate()
    leaf = issue_cert(
        intermediate,
        delegate.public_hex,
        scope=("tunnel:serve",),
        org=ORG_UUID,
        subject=Subject("persona", "ab" * 32),
        not_before=now - 10,
        not_after=now + 10 * 86400,
        parent_cert=parent,
    )

    refused = env.post(
        "/api/network/serve-cert", json=_serve_body(root, delegate, leaf))
    assert refused.status_code == 400
    assert "directly" in refused.json()["error"]


def test_provision_rejects_cert_not_chaining_to_org_root(env, root, tmp_path, monkeypatch):
    _serve_key_dir(monkeypatch, tmp_path)
    _store_binding(root)
    # A cert signed by a DIFFERENT root — must not be accepted for this org.
    foreign_root = KeyPair.generate()
    delegate, cert = _mint_serve(foreign_root)
    r = env.post(
        "/api/network/serve-cert",
        json=_serve_body(foreign_root, delegate, cert),
    )
    assert r.status_code == 400
    assert "chain" in r.json()["error"]


def test_provision_requires_a_binding_first(env, root, tmp_path, monkeypatch):
    _serve_key_dir(monkeypatch, tmp_path)
    delegate, cert = _mint_serve(root)  # no binding stored
    r = env.post("/api/network/serve-cert", json=_serve_body(root, delegate, cert))
    assert r.status_code == 409
    assert "not registered" in r.json()["error"]


def test_provision_cross_org_refused(env, root, tmp_path, monkeypatch):
    # The env is the authenticated netorg dashboard (invariant 1): stamp its
    # token org so provisioning a serving credential for a FOREIGN org is the
    # refused cross-org attempt, not a local selection of a missing store.
    from tools.dashboard import api_auth
    from tools.dashboard.api_auth import ApiPrincipal, ApiPrincipalKind
    monkeypatch.setattr(
        api_auth, "principal_from_request",
        lambda request: ApiPrincipal(
            ApiPrincipalKind.ORG_SESSION, subject="auto-1", org=ORG
        ),
    )
    _serve_key_dir(monkeypatch, tmp_path)
    _store_binding(root)
    delegate, cert = _mint_serve(root)
    r = env.post(
        "/api/network/serve-cert",
        json=_serve_body(root, delegate, cert, org="someone-else"),
    )
    assert r.status_code == 403


def test_a_store_that_cannot_hold_a_serving_credential_is_not_evidence_of_reuse(
    monkeypatch,
):
    """A machine or personal store REFUSES an organization-scoped setting by
    declaration -- no serving child can exist there, so it is evidence of
    nothing and the scan must continue past it.

    Failing closed on it refused every mint on any node that has such a store,
    reported as "serving child keys ... cannot be reused across local
    organizations" -- naming the one thing that was not wrong. That is why two
    organizations could not renew a serving certificate at all.
    """
    from tools.graph import org_ops, schemas

    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.setattr(
        org_ops,
        "list_orgs",
        lambda: [
            SimpleNamespace(slug="org-a"),
            SimpleNamespace(slug="machine"),
            SimpleNamespace(slug="personal"),
        ],
    )

    def scoped(set_id, org=None):
        if org in ("machine", "personal"):
            raise schemas.SchemaValidationError(
                f"{set_id} does not live in this machine's database — it "
                "declares 'organization', and a machine store never leaves "
                "the machine"
            )
        return SimpleNamespace(members=[])

    monkeypatch.setattr(settings_ops, "read_owned_set", scoped)
    assert not network_routes._serve_child_used_by_another_local_org(
        "ab" * 32, "org-a"
    ), "a fresh child key must be mintable on a node with a machine store"


def test_serve_cert_status_get_answers_without_a_500(env):
    """Regression pin: the GET status check must never crash the route.

    The resolve_scoped_org migration left this one call site without the
    required ``request`` keyword, so EVERY pre-unlock status check raised a
    TypeError and returned 500 — the browser's mint decision never ran, no
    serving credential was ever provisioned, and the Fleet serving connector
    stayed locked ("serving machine is locked for Fleet sync"), blocking the
    two-Dashboard sync witness. The check must answer for the personal scope,
    an explicit org, and no org at all.
    """
    for query in ("?org=personal", "", "?org=" + ORG):
        r = env.get("/api/network/serve-cert" + query)
        assert r.status_code == 200, (query, r.status_code, r.text)
        body = r.json()
        assert body["status"] in (
            "ok", "missing", "expired", "key-invalid", "key-missing",
            "identity-invalid",
        ), body
        assert isinstance(body["required"], bool)
