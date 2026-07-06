"""Tests for D3-14 (per-request client auth) and D3-20 (key provisioning)."""

from __future__ import annotations

import base64
import hashlib
import time

import pytest
from starlette.testclient import TestClient

from tools.dashboard.dao import local_signer_db as db
from tools.dashboard import local_signer_routes as routes
from tools.dashboard.local_signer_s2k import S2K_ITERATED_SALTED, S2K_SIMPLE


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _keypair():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization

    private = Ed25519PrivateKey.generate()
    public_bytes = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw,
    )
    return private, _b64(public_bytes)


def _sign(private_key, message: bytes) -> str:
    return _b64(private_key.sign(message))


def _iterated_salted_packet(*, cipher_id: int = 9, hash_id: int = 10, coded_count: int = 192) -> bytes:
    # coded_count=192 -> (16+0) << ((192>>4)+6) = 16 << 18 = 4_194_304 (exactly at floor)
    return bytes([cipher_id, S2K_ITERATED_SALTED, hash_id]) + b"\x00" * 8 + bytes([coded_count])


def _weak_packet() -> bytes:
    return bytes([3, S2K_SIMPLE, 1])  # CAST5, Simple S2K, MD5


@pytest.fixture(autouse=True)
def _local_signer_db_path(tmp_path, monkeypatch):
    path = tmp_path / "local_signer.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db(path)
    yield path


@pytest.fixture
def client(test_app):
    with TestClient(test_app) as c:
        yield c


def _register_device(client) -> tuple[str, object, str]:
    """Pair + approve a device end to end; return (device_id, private_key, public_key_b64)."""
    start = client.post("/api/capabilities/local-signer/v1/pairing/start", json={}).json()
    private, public_b64 = _keypair()
    message = routes._possession_message(start["device_code"])
    r = client.post(
        "/api/capabilities/local-signer/v1/pairing/complete",
        json={
            "device_code": start["device_code"],
            "client_public_key": public_b64,
            "possession_signature": _sign(private, message),
            "platform": "ios-pwa", "app_version": "1.0", "device_label": "Phone",
        },
    )
    assert r.status_code == 200
    decide = client.post(
        f"/api/dashboard/local-signer/pairing/{start['pairing_id']}/decide",
        json={"decision": "approve"},
    )
    assert decide.status_code == 200
    return decide.json()["device_id"], private, public_b64


def _step_up(client, *, device_id, key_id) -> str:
    r = client.post(
        "/api/dashboard/local-signer/key-material/step-up",
        json={"device_id": device_id, "key_id": key_id},
    )
    assert r.status_code == 200, r.text
    return r.json()["step_up_token"]


def _provision(client, *, device_id, private_key, key_id, step_up_token, nonce="n1", issued_at=None):
    issued_at = issued_at if issued_at is not None else time.time()
    message = routes._provision_message(device_id=device_id, key_id=key_id, client_nonce=nonce, issued_at=issued_at)
    return client.post(
        "/api/capabilities/local-signer/v1/key-material/provision",
        json={
            "device_id": device_id, "key_id": key_id, "client_nonce": nonce,
            "issued_at": issued_at, "signature": _sign(private_key, message),
            "step_up_token": step_up_token,
        },
    )


# ── D3-14 per-request client auth ──────────────────────────────────────


def test_verify_device_request_rejects_bad_signature(client):
    device_id, private, _pub = _register_device(client)
    message = b"some message"
    ok, err = routes.verify_device_request(
        device_id=device_id, message=message, signature=_sign(private, b"different message"),
        client_nonce="n1", now=time.time(),
    )
    assert ok is None
    assert err == "signature_invalid"


def test_verify_device_request_rejects_revoked_device_per_call(client):
    device_id, private, _pub = _register_device(client)
    message = b"some message"
    ok, err = routes.verify_device_request(
        device_id=device_id, message=message, signature=_sign(private, message),
        client_nonce="n1", now=time.time(),
    )
    assert ok is not None and err is None  # succeeds while active

    db.revoke_device(device_id, revoked_at=time.time(), reason="lost")
    ok2, err2 = routes.verify_device_request(
        device_id=device_id, message=message, signature=_sign(private, message),
        client_nonce="n2", now=time.time(),
    )
    assert ok2 is None
    assert err2 == "device_not_found_or_revoked"


def test_verify_device_request_rejects_replayed_nonce(client):
    device_id, private, _pub = _register_device(client)
    message = b"some message"
    now = time.time()
    ok1, err1 = routes.verify_device_request(
        device_id=device_id, message=message, signature=_sign(private, message), client_nonce="dup", now=now,
    )
    assert ok1 is not None and err1 is None
    ok2, err2 = routes.verify_device_request(
        device_id=device_id, message=message, signature=_sign(private, message), client_nonce="dup", now=now,
    )
    assert ok2 is None
    assert err2 == "nonce_replayed"


# ── D3-20 key-material provisioning ────────────────────────────────────


def test_provision_requires_both_client_auth_and_step_up(client, monkeypatch):
    device_id, private, _pub = _register_device(client)
    monkeypatch.setattr(
        routes, "KEY_MATERIAL_PROVIDER",
        lambda key_id: routes.KeyMaterial(
            ciphertext=_iterated_salted_packet(), public_material=b"pub", key_fingerprint="fp-1",
        ),
    )

    # No step-up token at all.
    r_no_stepup = client.post(
        "/api/capabilities/local-signer/v1/key-material/provision",
        json={
            "device_id": device_id, "key_id": "key-1", "client_nonce": "n1",
            "issued_at": time.time(),
            "signature": _sign(private, routes._provision_message(
                device_id=device_id, key_id="key-1", client_nonce="n1", issued_at=time.time(),
            )),
            "step_up_token": "",
        },
    )
    assert r_no_stepup.status_code == 400  # missing required field

    # Valid step-up but wrong device signature.
    token = _step_up(client, device_id=device_id, key_id="key-1")
    r_bad_sig = client.post(
        "/api/capabilities/local-signer/v1/key-material/provision",
        json={
            "device_id": device_id, "key_id": "key-1", "client_nonce": "n2",
            "issued_at": time.time(), "signature": _sign(private, b"wrong message"),
            "step_up_token": token,
        },
    )
    assert r_bad_sig.status_code == 403


def test_provision_accepts_floor_compliant_blob(client, monkeypatch):
    device_id, private, _pub = _register_device(client)
    monkeypatch.setattr(
        routes, "KEY_MATERIAL_PROVIDER",
        lambda key_id: routes.KeyMaterial(
            ciphertext=_iterated_salted_packet(), public_material=b"pub-bytes", key_fingerprint="fp-abc",
        ),
    )
    token = _step_up(client, device_id=device_id, key_id="key-1")
    r = _provision(client, device_id=device_id, private_key=private, key_id="key-1", step_up_token=token)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["key_fingerprint"] == "fp-abc"
    assert body["kdf_params"]["s2k_count"] == 4_194_304
    assert "encrypted_key_blob" in body


def test_provision_rejects_weak_kdf_no_blob_in_response(client, monkeypatch):
    device_id, private, _pub = _register_device(client)
    monkeypatch.setattr(
        routes, "KEY_MATERIAL_PROVIDER",
        lambda key_id: routes.KeyMaterial(
            ciphertext=_weak_packet(), public_material=b"pub", key_fingerprint="fp-weak",
        ),
    )
    token = _step_up(client, device_id=device_id, key_id="key-1")
    r = _provision(client, device_id=device_id, private_key=private, key_id="key-1", step_up_token=token)
    assert r.status_code == 422
    body = r.json()
    assert body["status"] == "rejected_weak_kdf"
    assert "encrypted_key_blob" not in body
    assert body["floor_violated"]


def test_provision_audit_event_has_no_ciphertext_or_ciphertext_leak(client, monkeypatch):
    device_id, private, _pub = _register_device(client)
    monkeypatch.setattr(
        routes, "KEY_MATERIAL_PROVIDER",
        lambda key_id: routes.KeyMaterial(
            ciphertext=_iterated_salted_packet(), public_material=b"pub", key_fingerprint="fp-1",
        ),
    )
    token = _step_up(client, device_id=device_id, key_id="key-1")
    _provision(client, device_id=device_id, private_key=private, key_id="key-1", step_up_token=token)

    conn = db._get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM local_signer_audit_events WHERE event_type = 'key_provisioned'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert "encrypted_key_blob" not in row["kdf_summary_json"]
    assert base64.b64encode(_iterated_salted_packet()).decode() not in row["kdf_summary_json"]


def test_step_up_token_is_single_use_and_scoped(client, monkeypatch):
    device_id, private, _pub = _register_device(client)
    monkeypatch.setattr(
        routes, "KEY_MATERIAL_PROVIDER",
        lambda key_id: routes.KeyMaterial(
            ciphertext=_iterated_salted_packet(), public_material=b"pub", key_fingerprint="fp-1",
        ),
    )
    token = _step_up(client, device_id=device_id, key_id="key-1")
    r1 = _provision(client, device_id=device_id, private_key=private, key_id="key-1", step_up_token=token, nonce="n1")
    assert r1.status_code == 200

    # Reusing the same token for a second provision of the same key fails.
    r2 = _provision(client, device_id=device_id, private_key=private, key_id="key-1", step_up_token=token, nonce="n2")
    assert r2.status_code == 403

    # A token minted for key-1 cannot be used for key-2.
    token_key1 = _step_up(client, device_id=device_id, key_id="key-1")
    r3 = _provision(client, device_id=device_id, private_key=private, key_id="key-2", step_up_token=token_key1, nonce="n3")
    assert r3.status_code == 403


def test_step_up_requires_operator_not_agent_session(client, monkeypatch):
    monkeypatch.setattr(
        "tools.dashboard.dao.auth_db.resolve_token",
        lambda token_hash: "auto-fake-session",
    )
    r = client.post(
        "/api/dashboard/local-signer/key-material/step-up",
        json={"device_id": "dev-1", "key_id": "key-1"},
        headers={"Authorization": "Bearer fake-agent-token"},
    )
    assert r.status_code == 403


# ── D3-22: register the operator's public signing key (DN3<->DN5 seam) ─


def test_successful_provision_registers_operators_signing_key(client, monkeypatch):
    device_id, private, device_public_key = _register_device(client)
    signing_public_material = b"THIS-IS-THE-SIGNING-KEYS-PUBLIC-HALF-NOT-THE-PAIRING-KEY"
    monkeypatch.setattr(
        routes, "KEY_MATERIAL_PROVIDER",
        lambda key_id: routes.KeyMaterial(
            ciphertext=_iterated_salted_packet(), public_material=signing_public_material,
            key_fingerprint="fp-signing-1", signing_kind="gpg",
        ),
    )
    calls = []
    monkeypatch.setattr(
        routes, "REGISTER_VERIFICATION_KEY",
        lambda **kwargs: calls.append(kwargs),
    )

    token = _step_up(client, device_id=device_id, key_id="key-1")
    r = _provision(client, device_id=device_id, private_key=private, key_id="key-1", step_up_token=token)
    assert r.status_code == 200

    assert len(calls) == 1
    call = calls[0]
    assert call["operator_id"] == db.get_device(device_id).operator_id
    assert call["signing_kind"] == "gpg"
    # The registered key is the signing key's public half — NOT the
    # device's own pairing keypair.
    assert call["public_material"] == signing_public_material
    assert call["public_material"] != device_public_key.encode()


def test_weak_kdf_rejection_never_registers_a_key(client, monkeypatch):
    device_id, private, _pub = _register_device(client)
    monkeypatch.setattr(
        routes, "KEY_MATERIAL_PROVIDER",
        lambda key_id: routes.KeyMaterial(
            ciphertext=_weak_packet(), public_material=b"pub", key_fingerprint="fp-weak",
        ),
    )
    calls = []
    monkeypatch.setattr(routes, "REGISTER_VERIFICATION_KEY", lambda **kwargs: calls.append(kwargs))

    token = _step_up(client, device_id=device_id, key_id="key-1")
    r = _provision(client, device_id=device_id, private_key=private, key_id="key-1", step_up_token=token)
    assert r.status_code == 422
    assert calls == []


def test_device_revocation_does_not_affect_verification_key_registration_call(client, monkeypatch):
    """Revoking the device after a successful provision must not retract
    the already-made registration call — this test documents that the
    two are independent actions with independent lifecycles (the actual
    keystore's persistence is DN5's concern; this only proves local
    signer code never tries to undo a registration on device revoke)."""
    device_id, private, _pub = _register_device(client)
    monkeypatch.setattr(
        routes, "KEY_MATERIAL_PROVIDER",
        lambda key_id: routes.KeyMaterial(
            ciphertext=_iterated_salted_packet(), public_material=b"signing-pub", key_fingerprint="fp-1",
        ),
    )
    calls = []
    monkeypatch.setattr(routes, "REGISTER_VERIFICATION_KEY", lambda **kwargs: calls.append(kwargs))
    token = _step_up(client, device_id=device_id, key_id="key-1")
    _provision(client, device_id=device_id, private_key=private, key_id="key-1", step_up_token=token)
    assert len(calls) == 1

    db.revoke_device(device_id, revoked_at=time.time(), reason="lost")
    # No deregistration call of any kind exists in this module — the
    # absence of one is the point (revocation never touches the
    # verification-key store, per DN3 §5's device-scoped revocation).
    assert len(calls) == 1
