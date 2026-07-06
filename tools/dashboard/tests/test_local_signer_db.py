from __future__ import annotations

import sqlite3

import pytest

from tools.dashboard.dao import commit_workflow_db as workflow_db
from tools.dashboard.dao import local_signer_db as db


# ── D3-1 local_signer_devices ─────────────────────────────────────────


def test_devices_table_schema_and_pk(tmp_path):
    path = tmp_path / "local_signer.db"
    db.init_db(path)
    conn = db._get_conn(path)
    try:
        cols = {row["name"]: row for row in conn.execute("PRAGMA table_info(local_signer_devices)")}
        expected = {
            "device_id", "operator_id", "device_label", "public_key", "platform",
            "app_version", "paired_at", "last_seen_at", "revoked_at",
            "revoked_reason", "pairing_ip_hash",
        }
        assert expected <= set(cols)
        assert cols["device_id"]["pk"] == 1
        for not_null_col in ("operator_id", "device_label", "public_key", "platform", "paired_at"):
            assert cols[not_null_col]["notnull"] == 1, not_null_col
    finally:
        conn.close()


def test_active_device_reads_back_with_revoked_at_null(tmp_path):
    path = tmp_path / "local_signer.db"
    db.init_db(path)
    db.insert_device(
        device_id="dev-1", operator_id="op-1", device_label="Jeremy's iPhone",
        public_key="pub-key-bytes", platform="ios-pwa", paired_at=1000.0,
        db_path=path,
    )
    device = db.get_device("dev-1", db_path=path)
    assert device is not None
    assert device.revoked_at is None
    assert device.active is True
    assert device.operator_id == "op-1"


def test_no_device_column_ever_stores_key_material(tmp_path):
    """No column on this table is or can hold a passphrase or private key —
    documented by construction: the schema has no such column at all."""
    path = tmp_path / "local_signer.db"
    db.init_db(path)
    conn = db._get_conn(path)
    try:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(local_signer_devices)")}
    finally:
        conn.close()
    forbidden = {"passphrase", "private_key", "decrypted_key", "secret"}
    assert not (cols & forbidden)


# ── D3-2 local_signer_pairing_requests ────────────────────────────────


def test_pairing_requests_table_schema(tmp_path):
    path = tmp_path / "local_signer.db"
    db.init_db(path)
    conn = db._get_conn(path)
    try:
        cols = {row["name"]: row for row in conn.execute("PRAGMA table_info(local_signer_pairing_requests)")}
        expected = {
            "pairing_id", "device_code", "verifier_hash", "operator_id", "status",
            "created_at", "expires_at", "pending_public_key", "pending_device_meta",
            "verifier_presented", "completed_device_id",
        }
        assert expected <= set(cols)
        assert cols["pairing_id"]["pk"] == 1
        for not_null_col in ("device_code", "verifier_hash", "operator_id", "status", "created_at", "expires_at"):
            assert cols[not_null_col]["notnull"] == 1, not_null_col

        indexes = conn.execute("PRAGMA index_list(local_signer_pairing_requests)").fetchall()
        assert any(idx["unique"] for idx in indexes), "device_code must have a UNIQUE index"
    finally:
        conn.close()


def test_duplicate_device_code_raises_integrity_error(tmp_path):
    path = tmp_path / "local_signer.db"
    db.init_db(path)
    db.insert_pairing_request(
        pairing_id="pr-1", device_code="ABCD1234", verifier_hash="hash-1",
        operator_id="op-1", created_at=1000.0, expires_at=1120.0, db_path=path,
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_pairing_request(
            pairing_id="pr-2", device_code="ABCD1234", verifier_hash="hash-2",
            operator_id="op-1", created_at=1001.0, expires_at=1121.0, db_path=path,
        )


def test_no_plaintext_verifier_column_exists(tmp_path):
    path = tmp_path / "local_signer.db"
    db.init_db(path)
    conn = db._get_conn(path)
    try:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(local_signer_pairing_requests)")}
    finally:
        conn.close()
    assert "verifier" not in cols
    assert "verifier_hash" in cols


# ── D3-3 local_signer_audit_events (append-only) ──────────────────────


def test_audit_events_table_schema(tmp_path):
    path = tmp_path / "local_signer.db"
    db.init_db(path)
    conn = db._get_conn(path)
    try:
        cols = {row["name"]: row for row in conn.execute("PRAGMA table_info(local_signer_audit_events)")}
        expected = {
            "audit_event_id", "occurred_at", "event_type", "device_id",
            "operator_id", "signing_request_id", "kdf_summary_json", "reason",
        }
        assert expected <= set(cols)
        assert cols["audit_event_id"]["pk"] == 1
        assert cols["occurred_at"]["notnull"] == 1
        assert cols["event_type"]["notnull"] == 1
    finally:
        conn.close()


def test_audit_events_reject_update_and_delete(tmp_path):
    path = tmp_path / "local_signer.db"
    db.init_db(path)
    db.append_audit_event(
        audit_event_id="ae-1", occurred_at=1000.0, event_type="pairing_started",
        operator_id="op-1", db_path=path,
    )
    conn = db._get_conn(path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE local_signer_audit_events SET reason = 'tampered' WHERE audit_event_id = 'ae-1'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM local_signer_audit_events WHERE audit_event_id = 'ae-1'")
    finally:
        conn.close()


def test_weak_kdf_rejection_audit_holds_only_algorithm_params(tmp_path):
    path = tmp_path / "local_signer.db"
    db.init_db(path)
    kdf_summary = '{"algorithm": "s2k", "s2k_type": 0, "cipher": "cast5"}'
    db.append_audit_event(
        audit_event_id="ae-2", occurred_at=1000.0,
        event_type="key_provision_rejected_weak_kdf",
        device_id="dev-1", kdf_summary_json=kdf_summary, db_path=path,
    )
    conn = db._get_conn(path)
    try:
        row = conn.execute(
            "SELECT kdf_summary_json FROM local_signer_audit_events WHERE audit_event_id = 'ae-2'"
        ).fetchone()
    finally:
        conn.close()
    assert "passphrase" not in row["kdf_summary_json"]
    assert "-----BEGIN" not in row["kdf_summary_json"]


def test_invalid_event_type_rejected(tmp_path):
    path = tmp_path / "local_signer.db"
    db.init_db(path)
    with pytest.raises(ValueError):
        db.append_audit_event(
            audit_event_id="ae-3", occurred_at=1000.0, event_type="not_a_real_event",
            db_path=path,
        )


# ── D3-4 commit_signing_requests.device_id — revocation query shape ────


def _seed_workflow(conn, workflow_id: str) -> None:
    conn.execute(
        "INSERT INTO commit_workflow_events (event_id, workflow_id, event_type, occurred_at, actor_type, repo_slug) "
        "VALUES (?, ?, 'proposed', 1.0, 'agent_session', 'repo')",
        (f"evt-{workflow_id}", workflow_id),
    )
    conn.execute(
        "INSERT INTO commit_workflow_states (workflow_id, repo_slug, status, last_event_id, created_at, updated_at, state_json) "
        "VALUES (?, 'repo', 'draft', ?, 1.0, 1.0, '{}')",
        (workflow_id, f"evt-{workflow_id}"),
    )


def test_device_id_filtered_query_isolates_requests_sharing_one_encrypted_key_ref(tmp_path):
    path = tmp_path / "local_signer.db"
    workflow_db.init_db(path)
    conn = workflow_db._get_conn(path)
    try:
        _seed_workflow(conn, "wf-1")
        _seed_workflow(conn, "wf-2")
        conn.execute(
            "INSERT INTO commit_signing_requests (signing_request_id, workflow_id, repo_slug, status, "
            "signing_method, trusted_object_store_ref, canonical_payload_hash, device_id, encrypted_key_ref, requested_at) "
            "VALUES ('sr-a','wf-1','repo','pending','ssh','store://ref','hash-a','device-a','key-shared',1.0)"
        )
        conn.execute(
            "INSERT INTO commit_signing_requests (signing_request_id, workflow_id, repo_slug, status, "
            "signing_method, trusted_object_store_ref, canonical_payload_hash, device_id, encrypted_key_ref, requested_at) "
            "VALUES ('sr-b','wf-2','repo','pending','ssh','store://ref','hash-b','device-b','key-shared',1.0)"
        )
        conn.commit()

        rows = conn.execute(
            "SELECT signing_request_id FROM commit_signing_requests WHERE device_id = ?",
            ("device-a",),
        ).fetchall()
        assert [r["signing_request_id"] for r in rows] == ["sr-a"]

        # Same encrypted_key_ref on both rows -- confirms the filter isolates by
        # device_id specifically, not by the (shared) key material reference.
        both = conn.execute(
            "SELECT DISTINCT encrypted_key_ref FROM commit_signing_requests"
        ).fetchall()
        assert [r["encrypted_key_ref"] for r in both] == ["key-shared"]
    finally:
        conn.close()


def test_device_id_column_survives_across_existing_rows(tmp_path):
    path = tmp_path / "local_signer.db"
    workflow_db.init_db(path)
    conn = workflow_db._get_conn(path)
    try:
        _seed_workflow(conn, "wf-1")
        conn.execute(
            "INSERT INTO commit_signing_requests (signing_request_id, workflow_id, repo_slug, status, "
            "signing_method, trusted_object_store_ref, canonical_payload_hash, requested_at) "
            "VALUES ('sr-legacy','wf-1','repo','pending','ssh','store://ref','hash-legacy',1.0)"
        )
        conn.commit()
        row = conn.execute(
            "SELECT device_id FROM commit_signing_requests WHERE signing_request_id = 'sr-legacy'"
        ).fetchone()
        assert row["device_id"] is None
    finally:
        conn.close()
