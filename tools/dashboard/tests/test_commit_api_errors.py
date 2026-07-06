from __future__ import annotations

from tools.dashboard.commit_api.errors import (
    COMMIT_API_ERROR_CODES,
    COMMIT_API_ERROR_HTTP_STATUS,
    CommitApiError,
    commit_api_error,
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
    err = commit_api_error("invalid_request", "bad input")
    assert err.correlation_id
    assert isinstance(err, CommitApiError)
    assert err.http_status == 400
    assert err.to_response()["code"] == "invalid_request"

