"""Personal identity + passkey enrollment routes (identity_routes).

The browser does the visible ceremony (Get started UI); these tests pin
the server half:

* status reflects none → identity-only → fully-enrolled, and the
  ``onboarding_needed`` activation condition (true for accounts that
  predate the identity system — no corner-case code path);
* the personal-identity write path stores ONLY the canonical
  password-encrypted armor (I1) — plaintext-shaped payloads are refused
  and a raw-bytes scan of the settings DB proves the seed never touched
  disk; the personal root is DISTINCT from the org key row;
* WebAuthn registration: the RP ID derives from the request host
  (localhost AND the .ts.net name both enroll working, domain-bound
  credentials; IP literals are refused), the challenge/origin/RP ID are
  frozen at options time, and verification accepts a well-formed
  authenticator response (built here from a real P-256 key, 'none'
  attestation — what platform authenticators send);
* tested rejections: unknown/expired/replayed challenge, origin
  mismatch, RP ID mismatch, missing user-verification flag, duplicate
  credential, and options before an identity exists; caller org context
  is ignored by every personal identity operation.
"""

from __future__ import annotations
from tools.network.idkit.root_factor_policy import mint_password_armor, open_armor_with_password

import base64
import hashlib
import json
import struct

import cbor2
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard import identity_routes
from tools.dashboard.dao import identity_sessions
from tools.graph import settings_ops
from tools.graph.schemas.personal_identity import (
    PASSKEY_SET_ID,
    PERSONAL_IDENTITY_SET_ID,
)
from tools.network.idkit import KeyPair

ORG = "idorg"
PASSWORD = "week-glacier-thirty-nine"
HOST = "localhost:8080"
TSNET_HOST = "dash.tail1234.ts.net"


@pytest.fixture
def root():
    return KeyPair.generate()


@pytest.fixture
def env(tmp_path, monkeypatch):
    from tools.graph.db import GraphDB

    GraphDB.close_all_pooled()
    # Honest personal-store routing (auto-01d2y, crypto-adjudicated):
    # the routes write at org=None -> personal.db, and the tests read
    # back at explicit org="personal" — same store by production's own
    # routing, no pin needed. GRAPH_ORG stays set to a DIFFERENT org so
    # the tests also prove personal routing is immune to ambient org.
    orgs_dir = tmp_path / "orgs"
    orgs_dir.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    GraphDB.create_org_db(
        "personal", type_="personal", path=orgs_dir / "personal.db").close()
    monkeypatch.setenv("GRAPH_ORG", ORG)
    # post_personal mints the bootstrap unlock session — keep its HMAC
    # secret out of the repo's data/ during tests.
    monkeypatch.setenv("DASHBOARD_SESSION_SECRET_FILE",
                       str(tmp_path / "session.secret"))
    monkeypatch.setenv("DASHBOARD_IDENTITY_SESSION_DB",
                       str(tmp_path / "identity-sessions.db"))
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    identity_routes._pending.clear()
    identity_sessions.reset_for_tests()
    with TestClient(Starlette(routes=identity_routes.ROUTES),
                    base_url=f"https://{HOST}") as client:
        yield client
    identity_routes._pending.clear()
    identity_sessions.reset_for_tests()
    GraphDB.close_all_pooled()


def _armor(root: KeyPair) -> str:
    return mint_password_armor(root, PASSWORD, iterations=10_000)


def _store_identity(client, root: KeyPair, name="Alex"):
    r = client.post("/api/identity/personal",
                    json={"display_name": name,
                          "armored_private_key": _armor(root)})
    assert r.status_code == 200, r.text
    return r.json()


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


# ── authenticator emulation ───────────────────────────────────────────
#
# A minimal software authenticator: P-256 credential, 'none' attestation
# (what platform authenticators send with attestation: 'none' options),
# flags UP|UV|AT. Enough to drive verify_registration_response for real.


def _cose_p256(private_key: ec.EllipticCurvePrivateKey) -> bytes:
    nums = private_key.public_key().public_numbers()
    return cbor2.dumps({
        1: 2,      # kty: EC2
        3: -7,     # alg: ES256
        -1: 1,     # crv: P-256
        -2: nums.x.to_bytes(32, "big"),
        -3: nums.y.to_bytes(32, "big"),
    })


def _make_attestation(challenge_b64url: str, *, rp_id: str, origin: str,
                      cred_id: bytes = b"test-credential-0001",
                      flags: int = 0x45,  # UP | UV | AT
                      sign_count: int = 0,
                      cred_type: str = "webauthn.create") -> dict:
    private_key = ec.generate_private_key(ec.SECP256R1())
    cose_key = _cose_p256(private_key)
    auth_data = (
        hashlib.sha256(rp_id.encode()).digest()
        + bytes([flags])
        + struct.pack(">I", sign_count)
        + b"\x00" * 16                      # AAGUID
        + struct.pack(">H", len(cred_id))
        + cred_id
        + cose_key
    )
    client_data = json.dumps({
        "type": cred_type,
        "challenge": challenge_b64url,
        "origin": origin,
        "crossOrigin": False,
    }).encode()
    attestation_object = cbor2.dumps({
        "fmt": "none", "attStmt": {}, "authData": auth_data,
    })
    return {
        "id": _b64url(cred_id),
        "rawId": _b64url(cred_id),
        "type": "public-key",
        "authenticatorAttachment": "platform",
        "clientExtensionResults": {},
        "response": {
            "clientDataJSON": _b64url(client_data),
            "attestationObject": _b64url(attestation_object),
            "transports": ["internal"],
        },
    }


def _statement_for(root, credential, opts):
    """The root-signed enrollment statement the register route requires, minted
    from a built attestation + its options — the test-side mirror of what the
    browser does. The tests hold the identity's root, so this verifies for real."""
    from tools.network.idkit import enrollment
    auth = cbor2.loads(_b64url_decode(
        credential["response"]["attestationObject"]))["authData"]
    cred_id_len = int.from_bytes(auth[53:55], "big")
    return enrollment.mint(
        root=root,
        credential_id=credential["rawId"],
        credential_public_key=auth[55 + cred_id_len:].hex(),
        rp_id=opts["rp_id"],
        origin=opts["origin"],
        nonce=opts["nonce"],
        created_hlc=(0, 0),
        initial_sign_count=int.from_bytes(auth[33:37], "big"),
    ).to_dict()


def _enroll(
    client, root, *, host=HOST, label=None,
    cred_id=b"test-credential-0001", flags=0x45,
):
    """Full happy-path ceremony against *host*; returns the verify response."""
    opts = client.post("/api/identity/passkey/register-options", json={},
                       headers={"host": host})
    assert opts.status_code == 200, opts.text
    body = opts.json()
    credential = _make_attestation(
        body["options"]["challenge"], rp_id=body["rp_id"], origin=body["origin"],
        cred_id=cred_id, flags=flags)
    payload = {"credential": credential,
               "statement": _statement_for(root, credential, body)}
    if label is not None:
        payload["label"] = label
    return client.post("/api/identity/passkey/register", json=payload,
                       headers={"host": host})


# ── status / activation condition ─────────────────────────────────────



def _policy_commit_body(root, base_generation, operations, candidate_armor):
    return {
        "base_generation": base_generation,
        "operations": operations,
        "candidate_armor": candidate_armor,
        "root_signature": root.sign_hex(identity_routes._transition_message(
            base_generation, operations, candidate_armor,
        )),
    }

def test_status_starts_needing_onboarding(env):
    r = env.get("/api/identity/status")
    assert r.status_code == 200
    body = r.json()
    assert body["personal_identity"] is None
    assert body["passkeys"] == []
    assert body["onboarding_needed"] is True
    assert body["signed_in"] is False
    assert body["method"] is None
    assert body["enforced"] is False
    assert body["gate_disabled"] is False


def test_status_identity_alone_still_needs_onboarding(env, root):
    """The activation condition is 'no personal identity / no passkey' —
    an account that created the identity but never enrolled a device
    still lands in the flow (at the device step)."""
    _store_identity(env, root)
    body = env.get("/api/identity/status").json()
    assert body["personal_identity"]["display_name"] == "Alex"
    assert body["personal_identity"]["root_pub"] == root.public_hex
    assert body["onboarding_needed"] is True
    assert body["signed_in"] is True
    assert body["method"] == "bootstrap"
    assert body["enforced"] is True


def test_status_fully_enrolled(env, root):
    _store_identity(env, root)
    assert _enroll(env, root).status_code == 200
    body = env.get("/api/identity/status").json()
    assert body["onboarding_needed"] is False
    assert len(body["passkeys"]) == 1
    assert body["passkeys"][0]["rp_id"] == "localhost"
    assert body["passkeys_for_host"] == 1


def test_status_never_leaks_the_armor(env, root):
    """Status is read on every page load — it carries public metadata
    only, never the encrypted blob (defense in depth on top of I1)."""
    _store_identity(env, root)
    body = env.get("/api/identity/status").json()
    assert "armored_private_key" not in json.dumps(body)


def test_status_pins_reads_to_personal_scope(env, root):
    """The shell's org header/query must never move personal identity."""
    _store_identity(env, root)
    response = env.get(
        "/api/identity/status?org=someone-else",
        headers={"X-Graph-Org": "someone-else"},
    )
    assert response.status_code == 200
    assert response.json()["personal_identity"]["display_name"] == "Alex"


def test_personal_scope_stays_consistent_when_graph_org_is_set(
    tmp_path, monkeypatch, root,
):
    """Writes, gate reads, and status reads use one physical personal DB.

    ``GRAPH_DB`` would make every org argument resolve to one fixture file,
    masking the production split this test is intended to catch. Materialize
    distinct personal and env-org databases instead.
    """
    from tools.dashboard import unlock_routes
    from tools.graph.db import GraphDB

    GraphDB.close_all_pooled()
    orgs_dir = tmp_path / "orgs"
    orgs_dir.mkdir()
    personal_db = orgs_dir / "personal.db"
    env_org_db = orgs_dir / f"{ORG}.db"
    GraphDB(personal_db).close()
    GraphDB(env_org_db).close()

    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.setenv("GRAPH_ORG", ORG)
    monkeypatch.setenv("DASHBOARD_SESSION_SECRET_FILE",
                       str(tmp_path / "session.secret"))
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    identity_routes._pending.clear()
    unlock_routes.bust_enforce_cache()

    with TestClient(Starlette(routes=identity_routes.ROUTES),
                    base_url=f"https://{HOST}") as client:
        created = client.post(
            "/api/identity/personal",
            json={"org": ORG, "display_name": "Personal Alex",
                  "armored_private_key": _armor(root)},
            headers={"X-Graph-Org": ORG},
        )
        assert created.status_code == 200, created.text
        status = client.get(
            "/api/identity/status?org=another-org",
            headers={"X-Graph-Org": "another-org"},
        ).json()

    assert status["personal_identity"]["display_name"] == "Personal Alex"
    assert status["enforced"] is True
    assert len(settings_ops.read_set(PERSONAL_IDENTITY_SET_ID,
                                     org=None).members) == 1
    # The personal set is pinned to the personal store: a read naming the
    # env org resolves to the SAME single identity, not a per-org copy —
    # the "stays consistent when GRAPH_ORG is set" this test is named for.
    via_env_org = settings_ops.read_set(PERSONAL_IDENTITY_SET_ID,
                                        org=ORG).members
    assert len(via_env_org) == 1
    assert via_env_org[0].payload["display_name"] == "Personal Alex"
    GraphDB.close_all_pooled()


def test_status_org_identity_does_not_satisfy_personal(env, root):
    """C1's ORG key is not a personal identity — an account with only
    the org root (the operator's real pre-existing state) still needs
    onboarding. The org root is the sealed revision-2 record (B4 Option B),
    the only org-key shape the schema now knows."""
    from tools.network.idkit.sealing import derive_encapsulation_keypair, seal
    from tools.graph.schemas.network_identity import (
        NETWORK_ORG_KEY_REVISION_2,
        NETWORK_ORG_KEY_SET_ID,
        ORG_ROOT_ARMOR_PURPOSE,
    )
    org_root = KeyPair.generate()
    # Seal the org seed to the owner's recipient key derived from THIS
    # account's personal root — exactly as org_ops._seal_org_key does.
    _, owner_kem_pub = derive_encapsulation_keypair(
        bytes.fromhex(root.private_hex), ORG_ROOT_ARMOR_PURPOSE)
    sealed = seal(bytes.fromhex(org_root.private_hex), owner_kem_pub,
                  ORG_ROOT_ARMOR_PURPOSE)
    settings_ops.upsert_by_key(
        NETWORK_ORG_KEY_SET_ID, NETWORK_ORG_KEY_REVISION_2, "default",
        {"root_pub": org_root.public_hex,
         "sealed_root_key": sealed.hex(),
         "owner_kem_pub": owner_kem_pub,
         "seal_purpose": ORG_ROOT_ARMOR_PURPOSE},
        org="personal")
    body = env.get("/api/identity/status").json()
    assert body["personal_identity"] is None
    assert body["onboarding_needed"] is True


# ── personal identity storage (I1) ────────────────────────────────────


def test_personal_roundtrip(env, root):
    body = _store_identity(env, root)
    assert body["root_pub"] == root.public_hex
    served = env.get("/api/identity/personal").json()
    assert served["display_name"] == "Alex"
    opened = open_armor_with_password(served["armored_private_key"], PASSWORD)
    assert opened.public_hex == root.public_hex


def test_personal_refuses_plaintext_key(env, root):
    r = env.post("/api/identity/personal",
                 json={"display_name": "Alex",
                       "armored_private_key": root.private_hex})
    assert r.status_code == 400
    assert "I1" in r.json()["error"]


def test_personal_refuses_overwrite(env, root):
    _store_identity(env, root)
    r = env.post("/api/identity/personal",
                 json={"display_name": "Eve",
                       "armored_private_key": _armor(KeyPair.generate())})
    assert r.status_code == 409
    # The original identity survives untouched.
    assert env.get("/api/identity/personal").json()["root_pub"] == root.public_hex


def test_personal_requires_display_name(env, root):
    r = env.post("/api/identity/personal",
                 json={"armored_private_key": _armor(root)})
    assert r.status_code == 400
    assert "display_name" in r.json()["error"]


def test_personal_is_distinct_from_org_key(env, root):
    """Writing the personal identity must not create/replace the org
    key row — the two roots live in different set_ids."""
    _store_identity(env, root)
    org_rows = settings_ops.read_set("autonomy.network.org-key",
                                     org="personal").members
    assert org_rows == []
    personal_rows = settings_ops.read_set(PERSONAL_IDENTITY_SET_ID,
                                          org=None).members
    assert len(personal_rows) == 1


def test_personal_seed_never_touches_disk(env, root, tmp_path):
    """I1 grep-pin: after storing, the raw seed bytes (and their base64
    wrap) are absent from the settings DB file."""
    import os

    _store_identity(env, root)
    db_bytes = open(os.environ["AUTONOMY_ORGS_DIR"] + "/personal.db", "rb").read()
    seed_hex = root.private_hex
    assert seed_hex.encode() not in db_bytes
    assert bytes.fromhex(seed_hex) not in db_bytes
    assert base64.b64encode(bytes.fromhex(seed_hex)) not in db_bytes


def test_personal_org_context_is_ignored(env, root):
    r = env.post("/api/identity/personal",
                 json={"org": "someone-else", "display_name": "Mallory",
                       "armored_private_key": _armor(root)},
                 headers={"X-Graph-Org": "someone-else"})
    assert r.status_code == 200
    r2 = env.get("/api/identity/personal", params={"org": "another-org"},
                 headers={"X-Graph-Org": "another-org"})
    assert r2.status_code == 200
    assert r2.json()["display_name"] == "Mallory"


# ── passkey options: RP ID from the request host ──────────────────────


def test_options_require_identity_first(env):
    r = env.post("/api/identity/passkey/register-options", json={})
    assert r.status_code == 409
    assert "identity first" in r.json()["error"]


def test_options_rp_id_localhost(env, root):
    _store_identity(env, root)
    r = env.post("/api/identity/passkey/register-options", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["rp_id"] == "localhost"
    assert body["origin"] == "https://localhost:8080"
    assert body["options"]["rp"]["id"] == "localhost"
    assert body["options"]["user"]["name"] == "Alex"
    sel = body["options"]["authenticatorSelection"]
    assert sel["residentKey"] == "required"
    assert sel["userVerification"] == "required"


def test_options_rp_id_follows_ts_net_host(env, root):
    """The SAME dashboard must mint domain-bound credentials for the
    host the browser is actually on — localhost and the .ts.net name."""
    _store_identity(env, root)
    r = env.post("/api/identity/passkey/register-options", json={},
                 headers={"host": TSNET_HOST})
    assert r.status_code == 200
    body = r.json()
    assert body["rp_id"] == TSNET_HOST
    assert body["origin"] == f"https://{TSNET_HOST}"


def test_options_refuse_ip_hosts(env, root):
    _store_identity(env, root)
    for host in ("127.0.0.1:8080", "192.168.1.7", "[::1]:8080"):
        r = env.post("/api/identity/passkey/register-options", json={},
                     headers={"host": host})
        assert r.status_code == 400, host
        assert "IP address" in r.json()["error"]


def test_options_challenges_are_unique(env, root):
    _store_identity(env, root)
    challenges = set()
    for _ in range(3):
        r = env.post("/api/identity/passkey/register-options", json={})
        challenges.add(r.json()["options"]["challenge"])
    assert len(challenges) == 3


# ── passkey verify: pinned ceremony ───────────────────────────────────


def test_register_happy_path_stores_credential(env, root):
    _store_identity(env, root)
    r = _enroll(env, root, label="This device")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rp_id"] == "localhost"
    rows = settings_ops.read_set(PASSKEY_SET_ID,
                                 org="personal").members
    assert len(rows) == 1
    stored = rows[0].payload
    assert stored["credential_id"] == body["credential_id"]
    assert stored["rp_id"] == "localhost"
    assert stored["origin"] == "https://localhost:8080"
    assert stored["label"] == "This device"
    assert stored["sign_count"] == 0
    assert stored["transports"] == ["internal"]
    assert stored["backup_eligible"] is False
    assert stored["backed_up"] is False
    # The stored public key parses as COSE — usable for assertions later.
    cose = cbor2.loads(_b64url_decode(stored["public_key"]))
    assert cose[1] == 2 and cose[3] == -7


def test_register_ts_net_end_to_end(env, root):
    _store_identity(env, root)
    r = _enroll(env, root, host=TSNET_HOST, cred_id=b"tsnet-credential-01")
    assert r.status_code == 200, r.text
    assert r.json()["rp_id"] == TSNET_HOST


def test_register_records_verified_multidevice_backup_flags(env, root):
    _store_identity(env, root)
    # UP | UV | BE | BS | AT: a sync-capable credential already backed up.
    response = _enroll(env, root, flags=0x5D)
    assert response.status_code == 200, response.text
    stored = settings_ops.read_set(PASSKEY_SET_ID, org="personal").members[0].payload
    assert stored["backup_eligible"] is True
    assert stored["backed_up"] is True


def test_register_refuses_unknown_challenge(env, root):
    _store_identity(env, root)
    credential = _make_attestation(
        _b64url(b"z" * 32), rp_id="localhost", origin="https://localhost:8080")
    r = env.post("/api/identity/passkey/register", json={"credential": credential})
    assert r.status_code == 400
    assert "no pending" in r.json()["error"]


def test_register_challenge_is_single_use(env, root):
    """Replaying the SAME verified response must fail — the pending
    ceremony is consumed on first use."""
    _store_identity(env, root)
    opts = env.post("/api/identity/passkey/register-options", json={}).json()
    credential = _make_attestation(
        opts["options"]["challenge"], rp_id="localhost",
        origin="https://localhost:8080")
    first = env.post("/api/identity/passkey/register", json={
        "credential": credential, "statement": _statement_for(root, credential, opts)})
    assert first.status_code == 200
    replay = env.post("/api/identity/passkey/register", json={"credential": credential})
    assert replay.status_code == 400
    assert "no pending" in replay.json()["error"]


def test_register_refuses_expired_challenge(env, root, monkeypatch):
    _store_identity(env, root)
    opts = env.post("/api/identity/passkey/register-options", json={}).json()
    credential = _make_attestation(
        opts["options"]["challenge"], rp_id="localhost",
        origin="https://localhost:8080")
    monkeypatch.setattr(identity_routes, "_now",
                        lambda: __import__("time").time() + 601)
    r = env.post("/api/identity/passkey/register", json={"credential": credential})
    assert r.status_code == 400
    assert "no pending" in r.json()["error"]


def test_register_refuses_wrong_origin(env, root):
    """The origin was frozen at options time; a response minted for a
    different origin (phishing page relaying a real challenge) fails."""
    _store_identity(env, root)
    opts = env.post("/api/identity/passkey/register-options", json={}).json()
    credential = _make_attestation(
        opts["options"]["challenge"], rp_id="localhost",
        origin="https://evil.example")
    r = env.post("/api/identity/passkey/register", json={"credential": credential})
    assert r.status_code == 400
    assert "did not verify" in r.json()["error"]


def test_register_refuses_wrong_rp_id_hash(env, root):
    """The authenticator's rpIdHash must match the RP ID pinned at
    options time — a credential minted for another domain is refused."""
    _store_identity(env, root)
    opts = env.post("/api/identity/passkey/register-options", json={}).json()
    credential = _make_attestation(
        opts["options"]["challenge"], rp_id="evil.example",
        origin="https://localhost:8080")
    r = env.post("/api/identity/passkey/register", json={"credential": credential})
    assert r.status_code == 400
    assert "did not verify" in r.json()["error"]


def test_register_cannot_cross_hosts_mid_ceremony(env, root):
    """Options minted on localhost cannot be completed as a .ts.net
    enrollment: the completion-host check refuses before any
    verification, and the pinned tuple would refuse the attestation
    anyway."""
    _store_identity(env, root)
    opts = env.post("/api/identity/passkey/register-options", json={},
                    headers={"host": HOST}).json()
    credential = _make_attestation(
        opts["options"]["challenge"], rp_id=TSNET_HOST,
        origin=f"https://{TSNET_HOST}")
    r = env.post("/api/identity/passkey/register", json={"credential": credential},
                 headers={"host": TSNET_HOST})
    assert r.status_code == 400
    assert "must complete on the host" in r.json()["error"]


def test_register_refuses_completion_from_different_host(env, root):
    """Codex finding 1 repro: a VALID attestation for the pinned
    localhost tuple, POSTed with a .ts.net Host header, must refuse —
    otherwise the stored row binds rp_id=localhost while the user is on
    the other host, a credential that can never assert where they sign
    in. The cross-host attempt also burns the ceremony (single-use)."""
    _store_identity(env, root)
    opts = env.post("/api/identity/passkey/register-options", json={},
                    headers={"host": HOST}).json()
    credential = _make_attestation(
        opts["options"]["challenge"], rp_id="localhost",
        origin="https://localhost:8080")   # valid for the frozen tuple
    r = env.post("/api/identity/passkey/register", json={"credential": credential},
                 headers={"host": TSNET_HOST})
    assert r.status_code == 400
    assert "must complete on the host" in r.json()["error"]
    # Nothing was stored...
    rows = settings_ops.read_set(PASSKEY_SET_ID,
                                 org="personal").members
    assert rows == []
    # ...and the burned ceremony cannot be replayed on the right host.
    retry = env.post("/api/identity/passkey/register",
                     json={"credential": credential}, headers={"host": HOST})
    assert retry.status_code == 400
    assert "no pending" in retry.json()["error"]


def test_two_browsers_can_complete_concurrent_registration_options(env, root):
    """Same-host browsers get independent, single-use challenges."""
    _store_identity(env, root)
    opts1 = env.post("/api/identity/passkey/register-options", json={}).json()
    opts2 = env.post("/api/identity/passkey/register-options", json={}).json()
    first = _make_attestation(
        opts1["options"]["challenge"], rp_id="localhost",
        origin="https://localhost:8080", cred_id=b"browser-one-credential")
    r = env.post("/api/identity/passkey/register", json={
        "credential": first, "statement": _statement_for(root, first, opts1)})
    assert r.status_code == 200, r.text
    second = _make_attestation(
        opts2["options"]["challenge"], rp_id="localhost",
        origin="https://localhost:8080", cred_id=b"browser-two-credential")
    r2 = env.post("/api/identity/passkey/register", json={
        "credential": second, "statement": _statement_for(root, second, opts2)})
    assert r2.status_code == 200, r2.text


def test_new_ceremony_leaves_other_hosts_pending(env, root):
    """Different RP IDs also remain independent and complete in parallel."""
    _store_identity(env, root)
    opts_local = env.post("/api/identity/passkey/register-options", json={},
                          headers={"host": HOST}).json()
    env.post("/api/identity/passkey/register-options", json={},
             headers={"host": TSNET_HOST})
    credential = _make_attestation(
        opts_local["options"]["challenge"], rp_id="localhost",
        origin="https://localhost:8080")
    r = env.post("/api/identity/passkey/register", json={
        "credential": credential,
        "statement": _statement_for(root, credential, opts_local)},
                 headers={"host": HOST})
    assert r.status_code == 200, r.text


def test_registration_pending_fifo_reserves_exact_capacity(env, monkeypatch):
    monkeypatch.setattr(identity_routes, "_now", lambda: 1000.0)
    identity_routes._pending.update({
        f"challenge-{index}": {"expires": 2000.0}
        for index in range(identity_routes.PENDING_MAX)
    })
    identity_routes._prune_pending(reserve=1)
    assert len(identity_routes._pending) == identity_routes.PENDING_MAX - 1
    assert "challenge-0" not in identity_routes._pending
    identity_routes._pending["newest"] = {"expires": 2000.0}
    assert len(identity_routes._pending) == identity_routes.PENDING_MAX


def test_register_requires_user_verification_flag(env, root):
    """UV is required (Face ID / PIN, the Gate-1 factor) — an
    attestation with only user-presence is refused."""
    _store_identity(env, root)
    opts = env.post("/api/identity/passkey/register-options", json={}).json()
    credential = _make_attestation(
        opts["options"]["challenge"], rp_id="localhost",
        origin="https://localhost:8080", flags=0x41)  # UP | AT, no UV
    r = env.post("/api/identity/passkey/register", json={"credential": credential})
    assert r.status_code == 400
    assert "did not verify" in r.json()["error"]


def test_register_refuses_webauthn_get_type(env, root):
    """A clientDataJSON of type webauthn.get (an assertion) can never
    register a credential."""
    _store_identity(env, root)
    opts = env.post("/api/identity/passkey/register-options", json={}).json()
    credential = _make_attestation(
        opts["options"]["challenge"], rp_id="localhost",
        origin="https://localhost:8080", cred_type="webauthn.get")
    r = env.post("/api/identity/passkey/register", json={"credential": credential})
    assert r.status_code == 400


def test_register_refuses_duplicate_credential(env, root):
    _store_identity(env, root)
    assert _enroll(env, root).status_code == 200
    r = _enroll(env, root)  # same cred_id, fresh challenge
    assert r.status_code == 409
    assert "already enrolled" in r.json()["error"]


def test_register_malformed_credential_is_400(env, root):
    _store_identity(env, root)
    opts = env.post("/api/identity/passkey/register-options", json={}).json()
    r = env.post("/api/identity/passkey/register", json={"credential": {
        "id": "x", "rawId": "x", "type": "public-key",
        "response": {"clientDataJSON": _b64url(json.dumps({
            "type": "webauthn.create",
            "challenge": opts["options"]["challenge"],
            "origin": "https://localhost:8080",
        }).encode()), "attestationObject": "AAAA"},
    }})
    assert r.status_code == 400


def test_register_unknown_transports_are_dropped(env, root):
    _store_identity(env, root)
    opts = env.post("/api/identity/passkey/register-options", json={}).json()
    credential = _make_attestation(
        opts["options"]["challenge"], rp_id="localhost",
        origin="https://localhost:8080")
    credential["response"]["transports"] = ["internal", "telepathy"]
    r = env.post("/api/identity/passkey/register", json={
        "credential": credential, "statement": _statement_for(root, credential, opts)})
    assert r.status_code == 200
    assert r.json()["transports"] == ["internal"]














def test_delete_passkey(env, root):
    _store_identity(env, root)
    assert _enroll(env, root).status_code == 200
    cred = env.get("/api/identity/status").json()["passkeys"][0]["credential_id"]
    assert env.delete(f"/api/identity/passkey/{cred}").status_code == 200
    assert env.get("/api/identity/status").json()["passkeys"] == []


def test_delete_unknown_passkey_is_404(env, root):
    _store_identity(env, root)
    assert env.delete("/api/identity/passkey/no-such-cred").status_code == 404


def _kem_for(prf: bytes) -> str:
    from tools.network.idkit.armor import PASSKEY_ARMOR_PURPOSE
    from tools.network.idkit.sealing import derive_encapsulation_keypair
    return derive_encapsulation_keypair(prf, PASSKEY_ARMOR_PURPOSE)[1]
















def test_factor_policy_preview_projects_batch_once_and_derives_roles(env, root):
    first = _password_descriptor(root, "password.old", "old-password")
    _store_policy_identity(
        env, root, [first], ["password.old"],
        {"op": "factor", "factor_id": "password.old"},
    )
    second = _password_descriptor(root, "password.new", "new-password")
    operations = [
        {"op": "remove_factor", "factor_id": "password.old"},
        {"op": "enroll_password", "factor": second, "access": True},
        {"op": "set_root_policy", "policy": {
            "op": "factor", "factor_id": "password.new",
        }},
    ]
    r = env.post("/api/identity/factor-policy/preview", json={
        "base_generation": 1, "operations": operations,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["generation"] == 2
    assert body["change_count"] == 1
    assert body["roles"] == {
        "password.new": {"access": "enabled", "root_role": "individual"},
    }


def test_factor_policy_commit_is_atomic_generation_cas(env, root):
    first = _password_descriptor(root, "password.one", "one-password")
    _store_policy_identity(
        env, root, [first], ["password.one"],
        {"op": "factor", "factor_id": "password.one"},
    )
    second = _password_descriptor(root, "password.two", "two-password")
    policy = {"op": "or", "children": [
        {"op": "factor", "factor_id": "password.one"},
        {"op": "factor", "factor_id": "password.two"},
    ]}
    operations = [
        {"op": "enroll_password", "factor": second, "access": False},
        {"op": "set_root_policy", "policy": policy},
    ]
    candidate = _policy_armor(
        root, 2, [first, second], ["password.one"], policy,
    )
    request = _policy_commit_body(root, 1, operations, candidate)
    r = env.post("/api/identity/factor-policy/commit", json=request)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["generation"] == 2
    assert {factor["factor_id"]: factor["root_role"] for factor in body["factors"]} == {
        "password.one": "individual", "password.two": "individual",
    }
    stale = env.post("/api/identity/factor-policy/commit", json=request)
    assert stale.status_code == 409


def _password_descriptor(root, factor_id, password):
    from tools.network.idkit.root_factor_policy import create_password_factor
    factor, seed = create_password_factor(
        root.public_hex, factor_id, password, iterations=10_000,
    )
    seed[:] = b"\x00" * len(seed)
    return factor



def _policy_armor(root, generation, factors, access, policy):
    from tools.network.idkit.root_factor_policy import build_envelope, emit_armored_envelope
    return emit_armored_envelope(build_envelope(
        root, generation=generation, factors=factors, access=access, policy=policy,
    ))


def _store_policy_identity(env, root, factors, access, policy, generation=1):
    armor = _policy_armor(root, generation, factors, access, policy)
    r = env.post("/api/identity/personal", json={
        "display_name": "Alex", "armored_private_key": armor,
    })
    assert r.status_code == 200, r.text
    return armor


def test_factor_policy_commit_binds_operations_and_candidate(env, root):
    first = _password_descriptor(root, "password.one", "one-password")
    _store_policy_identity(
        env, root, [first], ["password.one"],
        {"op": "factor", "factor_id": "password.one"},
    )
    second = _password_descriptor(root, "password.two", "two-password")
    policy = {"op": "or", "children": [
        {"op": "factor", "factor_id": "password.one"},
        {"op": "factor", "factor_id": "password.two"},
    ]}
    operations = [
        {"op": "enroll_password", "factor": second, "access": True},
        {"op": "set_root_policy", "policy": policy},
    ]
    candidate = _policy_armor(
        root, 2, [first, second], ["password.one", "password.two"], policy,
    )
    body = _policy_commit_body(root, 1, operations, candidate)
    body["operations"] = [*operations, {
        "op": "set_access", "factor_id": "password.two", "enabled": False,
    }]
    # Keep the candidate internally root-signed and consistent with the
    # tampered operation list.  The distinct transition signature must still
    # reject it because that one signature binds the original batch + armor.
    body["candidate_armor"] = _policy_armor(
        root, 2, [first, second], ["password.one"], policy,
    )
    r = env.post("/api/identity/factor-policy/commit", json=body)
    assert r.status_code == 403


def test_factor_metadata_rename_does_not_advance_policy(env, root):
    first = _password_descriptor(root, "password.one", "one-password")
    _store_policy_identity(
        env, root, [first], ["password.one"],
        {"op": "factor", "factor_id": "password.one"},
    )
    r = env.patch("/api/identity/factors/password.one/metadata", json={
        "label": "Travel password", "purpose": "Root recovery while away",
    })
    assert r.status_code == 200, r.text
    view = env.get("/api/identity/factor-policy").json()
    assert view["generation"] == 1
    assert view["factors"][0]["label"] == "Travel password"


def test_factor_recipient_metadata_rename_does_not_advance_policy(env, root):
    from tools.network.idkit.root_factor_policy import (
        FACTOR_RECIPIENT_PURPOSE,
        parse_armored_envelope,
        passkey_factor,
    )
    from tools.network.idkit.sealing import derive_encapsulation_keypair

    _private, recipient_public = derive_encapsulation_keypair(
        b"r" * 32, FACTOR_RECIPIENT_PURPOSE,
    )
    factor = passkey_factor(
        "passkey.icloud", "credential-rename", recipient_public,
        recipient_label="MacBook",
        recipient_created_at="2026-08-24T12:00:00Z",
    )
    original_armor = _store_policy_identity(
        env, root, [factor], ["passkey.icloud"],
        {"op": "factor", "factor_id": "passkey.icloud"},
    )

    response = env.patch(
        "/api/identity/factors/passkey.icloud/recipients/"
        f"{recipient_public}/metadata",
        json={"label": "Work laptop"},
    )
    assert response.status_code == 200, response.text
    view = env.get("/api/identity/factor-policy").json()
    assert view["generation"] == 1
    assert view["factors"][0]["recipients"][0]["label"] == "Work laptop"

    stored_armor = env.get("/api/identity/personal").json()["armored_private_key"]
    assert stored_armor == original_armor
    signed = parse_armored_envelope(stored_armor)
    assert signed["factors"][0]["recipients"][0]["label"] == "MacBook"

    unknown = env.patch(
        "/api/identity/factors/passkey.icloud/recipients/"
        f"{'0' * 64}/metadata",
        json={"label": "Unknown device"},
    )
    assert unknown.status_code == 404


def test_passkey_device_recipient_changes_preserve_one_logical_leaf(env, root):
    from tools.network.idkit.root_factor_policy import (
        FACTOR_RECIPIENT_PURPOSE,
        passkey_factor,
    )
    from tools.network.idkit.sealing import derive_encapsulation_keypair

    password = _password_descriptor(root, "password.one", "one-password")
    _store_policy_identity(
        env, root, [password], ["password.one"],
        {"op": "factor", "factor_id": "password.one"},
    )
    assert _enroll(env, root).status_code == 200
    credential_id = env.get("/api/identity/status").json()["passkeys"][0][
        "credential_id"
    ]
    _private, first_public = derive_encapsulation_keypair(
        b"1" * 32, FACTOR_RECIPIENT_PURPOSE,
    )
    passkey = passkey_factor(
        "passkey.icloud", credential_id, first_public,
        recipient_label="iPhone",
        recipient_created_at="2026-08-24T12:00:00Z",
    )
    policy = {"op": "factor", "factor_id": "password.one"}
    enroll_operations = [{
        "op": "enroll_passkey", "factor": passkey, "access": True,
    }]
    candidate = _policy_armor(
        root, 2, [password, passkey], ["password.one", "passkey.icloud"], policy,
    )
    response = env.post("/api/identity/factor-policy/commit", json=
                        _policy_commit_body(root, 1, enroll_operations, candidate))
    assert response.status_code == 200, response.text

    _private, second_public = derive_encapsulation_keypair(
        b"2" * 32, FACTOR_RECIPIENT_PURPOSE,
    )
    second_recipient = {
        "recipient_public_key": second_public,
        "label": "MacBook",
        "created_at": "2026-08-24T12:01:00Z",
    }
    add_operations = [{
        "op": "add_passkey_recipient",
        "factor_id": "passkey.icloud",
        "recipient": second_recipient,
    }]
    passkey_with_two_devices = dict(passkey)
    passkey_with_two_devices["recipients"] = [
        *passkey["recipients"], second_recipient,
    ]
    candidate = _policy_armor(
        root, 3, [password, passkey_with_two_devices],
        ["password.one", "passkey.icloud"], policy,
    )
    response = env.post("/api/identity/factor-policy/commit", json=
                        _policy_commit_body(root, 2, add_operations, candidate))
    assert response.status_code == 200, response.text
    passkey_view = next(
        factor for factor in response.json()["factors"]
        if factor["factor_id"] == "passkey.icloud"
    )
    assert passkey_view["credential_id"] == credential_id
    assert passkey_view["backup_eligible"] is False
    assert passkey_view["backed_up"] is False
    assert {row["label"] for row in passkey_view["recipients"]} == {
        "iPhone", "MacBook",
    }
