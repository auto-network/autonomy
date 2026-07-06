"""Tests for D3-15/16/17/18 (partial) — POST /requests/{id}/signature:
nonce binding, hash-display gate, and cryptographic verification gate.

Uses real SSH-format signing (ssh-keygen -Y sign/verify) for the crypto
gate, same technique as D4-5/D5-1..3 earlier this session -- no gpg
binary in this environment, but SSH commit signing verifies identically
under git verify-commit.
"""

from __future__ import annotations

import base64
import subprocess
import tempfile
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from tools.dashboard.dao import commit_workflow_db as cdb
from tools.dashboard.dao import local_signer_db as db
from tools.dashboard import local_signer_routes as routes
from tools.dashboard.services.trusted_git_object_store import ContentAddressedStore


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


def _ssh_keygen(*args, input_bytes=None):
    return subprocess.run(["ssh-keygen", *args], input=input_bytes, capture_output=True)


def _ssh_sign(signing_key_path: Path, payload: bytes) -> str:
    msg_path = signing_key_path.parent / "msg.bin"
    msg_path.write_bytes(payload)
    _ssh_keygen("-Y", "sign", "-f", str(signing_key_path), "-n", "file", str(msg_path))
    return (msg_path.parent / (msg_path.name + ".sig")).read_text()


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


@pytest.fixture
def signing_setup(tmp_path, monkeypatch):
    """Real SSH signing key (the operator's registered verification key)
    + a real ContentAddressedStore holding the canonical payload bytes
    under their own SHA-256 digest."""
    signing_key_path = tmp_path / "operator_signing_key"
    _ssh_keygen("-t", "ed25519", "-N", "", "-f", str(signing_key_path), "-q")
    public_key_line = signing_key_path.with_suffix(".pub").read_text().strip()

    store = ContentAddressedStore(tmp_path / "trusted_store")
    monkeypatch.setattr(routes, "TRUSTED_OBJECT_STORE", store)
    monkeypatch.setattr(routes, "VERIFICATION_KEY_PROVIDER", lambda *, operator_id, signing_kind: public_key_line)

    canonical_payload = b"tree abc\nauthor A <a@example.com> 1 +0000\ncommitter A <a@example.com> 1 +0000\n\nmsg\n"
    canonical_payload_hash = store.put(canonical_payload)
    return {
        "signing_key_path": signing_key_path,
        "store": store,
        "canonical_payload": canonical_payload,
        "canonical_payload_hash": canonical_payload_hash,
    }


def _register_device(client) -> tuple[str, object, str]:
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


def _seed_signing_request(*, signing_request_id, workflow_id, canonical_payload_hash):
    conn = cdb._get_conn()
    try:
        conn.execute(
            "INSERT INTO commit_workflow_events (event_id, workflow_id, event_type, occurred_at, actor_type, repo_slug) "
            "VALUES ('evt-1', ?, 'proposed', 1.0, 'agent_session', 'repo')",
            (workflow_id,),
        )
        conn.execute(
            "INSERT INTO commit_workflow_states (workflow_id, repo_slug, status, last_event_id, created_at, updated_at, state_json) "
            "VALUES (?, 'repo', 'awaiting_signature', 'evt-1', 1.0, 1.0, '{}')",
            (workflow_id,),
        )
        conn.execute(
            "INSERT INTO commit_signing_requests (signing_request_id, workflow_id, repo_slug, status, "
            "signing_method, trusted_object_store_ref, canonical_payload_hash, requested_at) "
            "VALUES (?, ?, 'repo', 'pending', 'ssh', 'store://ref', ?, 1.0)",
            (signing_request_id, workflow_id, canonical_payload_hash),
        )
        conn.commit()
    finally:
        conn.close()


def _get_nonce(client, *, signing_request_id, device_id, private_key, client_nonce="get-nonce-1"):
    message = routes._fetch_request_message(
        device_id=device_id, signing_request_id=signing_request_id, client_nonce=client_nonce,
    )
    r = client.get(
        f"/api/capabilities/local-signer/v1/requests/{signing_request_id}",
        params={"device_id": device_id, "client_nonce": client_nonce, "signature": _sign(private_key, message)},
    )
    assert r.status_code == 200, r.text
    return r.json()["challenge_nonce"]


def _post_signature(
    client, *, signing_request_id, device_id, device_private_key,
    challenge_nonce, displayed_payload_hash, armored_signature, issued_at="2026-01-01T00:00:00Z",
):
    path = f"/api/capabilities/local-signer/v1/requests/{signing_request_id}/signature"
    import hashlib as _hashlib
    body_hash = _hashlib.sha256(routes._json_dumps({
        "displayed_payload_hash": displayed_payload_hash, "armored_signature": armored_signature,
    }).encode()).hexdigest()
    envelope = routes._attach_envelope_message(
        challenge_nonce=challenge_nonce, device_id=device_id, signing_request_id=signing_request_id,
        http_method="POST", path=path, body_hash=body_hash, issued_at=issued_at,
    )
    auth_signature = _sign(device_private_key, envelope)
    return client.post(path, json={
        "device_id": device_id,
        "challenge_nonce": challenge_nonce,
        "displayed_payload_hash": displayed_payload_hash,
        "armored_signature": armored_signature,
        "issued_at": issued_at,
        "auth_signature": auth_signature,
    })


def test_D3_15_fully_valid_submission_is_accepted(client, signing_setup):
    device_id, private, _pub = _register_device(client)
    _seed_signing_request(
        signing_request_id="sr-1", workflow_id="wf-1",
        canonical_payload_hash=signing_setup["canonical_payload_hash"],
    )
    nonce = _get_nonce(client, signing_request_id="sr-1", device_id=device_id, private_key=private)
    sig = _ssh_sign(signing_setup["signing_key_path"], signing_setup["canonical_payload"])

    r = _post_signature(
        client, signing_request_id="sr-1", device_id=device_id, device_private_key=private,
        challenge_nonce=nonce, displayed_payload_hash=signing_setup["canonical_payload_hash"],
        armored_signature=sig,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "accepted"
    assert body["signed_commit_sha"]

    conn = cdb._get_conn()
    try:
        row = conn.execute("SELECT status FROM commit_signing_requests WHERE signing_request_id = 'sr-1'").fetchone()
    finally:
        conn.close()
    assert row["status"] == "signed"


def test_D3_16_stale_or_wrong_nonce_is_rejected_no_replay(client, signing_setup):
    """L7a + L7b: a replayed (already-consumed) nonce, and a well-formed
    but not-the-one-minted nonce, are both rejected."""
    device_id, private, _pub = _register_device(client)
    _seed_signing_request(
        signing_request_id="sr-1", workflow_id="wf-1",
        canonical_payload_hash=signing_setup["canonical_payload_hash"],
    )
    nonce = _get_nonce(client, signing_request_id="sr-1", device_id=device_id, private_key=private)
    sig = _ssh_sign(signing_setup["signing_key_path"], signing_setup["canonical_payload"])

    # L7b: well-formed, unused nonce that was never actually minted.
    wrong_nonce_resp = _post_signature(
        client, signing_request_id="sr-1", device_id=device_id, device_private_key=private,
        challenge_nonce="never-minted-nonce", displayed_payload_hash=signing_setup["canonical_payload_hash"],
        armored_signature=sig,
    )
    assert wrong_nonce_resp.status_code == 409
    assert wrong_nonce_resp.json()["reason"] == "stale_nonce"

    # A valid submission consumes the real nonce...
    ok = _post_signature(
        client, signing_request_id="sr-1", device_id=device_id, device_private_key=private,
        challenge_nonce=nonce, displayed_payload_hash=signing_setup["canonical_payload_hash"],
        armored_signature=sig,
    )
    assert ok.status_code == 200, ok.text

    # L7a: replaying the SAME already-consumed nonce is rejected too.
    replay = _post_signature(
        client, signing_request_id="sr-1", device_id=device_id, device_private_key=private,
        challenge_nonce=nonce, displayed_payload_hash=signing_setup["canonical_payload_hash"],
        armored_signature=sig,
    )
    assert replay.status_code == 409
    assert replay.json()["reason"] == "stale_nonce"


def test_D3_17_mismatched_displayed_hash_rejected_no_signed_event(client, signing_setup):
    device_id, private, _pub = _register_device(client)
    _seed_signing_request(
        signing_request_id="sr-1", workflow_id="wf-1",
        canonical_payload_hash=signing_setup["canonical_payload_hash"],
    )
    nonce = _get_nonce(client, signing_request_id="sr-1", device_id=device_id, private_key=private)
    sig = _ssh_sign(signing_setup["signing_key_path"], signing_setup["canonical_payload"])

    r = _post_signature(
        client, signing_request_id="sr-1", device_id=device_id, device_private_key=private,
        challenge_nonce=nonce, displayed_payload_hash="0" * 64, armored_signature=sig,
    )
    assert r.status_code == 422
    assert r.json()["reason"] == "hash_mismatch"

    conn = cdb._get_conn()
    try:
        row = conn.execute("SELECT status FROM commit_signing_requests WHERE signing_request_id = 'sr-1'").fetchone()
    finally:
        conn.close()
    assert row["status"] == "pending"


def test_D3_15_crypto_gate_rejects_signature_over_different_bytes_even_with_matching_hash_string(client, signing_setup):
    """The hash-display gate alone is a string check the client controls
    both sides of -- prove the crypto gate is what actually catches a
    signature that verifies over the WRONG bytes even when the displayed
    hash string matches."""
    device_id, private, _pub = _register_device(client)
    _seed_signing_request(
        signing_request_id="sr-1", workflow_id="wf-1",
        canonical_payload_hash=signing_setup["canonical_payload_hash"],
    )
    nonce = _get_nonce(client, signing_request_id="sr-1", device_id=device_id, private_key=private)

    # Sign DIFFERENT bytes than what's actually stored under canonical_payload_hash.
    wrong_bytes_sig = _ssh_sign(signing_setup["signing_key_path"], b"totally different payload bytes")

    r = _post_signature(
        client, signing_request_id="sr-1", device_id=device_id, device_private_key=private,
        challenge_nonce=nonce, displayed_payload_hash=signing_setup["canonical_payload_hash"],
        armored_signature=wrong_bytes_sig,
    )
    assert r.status_code == 422
    assert r.json()["reason"] == "signature_verification_failed"


def test_D3_15_signature_from_untrusted_key_rejected(client, signing_setup, tmp_path):
    """A structurally valid SSH signature, over the RIGHT bytes, but from
    a key that isn't the registered verification key, must be rejected."""
    device_id, private, _pub = _register_device(client)
    _seed_signing_request(
        signing_request_id="sr-1", workflow_id="wf-1",
        canonical_payload_hash=signing_setup["canonical_payload_hash"],
    )
    nonce = _get_nonce(client, signing_request_id="sr-1", device_id=device_id, private_key=private)

    other_key_path = tmp_path / "untrusted_key"
    _ssh_keygen("-t", "ed25519", "-N", "", "-f", str(other_key_path), "-q")
    sig_from_untrusted_key = _ssh_sign(other_key_path, signing_setup["canonical_payload"])

    r = _post_signature(
        client, signing_request_id="sr-1", device_id=device_id, device_private_key=private,
        challenge_nonce=nonce, displayed_payload_hash=signing_setup["canonical_payload_hash"],
        armored_signature=sig_from_untrusted_key,
    )
    assert r.status_code == 422
    assert r.json()["reason"] == "signature_verification_failed"


def test_D3_24_device_revoked_between_get_and_post_is_rejected_on_post(client, signing_setup):
    """Live challenge material fetched before revocation must not still
    work after -- revoked_at is re-read fresh on this exact POST call,
    not a decision cached from the earlier GET."""
    device_id, private, _pub = _register_device(client)
    _seed_signing_request(
        signing_request_id="sr-1", workflow_id="wf-1",
        canonical_payload_hash=signing_setup["canonical_payload_hash"],
    )
    nonce = _get_nonce(client, signing_request_id="sr-1", device_id=device_id, private_key=private)
    sig = _ssh_sign(signing_setup["signing_key_path"], signing_setup["canonical_payload"])

    db.revoke_device(device_id, revoked_at=time.time(), reason="lost")

    r = _post_signature(
        client, signing_request_id="sr-1", device_id=device_id, device_private_key=private,
        challenge_nonce=nonce, displayed_payload_hash=signing_setup["canonical_payload_hash"],
        armored_signature=sig,
    )
    assert r.status_code == 403
    assert r.json()["reason"] == "device_not_found_or_revoked"
