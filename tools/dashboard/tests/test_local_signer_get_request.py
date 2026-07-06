"""Tests for D3-12 (challenge-nonce mint-on-GET) and the D3-13 fields
buildable now (D3-13's canonical_payload_preview is deferred until
commit.request_signature exists and establishes what it stores)."""

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


def _seed_signing_request(*, signing_request_id: str, workflow_id: str, canonical_payload_hash: str = "hash-1"):
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


def _get(client, *, signing_request_id, device_id, private_key, client_nonce="nonce-1"):
    message = routes._fetch_request_message(
        device_id=device_id, signing_request_id=signing_request_id, client_nonce=client_nonce,
    )
    return client.get(
        f"/api/capabilities/local-signer/v1/requests/{signing_request_id}",
        params={"device_id": device_id, "client_nonce": client_nonce, "signature": _sign(private_key, message)},
    )


def test_D3_12_get_returns_a_challenge_nonce_bound_to_the_signing_request(client):
    device_id, private, _pub = _register_device(client)
    _seed_signing_request(signing_request_id="sr-1", workflow_id="wf-1")

    r = _get(client, signing_request_id="sr-1", device_id=device_id, private_key=private, client_nonce="n1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["signing_request_id"] == "sr-1"
    assert body["workflow_id"] == "wf-1"
    assert body["challenge_nonce"]

    stored = db.get_current_challenge_nonce("sr-1")
    assert stored is not None
    assert stored["nonce"] == body["challenge_nonce"]


def test_D3_12_second_get_overwrites_the_current_nonce(client):
    device_id, private, _pub = _register_device(client)
    _seed_signing_request(signing_request_id="sr-1", workflow_id="wf-1")

    first = _get(client, signing_request_id="sr-1", device_id=device_id, private_key=private, client_nonce="n1")
    assert first.status_code == 200, first.text
    second = _get(client, signing_request_id="sr-1", device_id=device_id, private_key=private, client_nonce="n2")
    assert second.status_code == 200, second.text

    assert first.json()["challenge_nonce"] != second.json()["challenge_nonce"]
    stored = db.get_current_challenge_nonce("sr-1")
    assert stored["nonce"] == second.json()["challenge_nonce"]


def test_D3_12_single_commit_request_has_null_batch_fields(client):
    device_id, private, _pub = _register_device(client)
    _seed_signing_request(signing_request_id="sr-1", workflow_id="wf-1")

    r = _get(client, signing_request_id="sr-1", device_id=device_id, private_key=private)
    body = r.json()
    assert body["batch_group_id"] is None
    assert body["position_in_batch"] is None
    assert body["batch_size"] is None
    assert body["batch_status"] is None


def test_D3_12_missing_signing_request_is_404(client):
    device_id, private, _pub = _register_device(client)

    r = _get(client, signing_request_id="no-such-request", device_id=device_id, private_key=private)
    assert r.status_code == 404


def test_D3_12_wrong_device_signature_is_rejected(client):
    device_id, _private, _pub = _register_device(client)
    _other_private, _other_pub = _keypair()
    _seed_signing_request(signing_request_id="sr-1", workflow_id="wf-1")

    r = _get(client, signing_request_id="sr-1", device_id=device_id, private_key=_other_private)
    assert r.status_code == 403


def test_D3_12_revoked_device_is_rejected(client):
    device_id, private, _pub = _register_device(client)
    db.revoke_device(device_id, revoked_at=time.time(), reason="lost")
    _seed_signing_request(signing_request_id="sr-1", workflow_id="wf-1")

    r = _get(client, signing_request_id="sr-1", device_id=device_id, private_key=private)
    assert r.status_code == 403


def test_D3_12_no_separate_mint_endpoint_exists(client):
    """D3-12's acceptance criterion: GET /requests/{id} is the ONLY place a
    signing-flow nonce is minted -- there is no dedicated mint route."""
    paths = {route.path for route in routes.ROUTES}
    nonce_specific_routes = {p for p in paths if "nonce" in p.lower() or "challenge" in p.lower()}
    assert nonce_specific_routes == set()


# ── D3-19/L13: sequential chain batch (waiting_on_predecessor) ─────────


def _seed_batch_signing_request(
    *, signing_request_id, workflow_id, batch_group_id, position_in_batch, batch_size,
    canonical_payload_hash="",
):
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
            "signing_method, trusted_object_store_ref, canonical_payload_hash, "
            "batch_group_id, position_in_batch, batch_size, requested_at) "
            "VALUES (?, ?, 'repo', 'pending', 'ssh', 'store://ref', ?, ?, ?, ?, 1.0)",
            (signing_request_id, workflow_id, canonical_payload_hash, batch_group_id, position_in_batch, batch_size),
        )
        conn.commit()
    finally:
        conn.close()


def test_D3_19_not_yet_ready_chain_member_reports_waiting_on_predecessor(client):
    device_id, private, _pub = _register_device(client)
    _seed_batch_signing_request(
        signing_request_id="sr-3", workflow_id="wf-chain", batch_group_id="batch-1",
        position_in_batch=3, batch_size=3, canonical_payload_hash="",
    )

    r = _get(client, signing_request_id="sr-3", device_id=device_id, private_key=private)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["batch_status"] == "waiting_on_predecessor"
    assert body["canonical_payload_hash"] is None
    assert body["challenge_nonce"] is None
    assert body["batch_group_id"] == "batch-1"
    assert body["position_in_batch"] == 3
    assert body["batch_size"] == 3

    # Never a 404 -- it's a real ordered member, just not ready.
    assert r.status_code != 404
    # And no nonce was actually persisted for it.
    assert db.get_current_challenge_nonce("sr-3") is None


def test_D3_19_ready_batch_member_reports_null_batch_status_with_real_fields(client):
    device_id, private, _pub = _register_device(client)
    _seed_batch_signing_request(
        signing_request_id="sr-1", workflow_id="wf-chain", batch_group_id="batch-1",
        position_in_batch=1, batch_size=3, canonical_payload_hash="hash-1",
    )

    r = _get(client, signing_request_id="sr-1", device_id=device_id, private_key=private)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["batch_status"] is None
    assert body["canonical_payload_hash"] == "hash-1"
    assert body["challenge_nonce"]
    assert body["batch_group_id"] == "batch-1"
    assert body["position_in_batch"] == 1
    assert body["batch_size"] == 3
