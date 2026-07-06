from __future__ import annotations

from tools.dashboard.commit_api.errors import (
    COMMIT_API_ERROR_CODES,
    COMMIT_API_ERROR_HTTP_STATUS,
    CommitApiError,
    commit_api_error,
    redact,
    redaction_misconfigured,
)


def test_error_codes_complete_with_http_status():
    expected = {
        "unauthenticated": 401,
        "scope_mismatch": 403,
        "operator_proof_required": 403,
        "route_not_trusted": 403,
        "workflow_not_found": 404,
        "approval_not_found": 404,
        "signing_request_not_found": 404,
        "invalid_request": 400,
        "invalid_transition": 409,
        "policy_incoherent": 409,
        "policy_changed_requires_reproposal": 409,
        "drift_detected": 409,
        "idempotency_conflict": 409,
        "idempotency_in_flight": 425,
        "duplicate_active_workflow": 409,
        "signature_verification_failed": 422,
        "ref_update_rejected": 409,
        "provider_error": 502,
        "redaction_misconfigured": 500,
        "approval_stale_requires_reapproval": 409,
    }
    assert COMMIT_API_ERROR_CODES == set(expected)
    assert COMMIT_API_ERROR_HTTP_STATUS == expected


def test_error_has_correlation_id():
    err = commit_api_error(
        "provider_error",
        "bad input",
        details={
            "provider_request": {"token": "secret-token", "body": "payload"},
            "nested": [{"passphrase": "s3cr3t"}],
            "encrypted_key_ciphertext": "ciphertext",
        },
    )
    assert err.correlation_id
    assert isinstance(err, CommitApiError)
    assert err.http_status == 502
    response = err.to_response()
    assert response["code"] == "provider_error"
    assert response["details"]["provider_request"] == "[redacted]"
    assert response["details"]["nested"][0]["passphrase"] == "[redacted]"
    assert response["details"]["encrypted_key_ciphertext"] == "[redacted]"
    assert "secret-token" not in repr(response)


def test_redaction_misconfigured_helper_returns_error():
    err = redaction_misconfigured("credential-bearing operation")
    assert err.code == "redaction_misconfigured"
    assert err.http_status == 500


def test_redact_nested_payload_masks_secret_keys():
    payload = {
        "outer": {
            "token": "abc",
            "body": {"passphrase": "def"},
        },
        "items": [{"local_signer_secret": "ghi"}],
    }
    redacted = redact(payload)
    assert redacted["outer"]["token"] == "[redacted]"
    assert redacted["outer"]["body"]["passphrase"] == "[redacted]"
    assert redacted["items"][0]["local_signer_secret"] == "[redacted]"
