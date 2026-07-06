"""Local signer capability routes — device pairing (DN3, graph note a498f525-6b0).

Own capability module: storage lives in ``tools.dashboard.dao.local_signer_db``,
route handlers live here, and ``server.py`` only imports and registers them —
mirroring how other Dashboard capability surfaces are kept out of the main
route file's body.

**Operator identity (interim).** Spec §4.4's real operator-passkey identity
model does not exist yet anywhere in this codebase. Until it lands, "operator"
here means "not an agent session": any caller presenting a valid agent
session-token bearer (the ``auth_db``/``_crosstalk_auth`` model) is rejected
from every operator-only route in this module (T1 — device authority never
comes from an agent-controlled caller). A single fixed ``operator_id`` (env
override ``DASHBOARD_OPERATOR_ID``, default ``"operator"``) stands in for the
authenticated-operator-identity this single-operator deployment doesn't yet
distinguish per human. This is a documented interim, not a design decision —
multi-operator scope isolation (DN3's L9) is explicitly out of reach until
§4.4 lands.

**Signing scheme.** Device keypairs and possession/attach signatures use
Ed25519 (``cryptography`` package) — fixed-size keys, no parameter ambiguity,
well-suited to a "device generates a keypair, signs a challenge" flow. Public
keys and signatures are exchanged as base64 (standard, unpadded-tolerant).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import secrets
import time
import uuid
from typing import Any

from starlette.responses import JSONResponse
from starlette.routing import Route

from tools.dashboard.dao import auth_db, local_signer_db as db

PAIRING_TTL_SECONDS = 120
POLL_INTERVAL_SECONDS = 3


# ── Crypto ───────────────────────────────────────────────────────────


def _b64decode(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded.encode())


def verify_ed25519(public_key_b64: str, message: bytes, signature_b64: str) -> bool:
    """Verify an Ed25519 signature. Returns False on any malformed input
    rather than raising — a malformed signature is just a rejected one."""
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        return False
    try:
        pubkey = Ed25519PublicKey.from_public_bytes(_b64decode(public_key_b64))
        signature = _b64decode(signature_b64)
    except (ValueError, binascii.Error):
        return False
    try:
        pubkey.verify(signature, message)
        return True
    except InvalidSignature:
        return False


def _possession_message(device_code: str) -> bytes:
    """Deterministic challenge for pairing-completion proof-of-possession.

    Derived from ``device_code`` alone (not ``pairing_id``) because a
    manual-code-entry client never learns ``pairing_id`` ahead of time —
    only a QR-scanning client does, since the QR payload embeds both. Both
    client types must be able to compute this before ever contacting the
    server. Uniqueness per ceremony comes from ``device_code`` itself
    (unique per pairing row), which is what stops a signature produced for
    one ceremony from being replayed against another.
    """
    return f"autonomy-local-signer-pairing:{device_code}".encode()


# ── Operator / agent-session identity (interim, see module docstring) ─


def _current_operator_id() -> str:
    return os.environ.get("DASHBOARD_OPERATOR_ID", "operator")


def _is_agent_session_request(request) -> bool:
    """True iff the request carries a bearer token that resolves to a live
    agent session — the credential T1 says must never grant device/pairing
    authority."""
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return False
    token_hash = hashlib.sha256(auth[7:].encode()).hexdigest()
    return auth_db.resolve_token(token_hash) is not None


def _reject_agent_session(request) -> JSONResponse | None:
    if _is_agent_session_request(request):
        return JSONResponse(
            {"error": "operator-only endpoint; agent sessions cannot pair a device"},
            status_code=403,
        )
    return None


# ── POST /api/capabilities/local-signer/v1/pairing/start ─────────────


async def api_local_signer_pairing_start(request):
    denied = _reject_agent_session(request)
    if denied is not None:
        return denied

    operator_id = _current_operator_id()
    now = time.time()
    pairing_id = str(uuid.uuid4())
    device_code = secrets.token_hex(4).upper()  # 8 hex chars, human-enterable
    verifier = secrets.token_urlsafe(16)  # ~128 bits
    verifier_hash = hashlib.sha256(verifier.encode()).hexdigest()
    expires_at = now + PAIRING_TTL_SECONDS
    qr_payload = (
        f"autonomy-signer://pair?pairing_id={pairing_id}&verifier={verifier}"
    )

    db.insert_pairing_request(
        pairing_id=pairing_id,
        device_code=device_code,
        verifier_hash=verifier_hash,
        operator_id=operator_id,
        created_at=now,
        expires_at=expires_at,
    )
    db.append_audit_event(
        audit_event_id=str(uuid.uuid4()),
        occurred_at=now,
        event_type="pairing_started",
        operator_id=operator_id,
    )
    return JSONResponse({
        "pairing_id": pairing_id,
        "device_code": device_code,
        "qr_payload": qr_payload,
        "expires_at": expires_at,
        "poll_interval_seconds": POLL_INTERVAL_SECONDS,
    })


# ── POST /api/capabilities/local-signer/v1/pairing/complete ──────────


async def api_local_signer_pairing_complete(request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    device_code = body.get("device_code")
    client_public_key = body.get("client_public_key")
    possession_signature = body.get("possession_signature")
    if not device_code or not client_public_key or not possession_signature:
        return JSONResponse(
            {"error": "device_code, client_public_key, and possession_signature are required"},
            status_code=400,
        )

    now = time.time()
    row = db.get_pairing_request_by_code(device_code)
    if row is None:
        return JSONResponse({"error": "not_found"}, status_code=404)

    if row.status != "pending" or row.expires_at <= now:
        if row.status != "pending":
            db.append_audit_event(
                audit_event_id=str(uuid.uuid4()),
                occurred_at=now,
                event_type="pairing_replay_rejected",
                operator_id=row.operator_id,
            )
            return JSONResponse({"error": "already_decided"}, status_code=409)
        return JSONResponse({"error": "expired"}, status_code=410)

    if not verify_ed25519(
        client_public_key, _possession_message(device_code), possession_signature,
    ):
        return JSONResponse({"error": "possession_check_failed"}, status_code=403)

    verifier = body.get("verifier")
    verifier_presented = bool(verifier) and (
        hashlib.sha256(str(verifier).encode()).hexdigest() == row.verifier_hash
    )
    pending_device_meta = _json_dumps({
        "platform": body.get("platform"),
        "app_version": body.get("app_version"),
        "device_label": body.get("device_label"),
    })

    pairing_id = db.complete_pairing_to_awaiting_confirm(
        device_code=device_code,
        pending_public_key=client_public_key,
        pending_device_meta=pending_device_meta,
        verifier_presented=verifier_presented,
        now=now,
    )
    if pairing_id is None:
        # Lost a concurrent race, or expired between our read above and the
        # atomic flip — re-read to report the precise reason.
        fresh = db.get_pairing_request_by_code(device_code)
        if fresh is not None and fresh.status != "pending":
            return JSONResponse({"error": "already_decided"}, status_code=409)
        return JSONResponse({"error": "expired"}, status_code=410)

    return JSONResponse({"pairing_id": pairing_id, "status": "awaiting_operator_confirm"})


def _json_dumps(obj: Any) -> str:
    import json
    return json.dumps(obj)


# ── POST /api/dashboard/local-signer/pairing/{pairing_id}/decide ─────


async def api_local_signer_pairing_decide(request):
    denied = _reject_agent_session(request)
    if denied is not None:
        return denied

    pairing_id = request.path_params["pairing_id"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    decision = body.get("decision")
    if decision not in ("approve", "deny"):
        return JSONResponse({"error": "decision must be 'approve' or 'deny'"}, status_code=400)

    row = db.get_pairing_request(pairing_id)
    if row is None:
        return JSONResponse({"error": "not_found"}, status_code=404)

    operator_id = _current_operator_id()
    if row.operator_id != operator_id:
        return JSONResponse({"error": "not your pairing request"}, status_code=403)
    if row.status != "awaiting_operator_confirm":
        return JSONResponse({"error": "not awaiting a decision"}, status_code=409)

    now = time.time()
    if decision == "approve":
        device_id = str(uuid.uuid4())
        import json as _json
        meta = _json.loads(row.pending_device_meta or "{}")
        db.insert_device(
            device_id=device_id,
            operator_id=row.operator_id,
            device_label=meta.get("device_label") or "Paired device",
            public_key=row.pending_public_key,
            platform=meta.get("platform") or "unknown",
            app_version=meta.get("app_version"),
            paired_at=now,
        )
        ok = db.decide_pairing(pairing_id=pairing_id, approve=True, device_id=device_id, now=now)
        if not ok:
            return JSONResponse({"error": "not awaiting a decision"}, status_code=409)
        db.append_audit_event(
            audit_event_id=str(uuid.uuid4()),
            occurred_at=now,
            event_type="pairing_completed",
            device_id=device_id,
            operator_id=operator_id,
        )
        return JSONResponse({"device_id": device_id, "device_label": meta.get("device_label") or "Paired device"})

    ok = db.decide_pairing(pairing_id=pairing_id, approve=False, device_id=None, now=now)
    if not ok:
        return JSONResponse({"error": "not awaiting a decision"}, status_code=409)
    return JSONResponse({"status": "denied"})


ROUTES = [
    Route("/api/capabilities/local-signer/v1/pairing/start", api_local_signer_pairing_start, methods=["POST"]),
    Route("/api/capabilities/local-signer/v1/pairing/complete", api_local_signer_pairing_complete, methods=["POST"]),
    Route("/api/dashboard/local-signer/pairing/{pairing_id}/decide", api_local_signer_pairing_decide, methods=["POST"]),
]
