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
import hashlib
import ipaddress
import json
import logging
import secrets
import time

_LOG = logging.getLogger("autonomy.dashboard.identity")

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from tools.graph import settings_ops
from tools.dashboard.network_routes import _first_member, _mock_mode
# Importing registers the autonomy.identity.* Setting schemas.
from tools.graph.schemas.personal_identity import (  # noqa: F401
    FACTOR_METADATA_REVISION,
    FACTOR_METADATA_SET_ID,
    FACTOR_RECIPIENT_METADATA_REVISION,
    FACTOR_RECIPIENT_METADATA_SET_ID,
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

# A root-authorized transition binds the optimistic-concurrency base, the
# complete staged operation list, and the byte-exact candidate armor.  The
# candidate also carries its own root signature (verified at every use); this
# distinct signature makes the *change request* non-malleable and non-replayable
# across generations without requiring another operator gesture.
FACTOR_POLICY_TRANSITION_DOMAIN = b"autonomy.identity.factor-policy-transition.v1\n"


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


def _factor_metadata_rows() -> dict[str, dict]:
    members = settings_ops.read_owned_set(FACTOR_METADATA_SET_ID, org=None).members
    return {
        member.key: dict(member.payload)
        for member in members
        if isinstance(member.payload, dict)
    }


def _factor_recipient_metadata_rows() -> dict[tuple[str, str], dict]:
    members = settings_ops.read_owned_set(
        FACTOR_RECIPIENT_METADATA_SET_ID, org=None,
    ).members
    return {
        (member.payload.get("factor_id"), member.payload.get("recipient_public_key")):
            dict(member.payload)
        for member in members
        if isinstance(member.payload, dict)
    }


def _factor_recipient_metadata_key(factor_id: str, public_key: str) -> str:
    return hashlib.sha256(f"{factor_id}\n{public_key}".encode("utf-8")).hexdigest()


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
            # The current MFA policy, so the sign-on UI can show whether a
            # required pair is in force before offering to change it.
            "require_pair": bool(personal.payload.get("require_pair", False)),
        }
    rows = [{
        "credential_id": m.payload.get("credential_id"),
        "label": m.payload.get("label"),
        "rp_id": m.payload.get("rp_id"),
        "transports": m.payload.get("transports") or [],
        "created_at": m.payload.get("created_at"),
        # The passkey's public vault-factor key, present only when the passkey
        # was enrolled with PRF. Its presence is "promotable"; the factor UI
        # uses it to seal the root to this passkey when promoting it.
        "provisioning_public_key":
            (m.payload.get("statement") or {}).get("provisioning_public_key"),
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
        "require_pair": bool(member.payload.get("require_pair", False)),
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
        canonicalize_armor,
    )
    try:
        # Either armor version: existing identities are v1, new ones are v2.
        canonical_armor = canonicalize_armor(body["armored_private_key"])
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


# The root signs the re-factored armor so the server knows the submitter opened
# the CURRENT armor (held a valid factor) rather than crafting a substitute.
# Distinct domain so the signature cannot be replayed as any other record.
REARMOR_DOMAIN = b"autonomy.identity.rearmor.v1\n"


def _root_reachable(factor_types: list, require_pair: bool) -> bool:
    """The backend copy of the sign-on machine's invariant (note 464c7021):
    the identity must keep a DAY-TO-DAY opener, and a required pair keeps a
    both-required opener.

    The daily openers are the password factor, a passkey armor factor, and the
    COMBINED (MFA) factor — the last one reaches the root only with BOTH the
    password and a passkey together, which is exactly the "require both" it
    embodies. The recovery factor is the emergency floor, never a day-to-day
    unlock, so an armor left with only recovery is refused. ``require_pair``
    (the MFA policy) demands a both-required opener: the combined factor is one
    (the current model), and the legacy two-standalone-factors form also
    satisfies it. This is enforcement, not a UX courtesy — the browser check is
    a courtesy; this is the gate.
    """
    has_password = "password" in factor_types
    has_passkey = "passkey" in factor_types
    has_combined = "combined" in factor_types
    if require_pair:
        return has_combined or (has_password and has_passkey)
    return has_password or has_passkey or has_combined


async def post_rearmor(request: Request) -> JSONResponse:
    """Replace the personal root's armor with a re-factored one — promote or
    demote a passkey, set or remove the password, require a pair.

    Body: ``{armored_private_key, require_pair?, signature}``. The browser opens
    the current armor (proving possession of a factor), re-wraps the SAME root
    seed under the new factor set, and signs the new armor with the root. The
    server verifies that signature against the STORED root — never the armor's
    own root_pub field alone, which a substitute armor could forge — and refuses
    any factor set that would leave the identity without a daily opener
    (rootReachable). I1: only the armor is stored, never plaintext.
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
    if not isinstance(body, dict) or not isinstance(body.get("armored_private_key"), str) \
            or not isinstance(body.get("signature"), str):
        return JSONResponse({"ok": False, "error": (
            "body must carry 'armored_private_key' and the root 'signature' over it"
        )}, status_code=400)

    from tools.network.idkit.armor import (
        ArmorError,
        armor_factor_types,
        armor_root_pub,
        armor_version,
        canonicalize_armor,
    )
    from tools.network.idkit.keys import verify_signature
    from tools.network.idkit.errors import IdkitError
    try:
        canonical_armor = canonicalize_armor(body["armored_private_key"])
        if armor_version(canonical_armor) != 2:
            return JSONResponse({"ok": False, "error": (
                "version 3 factor policies must use the generation-aware "
                "factor-policy preview and commit endpoints"
            )}, status_code=409)
        new_root_pub = armor_root_pub(canonical_armor)
        factor_types = armor_factor_types(canonical_armor)
    except ArmorError as e:
        return JSONResponse({"ok": False, "error": (
            f"not a canonical password-encrypted key armor (I1): {e}"
        )}, status_code=400)

    try:
        existing = _personal_member()
    except Exception as e:
        return JSONResponse({"ok": False,
                             "error": f"could not read the personal identity: {e}"},
                            status_code=500)
    if existing is None or not existing.payload.get("root_pub"):
        return JSONResponse({"ok": False, "error": (
            "no personal identity to re-armor — run Get started first"
        )}, status_code=409)
    stored_root_pub = existing.payload["root_pub"]
    if new_root_pub != stored_root_pub:
        return JSONResponse({"ok": False, "error": (
            "the new armor is for a different root key — re-armoring never "
            "changes which identity this is"
        )}, status_code=409)

    # Proof of possession: only someone who opened the CURRENT armor holds the
    # root, so only they can sign the replacement. A substitute armor forged
    # with a stolen root_pub cannot produce this signature.
    try:
        verify_signature(stored_root_pub, body["signature"],
                         REARMOR_DOMAIN + canonical_armor.encode("utf-8"))
    except IdkitError:
        return JSONResponse({"ok": False, "error": (
            "the re-armor signature does not verify against your root — only a "
            "holder of the current key may replace its armor"
        )}, status_code=403)

    require_pair = bool(body.get("require_pair", existing.payload.get("require_pair", False)))
    if not _root_reachable(factor_types, require_pair):
        need = ("a password AND a passkey" if require_pair
                else "a password or a passkey")
        return JSONResponse({"ok": False, "error": (
            f"this factor set would leave no daily way in — keep {need}"
        )}, status_code=400)

    payload = dict(existing.payload)
    payload["armored_private_key"] = canonical_armor
    payload["root_pub"] = stored_root_pub
    payload["require_pair"] = require_pair
    payload["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        with settings_ops.identity_write_context():
            settings_ops.upsert_by_key(
                PERSONAL_IDENTITY_SET_ID, PERSONAL_IDENTITY_REVISION,
                existing.key, payload, org=None,
            )
    except Exception as e:
        return JSONResponse({"ok": False,
                             "error": f"could not store the re-armored identity: {e}"},
                            status_code=500)
    return JSONResponse({"ok": True, "root_pub": stored_root_pub,
                         "factors": factor_types, "require_pair": require_pair})


# ── factor-policy generations ────────────────────────────────────────


def _legacy_factor_policy(armor_text: str) -> tuple[list[dict], dict]:
    """Describe a v2 armor without pretending its physical locks are v3.

    These synthetic ids are display/migration handles only.  A v2 password
    wrap is not yet a normalized public recipient, so the transition API
    accepts only one explicit ``migrate_legacy`` operation at generation zero.
    """
    from tools.network.idkit.armor import parse_armor
    from tools.network.idkit.root_factor_policy import canonical_expression

    data = parse_armor(armor_text)
    factors: list[dict] = []
    branches: list[dict] = []
    for index, factor in enumerate(data["factors"]):
        kind = factor["type"]
        if kind == "password":
            factor_id = "legacy.password"
            factors.append({"factor_id": factor_id, "type": "password"})
            branches.append({"op": "factor", "factor_id": factor_id})
        elif kind == "passkey":
            suffix = hashlib.sha256(
                f"{factor['credential_id']}:{factor['kem_pub']}".encode("utf-8")
            ).hexdigest()[:20]
            factor_id = f"legacy.passkey:{suffix}"
            factors.append({
                "factor_id": factor_id,
                "type": "passkey",
                "credential_id": factor["credential_id"],
                "recipients": [{
                    "recipient_public_key": factor["kem_pub"],
                    "label": "Legacy device",
                    "created_at": "1970-01-01T00:00:00Z",
                }],
            })
            branches.append({"op": "factor", "factor_id": factor_id})
        elif kind == "combined":
            suffix = hashlib.sha256(
                f"{index}:{factor['credential_id']}:{factor['kem_pub']}".encode("utf-8")
            ).hexdigest()[:20]
            password_id = f"legacy.combined-password:{suffix}"
            passkey_id = f"legacy.combined-passkey:{suffix}"
            factors.extend([
                {"factor_id": password_id, "type": "password"},
                {
                    "factor_id": passkey_id,
                    "type": "passkey",
                    "credential_id": factor["credential_id"],
                    "recipients": [{
                        "recipient_public_key": factor["kem_pub"],
                        "label": "Legacy device",
                        "created_at": "1970-01-01T00:00:00Z",
                    }],
                },
            ])
            branches.append({
                "op": "and",
                "children": [
                    {"op": "factor", "factor_id": password_id},
                    {"op": "factor", "factor_id": passkey_id},
                ],
            })
        # Recovery remains visible in the legacy personal endpoint but is not a
        # day-to-day factor in the new policy editor.
    if not branches:
        raise ValueError("legacy armor has no day-to-day root opener")
    policy = branches[0] if len(branches) == 1 else {
        "op": "or", "children": branches,
    }
    return factors, canonical_expression(policy)


def _factor_policy_state(member) -> dict:
    from tools.network.idkit.armor import armor_version
    from tools.network.idkit.root_factor_policy import (
        factor_roles,
        parse_armored_envelope,
    )

    armor_text = member.payload["armored_private_key"]
    version = armor_version(armor_text)
    if version == 3:
        envelope = parse_armored_envelope(armor_text)
        return {
            "armor_version": 3,
            "generation": envelope["generation"],
            "root_pub": envelope["root_pub"],
            "factors": envelope["factors"],
            "access": envelope["access"],
            "root_policy": envelope["policy"],
            "roles": factor_roles(
                envelope["policy"], envelope["factors"], envelope["access"],
            ),
            "envelope": envelope,
            "migration_required": False,
        }
    factors, policy = _legacy_factor_policy(armor_text)
    member_ids = {factor["factor_id"] for factor in factors}
    roles = {}
    from tools.network.idkit.root_factor_policy import policy_satisfied
    for factor in factors:
        factor_id = factor["factor_id"]
        roles[factor_id] = {
            "access": "enabled",
            "root_role": (
                "individual" if policy_satisfied(policy, {factor_id})
                else "mfa-member"
            ),
        }
    return {
        "armor_version": version,
        "generation": 0,
        "root_pub": member.payload["root_pub"],
        "factors": factors,
        "access": sorted(member_ids),
        "root_policy": policy,
        "roles": roles,
        "envelope": None,
        "migration_required": True,
    }


def _factor_policy_view(state: dict) -> dict:
    try:
        metadata = _factor_metadata_rows()
        recipient_metadata = _factor_recipient_metadata_rows()
        passkeys = {
            row.payload.get("credential_id"): row.payload for row in _passkey_rows()
        }
    except Exception:
        metadata, recipient_metadata, passkeys = {}, {}, {}
    factors = []
    for factor in state["factors"]:
        factor_id = factor["factor_id"]
        role = state["roles"].get(factor_id, {
            "access": "disabled", "root_role": "none",
        })
        meta = metadata.get(factor_id, {})
        credential = passkeys.get(factor.get("credential_id"), {})
        label = (
            meta.get("label")
            or credential.get("label")
            or ("Password" if factor["type"] == "password" else "Passkey")
        )
        row = {
            "factor_id": factor_id,
            "type": factor["type"],
            "label": label,
            "purpose": meta.get("purpose"),
            "access": role["access"],
            "root_role": role["root_role"],
            "capabilities": {
                "dashboard": role["access"] == "enabled",
                "root": (
                    role["root_role"] != "none"
                    and (
                        factor["type"] == "password"
                        or bool(factor.get("recipients"))
                    )
                ),
            },
        }
        if factor["type"] == "password":
            protector = factor.get("protector") or {}
            kdf = protector.get("kdf") or {}
            row["kdf"] = {
                "name": kdf.get("name"),
                "hash": kdf.get("hash"),
                "iterations": kdf.get("iterations"),
            }
        else:
            recipients = []
            for recipient in factor.get("recipients") or []:
                displayed = dict(recipient)
                override = recipient_metadata.get((
                    factor_id, recipient.get("recipient_public_key"),
                ))
                if override:
                    displayed["label"] = override["label"]
                recipients.append(displayed)
            row.update({
                "credential_id": factor.get("credential_id"),
                "rp_id": credential.get("rp_id"),
                "transports": credential.get("transports") or [],
                "created_at": credential.get("created_at"),
                "backup_eligible": credential.get("backup_eligible"),
                "backed_up": credential.get("backed_up"),
                "recipients": recipients,
            })
        factors.append(row)
    return {
        "version": 1,
        "armor_version": state["armor_version"],
        "generation": state["generation"],
        "root_pub": state["root_pub"],
        "root_policy": state["root_policy"],
        "factors": factors,
        "migration_required": state["migration_required"],
        "allowed_operations": (
            ["migrate_legacy"] if state["migration_required"] else [
                "enroll_password", "enroll_passkey", "change_password",
                "add_passkey_recipient", "remove_passkey_recipient",
                "remove_factor", "set_access", "set_root_policy",
            ]
        ),
    }


def _project_factor_policy(state: dict, operations: object) -> dict:
    from tools.network.idkit.root_factor_policy import (
        POLICY_VERSION,
        project_operations,
        validate_state,
    )
    if not isinstance(operations, list) or not operations:
        raise ValueError("operations must be a non-empty array")
    if not state["migration_required"]:
        return project_operations({
            "generation": state["generation"],
            "root_pub": state["root_pub"],
            "factors": state["factors"],
            "access": state["access"],
            "policy": state["root_policy"],
        }, operations)
    if len(operations) != 1 or not isinstance(operations[0], dict) \
            or set(operations[0]) != {"op", "factors", "access", "root_policy"} \
            or operations[0].get("op") != "migrate_legacy":
        raise ValueError(
            "generation zero accepts exactly one migrate_legacy operation "
            "carrying factors, access, and root_policy"
        )
    operation = operations[0]
    validated = validate_state(
        operation["factors"], operation["access"], operation["root_policy"],
        root_pub=state["root_pub"],
    )
    return {
        "version": POLICY_VERSION,
        "base_generation": 0,
        "generation": 1,
        "root_pub": state["root_pub"],
        "factors": validated["factors"],
        "access": validated["access"],
        "root_policy": validated["policy"],
        "roles": validated["roles"],
        "operations": ["migrate_legacy"],
        "change_count": 1,
    }


def _validate_passkey_bindings(projected: dict) -> None:
    enrolled = {
        row.payload.get("credential_id") for row in _passkey_rows()
        if isinstance(row.payload.get("credential_id"), str)
    }
    unknown = sorted({
        factor["credential_id"] for factor in projected["factors"]
        if factor["type"] == "passkey"
        and factor["credential_id"] not in enrolled
    })
    if unknown:
        raise ValueError(
            "root policy names passkeys that are not enrolled for dashboard "
            f"access: {unknown}"
        )


async def get_factor_policy(request: Request) -> JSONResponse:
    """Return the canonical root policy and the derived per-factor roles."""
    if _mock_mode():
        return JSONResponse({"error": "mock dashboard has no factor policy"},
                            status_code=404)
    try:
        member = _personal_member()
        if member is None or not member.payload.get("armored_private_key"):
            return JSONResponse({"error": "no personal identity is stored"},
                                status_code=404)
        return JSONResponse(_factor_policy_view(_factor_policy_state(member)))
    except Exception as exc:
        return JSONResponse({"error": f"could not read factor policy: {exc}"},
                            status_code=500)


async def post_factor_policy_preview(request: Request) -> JSONResponse:
    """Validate a staged batch against its final projected state only."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "body must be JSON"},
                            status_code=400)
    if not isinstance(body, dict) or set(body) != {"base_generation", "operations"}:
        return JSONResponse({"ok": False, "error": (
            "body must carry exactly base_generation and operations"
        )}, status_code=400)
    if not isinstance(body["base_generation"], int) \
            or isinstance(body["base_generation"], bool) \
            or body["base_generation"] < 0:
        return JSONResponse({"ok": False, "error": (
            "base_generation must be a non-negative integer"
        )}, status_code=400)
    try:
        member = _personal_member()
        if member is None:
            return JSONResponse({"ok": False, "error": "no personal identity is stored"},
                                status_code=404)
        state = _factor_policy_state(member)
        if body["base_generation"] != state["generation"]:
            return JSONResponse({"ok": False, "error": (
                "factor policy changed while you were editing; refresh and reapply"
            ), "generation": state["generation"]}, status_code=409)
        projected = _project_factor_policy(state, body["operations"])
        _validate_passkey_bindings(projected)
    except (TypeError, ValueError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({
        "ok": True,
        "base_generation": projected["base_generation"],
        "generation": projected["generation"],
        "root_policy": projected["root_policy"],
        "factors": projected["factors"],
        "access": projected["access"],
        "roles": projected["roles"],
        "change_count": 1,
    })


def _transition_message(base_generation: int, operations: list, armor: str) -> bytes:
    from tools.network.idkit.canonical import canonical_json
    return FACTOR_POLICY_TRANSITION_DOMAIN + canonical_json({
        "base_generation": base_generation,
        "candidate_sha256": hashlib.sha256(armor.encode("utf-8")).hexdigest(),
        "operations": operations,
    })


async def post_factor_policy_commit(request: Request) -> JSONResponse:
    """Atomically install one root-authorized factor-policy generation."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "body must be JSON"},
                            status_code=400)
    expected = {"base_generation", "operations", "candidate_armor", "root_signature"}
    if not isinstance(body, dict) or set(body) != expected \
            or not isinstance(body.get("candidate_armor"), str) \
            or not isinstance(body.get("root_signature"), str):
        return JSONResponse({"ok": False, "error": (
            "body must carry exactly base_generation, operations, "
            "candidate_armor, and root_signature"
        )}, status_code=400)
    if not isinstance(body["base_generation"], int) \
            or isinstance(body["base_generation"], bool) \
            or body["base_generation"] < 0:
        return JSONResponse({"ok": False, "error": (
            "base_generation must be a non-negative integer"
        )}, status_code=400)
    try:
        member = _personal_member()
        if member is None:
            return JSONResponse({"ok": False, "error": "no personal identity is stored"},
                                status_code=404)
        state = _factor_policy_state(member)
        if body["base_generation"] != state["generation"]:
            return JSONResponse({"ok": False, "error": (
                "factor policy changed while you were editing; refresh and reapply"
            ), "generation": state["generation"]}, status_code=409)
        projected = _project_factor_policy(state, body["operations"])
        _validate_passkey_bindings(projected)

        from tools.network.idkit.armor import canonicalize_armor
        from tools.network.idkit.canonical import canonical_json
        from tools.network.idkit.keys import verify_signature
        from tools.network.idkit.root_factor_policy import parse_armored_envelope
        canonical_armor = canonicalize_armor(body["candidate_armor"])
        if canonical_armor != body["candidate_armor"]:
            raise ValueError("candidate_armor must already be canonical")
        candidate = parse_armored_envelope(canonical_armor)
        expected_state = {
            "generation": projected["generation"],
            "root_pub": projected["root_pub"],
            "factors": projected["factors"],
            "access": projected["access"],
            "policy": projected["root_policy"],
        }
        candidate_state = {
            key: candidate[key] for key in
            ("generation", "root_pub", "factors", "access", "policy")
        }
        if canonical_json(candidate_state) != canonical_json(expected_state):
            raise ValueError(
                "candidate armor does not encode the previewed final policy"
            )
        verify_signature(
            state["root_pub"], body["root_signature"],
            _transition_message(
                body["base_generation"], body["operations"], canonical_armor,
            ),
        )
    except (TypeError, ValueError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception:
        return JSONResponse({"ok": False, "error": (
            "the factor-policy authorization did not verify against your root"
        )}, status_code=403)

    payload = dict(member.payload)
    payload["armored_private_key"] = canonical_armor
    payload["root_pub"] = state["root_pub"]
    payload.pop("require_pair", None)
    payload["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        with settings_ops.identity_write_context():
            settings_ops.upsert_by_key(
                PERSONAL_IDENTITY_SET_ID, PERSONAL_IDENTITY_REVISION,
                member.key, payload, org=None,
            )
    except Exception as exc:
        return JSONResponse({"ok": False, "error": (
            f"could not store factor-policy generation: {exc}"
        )}, status_code=500)
    from tools.dashboard import unlock_routes
    unlock_routes.bust_enforce_cache()
    installed = _factor_policy_state(_personal_member())
    return JSONResponse({"ok": True, **_factor_policy_view(installed)})


async def patch_factor_metadata(request: Request) -> JSONResponse:
    """Rename or describe a factor without re-armoring the root."""
    factor_id = request.path_params.get("factor_id")
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "body must be JSON"},
                            status_code=400)
    if not isinstance(body, dict) or not body or not set(body) <= {"label", "purpose"}:
        return JSONResponse({"ok": False, "error": (
            "body may carry only label and purpose"
        )}, status_code=400)
    try:
        member = _personal_member()
        state = _factor_policy_state(member) if member is not None else None
        if state is None or factor_id not in {
            factor["factor_id"] for factor in state["factors"]
        }:
            return JSONResponse({"ok": False, "error": "no such factor"},
                                status_code=404)
        old = _factor_metadata_rows().get(factor_id, {})
        label = body.get("label", old.get("label"))
        purpose = body.get("purpose", old.get("purpose"))
        if not isinstance(label, str) or not label.strip():
            return JSONResponse({"ok": False, "error": "label must be non-empty"},
                                status_code=400)
        payload = {
            "factor_id": factor_id,
            "label": label.strip(),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        if purpose is not None:
            if not isinstance(purpose, str) or not purpose.strip():
                return JSONResponse({"ok": False, "error": (
                    "purpose must be a non-empty string when supplied"
                )}, status_code=400)
            payload["purpose"] = purpose.strip()
        with settings_ops.identity_write_context():
            settings_ops.upsert_by_key(
                FACTOR_METADATA_SET_ID, FACTOR_METADATA_REVISION,
                factor_id, payload, org=None,
            )
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"could not update factor: {exc}"},
                            status_code=400)
    return JSONResponse({"ok": True, **payload})


async def patch_factor_recipient_metadata(request: Request) -> JSONResponse:
    """Rename one passkey device without changing the signed generation."""
    factor_id = request.path_params.get("factor_id")
    public_key = request.path_params.get("recipient_public_key")
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "body must be JSON"},
                            status_code=400)
    if not isinstance(body, dict) or set(body) != {"label"} \
            or not isinstance(body.get("label"), str) or not body["label"].strip():
        return JSONResponse({"ok": False, "error": (
            "body must carry exactly one non-empty label"
        )}, status_code=400)
    if len(body["label"].strip()) > 120:
        return JSONResponse({"ok": False, "error": "label is too long"},
                            status_code=400)
    try:
        member = _personal_member()
        state = _factor_policy_state(member) if member is not None else None
        factor = next(
            row for row in (state["factors"] if state is not None else [])
            if row["factor_id"] == factor_id and row["type"] == "passkey"
        )
        if public_key not in {
            row["recipient_public_key"] for row in factor.get("recipients") or []
        }:
            raise StopIteration
    except (StopIteration, TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "no such passkey recipient"},
                            status_code=404)
    payload = {
        "factor_id": factor_id,
        "recipient_public_key": public_key,
        "label": body["label"].strip(),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    try:
        with settings_ops.identity_write_context():
            settings_ops.upsert_by_key(
                FACTOR_RECIPIENT_METADATA_SET_ID,
                FACTOR_RECIPIENT_METADATA_REVISION,
                _factor_recipient_metadata_key(factor_id, public_key),
                payload,
                org=None,
            )
    except Exception as exc:
        return JSONResponse({"ok": False, "error": (
            f"could not update passkey recipient: {exc}"
        )}, status_code=400)
    return JSONResponse({"ok": True, **payload})


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
    # A single-use nonce the enrollment statement binds (idkit.enrollment):
    # frozen here alongside the challenge so post_register can confirm the
    # root-signed statement is for THIS ceremony, not one replayed from another.
    nonce = secrets.token_hex(32)
    _pending[challenge_key] = {
        "rp_id": rp_id,
        "origin": origin,
        "nonce": nonce,
        "expires": _now() + PENDING_TTL_S,
    }
    return JSONResponse({
        "ok": True,
        "rp_id": rp_id,
        "origin": origin,
        "nonce": nonce,
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

    # The root-signed enrollment statement (idkit.enrollment) is what turns this
    # row from an agent-writable claim into evidence. Verify it against the root
    # resolved from autonomy.identity.personal — NEVER the statement's own signer
    # (the fatal one-liner verify() warns about) — and confirm it describes the
    # credential THIS ceremony just registered.
    statement_data = body.get("statement")
    if not isinstance(statement_data, dict):
        return JSONResponse({"ok": False, "error": (
            "body must carry 'statement' — the root-signed enrollment statement"
        )}, status_code=400)
    try:
        personal = _personal_member()
    except Exception as e:
        return JSONResponse({"ok": False,
                             "error": f"could not read the personal identity: {e}"},
                            status_code=500)
    if personal is None or not personal.payload.get("root_pub"):
        return JSONResponse({"ok": False, "error": (
            "no personal identity to verify the enrollment statement against"
        )}, status_code=409)
    from tools.network.idkit import enrollment
    from tools.network.idkit.errors import IdkitError
    try:
        statement = enrollment.verify(
            statement_data, root_pub=personal.payload["root_pub"],
        )
    except IdkitError as e:
        return JSONResponse({"ok": False, "error": (
            f"enrollment statement did not verify: {e}"
        )}, status_code=400)
    # The browser sends the COSE key exactly as the authenticator emitted it;
    # py_webauthn stores its canonical re-encoding. Compare canonical-to-canonical
    # with py_webauthn's own helpers, so a non-minimal-but-valid encoding from the
    # device is accepted while a wrong key is not. No round trip: both values are
    # already in hand from the single request.
    from webauthn.helpers import encode_cbor, parse_cbor
    try:
        _stmt_cose = encode_cbor(parse_cbor(bytes.fromhex(statement.credential_public_key)))
    except Exception:  # noqa: BLE001 — malformed key hex/CBOR is just a mismatch
        _stmt_cose = None
    _stmt_bad = next((name for name, ok in (
        ("credential_id", statement.credential_id == credential_id),
        ("credential_public_key", _stmt_cose == verification.credential_public_key),
        ("rp_id", statement.rp_id == pending["rp_id"]),
        ("origin", statement.origin == pending["origin"]),
        ("nonce", statement.nonce == pending.get("nonce")),
        ("initial_sign_count",
         statement.initial_sign_count == verification.sign_count),
    ) if not ok), None)
    if _stmt_bad is not None:
        return JSONResponse({"ok": False, "error": (
            "the enrollment statement does not match the registered credential "
            f"({_stmt_bad})"
        )}, status_code=400)

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
    device_type = getattr(verification, "credential_device_type", None)
    payload["backup_eligible"] = (
        getattr(device_type, "value", None) == "multi_device"
    )
    payload["backed_up"] = bool(
        getattr(verification, "credential_backed_up", False)
    )
    payload["statement"] = statement.to_dict()
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


async def delete_passkey(request: Request) -> JSONResponse:
    """Remove an enrolled passkey (removeKey). The password floor keeps access
    open, so this never locks anyone out. Refused while the passkey is still a
    ROOT-ARMOR factor — a standalone passkey factor OR the passkey half of a
    combined (MFA) factor: demote it (or turn MFA off) first, so the credential
    list and the armor's factor set never disagree. Deleting the combined
    factor's member strands the unlock UI (it stops offering the passkey) while
    the armor still requires that passkey — the exact lockout this refuses.
    """
    if _mock_mode():
        return JSONResponse({"ok": False,
                             "error": "mock dashboard enrolls no passkeys"},
                            status_code=502)
    credential_id = request.path_params.get("credential_id")
    try:
        rows = _passkey_rows()
    except Exception as e:
        return JSONResponse({"ok": False,
                             "error": f"could not read enrolled passkeys: {e}"},
                            status_code=500)
    target = next((m for m in rows
                   if m.payload.get("credential_id") == credential_id), None)
    if target is None:
        return JSONResponse({"ok": False, "error": (
            "no enrolled passkey has that credential id"
        )}, status_code=404)

    # If this passkey still wraps the root — as a standalone passkey factor OR
    # as the passkey half of a combined (MFA) factor — removing the row alone
    # would strand that factor and the unlock UI. Refuse until it is demoted or
    # MFA is turned off.
    try:
        personal = _personal_member()
    except Exception:
        personal = None
    if personal is not None and personal.payload.get("armored_private_key"):
        try:
            from tools.network.idkit.armor import armor_version, parse_armor
            armor_text = personal.payload["armored_private_key"]
            if armor_version(armor_text) == 3:
                from tools.network.idkit.root_factor_policy import parse_armored_envelope
                factors = parse_armored_envelope(armor_text)["factors"]
            else:
                factors = parse_armor(armor_text)["factors"]
        except Exception:
            factors = []
        if any(f.get("type") == "passkey" and f.get("credential_id") == credential_id
               for f in factors):
            return JSONResponse({"ok": False, "error": (
                "this passkey still unlocks your key — remove it as a factor "
                "(demote it) before removing the device"
            )}, status_code=409)
        if any(f.get("type") == "combined" and f.get("credential_id") == credential_id
               for f in factors):
            return JSONResponse({"ok": False, "error": (
                "this passkey is half of your Multi-Factor lock — turn "
                "Multi-Factor off before removing the device, or you would be "
                "left unable to unlock"
            )}, status_code=409)

    try:
        with settings_ops.identity_write_context():
            settings_ops.exclude_setting(target.id, org=None)
    except Exception as e:
        return JSONResponse({"ok": False,
                             "error": f"could not remove the passkey: {e}"},
                            status_code=500)
    from tools.dashboard import unlock_routes
    unlock_routes.bust_enforce_cache()
    return JSONResponse({"ok": True, "credential_id": credential_id})


_CEREMONY_ERR_STACK_MAX = 4000
# Bounded like the Agent Test telemetry so the log cannot grow without limit.
MAX_CLIENT_ERRORS = 500


def _clip(value, limit=400):
    return "" if value is None else str(value)[:limit]


async def post_ceremony_error(request: Request) -> JSONResponse:
    """Capture a client-side ceremony failure so it survives past the browser.

    The factor and unlock ceremonies run their crypto in the BROWSER — the root
    never leaves the page (I1) — so their exceptions never reach the server on
    their own; before this they lived only in a red banner an operator on a phone
    cannot inspect (the same blind spot the network unlock-report closed). The
    failure is written to the dashboard log AND to a capped, personal-scoped
    append-only Settings log (``autonomy.identity.client-error``), trimmed to the
    most recent ``MAX_CLIENT_ERRORS`` exactly as the Agent Test telemetry bounds
    itself. DIAGNOSTIC ONLY: an error name/message/stack plus non-secret context
    (which transition, which factors are present). The client must never send —
    and this must never store — a password, PRF output, root seed, CEK, or armor
    plaintext; only failure descriptions and code locations belong here.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - diagnostics must never 500
        body = {}
    if not isinstance(body, dict):
        body = {}

    ctx = body.get("context")
    entry = {
        "ceremony": _clip(body.get("ceremony"), 80) or "unknown",
        "action": _clip(body.get("action"), 80),
        "name": _clip(body.get("name"), 120),
        "message": _clip(body.get("message"), 600),
        "stack": _clip(body.get("stack"), _CEREMONY_ERR_STACK_MAX),
        "context": _clip(
            json.dumps(ctx, sort_keys=True) if isinstance(ctx, (dict, list)) else ctx, 600,
        ),
        "recorded_at": time.time(),
    }
    _LOG.warning(
        "client ceremony error: ceremony=%s action=%s name=%s message=%s "
        "context=%s\n%s",
        entry["ceremony"], entry["action"] or "-", entry["name"] or "-",
        entry["message"] or "-", entry["context"] or "-", entry["stack"],
    )
    if not _mock_mode():
        try:
            from tools.graph.schemas.client_error import (
                CLIENT_ERROR_REVISION, CLIENT_ERROR_SET_ID,
            )
            with settings_ops.identity_write_context():
                settings_ops.append_log_entries(
                    CLIENT_ERROR_SET_ID, CLIENT_ERROR_REVISION,
                    [(secrets.token_hex(16), entry)], org=None,
                )
                members = list(settings_ops.read_owned_set(
                    CLIENT_ERROR_SET_ID, org=None).members)
                members.sort(
                    key=lambda m: (float(m.payload.get("recorded_at") or 0),
                                   m.created_at, m.id),
                    reverse=True,
                )
                stale = [m.id for m in members[MAX_CLIENT_ERRORS:]]
                if stale:
                    settings_ops.remove_raw_settings(stale, org=None)
        except Exception as exc:  # noqa: BLE001 - diagnostics must never 500
            _LOG.warning("client ceremony error not persisted: %s", exc)
    return JSONResponse({"ok": True})


async def get_ceremony_errors(request: Request) -> JSONResponse:
    """The most recent client ceremony failures, newest first (bounded)."""
    try:
        limit = min(int(request.query_params.get("limit", "50")), MAX_CLIENT_ERRORS)
    except (TypeError, ValueError):
        limit = 50
    from tools.graph.schemas.client_error import CLIENT_ERROR_SET_ID
    members = list(settings_ops.read_owned_set(CLIENT_ERROR_SET_ID, org=None).members)
    members.sort(
        key=lambda m: (float(m.payload.get("recorded_at") or 0), m.created_at, m.id),
        reverse=True,
    )
    return JSONResponse({"errors": [m.payload for m in members[:limit]]})


ROUTES = [
    Route("/api/identity/status", get_status, methods=["GET"]),
    Route("/api/identity/personal", get_personal, methods=["GET"]),
    Route("/api/identity/personal", post_personal, methods=["POST"]),
    Route("/api/identity/personal/armor", post_rearmor, methods=["POST"]),
    Route("/api/identity/factor-policy", get_factor_policy, methods=["GET"]),
    Route("/api/identity/factor-policy/preview", post_factor_policy_preview,
          methods=["POST"]),
    Route("/api/identity/factor-policy/commit", post_factor_policy_commit,
          methods=["POST"]),
    Route("/api/identity/factors/{factor_id}/metadata", patch_factor_metadata,
          methods=["PATCH"]),
    Route(
        "/api/identity/factors/{factor_id}/recipients/"
        "{recipient_public_key}/metadata",
        patch_factor_recipient_metadata,
        methods=["PATCH"],
    ),
    Route("/api/identity/passkey/register-options", post_register_options,
          methods=["POST"]),
    Route("/api/identity/passkey/register", post_register, methods=["POST"]),
    Route("/api/identity/passkey/{credential_id}", delete_passkey,
          methods=["DELETE"]),
    Route("/api/identity/ceremony-error", post_ceremony_error, methods=["POST"]),
    Route("/api/identity/ceremony-error", get_ceremony_errors, methods=["GET"]),
]
