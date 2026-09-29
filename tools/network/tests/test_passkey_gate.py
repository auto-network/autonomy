"""The dashboard's passkey gate helper (tools/network/passkey_gate.py).

Driven with a software authenticator (P-256, 'none' attestation, UP|UV)
so py_webauthn verifies for real: enrollment under the one-time token,
login with the enrolled credential, and every refusal the gate must make.
"""

from __future__ import annotations

import hashlib
import json
import struct
import time

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from starlette.testclient import TestClient

from tools.network import clock
from tools.network import passkey_gate as gate

RP_ID = "dashboard.alice-25dacd12af16373e566c.serve.auto.network"
ORIGIN = f"https://{RP_ID}"
TOKEN = "one-time-token-for-the-test"


# ── software authenticator ─────────────────────────────────────────────

def _cose_p256(private_key) -> bytes:
    nums = private_key.public_key().public_numbers()
    return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: nums.x.to_bytes(32, "big"), -3: nums.y.to_bytes(32, "big")})


def _attestation(private_key, challenge: str, *, rp_id=RP_ID, origin=ORIGIN,
                 cred_id=b"gate-credential-0001", flags=0x45, cred_type="webauthn.create") -> dict:
    auth_data = (hashlib.sha256(rp_id.encode()).digest() + bytes([flags]) + struct.pack(">I", 0)
                 + b"\x00" * 16 + struct.pack(">H", len(cred_id)) + cred_id + _cose_p256(private_key))
    client_data = json.dumps({"type": cred_type, "challenge": challenge, "origin": origin,
                              "crossOrigin": False}).encode()
    return {
        "id": gate._b64url(cred_id), "rawId": gate._b64url(cred_id), "type": "public-key",
        "authenticatorAttachment": "platform", "clientExtensionResults": {},
        "response": {"clientDataJSON": gate._b64url(client_data),
                     "attestationObject": gate._b64url(cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})),
                     "transports": ["internal"]},
    }


def _assertion(private_key, challenge: str, *, rp_id=RP_ID, origin=ORIGIN,
               cred_id=b"gate-credential-0001", flags=0x05, sign_count=1) -> dict:
    auth_data = hashlib.sha256(rp_id.encode()).digest() + bytes([flags]) + struct.pack(">I", sign_count)
    client_data = json.dumps({"type": "webauthn.get", "challenge": challenge, "origin": origin,
                              "crossOrigin": False}).encode()
    signature = private_key.sign(auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()))
    return {
        "id": gate._b64url(cred_id), "rawId": gate._b64url(cred_id), "type": "public-key",
        "authenticatorAttachment": "platform", "clientExtensionResults": {},
        "response": {"clientDataJSON": gate._b64url(client_data), "authenticatorData": gate._b64url(auth_data),
                     "signature": gate._b64url(signature)},
    }


# ── runtime directory ──────────────────────────────────────────────────

@pytest.fixture
def runtime(tmp_path):
    (tmp_path / "cookie-secret").write_text("11" * 32)
    (tmp_path / "helper-secret").write_text("helper-secret-hex")
    _write_record(tmp_path, credentials=[], enrollment=None)
    return tmp_path


def _write_record(directory, *, credentials, enrollment):
    (directory / "gate.json").write_text(json.dumps({
        "rp_id": RP_ID, "origin": ORIGIN, "dashboard_upstream": "10.0.0.5:8081",
        "credentials": credentials, "enrollment": enrollment,
    }))


def _open_enrollment(directory, *, token=TOKEN, expires_in=600, credentials=()):
    _write_record(directory, credentials=list(credentials), enrollment={
        "open": True, "token_sha256": gate.token_sha256(token), "expires_at": time.time() + expires_in,
    })


@pytest.fixture
def dashboard():
    """Records what the helper hands to the dashboard and closes enrollment
    the way the dashboard does."""
    calls: list = []

    def post(record, secret, path, payload):
        calls.append((secret, path, payload))
        return {"ok": True}

    return calls, post


def _client(runtime, post):
    return TestClient(gate.build_app(runtime, post_dashboard=post), base_url=ORIGIN)


def _enroll(client, runtime, key, *, token=TOKEN):
    options = client.post("/oauth2/enroll/options", json={"token": token}).json()["options"]
    credential = _attestation(key, options["challenge"])
    return client.post("/oauth2/enroll/verify", json={"token": token, "credential": credential})


def _enrolled_record(runtime, calls, sign_count=0):
    registered = calls[-1][2]
    _write_record(runtime, credentials=[{
        "credential_id": registered["credential_id"], "public_key": registered["public_key"],
        "sign_count": sign_count, "transports": registered["transports"],
    }], enrollment=None)


# ── forward-auth ───────────────────────────────────────────────────────

def test_auth_refuses_without_a_cookie_and_the_login_page_says_nothing_is_enrolled(runtime, dashboard):
    client = _client(runtime, dashboard[1])
    assert client.get("/oauth2/auth").status_code == 401
    page = client.get("/oauth2/start", params={"rd": ORIGIN + "/beads"})
    assert page.status_code == 200
    assert "No passkey enrolled" in page.text
    assert client.post("/oauth2/login/options", json={}).status_code == 409


def test_cookie_is_bound_to_the_key_and_its_expiry(runtime):
    key = bytes.fromhex("11" * 32)
    value = gate.mint_cookie(key, now=1000.0)
    assert gate.cookie_valid(key, value, now=1000.0 + clock.PASSKEY_GATE_SESSION_TTL_S - 1)
    assert not gate.cookie_valid(key, value, now=1000.0 + clock.PASSKEY_GATE_SESSION_TTL_S)
    assert not gate.cookie_valid(bytes.fromhex("22" * 32), value, now=1000.0)
    expiry, nonce, signature = value.split(".")
    assert not gate.cookie_valid(key, f"{int(expiry) + 1}.{nonce}.{signature}", now=1000.0)
    assert not gate.cookie_valid(key, None) and not gate.cookie_valid(key, "garbage")


# ── enrollment ─────────────────────────────────────────────────────────

def test_enrollment_needs_the_open_unexpired_token(runtime, dashboard):
    client = _client(runtime, dashboard[1])
    assert client.get("/oauth2/enroll", params={"token": TOKEN}).status_code == 403  # never opened
    _open_enrollment(runtime)
    assert client.get("/oauth2/enroll", params={"token": "wrong"}).status_code == 403
    assert client.get("/oauth2/enroll", params={"token": TOKEN}).status_code == 200
    assert client.post("/oauth2/enroll/options", json={"token": "wrong"}).status_code == 403
    _open_enrollment(runtime, expires_in=-1)
    assert client.get("/oauth2/enroll", params={"token": TOKEN}).status_code == 403
    assert client.post("/oauth2/enroll/options", json={"token": TOKEN}).status_code == 403


def test_enrollment_registers_one_passkey_through_the_dashboard_and_signs_the_browser_in(runtime, dashboard):
    calls, post = dashboard
    client = _client(runtime, post)
    _open_enrollment(runtime)
    key = ec.generate_private_key(ec.SECP256R1())
    response = _enroll(client, runtime, key)
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "redirect": "/"}
    secret, path, payload = calls[-1]
    assert secret == "helper-secret-hex" and path == gate.REGISTERED_PATH
    assert payload["token"] == TOKEN and payload["rp_id"] == RP_ID
    assert payload["credential_id"] == gate._b64url(b"gate-credential-0001")
    assert payload["transports"] == ["internal"] and payload["sign_count"] == 0
    assert gate.COOKIE_NAME in response.cookies
    assert client.get("/oauth2/auth").status_code == 200
    # The dashboard closed enrollment: the same token is dead.
    _enrolled_record(runtime, calls)
    assert client.get("/oauth2/enroll", params={"token": TOKEN}).status_code == 403


def test_enrollment_is_refused_when_the_dashboard_refuses_or_the_ceremony_is_wrong(runtime, dashboard):
    calls, _post = dashboard
    _open_enrollment(runtime)
    key = ec.generate_private_key(ec.SECP256R1())

    def refusing(record, secret, path, payload):
        return {"ok": False, "error": "token already used"}

    client = _client(runtime, refusing)
    refused = _enroll(client, runtime, key)
    assert refused.status_code == 502 and "token already used" in refused.json()["error"]
    assert gate.COOKIE_NAME not in refused.cookies
    # A challenge is single-use and must belong to this gate's origin.
    client = _client(runtime, dashboard[1])
    options = client.post("/oauth2/enroll/options", json={"token": TOKEN}).json()["options"]
    wrong_origin = _attestation(key, options["challenge"], origin="https://evil.example")
    assert client.post("/oauth2/enroll/verify", json={"token": TOKEN, "credential": wrong_origin}).status_code == 400
    replay = _attestation(key, options["challenge"])
    assert client.post("/oauth2/enroll/verify", json={"token": TOKEN, "credential": replay}).status_code == 400
    assert calls == []


# ── login ──────────────────────────────────────────────────────────────

def test_login_with_the_enrolled_passkey_sets_the_cookie_and_records_the_sign_count(runtime, dashboard):
    calls, post = dashboard
    client = _client(runtime, post)
    _open_enrollment(runtime)
    key = ec.generate_private_key(ec.SECP256R1())
    assert _enroll(client, runtime, key).status_code == 200
    _enrolled_record(runtime, calls)
    fresh = _client(runtime, post)
    assert fresh.get("/oauth2/auth").status_code == 401
    page = fresh.get("/oauth2/start", params={"rd": ORIGIN + "/beads"})
    assert "Continue with passkey" in page.text and ORIGIN + "/beads" in page.text
    options = fresh.post("/oauth2/login/options", json={}).json()["options"]
    # Discoverable login: the public page names no credential id.
    assert not options.get("allowCredentials")
    assert options["userVerification"] == "required"
    verified = fresh.post("/oauth2/login/verify", json={
        "rd": ORIGIN + "/beads", "credential": _assertion(key, options["challenge"], sign_count=7)})
    assert verified.status_code == 200, verified.text
    assert verified.json() == {"ok": True, "redirect": ORIGIN + "/beads"}
    assert fresh.get("/oauth2/auth").status_code == 200
    assert calls[-1][1] == gate.SIGN_COUNT_PATH and calls[-1][2]["sign_count"] == 7


def test_login_refusals(runtime, dashboard):
    calls, post = dashboard
    client = _client(runtime, post)
    _open_enrollment(runtime)
    key = ec.generate_private_key(ec.SECP256R1())
    assert _enroll(client, runtime, key).status_code == 200
    _enrolled_record(runtime, calls, sign_count=5)
    fresh = _client(runtime, post)
    options = fresh.post("/oauth2/login/options", json={}).json()["options"]
    other = ec.generate_private_key(ec.SECP256R1())
    wrong_key = fresh.post("/oauth2/login/verify", json={"credential": _assertion(other, options["challenge"], sign_count=6)})
    assert wrong_key.status_code == 403 and gate.COOKIE_NAME not in wrong_key.cookies
    options = fresh.post("/oauth2/login/options", json={}).json()["options"]
    unknown = fresh.post("/oauth2/login/verify", json={"credential": _assertion(key, options["challenge"], cred_id=b"other", sign_count=6)})
    assert unknown.status_code == 403
    options = fresh.post("/oauth2/login/options", json={}).json()["options"]
    cloned = fresh.post("/oauth2/login/verify", json={"credential": _assertion(key, options["challenge"], sign_count=5)})
    assert cloned.status_code == 403  # sign count did not advance: a cloned authenticator
    stale = fresh.post("/oauth2/login/verify", json={"credential": _assertion(key, options["challenge"], sign_count=9)})
    assert stale.status_code == 400  # the challenge was consumed above
    options = fresh.post("/oauth2/login/options", json={}).json()["options"]
    elsewhere = fresh.post("/oauth2/login/verify", json={
        "rd": "https://evil.example/", "credential": _assertion(key, options["challenge"], sign_count=9)})
    assert elsewhere.status_code == 200 and elsewhere.json()["redirect"] == "/"
    assert fresh.get("/oauth2/auth").status_code == 200


# ── compose service ────────────────────────────────────────────────────

def test_helper_service_runs_the_dashboards_own_image_and_code_read_only(tmp_path):
    service = gate.render_helper_service(
        tmp_path, "rev1", image="ghcr.io/x/autonomy-node@sha256:" + "a" * 64, port=4181,
        app_mount={"type": "volume", "source": "autonomy_autonomy-code"},
    )
    assert service["image"].startswith("ghcr.io/x/autonomy-node@sha256:")
    assert service["entrypoint"] == ["python3", "-m", "tools.network.passkey_gate"]
    assert service["command"] == ["--runtime", "/run/gate", "--port", "4181"]
    assert service["network_mode"] == "service:service-gateway"
    assert service["read_only"] is True and service["cap_drop"] == ["ALL"]
    assert "ports" not in service
    assert service["volumes"] == [
        {"type": "bind", "source": str(tmp_path), "target": "/run/gate", "read_only": True},
        {"type": "volume", "source": "autonomy_autonomy-code", "target": "/app", "read_only": True},
    ]
    assert service["labels"] == {"autonomy.auth-config": "rev1"}
    bare = gate.render_helper_service(tmp_path, "rev1", image="autonomy-node:local", port=4181)
    assert len(bare["volumes"]) == 1


def test_pages_send_no_referrer_and_no_cache(runtime, dashboard):
    """The enrollment URL carries the token; the post-enroll redirect must
    not hand it to the gateway's log as a Referer."""
    client = _client(runtime, dashboard[1])
    _open_enrollment(runtime)
    for path, params in (("/oauth2/start", {}), ("/oauth2/enroll", {"token": TOKEN}), ("/oauth2/enroll", {"token": "x"})):
        page = client.get(path, params=params)
        assert page.headers["referrer-policy"] == "no-referrer"
        assert page.headers["cache-control"] == "no-store"
        assert '<meta name="referrer" content="no-referrer">' in page.text


def test_a_rotated_cookie_key_refuses_every_earlier_cookie(runtime, dashboard):
    """Revoking a passkey rotates the cookie key on the dashboard side; the
    helper reads the key per request, so a session minted before the
    rotation is refused at once (a lost phone, U4)."""
    calls, post = dashboard
    client = _client(runtime, post)
    _open_enrollment(runtime)
    key = ec.generate_private_key(ec.SECP256R1())
    assert _enroll(client, runtime, key).status_code == 200
    assert client.get("/oauth2/auth").status_code == 200
    (runtime / "cookie-secret").write_text("22" * 32)
    assert client.get("/oauth2/auth").status_code == 401


# ── one passkey for the dashboard and every Personal service (auto-z98nc, D12) ──

SUFFIX = "alice-25dacd12af16373e566c.serve.auto.network"
SERVICE = "docs." + SUFFIX
SERVICE_ORIGIN = f"https://{SERVICE}"


def _write_shared_record(directory, *, credentials, enrollment, origins=(ORIGIN, SERVICE_ORIGIN)):
    (directory / "gate.json").write_text(json.dumps({
        "rp_id": SUFFIX, "origin": ORIGIN, "origins": list(origins), "dashboard_upstream": "10.0.0.5:8081",
        "credentials": credentials, "enrollment": enrollment,
    }))


def test_the_relying_party_is_the_operators_own_suffix():
    assert gate.gate_rp_id("dashboard.alice-x.serve.auto.network") == "alice-x.serve.auto.network"
    assert gate.gate_rp_id("docs.alice-x.serve.auto.network") == "alice-x.serve.auto.network"
    assert gate.gate_rp_id("themes.autonomy.example.com") == "autonomy.example.com"
    # never the relay's shared base, never shorter than the host itself
    assert gate.gate_rp_id("alice.serve.auto.network") == "alice.serve.auto.network"
    assert gate.gate_rp_id("dashboard.local") == "dashboard.local"
    assert gate.gate_rp_id("localhost") == "localhost"
    assert gate._rp_covers("alice-x.serve.auto.network", "docs.alice-x.serve.auto.network")
    assert not gate._rp_covers("alice-x.serve.auto.network", "docs.bob-y.serve.auto.network")
    assert not gate._rp_covers("alice-x.serve.auto.network", "evil-alice-x.serve.auto.network")


def test_one_passkey_enrolled_at_the_suffix_signs_in_at_the_dashboard_and_at_a_service(runtime, dashboard):
    calls, post = dashboard
    _write_shared_record(runtime, credentials=[], enrollment={
        "open": True, "token_sha256": gate.token_sha256(TOKEN), "expires_at": time.time() + 600})
    key = ec.generate_private_key(ec.SECP256R1())
    at_dashboard = _client(runtime, post)
    options = at_dashboard.post("/oauth2/enroll/options", json={"token": TOKEN}).json()["options"]
    assert options["rp"]["id"] == SUFFIX
    registered = at_dashboard.post("/oauth2/enroll/verify", json={
        "token": TOKEN, "credential": _attestation(key, options["challenge"], rp_id=SUFFIX)})
    assert registered.status_code == 200, registered.text
    assert calls[-1][2]["rp_id"] == SUFFIX
    saved = calls[-1][2]
    _write_shared_record(runtime, credentials=[{
        "credential_id": saved["credential_id"], "public_key": saved["public_key"],
        "sign_count": 0, "transports": saved["transports"], "rp_id": SUFFIX}], enrollment=None)
    # At the service's own hostname: its own login, the same passkey.
    at_service = TestClient(gate.build_app(runtime, post_dashboard=post), base_url=SERVICE_ORIGIN)
    assert at_service.get("/oauth2/auth").status_code == 401
    options = at_service.post("/oauth2/login/options", json={}).json()["options"]
    assert options["rpId"] == SUFFIX
    verified = at_service.post("/oauth2/login/verify", json={
        "rd": SERVICE_ORIGIN + "/app", "credential": _assertion(key, options["challenge"], rp_id=SUFFIX, origin=SERVICE_ORIGIN, sign_count=3)})
    assert verified.status_code == 200, verified.text
    assert verified.json() == {"ok": True, "redirect": SERVICE_ORIGIN + "/app"}
    assert at_service.get("/oauth2/auth").status_code == 200
    # The dashboard's own gate is unchanged: the same passkey, its own cookie.
    fresh = _client(runtime, post)
    assert fresh.get("/oauth2/auth").status_code == 401
    options = fresh.post("/oauth2/login/options", json={}).json()["options"]
    assert fresh.post("/oauth2/login/verify", json={
        "credential": _assertion(key, options["challenge"], rp_id=SUFFIX, origin=ORIGIN, sign_count=4)}).status_code == 200
    assert fresh.get("/oauth2/auth").status_code == 200
    # A redirect to the OTHER gated origin after a login here is not followed.
    options = fresh.post("/oauth2/login/options", json={}).json()["options"]
    crossed = fresh.post("/oauth2/login/verify", json={
        "rd": ORIGIN + "/x", "credential": _assertion(key, options["challenge"], rp_id=SUFFIX, origin=ORIGIN, sign_count=5)})
    assert crossed.json()["redirect"] == ORIGIN + "/x"
    assert fresh.get("/oauth2/start", params={"rd": SERVICE_ORIGIN + "/a"}).text.count(SERVICE_ORIGIN + "/a") == 1


def test_a_passkey_enrolled_for_the_dashboard_hostname_alone_opens_only_the_dashboard(runtime, dashboard):
    """A gate passkey from before services (Home, run 10) was registered for
    the dashboard hostname. It still opens the dashboard; a service address
    says nothing is enrolled for it until a passkey is enrolled at the suffix."""
    calls, post = dashboard
    key = ec.generate_private_key(ec.SECP256R1())
    _open_enrollment(runtime)   # legacy record: rp_id = the dashboard host
    client = _client(runtime, post)
    assert _enroll(client, runtime, key).status_code == 200
    legacy = calls[-1][2]
    _write_shared_record(runtime, credentials=[{
        "credential_id": legacy["credential_id"], "public_key": legacy["public_key"],
        "sign_count": 0, "transports": legacy["transports"], "rp_id": RP_ID}], enrollment=None)
    at_dashboard = _client(runtime, post)
    options = at_dashboard.post("/oauth2/login/options", json={}).json()["options"]
    assert options["rpId"] == RP_ID
    assert at_dashboard.post("/oauth2/login/verify", json={
        "credential": _assertion(key, options["challenge"], sign_count=2)}).status_code == 200
    at_service = TestClient(gate.build_app(runtime, post_dashboard=post), base_url=SERVICE_ORIGIN)
    refused = at_service.post("/oauth2/login/options", json={})
    assert refused.status_code == 409 and "no passkey is enrolled for this address" in refused.json()["error"]


def test_a_host_the_gate_does_not_stand_in_front_of_gets_no_ceremony(runtime, dashboard):
    calls, post = dashboard
    _write_shared_record(runtime, credentials=[{
        "credential_id": "Y3JlZA", "public_key": "cHVi", "sign_count": 0, "transports": [], "rp_id": SUFFIX}],
        enrollment={"open": True, "token_sha256": gate.token_sha256(TOKEN), "expires_at": time.time() + 600})
    elsewhere = TestClient(gate.build_app(runtime, post_dashboard=post), base_url="https://other." + SUFFIX)
    assert elsewhere.post("/oauth2/login/options", json={}).status_code == 403
    assert elsewhere.post("/oauth2/enroll/options", json={"token": TOKEN}).status_code == 403
    # The gateway's forwarded host is what counts, not the socket's Host.
    forwarded = TestClient(gate.build_app(runtime, post_dashboard=post), base_url="http://127.0.0.1:4181")
    options = forwarded.post("/oauth2/login/options", json={}, headers={"X-Forwarded-Host": SERVICE})
    assert options.status_code == 200 and options.json()["options"]["rpId"] == SUFFIX
