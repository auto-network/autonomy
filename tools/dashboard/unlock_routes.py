"""Dashboard UNLOCK gate — the human sign-in layer (Gate 1: access).

The enforcement half of the identity system whose enrollment half lives
in :mod:`identity_routes` (model notes ``graph://53f65f2f-d73`` two-gate
matrix, ``graph://80ef5131-9f0`` fail-open-then-enforce + password-as-
access-floor, mockup design d49be06b 'Unlock' state). Four pieces:

* **Passkey unlock** — the WebAuthn ASSERT ceremony ('Unlock with
  Face ID'). Same ceremony discipline as the register side: the
  challenge + RP ID + origin are frozen server-side at options time,
  single-use, 10-minute TTL, the completion must arrive on the host
  that minted the options, and minting new options invalidates prior
  pending ceremonies for the same personal identity + RP ID. Verification is
  py_webauthn ``verify_authentication_response`` against the STORED
  credential public key + sign count; a sign-count regression (clone
  signal) is a rejection, and the stored count advances on success.

* **Password unlock** — the always-available floor. The browser fetches
  the armored personal root (``GET /api/identity/personal``), decrypts
  it locally with the password (I1: the password and the plaintext seed
  never leave the browser), and proves possession by signing a
  server-minted single-use challenge with the root key. The server
  verifies the Ed25519 signature against the stored ``root_pub``.
  Removing the last passkey therefore never locks the user out — the
  password path needs no enrolled credential.

* **Approval unlock** — a headless session raises an ordinary pending
  approval with a one-use ephemeral key. An authenticated operator signs the
  server-frozen two-hour ``dashboard:ui`` grant with the personal root; the
  requester proves possession of the ephemeral private key and atomically
  redeems that grant into the same revocable session store.

* **The session + the gate** — success on either path mints a signed,
  tamper-evident session token (HMAC-SHA256 over a server-side secret)
  set as an HttpOnly cookie. :class:`HumanGateMiddleware` enforces it
  on the HUMAN/BROWSER path only — page loads, page fragments, and the
  browser websockets — per fail-open-then-enforce: with NO human auth
  method enrolled (no personal identity, no passkey) everything stays
  open so bootstrap can't lock itself out; the moment one exists,
  enforcement is on, automatically.

CRITICAL SCOPE LINE (do not move it): container sessions — the graph
CLI, the dispatcher, every agent — talk to this dashboard exclusively
through ``/api/...`` routes. Those stay FAIL-OPEN here; agent network
auth is a separate, later layer. The ONLY ``/api`` paths this gate
covers are the passkey *enrollment* endpoints, because leaving those
open once enforcement is on would let anyone on the network enroll
their own credential and walk through the front door. Agents never
enroll passkeys, so gating enrollment cannot lock an agent out.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
import urllib.parse
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from tools.graph import settings_ops
from tools.graph.db import GraphDBNotReady
from tools.data_paths import DATA_ROOT
from tools.dashboard.dao import identity_sessions
from tools.dashboard.dashboard_access_approvals import (
    GRANT_SIGNING_DOMAIN as APPROVAL_GRANT_SIGNING_DOMAIN,
    REDEEM_SIGNING_DOMAIN as APPROVAL_REDEEM_SIGNING_DOMAIN,
    valid_nonce as _valid_approval_nonce,
)
from tools.dashboard.network_routes import _mock_mode
from tools.dashboard.identity_routes import (
    PENDING_MAX,
    PENDING_TTL_S,
    _b64url,
    _b64url_decode,
    _client_challenge,
    _now,
    _passkey_rows,
    _personal_member,
    _rp_from_request,
)
from tools.graph.schemas.personal_identity import (
    PASSKEY_REVISION,
    PASSKEY_SET_ID,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: Domain separator for the password-unlock proof — the browser signs
#: this prefix + canonical JSON, never a bare challenge, so an unlock
#: signature can never be replayed as a registry request or a cert
#: (which use their own domains).
UNLOCK_SIGNING_DOMAIN = b"autonomy.identity.unlock.v1\n"

#: Recovery kill-switch (bead auto-dmnm9). Env-ONLY — nothing derived
#: from a request may ever feed this decision. Only these EXPLICIT
#: values disable the gate; unset, empty, or anything else (including
#: typos like "of") keeps enforcement ON — the switch fails safe.
_AUTH_DISABLING_VALUES = frozenset({"off", "0", "false", "no"})


def gate_disabled() -> bool:
    """True only while DASHBOARD_AUTH holds an explicit disabling value
    (off/0/false/no, case-insensitive). The operator's escape hatch if
    an unlock ever breaks; read per call so tests and in-process flips
    take effect immediately."""
    value = os.environ.get("DASHBOARD_AUTH")
    return value is not None and value.strip().lower() in _AUTH_DISABLING_VALUES


SESSION_COOKIE = "autonomy_dashboard_session"
#: 'Persistent session' per the two-gate model — access only; signing
#: authority still re-proves itself per action with the password.
SESSION_TTL_S = 7 * 24 * 3600

#: Pending ASSERT ceremonies: challenge(b64url) → {rp_id, origin, expires}.
#: Same store discipline as the register side.
_assert_pending: dict[str, dict] = {}

#: Pending PASSWORD challenges: challenge(hex) → {origin, expires}.
_pw_pending: dict[str, dict] = {}


def _prune(store: dict, *, reserve: int = 0) -> None:
    """Expire old ceremonies and reserve bounded FIFO capacity for new ones."""
    cutoff = _now()
    for key in [k for k, p in store.items() if p["expires"] <= cutoff]:
        store.pop(key, None)
    limit = max(PENDING_MAX - reserve, 0)
    while len(store) > limit:
        store.pop(next(iter(store)))


# ── session tokens ────────────────────────────────────────────────────


def _secret_path() -> Path:
    override = os.environ.get("DASHBOARD_SESSION_SECRET_FILE")
    if override:
        return Path(override)
    return DATA_ROOT / "dashboard-session.secret"


_secret_cache: dict = {"path": None, "value": None}


def _session_secret() -> bytes:
    """The HMAC key for session tokens — generated once, file-persisted
    (0600) so sessions survive dashboard restarts."""
    path = _secret_path()
    if _secret_cache["path"] == path and _secret_cache["value"]:
        return _secret_cache["value"]
    try:
        raw = bytes.fromhex(path.read_text().strip())
        if len(raw) < 32:
            raise ValueError("secret too short")
    except (FileNotFoundError, ValueError):
        raw = secrets.token_bytes(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # O_EXCL so concurrent workers can't clobber each other: the
            # first to create the file wins, the losers read the winner's
            # value back — otherwise each worker would cache its own
            # secret and reject the others' cookies (random 401s behind a
            # load balancer).
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, raw.hex().encode("ascii"))
            finally:
                os.close(fd)
        except FileExistsError:
            raw = bytes.fromhex(path.read_text().strip())
    _secret_cache["path"] = path
    _secret_cache["value"] = raw
    return raw


def _request_session_metadata(request: Request | None) -> tuple[str | None, str | None]:
    if request is None:
        return None, None
    user_agent = (request.headers.get("user-agent") or "").strip()[:1024] or None
    source_ip = request.client.host[:128] if request.client and request.client.host else None
    return user_agent, source_ip


def mint_session_token(method: str, *, request: Request | None = None,
                       credential_id: str | None = None,
                       grantee: str | None = None, scope=None,
                       expires_at: int | None = None) -> str:
    """Persist and sign a revocable dashboard session token.

    The durable row is written before the token can reach a caller.  A valid
    HMAC without its matching active row is deliberately not a session.
    """
    issued_at = int(_now())
    expiry = expires_at if expires_at is not None else issued_at + SESSION_TTL_S
    payload = {
        "v": 1,
        "sid": secrets.token_hex(16),
        "method": method,
        "iat": issued_at,
        "exp": expiry,
    }
    secret = _session_secret()
    body = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = _b64url(hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest())
    user_agent, source_ip = _request_session_metadata(request)
    identity_sessions.create_session(
        sid=payload["sid"], method=method, credential_id=credential_id,
        created_at=issued_at, expires_at=expiry, last_activity=_now(),
        user_agent=user_agent, source_ip=source_ip,
        grantee=grantee, scope=scope,
    )
    return f"{body}.{sig}"


def _token_for_persisted_session(*, sid: str, method: str, issued_at: int,
                                 expires_at: int, secret: bytes) -> str:
    """Sign token claims for a session already inserted transactionally."""
    payload = {
        "v": 1, "sid": sid, "method": method,
        "iat": issued_at, "exp": expires_at,
    }
    body = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = _b64url(hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}"


def _verified_session_payload(token: str | None) -> dict | None:
    """Verify only the cookie's HMAC and structural signed claims."""
    if not token or not isinstance(token, str) or token.count(".") != 1:
        return None
    body, sig = token.split(".")
    try:
        expected = hmac.new(_session_secret(), body.encode("ascii"),
                            hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64url_decode(sig)):
            return None
        payload = json.loads(_b64url_decode(body))
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("v") != 1:
        return None
    if not isinstance(payload.get("sid"), str) or not payload["sid"] \
            or not isinstance(payload.get("method"), str) or not payload["method"] \
            or not isinstance(payload.get("iat"), int) \
            or isinstance(payload.get("iat"), bool) \
            or not isinstance(payload.get("exp"), int) \
            or isinstance(payload.get("exp"), bool) \
            or payload["exp"] <= payload["iat"]:
        return None
    return payload


def verify_session_token(token: str | None) -> dict | None:
    """The payload when both cookie and durable revocation state are valid.

    Store absence or failure is fail-closed.  The enrollment check remains
    store-independent, so an unenrolled dashboard still bootstraps open and the
    env-only recovery switch can always bypass this lookup.
    """
    payload = _verified_session_payload(token)
    if payload is None:
        return None
    try:
        active = identity_sessions.check_active(
            sid=payload["sid"], method=payload["method"],
            created_at=payload["iat"], expires_at=payload["exp"], now=_now(),
        )
    except identity_sessions.SessionStoreError:
        return None
    return payload if active else None


def attach_session_cookie(response: Response, request: Request, token: str,
                          *, max_age: int = SESSION_TTL_S) -> None:
    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=max_age, path="/", httponly=True, samesite="lax",
        # Authentication never rides cleartext.  This also avoids trusting a
        # caller-controlled X-Forwarded-Proto header or a proxy that omits it.
        secure=True,
    )


def session_from_request(request: Request) -> dict | None:
    return verify_session_token(request.cookies.get(SESSION_COOKIE))


# ── fail-open-then-enforce ────────────────────────────────────────────

#: Enrollment state is read at most once per TTL; identity_routes busts
#: the cache on writes so enforcement flips the moment enrollment lands.
_ENFORCE_TTL_S = 3.0
_ENROLLMENT_READ_RETRY_S = 0.1
_enforce_cache: dict = {"at": 0.0, "value": None}


def bust_enforce_cache() -> None:
    _enforce_cache["at"] = 0.0


def human_auth_enrolled() -> bool:
    """True the moment ANY human auth method exists — a personal
    identity (password floor) or an enrolled passkey. This is the
    activation condition: False → the gate stays open (bootstrap is
    never locked out); True → the human path requires a session.

    SECURITY: enrollment is always read from explicit personal scope
    (``org=None``). It never follows the request's ``X-Graph-Org`` context or
    the process's ``GRAPH_ORG`` fallback, either of which could otherwise
    split enrollment from shell status and poison the shared cache.
    """
    if _mock_mode():
        return False
    now = _now()
    if _enforce_cache["value"] is not None \
            and now - _enforce_cache["at"] < _ENFORCE_TTL_S:
        return _enforce_cache["value"]
    try:
        for attempt in range(2):
            try:
                personal = _personal_member()
                has_identity = (
                    personal is not None
                    and bool(personal.payload.get("armored_private_key"))
                )
                enrolled = has_identity or bool(_passkey_rows())
                break
            except (GraphDBNotReady, sqlite3.OperationalError):
                if attempt:
                    raise
                time.sleep(_ENROLLMENT_READ_RETRY_S)
    except Exception:
        # Storage unreadable means enrollment is unverifiable, so enforce.
        # A fresh install reads empty and never reaches this branch;
        # DASHBOARD_AUTH remains the recovery path for a wedged store.
        return True
    _enforce_cache["value"] = enrolled
    _enforce_cache["at"] = now
    return enrolled


# ── passkey unlock (WebAuthn assert) ──────────────────────────────────


async def post_unlock_passkey_options(request: Request) -> JSONResponse:
    """Mint WebAuthn authentication options for this host's credentials.

    Challenge/RP ID/origin frozen here, exactly like the register side;
    only credentials enrolled for THIS host are offered (passkeys are
    domain-bound — a localhost credential cannot assert on .ts.net).
    """
    if _mock_mode():
        return JSONResponse({"ok": False,
                             "error": "mock dashboard has no passkeys"},
                            status_code=502)
    try:
        body = await request.json() if await request.body() else {}
    except Exception:
        return JSONResponse({"ok": False, "error": "body must be JSON"},
                            status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "body must be a JSON object"},
                            status_code=400)
    rp_id, origin, rp_err = _rp_from_request(request)
    if rp_err is not None:
        return rp_err

    try:
        rows = [r for r in _passkey_rows()
                if r.payload.get("rp_id") == rp_id]
    except Exception as e:
        return JSONResponse({"ok": False,
                             "error": f"could not read enrolled passkeys: {e}"},
                            status_code=500)
    if not rows:
        return JSONResponse({"ok": False, "fallback": "password", "error": (
            f"no passkey is enrolled for {rp_id!r} — unlock with your "
            "password instead"
        )}, status_code=409)

    from webauthn import generate_authentication_options, options_to_json
    from webauthn.helpers.structs import (
        AuthenticatorTransport,
        PublicKeyCredentialDescriptor,
        UserVerificationRequirement,
    )

    allow = []
    for row in rows:
        try:
            transports = [AuthenticatorTransport(t)
                          for t in row.payload.get("transports") or []]
            allow.append(PublicKeyCredentialDescriptor(
                id=_b64url_decode(row.payload["credential_id"]),
                transports=transports or None,
            ))
        except Exception:
            continue
    if not allow:
        return JSONResponse({"ok": False, "fallback": "password", "error": (
            "no stored credential for this host is readable — unlock with "
            "your password instead"
        )}, status_code=409)

    options = generate_authentication_options(
        rp_id=rp_id,
        allow_credentials=allow,
        # The access factor: the person must be verified (Face ID /
        # Touch ID / PIN), not merely present.
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    # Multiple browsers may legitimately start on the same RP ID.  Each
    # challenge is already single-use; keep concurrent ceremonies while the
    # global FIFO bound prevents abandoned-option memory growth.
    _prune(_assert_pending, reserve=1)
    _assert_pending[_b64url(options.challenge)] = {
        "rp_id": rp_id,
        "origin": origin,
        "expires": _now() + PENDING_TTL_S,
    }
    return JSONResponse({
        "ok": True,
        "rp_id": rp_id,
        "origin": origin,
        "options": json.loads(options_to_json(options)),
    })


async def post_unlock_passkey(request: Request) -> JSONResponse:
    """Verify an authenticator's assertion → mint the dashboard session.

    Body: ``{credential: <AuthenticationResponseJSON>}``. The
    pending ceremony is consumed on lookup (single-use); verification is
    pinned to the tuple frozen at options time and to the STORED
    credential public key. The stored sign count advances on success —
    py_webauthn rejects a regression (clone signal) for us.
    """
    if _mock_mode():
        return JSONResponse({"ok": False,
                             "error": "mock dashboard has no passkeys"},
                            status_code=502)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "body must be JSON"},
                            status_code=400)
    if not isinstance(body, dict) or not isinstance(body.get("credential"), dict):
        return JSONResponse({"ok": False, "error": (
            "body must carry 'credential' — the JSON-serialized "
            "navigator.credentials.get() result"
        )}, status_code=400)
    credential = body["credential"]
    challenge_key = _client_challenge(credential)
    _prune(_assert_pending)
    pending = _assert_pending.pop(challenge_key, None) if challenge_key else None
    if pending is None:
        return JSONResponse({"ok": False, "error": (
            "no pending unlock ceremony matches this response — request "
            "fresh options and retry (challenges are single-use and expire "
            "after 10 minutes)"
        )}, status_code=400)
    rp_id, origin, rp_err = _rp_from_request(request)
    if rp_err is not None:
        return rp_err
    if rp_id != pending["rp_id"] or origin != pending["origin"]:
        return JSONResponse({"ok": False, "error": (
            f"this unlock ceremony was started on {pending['origin']} but "
            f"is being completed from {origin} — ceremonies must complete "
            "on the host that minted them"
        )}, status_code=400)

    raw_id = credential.get("rawId") or credential.get("id")
    if not isinstance(raw_id, str):
        return JSONResponse({"ok": False, "error": (
            "the credential response carries no id"
        )}, status_code=400)
    try:
        row = next((r for r in _passkey_rows()
                    if r.payload.get("credential_id") == raw_id), None)
    except Exception as e:
        return JSONResponse({"ok": False,
                             "error": f"could not read enrolled passkeys: {e}"},
                            status_code=500)
    if row is None or row.payload.get("rp_id") != pending["rp_id"]:
        return JSONResponse({"ok": False, "error": (
            "this credential is not enrolled for this host"
        )}, status_code=403)

    from webauthn import verify_authentication_response
    from webauthn.helpers.exceptions import InvalidAuthenticationResponse

    try:
        verification = verify_authentication_response(
            credential=credential,
            expected_challenge=_b64url_decode(challenge_key),
            expected_rp_id=pending["rp_id"],
            expected_origin=pending["origin"],
            credential_public_key=_b64url_decode(row.payload["public_key"]),
            credential_current_sign_count=int(row.payload.get("sign_count") or 0),
            require_user_verification=True,
        )
    except InvalidAuthenticationResponse as e:
        return JSONResponse({"ok": False, "error": (
            f"passkey assertion did not verify: {e}"
        )}, status_code=403)
    except Exception as e:
        return JSONResponse({"ok": False, "error": (
            f"malformed passkey assertion response: {e}"
        )}, status_code=400)

    payload = dict(row.payload)
    payload["sign_count"] = verification.new_sign_count
    try:
        # Advancing the stored sign count is a write to a protected
        # identity set; the unlock route carries the capability.
        with settings_ops.identity_write_context():
            settings_ops.upsert_by_key(
                PASSKEY_SET_ID, PASSKEY_REVISION, row.key, payload, org=None,
            )
    except Exception as e:
        return JSONResponse({"ok": False, "error": (
            f"could not advance the credential sign count: {e}"
        )}, status_code=500)

    response = JSONResponse({"ok": True, "method": "passkey"})
    try:
        token = mint_session_token(
            "passkey", request=request, credential_id=raw_id,
        )
    except (OSError, identity_sessions.SessionStoreError) as exc:
        return JSONResponse({"ok": False, "error": (
            "the credential verified, but the dashboard could not create a "
            f"revocable session: {exc}"
        )}, status_code=503)
    attach_session_cookie(response, request, token)
    return response


# ── password unlock (the floor) ───────────────────────────────────────


async def post_unlock_password_options(request: Request) -> JSONResponse:
    """Mint a single-use challenge for the password-unlock proof.

    The browser decrypts the personal armor with the password (locally —
    I1) and signs this challenge with the root key; possession of the
    decrypted root IS the password proof.
    """
    if _mock_mode():
        return JSONResponse({"ok": False,
                             "error": "mock dashboard has no identity"},
                            status_code=502)
    try:
        body = await request.json() if await request.body() else {}
    except Exception:
        return JSONResponse({"ok": False, "error": "body must be JSON"},
                            status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "body must be a JSON object"},
                            status_code=400)
    rp_id, origin, rp_err = _rp_from_request(request)
    if rp_err is not None:
        return rp_err
    try:
        personal = _personal_member()
    except Exception as e:
        return JSONResponse({"ok": False,
                             "error": f"could not read the personal identity: {e}"},
                            status_code=500)
    if personal is None or not personal.payload.get("armored_private_key"):
        return JSONResponse({"ok": False, "error": (
            "no personal identity is stored — there is nothing a password "
            "can unlock"
        )}, status_code=409)

    _prune(_pw_pending, reserve=1)
    challenge = secrets.token_hex(32)
    _pw_pending[challenge] = {
        "origin": origin,
        "expires": _now() + PENDING_TTL_S,
    }
    return JSONResponse({"ok": True, "challenge": challenge, "origin": origin,
                         "domain": UNLOCK_SIGNING_DOMAIN.decode("ascii")})


async def post_unlock_password(request: Request) -> JSONResponse:
    """Verify the root-key signature over the challenge → mint the session.

    Body: ``{challenge, signature}`` where ``signature`` is hex
    Ed25519 over ``UNLOCK_SIGNING_DOMAIN + canonical_json({v, challenge,
    origin})``. Verified against the STORED ``root_pub`` — the client
    never says which key it used.
    """
    if _mock_mode():
        return JSONResponse({"ok": False,
                             "error": "mock dashboard has no identity"},
                            status_code=502)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "body must be JSON"},
                            status_code=400)
    if not isinstance(body, dict) or not isinstance(body.get("challenge"), str) \
            or not isinstance(body.get("signature"), str):
        return JSONResponse({"ok": False, "error": (
            "body must carry 'challenge' and 'signature' (hex)"
        )}, status_code=400)
    _prune(_pw_pending)
    pending = _pw_pending.pop(body["challenge"], None)
    if pending is None:
        return JSONResponse({"ok": False, "error": (
            "no pending unlock challenge matches — request a fresh one and "
            "retry (challenges are single-use and expire after 10 minutes)"
        )}, status_code=400)
    rp_id, origin, rp_err = _rp_from_request(request)
    if rp_err is not None:
        return rp_err
    if origin != pending["origin"]:
        return JSONResponse({"ok": False, "error": (
            f"this unlock challenge was minted on {pending['origin']} but "
            f"is being completed from {origin} — complete it on the host "
            "that minted it"
        )}, status_code=400)

    try:
        personal = _personal_member()
    except Exception as e:
        return JSONResponse({"ok": False,
                             "error": f"could not read the personal identity: {e}"},
                            status_code=500)
    if personal is None or not personal.payload.get("armored_private_key"):
        return JSONResponse({"ok": False, "error": (
            "no personal identity is stored"
        )}, status_code=409)
    root_pub = personal.payload.get("root_pub")
    if not root_pub:
        # Older rows might omit the optional column; the armor always
        # encloses the public half.
        from tools.network.idkit.armor import parse_armor
        try:
            root_pub = parse_armor(personal.payload["armored_private_key"])["root_pub"]
        except Exception as e:
            return JSONResponse({"ok": False, "error": (
                f"the stored identity's public key is unreadable: {e}"
            )}, status_code=500)

    from tools.network.idkit.canonical import canonical_json
    from tools.network.idkit.errors import IdkitError
    from tools.network.idkit.keys import verify_signature

    message = UNLOCK_SIGNING_DOMAIN + canonical_json({
        "v": 1,
        "challenge": body["challenge"],
        "origin": pending["origin"],
    })
    try:
        verify_signature(root_pub, body["signature"], message)
    except IdkitError:
        return JSONResponse({"ok": False, "error": (
            "the unlock proof does not verify — the signing key is not "
            "this dashboard's personal root"
        )}, status_code=403)

    response = JSONResponse({"ok": True, "method": "password",
                             "display_name": personal.payload.get("display_name")})
    try:
        token = mint_session_token("password", request=request)
    except (OSError, identity_sessions.SessionStoreError) as exc:
        return JSONResponse({"ok": False, "error": (
            "the password proof verified, but the dashboard could not create "
            f"a revocable session: {exc}"
        )}, status_code=503)
    attach_session_cookie(response, request, token)
    return response


# ── operator-approved headless unlock ─────────────────────────────────


async def post_unlock_approval(request: Request) -> JSONResponse:
    """Redeem one personal-root-approved grant with ephemeral-key proof."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "body must be JSON"},
                            status_code=400)
    if not isinstance(body, dict) or set(body) != {"nonce", "proof"} \
            or not isinstance(body.get("nonce"), str) \
            or not isinstance(body.get("proof"), str):
        return JSONResponse({"ok": False, "error": (
            "body must carry only 'nonce' and 'proof' strings"
        )}, status_code=400)
    if not _valid_approval_nonce(body["nonce"]):
        return JSONResponse({"ok": False,
                             "error": "nonce must be 64 lowercase hex characters"},
                            status_code=400)
    try:
        grant = identity_sessions.get_access_grant(body["nonce"])
    except identity_sessions.SessionStoreError as exc:
        return JSONResponse({"ok": False, "error": (
            f"could not read the dashboard access grant: {exc}"
        )}, status_code=503)
    if grant is None:
        return JSONResponse({"ok": False, "error": "access grant not found"},
                            status_code=404)
    if grant["consumed_at"] is not None:
        return JSONResponse({"ok": False,
                             "error": "access grant has already been used"},
                            status_code=409)
    now = _now()
    if grant["expires_at"] <= now:
        return JSONResponse({"ok": False, "error": "access grant has expired"},
                            status_code=410)

    from tools.network.idkit.canonical import canonical_json
    from tools.network.idkit.errors import IdkitError
    from tools.network.idkit.keys import verify_signature

    proof_input = APPROVAL_REDEEM_SIGNING_DOMAIN + canonical_json({
        "v": 1, "nonce": body["nonce"],
    })
    try:
        verify_signature(grant["ephemeral_pub"], body["proof"], proof_input)
    except IdkitError:
        return JSONResponse({"ok": False,
                             "error": "the ephemeral-key proof does not verify"},
                            status_code=403)

    # Load the cookie key before the atomic datastore transition. If its file
    # cannot be read or created, the grant remains unconsumed and retryable.
    try:
        secret = _session_secret()
    except OSError as exc:
        return JSONResponse({"ok": False,
                             "error": f"could not load the session key: {exc}"},
                            status_code=503)
    issued_at = int(now)
    sid = secrets.token_hex(16)
    user_agent, source_ip = _request_session_metadata(request)
    try:
        redeemed = identity_sessions.redeem_access_grant(
            nonce=body["nonce"], sid=sid, now=now, created_at=issued_at,
            user_agent=user_agent, source_ip=source_ip,
        )
    except (ValueError, identity_sessions.SessionStoreError) as exc:
        return JSONResponse({"ok": False,
                             "error": f"could not create the approved session: {exc}"},
                            status_code=503)
    if redeemed["status"] == "not_found":
        return JSONResponse({"ok": False, "error": "access grant not found"},
                            status_code=404)
    if redeemed["status"] == "expired":
        return JSONResponse({"ok": False, "error": "access grant has expired"},
                            status_code=410)
    if redeemed["status"] == "consumed":
        return JSONResponse({"ok": False,
                             "error": "access grant has already been used"},
                            status_code=409)

    token = _token_for_persisted_session(
        sid=redeemed["sid"], method="approval",
        issued_at=redeemed["created_at"], expires_at=redeemed["expires_at"],
        secret=secret,
    )
    response = JSONResponse({
        "ok": True,
        "method": "approval",
        "grantee": redeemed["grantee"],
        "scope": redeemed["scope"],
        "expires_at": redeemed["expires_at"],
    })
    attach_session_cookie(
        response, request, token,
        max_age=max(1, redeemed["expires_at"] - issued_at),
    )
    return response


# ── session chrome + lock ─────────────────────────────────────────────


async def get_session(request: Request) -> JSONResponse:
    """What the chrome needs: is the gate enforced, are we unlocked."""
    if _mock_mode():
        return JSONResponse({"enforced": False, "unlocked": False,
                             "method": None,
                             "gate_disabled": gate_disabled()})
    payload = session_from_request(request)
    disabled = gate_disabled()
    return JSONResponse({
        # 'enforced' is what the gate actually DOES right now — the
        # kill-switch zeroes it even while enrollment exists, so the
        # chrome renders the forced-open marker instead of 'Locked'.
        "enforced": human_auth_enrolled() and not disabled,
        "unlocked": payload is not None,
        "method": (payload or {}).get("method"),
        "expires_at": (payload or {}).get("exp"),
        "gate_disabled": disabled,
    })


async def post_lock(request: Request) -> JSONResponse:
    """End this browser's server session and drop only its cookie."""
    payload = _verified_session_payload(request.cookies.get(SESSION_COOKIE))
    if payload is not None:
        try:
            identity_sessions.end_session(
                payload["sid"], reason="locked", now=_now(),
            )
        except identity_sessions.SessionStoreError as exc:
            # Still clear this browser's cookie.  A 503 is honest that the
            # durable row could not be changed; verification remains fail-closed
            # while the store is unavailable and the operator can retry.
            response = JSONResponse({"ok": False, "error": (
                f"could not end the server-side dashboard session: {exc}"
            )}, status_code=503)
            response.delete_cookie(SESSION_COOKIE, path="/")
            return response
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


# ── the gate ──────────────────────────────────────────────────────────

#: Enrollment endpoints that must NOT stay open once enforcement is on:
#: an open register path would let anyone on the network enroll their
#: own credential and mint themselves a session. Agents never call
#: these. (POST /api/identity/personal needs no entry here — it refuses
#: overwrite with 409 once an identity exists, and an identity existing
#: is exactly when enforcement is on.)
_GATED_API_PATHS = frozenset({
    "/api/identity/passkey/register-options",
    "/api/identity/passkey/register",
})

#: The agent/container surface (graph CLI, dispatcher — ``/api/...``)
#: and the assets the Unlock page itself needs. Everything else is the
#: human path.
#:
#: ``/missions/`` is the one deliberate exception to "everything else is
#: the human path": Mission Control's chromeless site-serving route
#: (``/missions/<mission_id>``), operator-ratified to bypass the gate the
#: same way Present's high-entropy deck links do — the operator hands
#: these URLs to team members outside the dashboard, and the id itself
#: (a UUID4) is the access control until P2's identity shim exists. This
#: does NOT cover ``/mission-control`` (no trailing slash, hyphenated) —
#: that's the plugin's own management page and stays gated.
_OPEN_PREFIXES = ("/api/", "/static/", "/missions/")


def _path_is_gated(path: str) -> bool:
    if path in _GATED_API_PATHS:
        return True
    if path == "/unlock":
        return False
    return not path.startswith(_OPEN_PREFIXES)


def _cookie_from_scope(scope) -> str | None:
    for name, value in scope.get("headers") or []:
        if name == b"cookie":
            for part in value.decode("latin-1").split(";"):
                k, _, v = part.strip().partition("=")
                if k == SESSION_COOKIE:
                    return v
    return None


class HumanGateMiddleware:
    """Enforce the dashboard session on the human/browser path only.

    Pure ASGI (not BaseHTTPMiddleware) so websocket handshakes are
    covered too: an unauthenticated ``/ws/terminal`` connect would
    otherwise hand out a shell prompt without ever loading a page.

    Fail-open-then-enforce: :func:`human_auth_enrolled` decides per
    request (cached, write-busted). :func:`gate_disabled` — the env-only
    DASHBOARD_AUTH recovery switch — short-circuits enforcement entirely
    and is checked before the enrollment read so a wedged settings DB
    can't defeat the escape hatch. Refusals: page loads redirect to
    ``/unlock?next=…`` so the human lands on the mockup's lock screen;
    fragment/API-shaped requests get 401 JSON; websockets get a 4401
    handshake close.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if not _path_is_gated(path):
            await self.app(scope, receive, send)
            return
        if gate_disabled():
            await self.app(scope, receive, send)
            return
        if not human_auth_enrolled():
            await self.app(scope, receive, send)
            return
        if verify_session_token(_cookie_from_scope(scope)) is not None:
            await self.app(scope, receive, send)
            return

        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4401})
            return
        if scope.get("method") in ("GET", "HEAD") \
                and not path.startswith("/pages/") \
                and path not in _GATED_API_PATHS:
            nxt = path
            query = scope.get("query_string") or b""
            if query:
                nxt += "?" + query.decode("latin-1")
            response: Response = RedirectResponse(
                "/unlock?next=" + urllib.parse.quote(nxt, safe=""),
                status_code=302)
        else:
            response = JSONResponse({"error": (
                "the dashboard is locked — unlock it first"
            ), "unlock": "/unlock"}, status_code=401,
                headers={"x-autonomy-unlock": "/unlock"})
        await response(scope, receive, send)


def sanitize_next(raw: str | None) -> str:
    """A same-site path for the post-unlock redirect, never an absolute
    or protocol-relative URL (open-redirect guard)."""
    if not raw or not raw.startswith("/") or raw.startswith("//") \
            or "\\" in raw or ":" in raw.split("?", 1)[0]:
        return "/"
    return raw


ROUTES = [
    Route("/api/identity/unlock/passkey/options", post_unlock_passkey_options,
          methods=["POST"]),
    Route("/api/identity/unlock/passkey", post_unlock_passkey,
          methods=["POST"]),
    Route("/api/identity/unlock/password/options", post_unlock_password_options,
          methods=["POST"]),
    Route("/api/identity/unlock/password", post_unlock_password,
          methods=["POST"]),
    Route("/api/identity/unlock/approval", post_unlock_approval,
          methods=["POST"]),
    Route("/api/identity/session", get_session, methods=["GET"]),
    Route("/api/identity/lock", post_lock, methods=["POST"]),
]
