"""Tests for the local signer pairing routes (DN3, D3-5..D3-11).

Uses the same ``test_app``/``TestClient`` harness as other dashboard route
tests, with ``local_signer_db``'s ``DB_PATH`` pointed at a fresh temp file
per test for isolation (the tables live in the shared commit-workflow
store, but each test gets its own file).
"""

from __future__ import annotations

import base64
import hashlib
import time

import pytest
from starlette.testclient import TestClient

from tools.dashboard.dao import local_signer_db as db
from tools.dashboard import local_signer_routes as routes


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _keypair():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )
    from cryptography.hazmat.primitives import serialization

    private = Ed25519PrivateKey.generate()
    public = private.public_key()
    public_bytes = public.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
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


@pytest.fixture
def client(test_app):
    with TestClient(test_app) as c:
        yield c


def _start(client) -> dict:
    r = client.post("/api/capabilities/local-signer/v1/pairing/start", json={})
    assert r.status_code == 200, r.text
    return r.json()


def _complete(client, *, device_code, private_key, public_key_b64, verifier=None, label="Phone"):
    message = routes._possession_message(device_code)
    return client.post(
        "/api/capabilities/local-signer/v1/pairing/complete",
        json={
            "device_code": device_code,
            "verifier": verifier,
            "client_public_key": public_key_b64,
            "possession_signature": _sign(private_key, message),
            "platform": "ios-pwa",
            "app_version": "1.0",
            "device_label": label,
        },
    )


# ── D3-5 pairing/start ────────────────────────────────────────────────


def test_pairing_start_returns_expected_shape(client):
    body = _start(client)
    assert set(body) == {"pairing_id", "device_code", "qr_payload", "expires_at", "poll_interval_seconds"}
    assert body["poll_interval_seconds"] == 3
    assert "verifier" not in body  # no top-level readable verifier field
    row = db.get_pairing_request(body["pairing_id"])
    assert row is not None
    assert row.status == "pending"
    assert row.operator_id == routes._current_operator_id()
    assert abs((row.expires_at - row.created_at) - 120) < 1


def test_pairing_start_verifier_only_in_qr_payload_hashed_in_db(client):
    body = _start(client)
    row = db.get_pairing_request(body["pairing_id"])
    # qr_payload contains a verifier= param; the DB only ever holds its hash.
    assert "verifier=" in body["qr_payload"]
    verifier = body["qr_payload"].split("verifier=")[1]
    assert row.verifier_hash == hashlib.sha256(verifier.encode()).hexdigest()
    assert row.verifier_hash != verifier


# ── D3-6 / L1 — reject agent-session pairing ──────────────────────────


def test_pairing_start_rejects_agent_session_bearer(client, monkeypatch):
    monkeypatch.setattr(
        "tools.dashboard.dao.auth_db.resolve_token",
        lambda token_hash: "auto-fake-session",
    )
    r = client.post(
        "/api/capabilities/local-signer/v1/pairing/start",
        json={},
        headers={"Authorization": "Bearer fake-agent-token"},
    )
    assert r.status_code == 403
    conn = db._get_conn()
    try:
        count = conn.execute("SELECT COUNT(*) AS n FROM local_signer_pairing_requests").fetchone()["n"]
    finally:
        conn.close()
    assert count == 0


# ── D3-7/D3-8 pairing/complete ─────────────────────────────────────────


def test_pairing_complete_reaches_awaiting_confirm_never_returns_device_id(client):
    start = _start(client)
    private, public_b64 = _keypair()
    r = _complete(client, device_code=start["device_code"], private_key=private, public_key_b64=public_b64)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"pairing_id": start["pairing_id"], "status": "awaiting_operator_confirm"}
    assert "device_id" not in body
    row = db.get_pairing_request(start["pairing_id"])
    assert row.status == "awaiting_operator_confirm"
    assert row.pending_public_key == public_b64


def test_pairing_complete_verifier_present_vs_absent(client):
    start = _start(client)
    verifier = start["qr_payload"].split("verifier=")[1]

    private1, pub1 = _keypair()
    r1 = _complete(client, device_code=start["device_code"], private_key=private1, public_key_b64=pub1, verifier=verifier)
    assert r1.status_code == 200
    row = db.get_pairing_request(start["pairing_id"])
    assert row.verifier_presented == 1

    start2 = _start(client)
    private2, pub2 = _keypair()
    r2 = _complete(client, device_code=start2["device_code"], private_key=private2, public_key_b64=pub2, verifier=None)
    assert r2.status_code == 200
    row2 = db.get_pairing_request(start2["pairing_id"])
    assert row2.verifier_presented == 0


def test_pairing_complete_no_device_row_created(client):
    start = _start(client)
    private, public_b64 = _keypair()
    _complete(client, device_code=start["device_code"], private_key=private, public_key_b64=public_b64)
    conn = db._get_conn()
    try:
        count = conn.execute("SELECT COUNT(*) AS n FROM local_signer_devices").fetchone()["n"]
    finally:
        conn.close()
    assert count == 0


def test_pairing_complete_possession_check_failed_no_state_change(client):
    """L12: swapped public key means the signature doesn't verify against
    it — reject, and the row must stay pending (never reach
    awaiting_operator_confirm)."""
    start = _start(client)
    private_a, _pub_a = _keypair()
    _private_b, pub_b = _keypair()  # attacker's own key, submitted instead
    r = _complete(client, device_code=start["device_code"], private_key=private_a, public_key_b64=pub_b)
    assert r.status_code == 403
    assert r.json()["error"] == "possession_check_failed"
    row = db.get_pairing_request(start["pairing_id"])
    assert row.status == "pending"


# ── D3-9 / L2 / L3 — replay + expiry ───────────────────────────────────


def test_replay_on_non_pending_code_rejected_and_audited(client):
    start = _start(client)
    private, public_b64 = _keypair()
    r1 = _complete(client, device_code=start["device_code"], private_key=private, public_key_b64=public_b64)
    assert r1.status_code == 200

    r2 = _complete(client, device_code=start["device_code"], private_key=private, public_key_b64=public_b64)
    assert r2.status_code == 409
    assert r2.json()["error"] == "already_decided"

    conn = db._get_conn()
    try:
        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM local_signer_audit_events WHERE event_type = 'pairing_replay_rejected'"
        ).fetchone()
    finally:
        conn.close()
    assert rows["n"] == 1


def test_expired_code_rejected(client, monkeypatch):
    start = _start(client)
    private, public_b64 = _keypair()
    monkeypatch.setattr(time, "time", lambda: start["expires_at"] + 1)
    r = _complete(client, device_code=start["device_code"], private_key=private, public_key_b64=public_b64)
    assert r.status_code == 410
    assert r.json()["error"] == "expired"


def test_L2b_concurrent_completions_exactly_one_wins(client, tmp_path):
    """Two concurrent completions racing the same pending code: exactly one
    reaches awaiting_operator_confirm, never two."""
    import threading

    start = _start(client)
    results = []
    lock = threading.Lock()

    def attempt():
        private, public_b64 = _keypair()
        r = _complete(client, device_code=start["device_code"], private_key=private, public_key_b64=public_b64)
        with lock:
            results.append(r.status_code)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == [200, 409]
    row = db.get_pairing_request(start["pairing_id"])
    assert row.status == "awaiting_operator_confirm"


def test_stale_pairing_sweep_expires_awaiting_confirm_not_completed(client):
    start = _start(client)
    private, public_b64 = _keypair()
    _complete(client, device_code=start["device_code"], private_key=private, public_key_b64=public_b64)
    db.expire_stale_pairing_requests(now=start["expires_at"] + 1)
    row = db.get_pairing_request(start["pairing_id"])
    assert row.status == "expired"


# ── D3-10 decide (approve/deny) ─────────────────────────────────────────


def test_decide_approve_creates_device_and_completes(client):
    start = _start(client)
    private, public_b64 = _keypair()
    _complete(client, device_code=start["device_code"], private_key=private, public_key_b64=public_b64, label="My Phone")

    r = client.post(
        f"/api/dashboard/local-signer/pairing/{start['pairing_id']}/decide",
        json={"decision": "approve"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["device_label"] == "My Phone"
    device = db.get_device(body["device_id"])
    assert device is not None
    assert device.operator_id == routes._current_operator_id()
    assert device.public_key == public_b64
    row = db.get_pairing_request(start["pairing_id"])
    assert row.status == "completed"
    assert row.completed_device_id == body["device_id"]


def test_decide_deny_creates_no_device(client):
    start = _start(client)
    private, public_b64 = _keypair()
    _complete(client, device_code=start["device_code"], private_key=private, public_key_b64=public_b64)

    r = client.post(
        f"/api/dashboard/local-signer/pairing/{start['pairing_id']}/decide",
        json={"decision": "deny"},
    )
    assert r.status_code == 200
    assert r.json() == {"status": "denied"}
    row = db.get_pairing_request(start["pairing_id"])
    assert row.status == "denied"
    conn = db._get_conn()
    try:
        count = conn.execute("SELECT COUNT(*) AS n FROM local_signer_devices").fetchone()["n"]
    finally:
        conn.close()
    assert count == 0


def test_decide_by_different_operator_denied(client, monkeypatch):
    start = _start(client)
    private, public_b64 = _keypair()
    _complete(client, device_code=start["device_code"], private_key=private, public_key_b64=public_b64)

    monkeypatch.setattr(routes, "_current_operator_id", lambda: "someone-else")
    r = client.post(
        f"/api/dashboard/local-signer/pairing/{start['pairing_id']}/decide",
        json={"decision": "approve"},
    )
    assert r.status_code == 403


def test_second_decide_on_terminal_pairing_rejected(client):
    start = _start(client)
    private, public_b64 = _keypair()
    _complete(client, device_code=start["device_code"], private_key=private, public_key_b64=public_b64)
    r1 = client.post(
        f"/api/dashboard/local-signer/pairing/{start['pairing_id']}/decide",
        json={"decision": "approve"},
    )
    assert r1.status_code == 200
    r2 = client.post(
        f"/api/dashboard/local-signer/pairing/{start['pairing_id']}/decide",
        json={"decision": "approve"},
    )
    assert r2.status_code == 409


# ── D3-11 / L10 / L11 — the fixed pairing gap, regression ──────────────


def test_L10_device_code_alone_never_registers_device(client):
    """Code-only (no verifier) completion reaches awaiting_operator_confirm
    with verifier_presented=0 and registers NO device — even if the
    operator never acts and the TTL elapses."""
    start = _start(client)
    private, public_b64 = _keypair()
    r = _complete(client, device_code=start["device_code"], private_key=private, public_key_b64=public_b64, verifier=None)
    assert r.status_code == 200
    row = db.get_pairing_request(start["pairing_id"])
    assert row.status == "awaiting_operator_confirm"
    assert row.verifier_presented == 0

    db.expire_stale_pairing_requests(now=start["expires_at"] + 1)
    row_after = db.get_pairing_request(start["pairing_id"])
    assert row_after.status == "expired"
    conn = db._get_conn()
    try:
        count = conn.execute("SELECT COUNT(*) AS n FROM local_signer_devices").fetchone()["n"]
    finally:
        conn.close()
    assert count == 0


def test_L10_positive_control_verifier_present_still_no_auto_approve(client):
    start = _start(client)
    verifier = start["qr_payload"].split("verifier=")[1]
    private, public_b64 = _keypair()
    r = _complete(client, device_code=start["device_code"], private_key=private, public_key_b64=public_b64, verifier=verifier)
    assert r.status_code == 200
    row = db.get_pairing_request(start["pairing_id"])
    assert row.verifier_presented == 1
    assert row.status == "awaiting_operator_confirm"  # not auto-completed
    conn = db._get_conn()
    try:
        count = conn.execute("SELECT COUNT(*) AS n FROM local_signer_devices").fetchone()["n"]
    finally:
        conn.close()
    assert count == 0


def test_L11_deny_then_replay_is_rejected(client):
    start = _start(client)
    private, public_b64 = _keypair()
    _complete(client, device_code=start["device_code"], private_key=private, public_key_b64=public_b64)
    client.post(
        f"/api/dashboard/local-signer/pairing/{start['pairing_id']}/decide",
        json={"decision": "deny"},
    )
    r = _complete(client, device_code=start["device_code"], private_key=private, public_key_b64=public_b64)
    assert r.status_code == 409
    assert r.json()["error"] == "already_decided"
