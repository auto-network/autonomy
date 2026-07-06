"""Uniform commit API error envelope and code registry."""

from __future__ import annotations

from dataclasses import dataclass, field
import uuid
from typing import Any, Mapping

from .types import CommitApiModel, NextAction


def _new_correlation_id() -> str:
    return uuid.uuid4().hex


REDACTION_MASK = "[redacted]"
DEFAULT_REDACTION_RULES: dict[str, str] = {
    "provider_request": REDACTION_MASK,
    "provider_request_body": REDACTION_MASK,
    "token": REDACTION_MASK,
    "passphrase": REDACTION_MASK,
    "decrypted_key_material": REDACTION_MASK,
    "local_signer_secret": REDACTION_MASK,
    "encrypted_key_ciphertext": REDACTION_MASK,
}


def redact(payload: Any, redaction_rules: Mapping[str, str] | None = None) -> Any:
    rules = dict(DEFAULT_REDACTION_RULES)
    if redaction_rules is not None:
        rules.update(redaction_rules)
    if isinstance(payload, dict):
        redacted: dict[str, Any] = {}
        for key, value in payload.items():
            if key in rules:
                redacted[key] = rules[key]
            else:
                redacted[key] = redact(value, redaction_rules=redaction_rules)
        return redacted
    if isinstance(payload, list):
        return [redact(item, redaction_rules=redaction_rules) for item in payload]
    if isinstance(payload, tuple):
        return tuple(redact(item, redaction_rules=redaction_rules) for item in payload)
    return payload


def redaction_misconfigured(operation: str) -> "CommitApiError":
    return commit_api_error(
        "redaction_misconfigured",
        f"missing redaction rule for {operation}",
    )


COMMIT_API_ERROR_HTTP_STATUS: dict[str, int] = {
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

COMMIT_API_ERROR_CODES = frozenset(COMMIT_API_ERROR_HTTP_STATUS)


@dataclass(frozen=True)
class CommitApiError(CommitApiModel):
    code: str
    message: str
    http_status: int
    retryable: bool
    safe_to_display: bool
    details: dict[str, Any]
    next_action: NextAction | None
    correlation_id: str = field(default_factory=_new_correlation_id)

    def __post_init__(self) -> None:
        if self.code not in COMMIT_API_ERROR_CODES:
            raise ValueError(f"unknown commit API error code: {self.code!r}")
        expected_status = COMMIT_API_ERROR_HTTP_STATUS[self.code]
        if self.http_status != expected_status:
            raise ValueError(
                f"{self.code} must use http_status={expected_status}, got {self.http_status}"
            )
        if not self.correlation_id:
            object.__setattr__(self, "correlation_id", _new_correlation_id())

    @classmethod
    def from_code(
        cls,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        next_action: NextAction | None = None,
        retryable: bool | None = None,
        safe_to_display: bool = True,
        correlation_id: str | None = None,
    ) -> "CommitApiError":
        http_status = COMMIT_API_ERROR_HTTP_STATUS[code]
        if retryable is None:
            retryable = code in {
                "idempotency_in_flight",
                "provider_error",
                "drift_detected",
            }
        return cls(
            code=code,
            message=message,
            http_status=http_status,
            retryable=retryable,
            safe_to_display=safe_to_display,
            details=details or {},
            next_action=next_action,
            correlation_id=correlation_id or _new_correlation_id(),
        )

    def to_response(self, *, redaction_rules: Mapping[str, str] | None = None) -> dict[str, Any]:
        return redact(self.to_dict(), redaction_rules=redaction_rules)

    def to_log_payload(self, *, redaction_rules: Mapping[str, str] | None = None) -> dict[str, Any]:
        return self.to_response(redaction_rules=redaction_rules)


def commit_api_error(
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
    next_action: NextAction | None = None,
    retryable: bool | None = None,
    safe_to_display: bool = True,
    correlation_id: str | None = None,
) -> CommitApiError:
    return CommitApiError.from_code(
        code,
        message,
        details=details,
        next_action=next_action,
        retryable=retryable,
        safe_to_display=safe_to_display,
        correlation_id=correlation_id,
    )
