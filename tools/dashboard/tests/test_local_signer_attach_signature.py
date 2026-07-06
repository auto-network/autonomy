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


_ssh_sign_counter = [0]


def _ssh_sign(signing_key_path: Path, payload: bytes) -> str:
    _ssh_sign_counter[0] += 1
    msg_path = signing_key_path.parent / f"msg-{_ssh_sign_counter[0]}.bin"
    msg_path.write_bytes(payload)
    result = _ssh_keygen("-Y", "sign", "-f", str(signing_key_path), "-n", "file", str(msg_path))
    assert result.returncode == 0, result.stderr
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


# ── D3-18: batch cross-wire (L8) ────────────────────────────────────────


def test_D3_18_L8_request_1_signature_rejected_against_request_2_even_with_matching_displayed_hash(
    client, signing_setup, tmp_path,
):
    """Adversarial L8: two independent signing requests (the batch case --
    each commit in a chain gets its own signing_request_id + its own
    canonical_payload_hash bound to its own trusted-store bytes). Submit
    request 1's REAL signature against request 2, with displayed_payload_hash
    deliberately set to request 2's own canonical_payload_hash so the
    string gate is satisfied -- must still be rejected on the crypto gate,
    never accepted."""
    device_id, private, _pub = _register_device(client)

    store = signing_setup["store"]
    payload_1 = signing_setup["canonical_payload"]
    hash_1 = signing_setup["canonical_payload_hash"]
    payload_2 = b"tree def\nparent " + b"a" * 40 + b"\nauthor A <a@example.com> 2 +0000\ncommitter A <a@example.com> 2 +0000\n\nmsg2\n"
    hash_2 = store.put(payload_2)
    assert hash_1 != hash_2, "the two requests must have genuinely different payload bytes"

    _seed_signing_request(signing_request_id="sr-1", workflow_id="wf-1", canonical_payload_hash=hash_1)
    _seed_signing_request(signing_request_id="sr-2", workflow_id="wf-1", canonical_payload_hash=hash_2)

    sig_1 = _ssh_sign(signing_setup["signing_key_path"], payload_1)
    sig_2 = _ssh_sign(signing_setup["signing_key_path"], payload_2)

    nonce_for_2 = _get_nonce(client, signing_request_id="sr-2", device_id=device_id, private_key=private, client_nonce="get-2")

    # The cross-wire attempt: request 1's real signature, submitted
    # against request 2, with the displayed hash deliberately forged to
    # request 2's own hash so the string gate alone would pass it.
    cross_wired = _post_signature(
        client, signing_request_id="sr-2", device_id=device_id, device_private_key=private,
        challenge_nonce=nonce_for_2, displayed_payload_hash=hash_2, armored_signature=sig_1,
    )
    assert cross_wired.status_code == 422
    assert cross_wired.json()["reason"] == "signature_verification_failed"

    conn = cdb._get_conn()
    try:
        row2 = conn.execute("SELECT status FROM commit_signing_requests WHERE signing_request_id = 'sr-2'").fetchone()
    finally:
        conn.close()
    assert row2["status"] == "pending", "the cross-wired attempt must not have signed request 2"

    # Positive control: each request's OWN correctly-matched signature is accepted.
    nonce_for_1 = _get_nonce(client, signing_request_id="sr-1", device_id=device_id, private_key=private, client_nonce="get-1")
    ok_1 = _post_signature(
        client, signing_request_id="sr-1", device_id=device_id, device_private_key=private,
        challenge_nonce=nonce_for_1, displayed_payload_hash=hash_1, armored_signature=sig_1,
    )
    assert ok_1.status_code == 200, ok_1.text

    nonce_for_2_retry = _get_nonce(client, signing_request_id="sr-2", device_id=device_id, private_key=private, client_nonce="get-2-retry")
    ok_2 = _post_signature(
        client, signing_request_id="sr-2", device_id=device_id, device_private_key=private,
        challenge_nonce=nonce_for_2_retry, displayed_payload_hash=hash_2, armored_signature=sig_2,
    )
    assert ok_2.status_code == 200, ok_2.text


# ── D3-19/L13: signature submission for a not-yet-ready member is rejected ─


def test_D3_19_signature_submission_for_not_ready_chain_member_is_rejected(client, signing_setup):
    """A chain member whose canonical_payload_hash is still NULL was never
    GET-fetchable for real, so it never had a nonce minted -- any
    submitted challenge_nonce necessarily fails the existing
    nonce-binding gate. No new POST-side check is needed."""
    device_id, private, _pub = _register_device(client)
    conn = cdb._get_conn()
    try:
        conn.execute(
            "INSERT INTO commit_workflow_events (event_id, workflow_id, event_type, occurred_at, actor_type, repo_slug) "
            "VALUES ('evt-chain', 'wf-chain', 'proposed', 1.0, 'agent_session', 'repo')"
        )
        conn.execute(
            "INSERT INTO commit_workflow_states (workflow_id, repo_slug, status, last_event_id, created_at, updated_at, state_json) "
            "VALUES ('wf-chain', 'repo', 'awaiting_signature', 'evt-chain', 1.0, 1.0, '{}')"
        )
        conn.execute(
            "INSERT INTO commit_signing_requests (signing_request_id, workflow_id, repo_slug, status, "
            "signing_method, trusted_object_store_ref, canonical_payload_hash, "
            "batch_group_id, position_in_batch, batch_size, requested_at) "
            "VALUES ('sr-not-ready', 'wf-chain', 'repo', 'pending', 'ssh', 'store://ref', '', 'batch-1', 3, 3, 1.0)"
        )
        conn.commit()
    finally:
        conn.close()

    sig = _ssh_sign(signing_setup["signing_key_path"], signing_setup["canonical_payload"])
    r = _post_signature(
        client, signing_request_id="sr-not-ready", device_id=device_id, device_private_key=private,
        challenge_nonce="any-nonce-nothing-was-ever-minted",
        displayed_payload_hash=signing_setup["canonical_payload_hash"], armored_signature=sig,
    )
    assert r.status_code == 409
    assert r.json()["reason"] == "stale_nonce"
