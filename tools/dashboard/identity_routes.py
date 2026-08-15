"""Personal identity + passkey enrollment routes — the onboarding server side.

Backs the "Get started" flow in ``static/js/network-onboarding.js``
(model notes ``graph://53f65f2f-d73`` two-gate matrix, ``graph://
80ef5131-9f0`` fail-open-then-enforce, mockup design d49be06b). This
module is ENROLLMENT ONLY: it creates state; the sign-in gate that
enforces it lives in :mod:`unlock_routes` (passkey assert + password
unlock + the HumanGateMiddleware). Two touch points back: a successful
``POST /api/identity/personal`` mints the BOOTSTRAP session (the
enrolling browser must survive the gate turning itself on mid-flow),
and both write paths bust the gate's enrollment cache.

* ``GET  /api/identity/status`` — what exists: personal identity
  (public metadata only), enrolled passkeys, and whether onboarding is
  still needed (no personal identity OR no passkey — the activation
  condition, true for accounts that predate the identity system: running
  the flow ADDS identity on top of existing data).
* ``GET/POST /api/identity/personal`` — the PERSONAL root key, distinct
  from the org key (``/api/network/org-key``). Same I1 discipline: the
  server stores and serves the password-encrypted armor only; the
  canonical-armor check lives in the ``autonomy.identity.personal``
  schema so every write path shares it. Refuses overwrite (409) — one
  personal root per person; replacing it is a recovery concern.
* ``POST /api/identity/passkey/register-options`` — mints WebAuthn
  registration options (py_webauthn). The RP ID is derived from the
  REQUEST host, so the same dashboard enrolls working passkeys on both
  ``localhost`` and its ``.ts.net`` name (passkeys are domain-bound).
  The challenge + RP ID + origin are frozen server-side per ceremony;
  the verify step accepts only what this step pinned.
* ``POST /api/identity/passkey/register`` — verifies the authenticator's
  attestation response against the pinned challenge/origin/RP ID and
  stores the credential (``autonomy.identity.passkey``, keyed by
  credential id — one row per enrolled device/install). Ceremony
  binding (Codex validation): the completion request must arrive on the
  SAME host that minted the options, and minting new options
  invalidates prior pending ceremonies for the same personal identity + RP ID.
  Tested rejections: unknown/expired/replayed/superseded challenge,
  cross-host completion, origin or RP ID mismatch, duplicate
  credential, malformed attestation.

The pending-challenge store is in-process (the dashboard runs as one
uvicorn process); a restart mid-ceremony just means re-requesting
options, which the browser flow does anyway.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import time

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from tools.graph import settings_ops
from tools.dashboard.network_routes import _first_member, _mock_mode
# Importing registers the autonomy.identity.* Setting schemas.
from tools.graph.schemas.personal_identity import (  # noqa: F401
    PASSKEY_REVISION,
    PASSKEY_SET_ID,
    PASSKEY_TRANSPORTS,
    PERSONAL_IDENTITY_REVISION,
    PERSONAL_IDENTITY_SET_ID,
    _RP_ID_RE,
)

RP_NAME = "Autonomy"

#: A registration ceremony (options → authenticator → verify) is a human
#: gesture away; 10 minutes is generous, and expiry is a tested rejection.
PENDING_TTL_S = 600
#: Backstop against an abandoned-ceremony flood; oldest rows fall off.
PENDING_MAX = 64

#: challenge(b64url, unpadded) → {rp_id, origin, expires}
_pending: dict[str, dict] = {}


def _now() -> float:
    return time.time()


def _prune_pending(*, reserve: int = 0) -> None:
    cutoff = _now()
    for challenge in [c for c, p in _pending.items() if p["expires"] <= cutoff]:
        _pending.pop(challenge, None)
    limit = max(PENDING_MAX - reserve, 0)
    while len(_pending) > limit:
        _pending.pop(next(iter(_pending)))


def _is_ip_literal(hostname: str) -> bool:
    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        return False


def _rp_from_request(request: Request):
    """Derive (rp_id, origin) from the request the browser actually made.

    The RP ID is the request's host name — that is the WebAuthn contract
    (credentials are scoped to the domain the page is on), and it is what
    makes the SAME dashboard enroll working passkeys on ``localhost`` AND
    on its ``.ts.net`` name. IP literals cannot be RP IDs (WebAuthn
    requires a registrable domain; ``localhost`` is the blessed
    exception), so those are refused with a pointer to use a name.

    Returns ``(rp_id, origin, None)`` or ``(None, None, JSONResponse)``.
    """
    host = (request.headers.get("host") or "").strip()
    if not host:
        return None, None, JSONResponse(
            {"error": "request carries no Host header — cannot derive the "
                      "passkey RP ID"}, status_code=400)
    if host.startswith("["):  # bracketed IPv6 literal, with or without port
        hostname = host.split("]")[0].lstrip("[")
    else:
        hostname = host.rsplit(":", 1)[0] if ":" in host else host
    hostname = hostname.strip().lower().rstrip(".")
    if _is_ip_literal(hostname):
        return None, None, JSONResponse(
            {"error": (
                f"passkeys cannot be bound to an IP address ({hostname!r}) — "
                "open the dashboard by name (localhost or its .ts.net name) "
                "to enroll"
            )}, status_code=400)
    if not _RP_ID_RE.match(hostname):
        return None, None, JSONResponse(
            {"error": f"request host {hostname!r} is not a valid domain name"},
            status_code=400)
    scheme = (request.headers.get("x-forwarded-proto")
              or request.url.scheme or "https").split(",")[0].strip()
    return hostname, f"{scheme}://{host.lower()}", None


#: The one label a personal identity is ever written under (post_personal
#: defaults to it and refuses overwrite — one root per person).
PERSONAL_CANONICAL_LABEL = "default"


def _personal_member():
    """The canonical personal identity row.

    Defense-in-depth against a shadowing row: ``_first_member`` picks the
    lexically-FIRST key, so a stray ``autonomy.identity.personal`` row
    with a low-sorting key (e.g. ``000-…``) would shadow the operator's
    ``default`` and be verified against on password unlock. The write
    guard (settings_ops.PROTECTED_IDENTITY_SET_IDS) blocks such a row from
    ever being injected via the generic API, but the selection is pinned
    to the canonical ``default`` label anyway so the gate can never be
    fooled by key ordering. Only when no ``default`` exists (legacy rows
    predating this label) does it fall back to the first member.
    """
    members = [m for m in settings_ops.read_owned_set(PERSONAL_IDENTITY_SET_ID,
                                                      org=None).members
               if isinstance(m.payload, dict)]
    for m in members:
        if m.key == PERSONAL_CANONICAL_LABEL:
            return m
    return _first_member(PERSONAL_IDENTITY_SET_ID, None)


def _passkey_rows():
    members = sorted(
        settings_ops.read_owned_set(PASSKEY_SET_ID, org=None).members,
        key=lambda m: m.key,
    )
    return [m for m in members if isinstance(m.payload, dict)]


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


# ── status ────────────────────────────────────────────────────────────


async def get_status(request: Request) -> JSONResponse:
    """Personal identity, enrollment, and current dashboard access state.

    Personal identity never follows the caller-org context. The shell stamps
    ``X-Graph-Org`` on app data requests, and allowing that header to select
    this read would make the same person appear to vanish between org views.
    A literal ``org=None`` pins the read to the personal database.
    """
    # Late import: unlock_routes imports from this module at load time.
    from tools.dashboard.unlock_routes import (
        gate_disabled,
        human_auth_enrolled,
        session_from_request,
    )
    disabled = gate_disabled()
    if _mock_mode():
        # The mock dashboard has no settings DB; land deterministically
        # enrolled-enough that no onboarding overlay covers the fixtures.
        return JSONResponse({"personal_identity": None, "passkeys": [],
                             "onboarding_needed": False,
                             "rp_id": None, "passkeys_for_host": 0,
                             "signed_in": False, "method": None,
                             "enforced": False,
                             "gate_disabled": disabled})
    rp_id, _origin, _rp_err = _rp_from_request(request)
    try:
        personal = _personal_member()
        passkeys = _passkey_rows()
    except Exception as e:
        return JSONResponse({"error": f"could not read identity settings: {e}"},
                            status_code=500)
    session = session_from_request(request)
    identity = None
    if personal is not None and personal.payload.get("armored_private_key"):
        identity = {
            "label": personal.key,
            "display_name": personal.payload.get("display_name"),
            "root_pub": personal.payload.get("root_pub"),
            "created_at": personal.payload.get("created_at"),
        }
    rows = [{
        "credential_id": m.payload.get("credential_id"),
        "label": m.payload.get("label"),
        "rp_id": m.payload.get("rp_id"),
        "transports": m.payload.get("transports") or [],
        "created_at": m.payload.get("created_at"),
    } for m in passkeys]
    return JSONResponse({
        "personal_identity": identity,
        "passkeys": rows,
        # The activation condition (this bead's §4): no personal identity
        # or no passkey anywhere → the Get-started flow applies, including
        # on accounts that predate the identity system.
        "onboarding_needed": identity is None or not rows,
        # Which of the stored passkeys could assert on THIS host —
        # informational today, the gate's input later.
        "rp_id": rp_id,
        "passkeys_for_host": sum(1 for r in rows if r["rp_id"] == rp_id),
        "signed_in": session is not None,
        "method": (session or {}).get("method"),
        "enforced": human_auth_enrolled() and not disabled,
        # DASHBOARD_AUTH kill-switch state — the indicator renders its
        # forced-open marker from this, never from probing the gate.
        "gate_disabled": disabled,
    })


# ── personal identity (I1: encrypted armor only) ──────────────────────


async def get_personal(request: Request) -> JSONResponse:
    """The armored (password-encrypted) personal root key, or 404."""
    if _mock_mode():
        return JSONResponse({"error": "no personal identity configured"},
                            status_code=404)
    try:
        member = _personal_member()
    except Exception as e:
        return JSONResponse({"error": f"could not read the personal identity: {e}"},
                            status_code=500)
    if member is None or not member.payload.get("armored_private_key"):
        return JSONResponse({"error": (
            "no personal identity is stored — run the Get started flow first"
        )}, status_code=404)
    return JSONResponse({
        "label": member.key,
        "display_name": member.payload.get("display_name"),
        "armored_private_key": member.payload["armored_private_key"],
        "root_pub": member.payload.get("root_pub"),
        "created_at": member.payload.get("created_at"),
    })


async def post_personal(request: Request) -> JSONResponse:
    """Store the personal root key's password-encrypted armor (step 1, 'You').

    Body: ``{label?, display_name, armored_private_key, root_pub?}``.
    The canonical-armor check (I1) lives in the PersonalIdentityV1 schema
    — shared by every write path — but is ALSO applied here so the error
    surfaces as a 400 with a clear message rather than a schema string.
    An existing personal identity gets 409: one root per person, and the
    create flow never overwrites it.
    """
    if _mock_mode():
        return JSONResponse({"ok": False,
                             "error": "mock dashboard stores no identities"},
                            status_code=502)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "body must be JSON"},
                            status_code=400)
    if not isinstance(body, dict) or not isinstance(body.get("armored_private_key"), str):
        return JSONResponse({"ok": False, "error": (
            "body must carry 'armored_private_key' as the armor text"
        )}, status_code=400)
    display_name = body.get("display_name")
    if not isinstance(display_name, str) or not display_name.strip() \
            or len(display_name) > 120:
        return JSONResponse({"ok": False, "error": (
            "body must carry 'display_name' as a non-empty string (at most "
            "120 chars)"
        )}, status_code=400)
    display_name = display_name.strip()

    from tools.network.idkit.armor import (
        ArmorError,
        armor_root_pub,
        canonicalize_armor_any,
    )
    try:
        # Either armor version: existing identities are v1, new ones are v2.
        canonical_armor = canonicalize_armor_any(body["armored_private_key"])
        armor_data = {"root_pub": armor_root_pub(canonical_armor)}
    except ArmorError as e:
        return JSONResponse({"ok": False, "error": (
            f"refusing to store: not a canonical password-encrypted key "
            f"armor (I1 — plaintext key material must never be persisted): {e}"
        )}, status_code=400)
    root_pub = armor_data["root_pub"]
    if body.get("root_pub") is not None and body["root_pub"] != root_pub:
        return JSONResponse({"ok": False, "error": (
            "root_pub does not match the armor's enclosed public key"
        )}, status_code=400)

    label = body.get("label") or "default"
    if not isinstance(label, str) or len(label) > 64:
        return JSONResponse({"ok": False, "error": "label must be a short string"},
                            status_code=400)
    try:
        existing = _personal_member()
    except Exception as e:
        return JSONResponse({"ok": False,
                             "error": f"could not read the personal identity: {e}"},
                            status_code=500)
    if existing is not None:
        return JSONResponse({"ok": False, "error": (
            "a personal identity already exists — the Get started flow "
            "never overwrites your root key"
        )}, status_code=409)
    payload = {
        "armored_private_key": canonical_armor,
        "root_pub": root_pub,
        "display_name": display_name,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    try:
        # The identity sets are write-protected against the generic
        # settings API; the enrollment routes carry the capability.
        with settings_ops.identity_write_context():
            settings_ops.upsert_by_key(
                PERSONAL_IDENTITY_SET_ID, PERSONAL_IDENTITY_REVISION, label,
                payload, org=None,
            )
    except Exception as e:
        return JSONResponse({"ok": False,
                             "error": f"could not store the personal identity: {e}"},
                            status_code=500)
    response = JSONResponse({"ok": True, "label": label, "root_pub": root_pub,
                             "display_name": display_name})
    # Creating the identity is the moment the unlock gate turns itself ON
    # (fail-open-then-enforce). The browser that created it — in the
    # fail-open window, by definition the operator's — gets the FIRST
    # session, so onboarding continues seamlessly into the now-gated
    # passkey-enrollment step instead of locking its own user out.
    #
    # Best-effort: if the session-secret store is unwritable (read-only
    # mount, full disk) minting throws — but the identity is already
    # persisted, and failing the whole request here would 500 AFTER the
    # write, leaving the operator with a 409-on-retry and a gate they
    # can't pass. Swallow it: they land on /unlock and the password floor
    # still works (it re-mints on the same store, surfacing the real
    # error there if it persists).
    from tools.dashboard import unlock_routes
    unlock_routes.bust_enforce_cache()
    try:
        unlock_routes.attach_session_cookie(
            response, request,
            unlock_routes.mint_session_token("bootstrap", request=request),
        )
    except Exception:
        pass
    return response


# ── passkey enrollment (WebAuthn) ─────────────────────────────────────


async def post_register_options(request: Request) -> JSONResponse:
    """Mint WebAuthn registration options (step 2, 'This device').

    Requires the personal identity to exist first — the flow order is
    You → This device, and the identity's public key is the stable
    WebAuthn user handle. The challenge, RP ID, and origin are frozen
    here, server-side; the verify step accepts only this tuple.
    """
    if _mock_mode():
        return JSONResponse({"ok": False,
                             "error": "mock dashboard enrolls no passkeys"},
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
    if personal is None or not personal.payload.get("root_pub"):
        return JSONResponse({"ok": False, "error": (
            "create your personal identity first — the passkey enrolls "
            "against it"
        )}, status_code=409)

    from webauthn import generate_registration_options, options_to_json
    from webauthn.helpers.structs import (
        AuthenticatorSelectionCriteria,
        PublicKeyCredentialDescriptor,
        ResidentKeyRequirement,
        UserVerificationRequirement,
    )

    try:
        existing = _passkey_rows()
    except Exception as e:
        return JSONResponse({"ok": False,
                             "error": f"could not read enrolled passkeys: {e}"},
                            status_code=500)
    exclude = []
    for row in existing:
        if row.payload.get("rp_id") != rp_id:
            continue
        try:
            exclude.append(PublicKeyCredentialDescriptor(
                id=_b64url_decode(row.payload["credential_id"])))
        except Exception:
            continue

    options = generate_registration_options(
        rp_id=rp_id,
        rp_name=RP_NAME,
        user_id=bytes.fromhex(personal.payload["root_pub"]),
        user_name=personal.payload.get("display_name") or "you",
        user_display_name=personal.payload.get("display_name") or "you",
        # A passkey proper: discoverable on the device, user-verified
        # (Face ID / Touch ID / PIN) — this is the Gate-1 access factor.
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=exclude or None,
    )

    # Two browsers can enroll concurrently on the same RP ID.  Challenges are
    # independently single-use; exact FIFO capacity bounds abandoned options.
    _prune_pending(reserve=1)
    challenge_key = _b64url(options.challenge)
    _pending[challenge_key] = {
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


def _client_challenge(credential: dict) -> str | None:
    """The challenge the authenticator actually signed over, from
    clientDataJSON — used ONLY to look up the pending ceremony; the
    cryptographic comparison is py_webauthn's."""
    try:
        raw = _b64url_decode(credential["response"]["clientDataJSON"])
        parsed = json.loads(raw)
        challenge = parsed.get("challenge")
        return challenge if isinstance(challenge, str) else None
    except Exception:
        return None


async def post_register(request: Request) -> JSONResponse:
    """Verify + store an authenticator's registration response.

    Body: ``{label?, credential: <RegistrationResponseJSON>}``.
    The pending ceremony is looked up by the challenge inside the
    response's clientDataJSON and CONSUMED (single use — a replay of the
    same response is a tested rejection). Verification is pinned to the
    tuple frozen at options time: challenge bytes, RP ID, origin.
    """
    if _mock_mode():
        return JSONResponse({"ok": False,
                             "error": "mock dashboard enrolls no passkeys"},
                            status_code=502)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "body must be JSON"},
                            status_code=400)
    if not isinstance(body, dict) or not isinstance(body.get("credential"), dict):
        return JSONResponse({"ok": False, "error": (
            "body must carry 'credential' — the JSON-serialized "
            "navigator.credentials.create() result"
        )}, status_code=400)
    label = body.get("label") or "This device"
    if not isinstance(label, str) or not label.strip() or len(label) > 120:
        return JSONResponse({"ok": False, "error": "label must be a short string"},
                            status_code=400)
    label = label.strip()

    credential = body["credential"]
    challenge_key = _client_challenge(credential)
    _prune_pending()
    pending = _pending.pop(challenge_key, None) if challenge_key else None
    if pending is None:
        return JSONResponse({"ok": False, "error": (
            "no pending passkey ceremony matches this response — request "
            "fresh registration options and retry (challenges are single-"
            "use and expire after 10 minutes)"
        )}, status_code=400)
    # The COMPLETION request must arrive on the same host the ceremony
    # was minted for (Codex finding 1): without this, a credential could
    # be stored bound to a host the user isn't actually on — passkeys
    # are domain-bound, so that row could never assert where the user
    # signs in. The ceremony is already consumed (single-use): a
    # cross-host completion burns it; re-request options on the right
    # host.
    rp_id, origin, rp_err = _rp_from_request(request)
    if rp_err is not None:
        return rp_err
    if rp_id != pending["rp_id"] or origin != pending["origin"]:
        return JSONResponse({"ok": False, "error": (
            f"this passkey ceremony was started on {pending['origin']} but "
            f"is being completed from {origin} — ceremonies must complete "
            "on the host that minted them; request fresh registration "
            "options from this host"
        )}, status_code=400)

    from webauthn import verify_registration_response
    from webauthn.helpers.exceptions import InvalidRegistrationResponse

    try:
        verification = verify_registration_response(
            credential=credential,
            expected_challenge=_b64url_decode(challenge_key),
            expected_rp_id=pending["rp_id"],
            expected_origin=pending["origin"],
            require_user_verification=True,
        )
    except InvalidRegistrationResponse as e:
        return JSONResponse({"ok": False, "error": (
            f"passkey registration did not verify: {e}"
        )}, status_code=400)
    except Exception as e:
        return JSONResponse({"ok": False, "error": (
            f"malformed passkey registration response: {e}"
        )}, status_code=400)

    credential_id = _b64url(verification.credential_id)
    try:
        existing = {row.key for row in _passkey_rows()}
    except Exception as e:
        return JSONResponse({"ok": False,
                             "error": f"could not read enrolled passkeys: {e}"},
                            status_code=500)
    if credential_id in existing:
        return JSONResponse({"ok": False, "error": (
            "this credential is already enrolled on this dashboard"
        )}, status_code=409)

    raw_transports = (credential.get("response") or {}).get("transports") or []
    transports = [t for t in raw_transports
                  if isinstance(t, str) and t in PASSKEY_TRANSPORTS]
    payload = {
        "credential_id": credential_id,
        "public_key": _b64url(verification.credential_public_key),
        "sign_count": verification.sign_count,
        "rp_id": pending["rp_id"],
        "origin": pending["origin"],
        "label": label,
        "transports": transports,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if verification.aaguid:
        payload["aaguid"] = verification.aaguid
    try:
        with settings_ops.identity_write_context():
            settings_ops.upsert_by_key(
                PASSKEY_SET_ID, PASSKEY_REVISION, credential_id, payload, org=None,
            )
    except Exception as e:
        return JSONResponse({"ok": False,
                             "error": f"could not store the credential: {e}"},
                            status_code=500)
    # A first passkey also flips the unlock gate on (fail-open-then-
    # enforce) — make enforcement see it immediately.
    from tools.dashboard import unlock_routes
    unlock_routes.bust_enforce_cache()
    return JSONResponse({"ok": True, "credential_id": credential_id,
                         "rp_id": pending["rp_id"], "label": label,
                         "transports": transports})


ROUTES = [
    Route("/api/identity/status", get_status, methods=["GET"]),
    Route("/api/identity/personal", get_personal, methods=["GET"]),
    Route("/api/identity/personal", post_personal, methods=["POST"]),
    Route("/api/identity/passkey/register-options", post_register_options,
          methods=["POST"]),
    Route("/api/identity/passkey/register", post_register, methods=["POST"]),
]
