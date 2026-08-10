"""Agent-triggered secure-setting provisioning — one more kind on the
generalized approval rendezvous (see approvals_routes.py), not a parallel
system.

An agent asks the operator for a secret (connector credentials, an API
token) by describing a small form: ``schema`` maps each form-field label
to the dict key it fills in the sealed payload. The server freezes the
recipient public key, its fingerprint, a single-use nonce, and the HPKE
purpose label at create time; the operator's browser renders the form,
seals the completed dict to the staged public key
(``static/js/ceremony/sealing.js`` :: ``sealToEncapsulationKey``), and the
decision carries ONLY ``approved``, ``nonce``, and ``sealed_payload``.
The executor validates the binding and upserts the ciphertext into the
``autonomy.secure.setting#1`` Setting — the plaintext exists nowhere on
the server, in the approval store, or in any log; only the holder of the
host key file (``REPL_LOGIN_KEY_FILE``, mode 0600) can open it.
"""

from __future__ import annotations

import hmac
import re
import secrets
import time

from starlette.requests import Request

from tools.dashboard import secure_setting_keys

# Importing the schema module registers ``autonomy.secure.setting#1`` at
# startup (SettingSchema self-registers on import), matching how
# approvals_routes registers the commit-signing-key schema.
from tools.graph.schemas import secure_setting as _secure_setting_schema

KIND = "secure_setting"
PURPOSE_PREFIX = "autonomy.secure-setting.v1"

_REQUIRED = {"target_key", "origin", "schema", "title", "description", "org"}
_TARGET_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_ORG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_NONCE_RE = re.compile(r"^[0-9a-f]{64}$")
_HEX_RE = re.compile(r"^[0-9a-f]*$")

MAX_FIELDS = 16
#: suite byte + 32-byte KEM enc + 16-byte AEAD tag — the smallest record
#: the sealing wire format can produce.
MIN_SEALED_BYTES = 1 + 32 + 16
MAX_SEALED_BYTES = 64 * 1024
SUITE_X25519_HKDF_SHA256_CHACHA20POLY1305 = 1


def _require_short_str(request: dict, key: str, *, max_len: int) -> str:
    value = request.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > max_len:
        raise ValueError(
            f"secure_setting request field {key!r} must be a non-empty "
            f"string up to {max_len} characters"
        )
    return value


def _normalize_fields(schema: object) -> list[dict]:
    """``schema`` (form-field label -> dict key, or -> {key, secret,
    placeholder}) into an ordered, validated field list."""
    if not isinstance(schema, dict) or not schema:
        raise ValueError("secure_setting schema must be a non-empty object "
                         "mapping form-field labels to payload dict keys")
    if len(schema) > MAX_FIELDS:
        raise ValueError(f"secure_setting schema allows at most {MAX_FIELDS} fields")
    fields: list[dict] = []
    seen_keys: set[str] = set()
    for label, spec in schema.items():
        if not isinstance(label, str) or not label.strip() or len(label) > 128:
            raise ValueError("every schema form-field label must be a "
                             "non-empty string up to 128 characters")
        if isinstance(spec, str):
            spec = {"key": spec}
        if not isinstance(spec, dict):
            raise ValueError(
                f"schema entry {label!r} must be a dict key string or an "
                "object with 'key' (and optional 'secret'/'placeholder')"
            )
        unknown = set(spec) - {"key", "secret", "placeholder"}
        if unknown:
            raise ValueError(
                f"schema entry {label!r} has unknown options: {sorted(unknown)}")
        key = spec.get("key")
        if not isinstance(key, str) or not _TARGET_KEY_RE.fullmatch(key):
            raise ValueError(
                f"schema entry {label!r} needs a payload dict key of "
                "lowercase letters, digits, '.', '_' or '-'"
            )
        if key in seen_keys:
            raise ValueError(f"schema maps two form fields to dict key {key!r}")
        seen_keys.add(key)
        secret = spec.get("secret", True)
        if not isinstance(secret, bool):
            raise ValueError(f"schema entry {label!r}: 'secret' must be a boolean")
        placeholder = spec.get("placeholder", "")
        if not isinstance(placeholder, str) or len(placeholder) > 128:
            raise ValueError(
                f"schema entry {label!r}: 'placeholder' must be a string "
                "up to 128 characters"
            )
        fields.append({
            "label": label.strip(), "key": key,
            "secret": secret, "placeholder": placeholder,
        })
    return fields


def purpose_for(org: str, target_key: str, nonce: str) -> str:
    """The HPKE purpose label — binds org, target key, and the single-use
    nonce into the encryption context, so a sealed record can never be
    replayed into another setting, org, or request."""
    return f"{PURPOSE_PREFIX}|{org}|{target_key}|{nonce}"


def prepare_create(session: str, request: dict) -> tuple[dict, dict]:
    """Validate requester input and freeze the sealing context."""
    if set(request) != _REQUIRED:
        raise ValueError(
            "secure_setting request must carry exactly "
            f"{sorted(_REQUIRED)}"
        )
    target_key = request.get("target_key")
    if not isinstance(target_key, str) or not _TARGET_KEY_RE.fullmatch(target_key):
        raise ValueError(
            "target_key must be 1-128 characters of lowercase letters, "
            "digits, '.', '_' or '-'"
        )
    org = request.get("org")
    if not isinstance(org, str) or not _ORG_RE.fullmatch(org):
        raise ValueError("org must be a valid org slug")
    origin = _require_short_str(request, "origin", max_len=256)
    title = _require_short_str(request, "title", max_len=160)
    description = _require_short_str(request, "description", max_len=2000)
    fields = _normalize_fields(request.get("schema"))
    try:
        recipient_pub, key_id = secure_setting_keys.recipient_public_key()
    except (OSError, ValueError) as exc:
        raise ValueError(f"the host recipient key is unavailable: {exc}") from exc
    nonce = secrets.token_hex(32)
    frozen = {
        "target_key": target_key, "org": org, "origin": origin,
        "title": title, "description": description,
        "schema": request["schema"],
    }
    staged = {
        "v": 1,
        "nonce": nonce,
        "recipient_pub": recipient_pub,
        "key_id": key_id,
        "purpose": purpose_for(org, target_key, nonce),
        "fields": fields,
        "target_key": target_key,
        "org": org,
    }
    return frozen, staged


def enrich(row: dict) -> dict:
    """Expose exactly the server-frozen sealing context the browser uses."""
    return {"staged": row.get("staged")}


def authorize_decision(request: Request, _row: dict, _decision: dict) -> str | None:
    """Only a human-origin, unlocked operator session may provision a
    secret. Mirrors dashboard_access_approvals.authorize_decision."""
    from tools.dashboard import unlock_routes

    if unlock_routes.gate_disabled():
        return None
    session = unlock_routes.session_from_request(request)
    if session is None or session.get("method") not in {
        "bootstrap", "passkey", "password",
    }:
        return "unlock the dashboard before deciding this provisioning request"
    return None


def _validate_sealed_payload(value: object) -> str:
    if not isinstance(value, str) or len(value) % 2 != 0 \
            or not _HEX_RE.fullmatch(value):
        raise ValueError("sealed_payload must be lowercase hex")
    size = len(value) // 2
    if size < MIN_SEALED_BYTES:
        raise ValueError("sealed_payload is truncated")
    if size > MAX_SEALED_BYTES:
        raise ValueError("sealed_payload exceeds the size limit")
    if int(value[:2], 16) != SUITE_X25519_HKDF_SHA256_CHACHA20POLY1305:
        raise ValueError("sealed_payload does not use the expected sealing suite")
    return value


async def execute(row: dict, decision: dict) -> dict:
    """Validate the binding and store the ciphertext — never the plaintext.

    The nonce is single-use by construction: it is staged on exactly one
    approval row, and the rendezvous executes an approved row at most once
    (first-writer-wins result + the in-flight guard in approvals_routes).
    """
    if set(decision) != {"approved", "nonce", "sealed_payload"}:
        return {"ok": False, "error": (
            "secure_setting approval must carry only approved, nonce, "
            "and sealed_payload"
        )}
    staged = row.get("staged")
    req = row.get("request") or {}
    if not isinstance(staged, dict) or not _NONCE_RE.fullmatch(
            str(staged.get("nonce", ""))):
        return {"ok": False, "error": "this request has no server-frozen sealing context"}
    # The staged purpose must still be derivable from the frozen request —
    # a staged blob that disagrees with the request it was frozen for is
    # tampering, not drift.
    expected_purpose = purpose_for(
        req.get("org", ""), req.get("target_key", ""), staged["nonce"])
    if staged.get("purpose") != expected_purpose \
            or staged.get("target_key") != req.get("target_key") \
            or staged.get("org") != req.get("org"):
        return {"ok": False,
                "error": "the staged sealing context does not match the frozen request"}
    nonce = decision.get("nonce")
    if not isinstance(nonce, str) or not hmac.compare_digest(nonce, staged["nonce"]):
        return {"ok": False, "error": "the decision nonce does not match this request"}
    try:
        ciphertext_hex = _validate_sealed_payload(decision.get("sealed_payload"))
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    try:
        _current_pub, current_key_id = secure_setting_keys.recipient_public_key()
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"the host recipient key is unavailable: {exc}"}
    if current_key_id != staged.get("key_id"):
        return {"ok": False, "error": (
            "the host recipient key changed after this request was staged — "
            "decline and request provisioning again"
        )}
    from tools.graph import settings_ops

    payload = {
        "ciphertext_hex": ciphertext_hex,
        "key_id": staged["key_id"],
        "purpose": staged["purpose"],
        "origin": req.get("origin", ""),
        "title": req.get("title", ""),
        "description": req.get("description", ""),
        "payload_keys": [f["key"] for f in staged.get("fields", [])],
        "provisioned_at": time.time(),
        "approval_id": row["id"],
        "requested_by_session": row.get("session", ""),
    }
    try:
        setting_id = settings_ops.upsert_by_key(
            _secure_setting_schema.SECURE_SETTING_SET_ID,
            _secure_setting_schema.SECURE_SETTING_REVISION,
            staged["target_key"], payload, org=staged["org"],
        )
    except Exception as exc:
        return {"ok": False, "error": f"could not store the sealed setting: {exc}"}
    return {
        "ok": True,
        "setting_id": setting_id,
        "set_id": _secure_setting_schema.SECURE_SETTING_SET_ID,
        "key": staged["target_key"],
        "org": staged["org"],
        "key_id": staged["key_id"],
    }


PREPARE_CREATE = {KIND: prepare_create}
ENRICH = {KIND: enrich}
EXECUTORS = {KIND: execute}
AUTHORIZE_DECISION = {KIND: authorize_decision}
