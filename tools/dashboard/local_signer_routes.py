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


# ── D3-14: per-request client auth (device signature + live revocation) ─
#
# No bearer token exists anywhere after pairing (DN3 §4). Every
# authenticated device call signs a message specific to that call and the
# server: (1) verifies it against the device's registered public key,
# (2) re-reads revoked_at fresh on this exact call — never a decision
# cached from an earlier call in the same client run (T7) — and
# (3) enforces the accompanying client_nonce is single-use. Each endpoint
# builds its own canonical ``message`` (the thing that's actually being
# authorized); this function only owns the three checks above.


def verify_device_request(
    *, device_id: str, message: bytes, signature: str, client_nonce: str, now: float,
) -> tuple[Any, str | None]:
    """Returns ``(device, None)`` on success or ``(None, error_code)``."""
    device = db.get_device(device_id)
    if device is None or not device.active:
        return None, "device_not_found_or_revoked"
    if not verify_ed25519(device.public_key, message, signature):
        return None, "signature_invalid"
    if not db.consume_request_nonce(device_id=device_id, nonce=client_nonce, now=now):
        return None, "nonce_replayed"
    return device, None


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
        # Device-create and the pairing decision must be ATOMIC: two
        # concurrent approves must not both insert a device row (an
        # orphaned active device with no completed pairing could sign).
        # The status re-check, the insert, and the decision update all
        # happen inside one transaction in approve_pairing_and_create_device
        # so a lost race writes nothing at all, not just a losing decision.
        ok = db.approve_pairing_and_create_device(
            pairing_id=pairing_id,
            device_id=device_id,
            operator_id=row.operator_id,
            device_label=meta.get("device_label") or "Paired device",
            public_key=row.pending_public_key,
            platform=meta.get("platform") or "unknown",
            app_version=meta.get("app_version"),
            paired_at=now,
        )
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


# ── D3-20: POST /key-material/provision (+ operator step-up) ──────────


class KeyMaterial:
    """What a real credential store hands back for one ``key_id``.

    ``public_material`` is never secret (it's the public half); a real
    store can return it in the clear alongside the encrypted private
    blob. Deliberately NOT a dataclass with a fixed schema yet — the real
    credential store (out of scope for this backlog, see the index note's
    "Not in this backlog") owns the final shape. This is the seam a
    future integration replaces.
    """

    def __init__(self, ciphertext: bytes, public_material: bytes, key_fingerprint: str, signing_kind: str = "gpg"):
        self.ciphertext = ciphertext
        self.public_material = public_material
        self.key_fingerprint = key_fingerprint
        self.signing_kind = signing_kind


def _default_key_material_provider(key_id: str) -> KeyMaterial | None:
    """No real credential store exists yet — tests monkeypatch
    ``KEY_MATERIAL_PROVIDER``. Production wiring is a follow-up task once
    that store exists."""
    return None


KEY_MATERIAL_PROVIDER = _default_key_material_provider


# ── D3-22: register the operator's PUBLIC signing key at provision time
# (the DN3<->DN5 seam) ──────────────────────────────────────────────────
#
# tools.dashboard.commit_broker.keys (DN5, Fable) isn't on master yet.
# This indirection is the same shape as KEY_MATERIAL_PROVIDER above:
# production wiring swaps in the real import once that module lands;
# tests monkeypatch REGISTER_VERIFICATION_KEY directly until then. The
# signing key registered here is the key provisioned above — distinct
# from the device's own pairing keypair (§2/§3), which never signs
# commits and is never written to any verification store.


def _default_register_verification_key(*, operator_id: str, signing_kind: str, public_material: bytes):
    """No-op placeholder — see module docstring. Swapped for the real
    ``tools.dashboard.commit_broker.keys.register_verification_key`` once
    DN5's branch lands; tests monkeypatch this directly until then."""
    return None


REGISTER_VERIFICATION_KEY = _default_register_verification_key

STEP_UP_TTL_SECONDS = 60


async def api_local_signer_step_up(request):
    """Operator-only: mint a short-lived, single-use, device/key-scoped
    token proving a live operator re-affirmed this specific provisioning
    intent (DN3 §11: "operator-authorized, audited, short-window")."""
    denied = _reject_agent_session(request)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    device_id = body.get("device_id")
    key_id = body.get("key_id")
    if not device_id or not key_id:
        return JSONResponse({"error": "device_id and key_id are required"}, status_code=400)

    now = time.time()
    token = secrets.token_urlsafe(24)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    db.insert_step_up_token(
        token_hash=token_hash,
        operator_id=_current_operator_id(),
        device_id=device_id,
        key_id=key_id,
        created_at=now,
        expires_at=now + STEP_UP_TTL_SECONDS,
    )
    return JSONResponse({"step_up_token": token, "expires_at": now + STEP_UP_TTL_SECONDS})


def _provision_message(*, device_id: str, key_id: str, client_nonce: str, issued_at: float) -> bytes:
    return f"autonomy-local-signer-provision:{device_id}:{key_id}:{client_nonce}:{issued_at}".encode()


async def api_local_signer_key_material_provision(request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    device_id = body.get("device_id")
    key_id = body.get("key_id")
    client_nonce = body.get("client_nonce")
    issued_at = body.get("issued_at")
    signature = body.get("signature")
    step_up_token = body.get("step_up_token")
    if not all([device_id, key_id, client_nonce, issued_at, signature, step_up_token]):
        return JSONResponse(
            {"error": "device_id, key_id, client_nonce, issued_at, signature, step_up_token are all required"},
            status_code=400,
        )

    now = time.time()

    # Gate 1: per-request client auth (D3-14) — proves a live, non-revoked
    # paired device is making this exact call.
    message = _provision_message(device_id=device_id, key_id=key_id, client_nonce=client_nonce, issued_at=issued_at)
    device, error = verify_device_request(
        device_id=device_id, message=message, signature=signature,
        client_nonce=client_nonce, now=now,
    )
    if device is None:
        return JSONResponse({"error": error}, status_code=403)

    # Gate 2: fresh operator step-up, scoped to this exact device/key and
    # consumed atomically — missing or already-used is rejected the same way.
    token_hash = hashlib.sha256(step_up_token.encode()).hexdigest()
    if not db.consume_step_up_token(token_hash=token_hash, device_id=device_id, key_id=key_id, now=now):
        return JSONResponse({"error": "step_up_required_or_expired"}, status_code=403)

    material = KEY_MATERIAL_PROVIDER(key_id)
    if material is None:
        return JSONResponse({"error": "unknown key_id"}, status_code=404)

    from tools.dashboard.local_signer_s2k import evaluate_floor, parse_s2k_packet, MalformedS2KPacket

    try:
        params = parse_s2k_packet(material.ciphertext)
        violations = evaluate_floor(params)
    except MalformedS2KPacket as e:
        violations = [str(e)]
        params = None

    kdf_summary = params.to_kdf_params() if params is not None else {"error": "malformed_packet"}
    if violations:
        db.append_audit_event(
            audit_event_id=str(uuid.uuid4()),
            occurred_at=now,
            event_type="key_provision_rejected_weak_kdf",
            device_id=device_id,
            operator_id=device.operator_id,
            kdf_summary_json=_json_dumps(kdf_summary),
            reason="; ".join(violations),
        )
        return JSONResponse(
            {"status": "rejected_weak_kdf", "kdf_params": kdf_summary, "floor_violated": violations},
            status_code=422,
        )

    # Load-bearing seam (D3-22, DN5 §3.1): register the signing key's
    # PUBLIC half the moment it's provisioned — never deferred, never a
    # separate operator step, since the public half isn't secret. Without
    # this, DN5's request_signature pre-flight has nothing to verify
    # against and every signed commit dead-ends unverifiable.
    REGISTER_VERIFICATION_KEY(
        operator_id=device.operator_id,
        signing_kind=material.signing_kind,
        public_material=material.public_material,
    )

    encrypted_key_blob = base64.b64encode(material.ciphertext).decode()
    db.append_audit_event(
        audit_event_id=str(uuid.uuid4()),
        occurred_at=now,
        event_type="key_provisioned",
        device_id=device_id,
        operator_id=device.operator_id,
        kdf_summary_json=_json_dumps(kdf_summary),
    )
    return JSONResponse({
        "encrypted_key_blob": encrypted_key_blob,
        "kdf_params": kdf_summary,
        "key_fingerprint": material.key_fingerprint,
        "provisioned_at": now,
    })


ROUTES = [
    Route("/api/capabilities/local-signer/v1/pairing/start", api_local_signer_pairing_start, methods=["POST"]),
    Route("/api/capabilities/local-signer/v1/pairing/complete", api_local_signer_pairing_complete, methods=["POST"]),
    Route("/api/dashboard/local-signer/pairing/{pairing_id}/decide", api_local_signer_pairing_decide, methods=["POST"]),
    Route("/api/dashboard/local-signer/key-material/step-up", api_local_signer_step_up, methods=["POST"]),
    Route("/api/capabilities/local-signer/v1/key-material/provision", api_local_signer_key_material_provision, methods=["POST"]),
]
