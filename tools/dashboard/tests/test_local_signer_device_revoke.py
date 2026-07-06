"""Tests for D3-23 (device-scoped revocation endpoint) and D3-24's
adversarial regressions (per-call revocation re-read, two-device
isolation)."""

from __future__ import annotations

import base64
import time

import pytest
from starlette.testclient import TestClient

from tools.dashboard.dao import commit_workflow_db as cdb
from tools.dashboard.dao import local_signer_db as db
from tools.dashboard import local_signer_routes as routes


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


@pytest.fixture(autouse=True)
def _local_signer_db_path(tmp_path, monkeypatch):
    path = tmp_path / "local_signer.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db(path)
    yield path


@pytest.fixture(autouse=True)
def _workflow_db_path(tmp_path, monkeypatch):
    path = tmp_path / "commit_workflow.db"
    monkeypatch.setattr(cdb, "DB_PATH", path)
    cdb.init_db(path)
    yield path


@pytest.fixture
def client(test_app):
    with TestClient(test_app) as c:
        yield c


def _register_device(client, label="Phone") -> tuple[str, object, str]:
    start = client.post("/api/capabilities/local-signer/v1/pairing/start", json={}).json()
    private, public_b64 = _keypair()
    message = routes._possession_message(start["device_code"])
    r = client.post(
        "/api/capabilities/local-signer/v1/pairing/complete",
        json={
            "device_code": start["device_code"],
            "client_public_key": public_b64,
            "possession_signature": _sign(private, message),
            "platform": "ios-pwa", "app_version": "1.0", "device_label": label,
        },
    )
    assert r.status_code == 200
    decide = client.post(
        f"/api/dashboard/local-signer/pairing/{start['pairing_id']}/decide",
        json={"decision": "approve"},
    )
    assert decide.status_code == 200
    return decide.json()["device_id"], private, public_b64


def _seed_pending_signing_request(*, signing_request_id, workflow_id, device_id):
    conn = cdb._get_conn()
    try:
        event_id = f"evt-{workflow_id}"
        conn.execute(
            "INSERT OR IGNORE INTO commit_workflow_events (event_id, workflow_id, event_type, occurred_at, actor_type, repo_slug) "
            "VALUES (?, ?, 'proposed', 1.0, 'agent_session', 'repo')",
            (event_id, workflow_id),
        )
        conn.execute(
            "INSERT OR IGNORE INTO commit_workflow_states (workflow_id, repo_slug, status, last_event_id, created_at, updated_at, state_json) "
            "VALUES (?, 'repo', 'awaiting_signature', ?, 1.0, 1.0, '{}')",
            (workflow_id, event_id),
        )
        conn.execute(
            "INSERT INTO commit_signing_requests (signing_request_id, workflow_id, repo_slug, status, "
            "signing_method, trusted_object_store_ref, canonical_payload_hash, device_id, encrypted_key_ref, requested_at) "
            "VALUES (?, ?, 'repo', 'pending', 'ssh', 'store://ref', 'hash-1', ?, 'shared-key-ref', 1.0)",
            (signing_request_id, workflow_id, device_id),
        )
        conn.commit()
    finally:
        conn.close()


def _signing_request_status(signing_request_id: str) -> str:
    conn = cdb._get_conn()
    try:
        row = conn.execute(
            "SELECT status FROM commit_signing_requests WHERE signing_request_id = ?", (signing_request_id,),
        ).fetchone()
    finally:
        conn.close()
    return row["status"]


def test_D3_23_revoke_sets_revoked_at_and_fails_the_devices_own_pending_requests(client):
    device_id, _private, _pub = _register_device(client)
    _seed_pending_signing_request(signing_request_id="sr-1", workflow_id="wf-1", device_id=device_id)

    r = client.post(
        f"/api/dashboard/local-signer/key-material/{device_id}/revoke",
        json={"reason": "device lost"},
    )
    assert r.status_code == 200, r.text
    assert "revoked_at" in r.json()

    device = db.get_device(device_id)
    assert device.revoked_at is not None
    assert device.revoked_reason == "device lost"
    assert _signing_request_status("sr-1") == "failed"


def test_D3_23_agent_session_cannot_revoke(client, monkeypatch):
    device_id, _private, _pub = _register_device(client)
    monkeypatch.setattr(routes, "_is_agent_session_request", lambda request: True)

    r = client.post(f"/api/dashboard/local-signer/key-material/{device_id}/revoke", json={"reason": "x"})
    assert r.status_code == 403

    device = db.get_device(device_id)
    assert device.revoked_at is None


def test_D3_23_verification_key_registration_is_untouched_by_revoke(client, monkeypatch):
    """The endpoint itself must never call any deregistration path --
    revoking a device is device-scoped, the operator's verification key
    is not the device's to revoke."""
    device_id, _private, _pub = _register_device(client)
    deregister_calls = []
    if hasattr(routes, "DEREGISTER_VERIFICATION_KEY"):
        monkeypatch.setattr(routes, "DEREGISTER_VERIFICATION_KEY", lambda **kwargs: deregister_calls.append(kwargs))

    r = client.post(f"/api/dashboard/local-signer/key-material/{device_id}/revoke", json={"reason": "lost"})
    assert r.status_code == 200

    assert deregister_calls == []


def test_D3_24_two_devices_sharing_one_key_ref_revocation_is_isolated(client):
    """The core blast-radius test: revoking device A must fail only A's
    pending requests and leave device B's completely untouched, even
    though both share the same encrypted_key_ref."""
    device_a, _priv_a, _pub_a = _register_device(client, label="Phone A")
    device_b, _priv_b, _pub_b = _register_device(client, label="Phone B")
    _seed_pending_signing_request(signing_request_id="sr-a", workflow_id="wf-a", device_id=device_a)
    _seed_pending_signing_request(signing_request_id="sr-b", workflow_id="wf-b", device_id=device_b)

    r = client.post(f"/api/dashboard/local-signer/key-material/{device_a}/revoke", json={"reason": "lost"})
    assert r.status_code == 200

    assert db.get_device(device_a).revoked_at is not None
    assert db.get_device(device_b).revoked_at is None  # NOT over-broad
    assert _signing_request_status("sr-a") == "failed"
    assert _signing_request_status("sr-b") == "pending"  # untouched


def test_D3_24_revoked_mid_session_rejected_on_both_get_and_post(client, tmp_path, monkeypatch):
    """A device revoked while it still holds live challenge material must
    be rejected on both GET and POST -- revoked_at is re-read fresh on
    each exact call, never a decision cached from an earlier one."""
    from tools.dashboard.services.trusted_git_object_store import ContentAddressedStore
    import subprocess

    signing_key_path = tmp_path / "operator_key"
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(signing_key_path), "-q"], check=True)
    store = ContentAddressedStore(tmp_path / "store")
    monkeypatch.setattr(routes, "TRUSTED_OBJECT_STORE", store)
    monkeypatch.setattr(routes, "VERIFICATION_KEY_PROVIDER", lambda *, operator_id, signing_kind: "unused")
    payload = b"payload bytes"
    payload_hash = store.put(payload)

    device_id, private, _pub = _register_device(client)
    _seed_pending_signing_request(signing_request_id="sr-1", workflow_id="wf-1", device_id=device_id)
    conn = cdb._get_conn()
    try:
        conn.execute(
            "UPDATE commit_signing_requests SET canonical_payload_hash = ? WHERE signing_request_id = 'sr-1'",
            (payload_hash,),
        )
        conn.commit()
    finally:
        conn.close()

    # Fetch while still live -- holds a real challenge_nonce.
    message = routes._fetch_request_message(device_id=device_id, signing_request_id="sr-1", client_nonce="n1")
    get_ok = client.get(
        "/api/capabilities/local-signer/v1/requests/sr-1",
        params={"device_id": device_id, "client_nonce": "n1", "signature": _sign(private, message)},
    )
    assert get_ok.status_code == 200, get_ok.text
    nonce = get_ok.json()["challenge_nonce"]

    client.post(f"/api/dashboard/local-signer/key-material/{device_id}/revoke", json={"reason": "compromised"})

    # GET now rejected.
    message2 = routes._fetch_request_message(device_id=device_id, signing_request_id="sr-1", client_nonce="n2")
    get_after = client.get(
        "/api/capabilities/local-signer/v1/requests/sr-1",
        params={"device_id": device_id, "client_nonce": "n2", "signature": _sign(private, message2)},
    )
    assert get_after.status_code == 403

    # POST (using the nonce fetched before revocation) also rejected.
    import hashlib
    path = "/api/capabilities/local-signer/v1/requests/sr-1/signature"
    body_hash = hashlib.sha256(routes._json_dumps({
        "displayed_payload_hash": payload_hash, "armored_signature": "sig",
    }).encode()).hexdigest()
    envelope = routes._attach_envelope_message(
        challenge_nonce=nonce, device_id=device_id, signing_request_id="sr-1",
        http_method="POST", path=path, body_hash=body_hash, issued_at="2026-01-01T00:00:00Z",
    )
    post_after = client.post(path, json={
        "device_id": device_id, "challenge_nonce": nonce, "displayed_payload_hash": payload_hash,
        "armored_signature": "sig", "issued_at": "2026-01-01T00:00:00Z",
        "auth_signature": _sign(private, envelope),
    })
    assert post_after.status_code == 403
