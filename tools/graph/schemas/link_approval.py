"""Organization-homed Link approval application records.

Central approval authority remains personal-homed.  These two raw
organization Settings are the Links application's frozen input and durable
execution result.  They deliberately use the Central approval id only as the
row key; the payload never repeats it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from typing import Any
from urllib.parse import urlsplit

from .network_identity import TARGET_TYPES
from .registry import (
    SchemaValidationError,
    SettingSchema,
    field,
    home,
    keyed_per_entity,
    publication_band,
)


LINK_APPROVAL_INTENT_SET_ID = "autonomy.network.link-approval-intent"
LINK_APPROVAL_INTENT_REVISION = 1
LINK_APPROVAL_RESULT_SET_ID = "autonomy.network.link-approval-result"
LINK_APPROVAL_RESULT_REVISION = 1

_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_SAFE_CODE_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_OPERATIONS = ("publish", "revoke")
_RESULT_STATES = ("succeeded", "failed")
_VIA = ("tunnel-control", "registry-http")
_MAX_LINK_TTL_SECONDS = 365 * 24 * 60 * 60
_FORBIDDEN_NAMES = frozenset(
    {
        "password",
        "private_key",
        "private_seed",
        "session_cookie",
        "origin_proof",
        "invite_token",
        "invitation_token",
        "fragment_secret",
        "browser_credential",
    }
)
_FORBIDDEN_NAME_PARTS = ("password", "private", "secret", "cookie", "origin_proof", "invite_token")


def _require_hex(payload: dict, name: str, pattern: re.Pattern[str]) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise SchemaValidationError(
            f"{name!r} must be exactly {32 if pattern is _HEX32_RE else 64} "
            "lowercase hexadecimal characters"
        )
    return value


def _require_finite_time(payload: dict, name: str) -> float:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SchemaValidationError(f"{name!r} must be a finite Unix timestamp")
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise SchemaValidationError(f"{name!r} must be a finite Unix timestamp") from exc
    if not math.isfinite(out) or out < 0:
        raise SchemaValidationError(f"{name!r} must be a finite Unix timestamp")
    return out


def _utf8_size(value: str, *, path: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeError as exc:
        raise SchemaValidationError(f"{path} must be valid UTF-8 text") from exc


def _canonical_digest(value: Any) -> str:
    try:
        from tools.network.idkit import canonical_json

        return hashlib.sha256(canonical_json(value)).hexdigest()
    except Exception as exc:
        raise SchemaValidationError("Link approval digest input is not canonical JSON") from exc


def link_registry_input_digest(registry_input: dict) -> str:
    """Digest the exact public registry wire object frozen by Links."""
    return _canonical_digest(registry_input)


def link_local_intent_digest(
    *,
    target: dict,
    binding: dict,
    review: dict,
    local_intent: dict,
    origin_destination_id: str,
) -> str:
    """Bind every organization-local fact without disclosing it externally."""
    return _canonical_digest(
        [
            "autonomy.link.local-intent",
            1,
            target,
            binding,
            review,
            local_intent,
            origin_destination_id,
        ]
    )


def link_revoke_operand_digest(token: str) -> str:
    """Domain-separated digest of the organization-local revoke bearer."""
    return _canonical_digest(["autonomy.link.operand", 1, "revoke", token])


def _validate_public_json(value: Any, *, path: str, depth: int = 0) -> int:
    """Bound frozen application facts and reject secret-shaped fields."""
    if depth > 8:
        raise SchemaValidationError(f"{path} exceeds the maximum nesting depth")
    if value is None or isinstance(value, (str, bool)):
        if isinstance(value, str) and _utf8_size(value, path=path) > 4096:
            raise SchemaValidationError(f"{path} contains oversized text")
        return 1
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            numeric = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise SchemaValidationError(f"{path} contains a non-finite number") from exc
        if not math.isfinite(numeric):
            raise SchemaValidationError(f"{path} contains a non-finite number")
        return 1
    if isinstance(value, list):
        if len(value) > 64:
            raise SchemaValidationError(f"{path} contains too many list members")
        total = 1
        for index, item in enumerate(value):
            total += _validate_public_json(item, path=f"{path}[{index}]", depth=depth + 1)
        if total > 512:
            raise SchemaValidationError(f"{path} contains too many values")
        return total
    if isinstance(value, dict):
        if len(value) > 64:
            raise SchemaValidationError(f"{path} contains too many object members")
        total = 1
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 128:
                raise SchemaValidationError(f"{path} contains an invalid object key")
            normalized_key = key.lower().replace("-", "_")
            if normalized_key in _FORBIDDEN_NAMES or any(
                part in normalized_key for part in _FORBIDDEN_NAME_PARTS
            ):
                raise SchemaValidationError(f"{path} must not contain secret field {key!r}")
            total += _validate_public_json(item, path=f"{path}.{key}", depth=depth + 1)
        if total > 512:
            raise SchemaValidationError(f"{path} contains too many values")
        return total
    raise SchemaValidationError(f"{path} contains unsupported JSON type {type(value).__name__}")


@publication_band(min="raw", max="raw")
@home("organization")
@keyed_per_entity(key_strategy="approval_id")
class LinkApprovalIntentV1(SettingSchema):
    """Frozen organization-local Link input, keyed by Central approval id."""

    set_id = LINK_APPROVAL_INTENT_SET_ID
    schema_revision = LINK_APPROVAL_INTENT_REVISION

    operation: str = field(required=True, enum=list(_OPERATIONS))
    operation_id: str = field(required=True)
    target: dict = field(required=True)
    binding: dict = field(required=True)
    review: dict = field(required=True)
    registry_input: dict = field(required=True)
    registry_input_digest: str = field(required=True)
    local_intent: dict = field(required=True)
    local_intent_digest: str = field(required=True)
    origin_destination_id: str = field(required=True)
    origin_proof_commitment: str = field(required=True)
    operand_digest: str = field(required=False)
    revoke_token: str = field(required=False)
    invite_ref: str = field(required=False)

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        operation = payload.get("operation")
        _require_hex(payload, "operation_id", _HEX64_RE)
        _require_hex(payload, "registry_input_digest", _HEX64_RE)
        _require_hex(payload, "local_intent_digest", _HEX64_RE)
        _require_hex(payload, "origin_proof_commitment", _HEX64_RE)
        _require_hex(payload, "origin_destination_id", _HEX64_RE)
        for name in ("target", "binding", "review", "registry_input", "local_intent"):
            value = payload.get(name)
            if not isinstance(value, dict):
                raise SchemaValidationError(f"{name!r} must be an object")
            _validate_public_json(value, path=name)
        target = payload["target"]
        target_uuid = target.get("target_uuid")
        target_type = target.get("target_type")
        if not isinstance(target_uuid, str) or _UUID_RE.fullmatch(target_uuid) is None:
            raise SchemaValidationError("target.target_uuid must be a canonical UUID")
        if target_type not in TARGET_TYPES:
            raise SchemaValidationError("target.target_type is outside the registered vocabulary")
        try:
            encoded_size = len(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            )
        except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError) as exc:
            raise SchemaValidationError("Link approval intent is not canonical JSON") from exc
        if encoded_size > 65536:
            raise SchemaValidationError("Link approval intent exceeds 65536 bytes")

        registry_input = payload["registry_input"]
        allowed_wire = {
            "operation_id", "target_uuid", "target_type", "meta", "invite_ref",
            "source_expires_at_ms",
        }
        unknown_wire = set(registry_input) - allowed_wire
        if unknown_wire:
            raise SchemaValidationError(
                f"registry_input carries local-only or unknown fields: {sorted(unknown_wire)}"
            )
        if registry_input.get("operation_id") != payload["operation_id"]:
            raise SchemaValidationError("registry_input.operation_id must match operation_id")
        if not hmac.compare_digest(
            payload["registry_input_digest"],
            link_registry_input_digest(registry_input),
        ):
            raise SchemaValidationError("registry_input_digest does not match registry_input")
        expected_local_intent_digest = link_local_intent_digest(
            target=payload["target"],
            binding=payload["binding"],
            review=payload["review"],
            local_intent=payload["local_intent"],
            origin_destination_id=payload["origin_destination_id"],
        )
        if not hmac.compare_digest(
            payload["local_intent_digest"], expected_local_intent_digest
        ):
            raise SchemaValidationError("local_intent_digest does not match local_intent")
        meta = registry_input.get("meta", {})
        if not isinstance(meta, dict) or set(meta) - {"ttl", "label"}:
            raise SchemaValidationError("registry_input.meta permits only ttl and label")
        ttl = meta.get("ttl")
        if ttl is not None and (
            type(ttl) is not int or ttl <= 0 or ttl > _MAX_LINK_TTL_SECONDS
        ):
            raise SchemaValidationError(
                "registry_input.meta.ttl must be between 1 second and 365 days"
            )
        label = meta.get("label")
        if label is not None and (
            not isinstance(label, str)
            or _utf8_size(label, path="registry_input.meta.label") > 256
        ):
            raise SchemaValidationError("registry_input.meta.label must be bounded text")

        if operation == "revoke":
            revoke_token = _require_hex(payload, "revoke_token", _HEX32_RE)
            operand_digest = _require_hex(payload, "operand_digest", _HEX64_RE)
            expected_operand = link_revoke_operand_digest(revoke_token)
            if not hmac.compare_digest(operand_digest, expected_operand):
                raise SchemaValidationError("operand_digest does not match revoke_token")
            if "invite_ref" in payload:
                raise SchemaValidationError("revoke intent must not carry invite_ref")
            if set(registry_input) != {"operation_id"}:
                raise SchemaValidationError("revoke registry_input carries only operation_id")
        else:
            if "revoke_token" in payload or "operand_digest" in payload:
                raise SchemaValidationError("publish intent must not carry revoke operand fields")
            if "invite_ref" in payload:
                _require_hex(payload, "invite_ref", _HEX64_RE)
            wire_target_type = registry_input.get("target_type")
            if not isinstance(registry_input.get("target_uuid"), str) or not isinstance(
                wire_target_type, str
            ):
                raise SchemaValidationError(
                    "publish registry_input requires target_uuid and target_type"
                )
            if (
                registry_input.get("target_uuid") != target_uuid
                or wire_target_type != target_type
            ):
                raise SchemaValidationError(
                    "publish registry input must match the frozen target coordinates"
                )
            if wire_target_type == "org:join":
                if (
                    "invite_ref" not in payload
                    or registry_input.get("invite_ref") != payload.get("invite_ref")
                ):
                    raise SchemaValidationError(
                        "org:join registry input must carry the frozen public invite_ref"
                    )
                binding_org_uuid = payload["binding"].get("org_uuid")
                if (
                    not isinstance(binding_org_uuid, str)
                    or _UUID_RE.fullmatch(binding_org_uuid) is None
                    or target_uuid != binding_org_uuid
                ):
                    raise SchemaValidationError(
                        "org:join target UUID must equal the frozen binding organization"
                    )
                if "ttl" in meta:
                    raise SchemaValidationError(
                        "org:join uses only its fixed source expiry, never meta.ttl"
                    )
                deadline = registry_input.get("source_expires_at_ms")
                if (
                    type(deadline) is not int
                    or deadline < 0
                    or deadline > 9_007_199_254_740_991
                ):
                    raise SchemaValidationError(
                        "org:join requires source_expires_at_ms as a non-negative safe integer"
                    )
            elif (
                "invite_ref" in payload
                or "invite_ref" in registry_input
                or "source_expires_at_ms" in registry_input
            ):
                raise SchemaValidationError(
                    "invite_ref/source_expires_at_ms are only valid for org:join"
                )


@publication_band(min="raw", max="raw")
@home("organization")
@keyed_per_entity(key_strategy="approval_id")
class LinkApprovalResultV1(SettingSchema):
    """Durable Links application output, never approval authority."""

    set_id = LINK_APPROVAL_RESULT_SET_ID
    schema_revision = LINK_APPROVAL_RESULT_REVISION

    operation: str = field(required=True, enum=list(_OPERATIONS))
    state: str = field(required=True, enum=list(_RESULT_STATES))
    completed_at: float = field(required=True)
    operation_id: str = field(required=True)
    error_code: str = field(required=False)
    error_message: str = field(required=False)
    token: str = field(required=False)
    url: str = field(required=False)
    serving: dict = field(required=False)
    cache_removed: bool = field(required=False)
    registry_status: int = field(required=False)
    via: str = field(required=False, enum=list(_VIA))
    revoked_at: float = field(required=False)

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        operation = payload.get("operation")
        state = payload.get("state")
        _require_hex(payload, "operation_id", _HEX64_RE)
        _require_finite_time(payload, "completed_at")
        error_fields = {"error_code", "error_message"} & set(payload)
        publish_fields = {"token", "url", "serving"} & set(payload)
        revoke_fields = {"cache_removed", "registry_status", "via", "revoked_at"} & set(payload)

        if state == "failed":
            if not error_fields:
                raise SchemaValidationError("failed result requires a bounded error_code or error_message")
            if publish_fields or revoke_fields:
                raise SchemaValidationError("failed result must not carry success output")
            code = payload.get("error_code")
            if code is not None and (not isinstance(code, str) or _SAFE_CODE_RE.fullmatch(code) is None):
                raise SchemaValidationError("error_code must be a bounded lowercase code")
            message = payload.get("error_message")
            if message is not None and (
                not isinstance(message, str)
                or _utf8_size(message, path="error_message") > 512
            ):
                raise SchemaValidationError("error_message must be at most 512 UTF-8 bytes")
            return
        if error_fields:
            raise SchemaValidationError("successful result must not carry error fields")

        if operation == "publish":
            if revoke_fields or publish_fields != {"token", "url", "serving"}:
                raise SchemaValidationError("publish success requires only token, url, and serving output")
            token = _require_hex(payload, "token", _HEX32_RE)
            url = payload.get("url")
            if not isinstance(url, str) or _utf8_size(url, path="url") > 2048:
                raise SchemaValidationError("url must be bounded canonical HTTPS text")
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https"
                or not parsed.netloc
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path != f"/l/{token}"
                or parsed.query
                or parsed.fragment
            ):
                raise SchemaValidationError("url must be the canonical HTTPS /l/<token> URL")
            serving = payload.get("serving")
            if not isinstance(serving, dict) or type(serving.get("live")) is not bool:
                raise SchemaValidationError("serving must be an object with boolean live")
            if set(serving) - {"live", "via", "status", "detail"}:
                raise SchemaValidationError("serving carries unknown fields")
            serving_via = serving.get("via")
            if serving_via is not None and serving_via not in _VIA:
                raise SchemaValidationError("serving.via is outside the closed transport vocabulary")
            serving_status = serving.get("status")
            if serving_status is not None and (
                type(serving_status) is not int
                or serving_status < 100
                or serving_status > 599
            ):
                raise SchemaValidationError("serving.status must be an HTTP status integer")
            detail = serving.get("detail")
            if detail is not None and (
                not isinstance(detail, str)
                or _utf8_size(detail, path="serving.detail") > 512
            ):
                raise SchemaValidationError("serving.detail must be at most 512 UTF-8 bytes")
            _validate_public_json(serving, path="serving")
        else:
            if publish_fields or revoke_fields != {"cache_removed", "via", "revoked_at"} and revoke_fields != {"cache_removed", "registry_status", "via", "revoked_at"}:
                raise SchemaValidationError("revoke success requires cache_removed, via, and revoked_at")
            if type(payload.get("cache_removed")) is not bool:
                raise SchemaValidationError("cache_removed must be a boolean")
            status = payload.get("registry_status")
            if status is not None and (type(status) is not int or status < 100 or status > 599):
                raise SchemaValidationError("registry_status must be an HTTP status integer")
            revoked_at = _require_finite_time(payload, "revoked_at")
            if revoked_at > _require_finite_time(payload, "completed_at"):
                raise SchemaValidationError("revoked_at must not follow completed_at")
