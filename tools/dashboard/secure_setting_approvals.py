"""Agent-triggered secure-setting provisioning — one more kind on the
generalized approval rendezvous (see approvals_routes.py), not a parallel
system.

An agent asks the operator for a secret (connector credentials, an API
token) by describing a small form: ``schema`` maps each form-field label
to the dict key it fills in the sealed payload, and ``workspaces`` names
the workspace allowlist that may decrypt the result. The server freezes
the recipient public key, its fingerprint, a single-use nonce, and one
HPKE purpose label PER ALLOWLISTED WORKSPACE at create time; the
operator's browser renders the form (allowlist displayed), seals the
completed dict once per workspace label
(``static/js/ceremony/sealing.js`` :: ``sealToEncapsulationKey``), and the
decision carries ONLY ``approved``, ``nonce``, and ``sealed_payloads``.
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
PURPOSE_PREFIX = "autonomy.secure-setting.v2"

_REQUIRED = {"target_key", "origin", "schema", "title", "description", "org",
             "workspaces"}
_TARGET_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_ORG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_WORKSPACE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_NONCE_RE = re.compile(r"^[0-9a-f]{64}$")
_HEX_RE = re.compile(r"^[0-9a-f]*$")

MAX_FIELDS = 16
MAX_WORKSPACES = 8
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


def purpose_for(org: str, target_key: str, nonce: str, workspace: str) -> str:
    """The HPKE purpose label — binds org, target key, the single-use
    nonce, AND one allowed workspace into the encryption context. A sealed
    record can never be replayed into another setting, org, or request —
    and never opens for a workspace other than the one named in its label.
    The consuming REPL reconstructs this label from its own host-derived
    view of the caller's workspace (``tools/connectors/repl_login.py``),
    so the allowlist is enforced by decryption, not by a check anyone with
    Setting write access could edit around."""
    return f"{PURPOSE_PREFIX}|{org}|{target_key}|{nonce}|workspace={workspace}"


def _normalize_workspaces(value: object) -> list[str]:
    """Validate the requested workspace allowlist against the platform's
    known workspaces. Unknown ids are refused at request time — a typo'd
    allowlist would otherwise seal records nobody can ever open."""
    if not isinstance(value, list) or not value:
        raise ValueError(
            "secure_setting request field 'workspaces' must be a non-empty "
            "list of workspace ids allowed to decrypt this credential"
        )
    if len(value) > MAX_WORKSPACES:
        raise ValueError(
            f"secure_setting allows at most {MAX_WORKSPACES} workspaces")
    seen: list[str] = []
    for ws in value:
        if not isinstance(ws, str) or not _WORKSPACE_RE.fullmatch(ws):
            raise ValueError(f"invalid workspace id in allowlist: {ws!r}")
        if ws in seen:
            raise ValueError(f"duplicate workspace in allowlist: {ws!r}")
        seen.append(ws)
    from agents.workspace_settings import get_workspace

    for ws in seen:
        try:
            get_workspace(ws)
        except KeyError as exc:
            raise ValueError(
                f"allowlist names an unknown workspace: {ws!r}") from exc
    return sorted(seen)


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
    workspaces = _normalize_workspaces(request.get("workspaces"))
    try:
        recipient_pub, key_id = secure_setting_keys.recipient_public_key()
    except (OSError, ValueError) as exc:
        raise ValueError(f"the host recipient key is unavailable: {exc}") from exc
    nonce = secrets.token_hex(32)
    frozen = {
        "target_key": target_key, "org": org, "origin": origin,
        "title": title, "description": description,
        "schema": request["schema"],
        "workspaces": workspaces,
    }
    staged = {
        "v": 2,
        "nonce": nonce,
        "recipient_pub": recipient_pub,
        "key_id": key_id,
        "purposes": {ws: purpose_for(org, target_key, nonce, ws)
                     for ws in workspaces},
        "workspaces": workspaces,
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
    if set(decision) != {"approved", "nonce", "sealed_payloads"}:
        return {"ok": False, "error": (
            "secure_setting approval must carry only approved, nonce, "
            "and sealed_payloads"
        )}
    staged = row.get("staged")
    req = row.get("request") or {}
    if not isinstance(staged, dict) or not _NONCE_RE.fullmatch(
            str(staged.get("nonce", ""))):
        return {"ok": False, "error": "this request has no server-frozen sealing context"}
    # The staged purposes must still be derivable from the frozen request —
    # a staged blob that disagrees with the request it was frozen for is
    # tampering, not drift.
    workspaces = req.get("workspaces")
    if not isinstance(workspaces, list) or not workspaces \
            or not all(isinstance(ws, str) for ws in workspaces):
        return {"ok": False,
                "error": "the frozen request has no workspace allowlist"}
    expected_purposes = {
        ws: purpose_for(req.get("org", ""), req.get("target_key", ""),
                        staged["nonce"], ws)
        for ws in workspaces
    }
    if staged.get("purposes") != expected_purposes \
            or staged.get("workspaces") != workspaces \
            or staged.get("target_key") != req.get("target_key") \
            or staged.get("org") != req.get("org"):
        return {"ok": False,
                "error": "the staged sealing context does not match the frozen request"}
    nonce = decision.get("nonce")
    if not isinstance(nonce, str) or not hmac.compare_digest(nonce, staged["nonce"]):
        return {"ok": False, "error": "the decision nonce does not match this request"}
    sealed_payloads = decision.get("sealed_payloads")
    if not isinstance(sealed_payloads, dict) \
            or set(sealed_payloads) != set(workspaces):
        return {"ok": False, "error": (
            "sealed_payloads must carry exactly one record per allowlisted "
            "workspace"
        )}
    ciphertexts_hex: dict[str, str] = {}
    for ws in workspaces:
        try:
            ciphertexts_hex[ws] = _validate_sealed_payload(sealed_payloads[ws])
        except ValueError as exc:
            return {"ok": False,
                    "error": f"sealed record for workspace {ws!r}: {exc}"}
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

    # No stored purpose string — the consumer reconstructs each label from
    # its own derived view of the caller's workspace, so a widened
    # ``workspaces``/``ciphertexts_hex`` edit yields labels that don't open.
    payload = {
        "ciphertexts_hex": ciphertexts_hex,
        "workspaces": workspaces,
        "nonce": staged["nonce"],
        "key_id": staged["key_id"],
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
            _secure_setting_schema.SECURE_SETTING_V2_REVISION,
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
