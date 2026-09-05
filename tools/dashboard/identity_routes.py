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
import hmac
import ipaddress
import json
import logging
import re
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


class StablePersonalIdentityUnavailable(RuntimeError):
    """The canonical personal root cannot safely identify a durable owner."""


def resolve_stable_personal_root_public_key() -> str:
    """Return the one canonical personal root anchor, fail closed otherwise.

    Durable device-shaped records must never fall back to the first arbitrary
    identity row.  They bind only to the personal-scope ``default`` member and
    verify that an explicit public anchor agrees with the encrypted armor.
    """

    try:
        result = settings_ops.read_set(
            PERSONAL_IDENTITY_SET_ID, org=None, peers=[],
        )
        if any(result.dropped.values()):
            raise StablePersonalIdentityUnavailable(
                "personal identity resolution dropped stored rows"
            )
        rows = result.to_dict()
        if set(rows) != {PERSONAL_CANONICAL_LABEL}:
            raise StablePersonalIdentityUnavailable(
                "exactly one canonical personal identity is required"
            )
        payload = rows[PERSONAL_CANONICAL_LABEL].payload
        if not isinstance(payload, dict):
            raise StablePersonalIdentityUnavailable("personal identity is malformed")
        explicit = payload.get("root_pub")
        if explicit is not None and (
            not isinstance(explicit, str)
            or not re.fullmatch(r"[0-9a-f]{64}", explicit)
        ):
            raise StablePersonalIdentityUnavailable("personal root anchor is malformed")
        armor = payload.get("armored_private_key")
        if not isinstance(armor, str):
            raise StablePersonalIdentityUnavailable("personal identity armor is missing")
        from tools.network.idkit.armor import armor_root_pub

        derived = armor_root_pub(armor)
        if not isinstance(derived, str) or not re.fullmatch(r"[0-9a-f]{64}", derived):
            raise StablePersonalIdentityUnavailable("personal root anchor is malformed")
        if explicit is not None and not hmac.compare_digest(explicit, derived):
            raise StablePersonalIdentityUnavailable("personal root anchors disagree")
        return explicit or derived
    except StablePersonalIdentityUnavailable:
        raise
    except Exception as exc:
        raise StablePersonalIdentityUnavailable(
            "personal identity is unavailable"
        ) from exc

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
                             "session_credential_id": None,
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
    # Which exact credential authenticated this session. The token claims
    # carry only the method; the credential is in the durable session row.
    session_credential_id = None
    if session is not None:
        from tools.dashboard.dao import identity_sessions
        try:
            row = identity_sessions.get_session(session["sid"], now=time.time())
            session_credential_id = (row or {}).get("credential_id")
        except Exception:
            session_credential_id = None
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
        # The passkey credential that opened this session (null for password
        # sessions) — lets factor UIs mark "the one you are".
        "session_credential_id": session_credential_id,
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
            "recovery": envelope.get("recovery"),
            "envelope": envelope,
            "migration_required": False,
        }
    raise ValueError(
        "this identity's armor is in a retired format and cannot be described "
        "by the factor-policy editor"
    )


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
    recovery = state.get("recovery")
    return {
        "version": 1,
        "armor_version": state["armor_version"],
        "generation": state["generation"],
        "root_pub": state["root_pub"],
        "root_policy": state["root_policy"],
        "factors": factors,
        "migration_required": state["migration_required"],
        # The recovery code is the emergency floor, surfaced (presence + the
        # code's declared signing key) so the panel can show it enrolled and
        # hide the "set up" banner. The sealed material stays in the armor.
        "recovery": None if recovery is None else {
            "recovery_pub": recovery["recovery_pub"],
            "created_at": recovery.get("created_at"),
        },
        "allowed_operations": [
            "enroll_password", "enroll_passkey", "change_password",
            "add_passkey_recipient", "remove_passkey_recipient",
            "remove_factor", "set_access", "set_root_policy", "set_recovery",
        ],
    }


def _project_factor_policy(state: dict, operations: object) -> dict:
    from tools.network.idkit.root_factor_policy import (
        POLICY_VERSION,
        project_operations,
        validate_state,
    )
    if not isinstance(operations, list) or not operations:
        raise ValueError("operations must be a non-empty array")
    return project_operations({
        "generation": state["generation"],
        "root_pub": state["root_pub"],
        "factors": state["factors"],
        "access": state["access"],
        "policy": state["root_policy"],
        "recovery": state.get("recovery"),
    }, operations)


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
        # Echo the projected recovery slot so the client builds a candidate
        # armor whose (randomized-seal) recovery field matches byte-for-byte.
        "recovery": projected.get("recovery"),
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
            "recovery": projected.get("recovery"),
        }
        candidate_state = {
            "generation": candidate["generation"],
            "root_pub": candidate["root_pub"],
            "factors": candidate["factors"],
            "access": candidate["access"],
            "policy": candidate["policy"],
            "recovery": candidate.get("recovery"),
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


def _oxford(items: list) -> str:
    """"a", "a and b", "a, b, and c" — the affected-scope lists the flag
    balloons and the restart prompt read."""
    items = [str(x) for x in items]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


def _refused_since(since: object) -> str:
    """" since 8pm" — a short local-clock stamp for the first refusal, or ''
    when it isn't known. The count matters; when it started makes it legible."""
    if not isinstance(since, (int, float)):
        return ""
    try:
        stamp = time.strftime("%-I%p", time.localtime(since)).lower()
        return f" since {stamp}"
    except Exception:
        return ""


async def get_unlock_state(request: Request) -> JSONResponse:
    """Pre-auth status for the profile/locked-screen flag tray.

    Seven live indicators the tray (static/js/identity-indicator.js) renders,
    each ``{needs: bool, detail: str, value?: str, ...}``. ``needs`` lights the
    tile amber; ``detail`` is the balloon text (what the flag IS when dim, what
    is broken when lit); ``value`` is the corner readout. The three cold-vault
    signals (identity, delegate, session) are read directly; the infra signals
    (certificate, tunnel, sync, approvals) are best-effort and degrade to a
    non-alarming "unavailable" rather than fabricate a state — a false green
    here would be worse than a dim tile. The ``sync`` flag asks each serving
    connector, over the control channel the CLI already uses, whether it is
    armed (holds its memory-only credential) and current (running the installed
    code); a scope never set up to serve is a quiet note, never a lit tile.
    """
    from tools.dashboard.unlock_routes import _agent_delegate, session_from_request

    flags: dict = {}

    # domain — the personal identity is anchored to this dashboard.
    try:
        personal = _personal_member()
        anchored = bool(personal is not None
                        and personal.payload.get("armored_private_key"))
    except Exception:
        anchored = False
    flags["domain"] = {
        "needs": not anchored,
        "detail": ("Your personal identity is anchored to this dashboard."
                   if anchored else "No personal identity is set up yet."),
    }

    # agent — the delegate signing key that keeps the fleet running. A cold
    # vault holds none, so this lights until you unlock with your root.
    try:
        delegate = _agent_delegate()
    except Exception:
        delegate = None
    flags["agent"] = {
        "needs": delegate is None,
        "detail": ("Running — it can fetch your secrets without asking again."
                   if delegate is not None
                   else "Not running. Unlock with your root to start it."),
    }

    # ttl — how long this dashboard session stays open.
    try:
        session = session_from_request(request)
    except Exception:
        session = None
    if not session:
        flags["ttl"] = {"needs": True,
                        "detail": "Signed out — unlock again to open a session."}
    else:
        remaining = int(session.get("exp") or 0) - int(_now())
        low = remaining < 3600
        hours = max(0, remaining // 3600)
        flags["ttl"] = {
            "needs": low,
            "detail": (f"About {hours} hour(s) left; it renews while you are signed in."
                       if not low else "Running low — unlock again to extend it."),
        }

    # agent (continued) — a lit delegate now says what stopped working, not
    # only which button to press (the balloon-remedy-alone gap, bead auto-sdrsa).
    if flags["agent"]["needs"]:
        flags["agent"]["detail"] = (
            "Your delegate key isn't loaded, so background work that needs "
            "your secrets can't run. Unlock with your root to start it."
        )
    flags["agent"]["value"] = "Locked" if flags["agent"]["needs"] else "Running"

    # certificates + sync are two questions about the SAME set of serving
    # connectors, so one pass over the serving scopes computes both. A scope
    # that was never set up to serve (no cert row at all — e.g. blindhash) is a
    # quiet fact, not a fault: it must never light the tray, or the tray stays
    # permanently amber for something that isn't broken (bead auto-sdrsa).
    cert_states: list[dict] = []  # actual state of every relevant certificate
    cert_never: list = []       # scopes never provisioned to serve (quiet note)
    serving_setup: list = []    # scopes provisioned to serve (any cert row)
    sync_unarmed: list = []     # serving scopes holding no credential
    sync_stale: list = []       # serving scopes running older code than on disk
    sync_refusals = 0
    sync_since = None
    try:
        from tools.dashboard import link_serving_supervisor as _sup
        from tools.network import build_version as _bv

        try:
            disk_commit = _bv.disk_head()
        except Exception:
            disk_commit = None
        try:
            serving_scopes = _sup._discover_startup_orgs()
        except Exception:
            serving_scopes = []

        for scope in serving_scopes:
            try:
                cert_status = _sup.serve_cert_state(scope).get("status", "missing")
            except Exception:
                continue
            label = scope or "personal"
            if cert_status == "missing":
                cert_never.append(label)
                continue
            serving_setup.append(label)
            if cert_status == "ok":
                cert_states.append({"scope": label, "state": "current",
                                    "reason": "The serving delegation certificate is current."})
            elif cert_status == "expired":
                cert_states.append({"scope": label, "state": "expired",
                                    "reason": "The serving delegation certificate has expired."})
            else:
                cert_states.append({"scope": label, "state": "issuance_failed",
                                    "reason": "The serving delegation certificate is invalid or unavailable."})
            # Fleet sync is anchored on the PERSONAL root: activate_local_runtime
            # publishes the fleet-runtime credential with org=None, so ONLY the
            # personal connector is ever armed for Fleet sync. A shared org's
            # database does not sync over the personal engine, so its connector
            # reports fleet_runtime_configured=False by design and holds no
            # sync credential BECAUSE IT NEVER SHOULD. Asking the org scopes the
            # fleet-sync question invented a fault that cannot exist — it lit the
            # tray with "anchore, autonomy, and dynbench hold no serving
            # credential" on connectors that are behaving exactly as intended.
            # fleet_doctor already skips org scopes for this check (644c88d74);
            # the tray must too. So the fleet-sync questions (armed? current
            # code? refusing pulls?) are asked of the personal scope alone; org
            # serve certs are still checked above, because an org connector does
            # serve its own grant-gated targets — that is a different question.
            is_personal = scope is None or scope == "personal"
            if not is_personal:
                continue
            try:
                reply = _sup.control(scope, "connector-status", {})
            except Exception:
                # The personal connector won't answer — from the fleet's side it
                # is refusing every sync pull; count it as unarmed.
                sync_unarmed.append(label)
                continue
            if not reply.get("fleet_runtime_configured"):
                sync_unarmed.append(label)
            process_commit = reply.get("process_commit")
            if disk_commit and process_commit and process_commit != disk_commit:
                sync_stale.append(label)
            try:
                sync_refusals += int(reply.get("locked_refusals") or 0)
            except (TypeError, ValueError):
                pass
            since = reply.get("locked_refusal_since")
            if isinstance(since, (int, float)) and (
                sync_since is None or since < sync_since
            ):
                sync_since = since
        # The persona wildcard TLS pair is the second certificate lifecycle
        # carried by this same operator-facing flag. It is relevant once this
        # node is configured to serve at least one scope; it is not a new
        # identity flag or a second key-unlock concept.
        from tools.dashboard import service_certificate_manager as _service_tls
        for item in _service_tls.certificate_states():
            cert_states.append({
                "scope": f"{item['org']} / {item['persona_label']}",
                "state": item["state"],
                "reason": item["reason"],
            })
        cert_available = True
    except Exception:
        cert_available = False

    if not cert_available:
        flags["certificates"] = {"needs": False, "value": "",
                                 "detail": "Certificate state is unavailable."}
    else:
        labels = {
            "missing": "Missing",
            "issuing": "Issuing",
            "current": "Current",
            "renewal_due": "Renewal due",
            "expired": "Expired",
            "issuance_failed": "Issuance failed",
        }
        priority = {
            "issuance_failed": 5,
            "expired": 4,
            "missing": 3,
            "issuing": 2,
            "renewal_due": 1,
            "current": 0,
        }
        headline = max(
            cert_states,
            key=lambda item: priority.get(item["state"], 5),
            default={"state": "current"},
        )["state"]
        problems = [item for item in cert_states if item["state"] != "current"]
        cert = {
            "needs": bool(problems),
            "scopes": sorted(item["scope"] for item in problems),
            "value": labels[headline],
            "state": headline,
        }
        if problems:
            cert["detail"] = " ".join(
                f"{item['scope']}: {item['reason']}" for item in problems
            )
        else:
            cert["detail"] = (
                " ".join(
                    f"{item['scope']}: {item['reason']}" for item in cert_states
                )
                or "Your dashboard's certificates are current."
            )
        if cert_never:
            cert["note"] = (
                _oxford(sorted(cert_never))
                + (" isn't" if len(cert_never) == 1 else " aren't")
                + " set up to serve — that's expected, not a problem."
            )
        flags["certificates"] = cert

    # Only the fleet's DESIGNATED tunnel server ever holds a serving credential:
    # the relay does not yet support multi-homed tunnels, so exactly one machine
    # runs the tunnel and every other machine syncs THROUGH it, not with it. A
    # machine that is not designated holds no serving credential by design and
    # never will until it is designated — reporting that as a fault raises an
    # alarm on a machine behaving exactly as intended, and its "unlock with your
    # root" remedy does nothing because unlocking cannot arm a non-server. So we
    # consult tunnel-server designation before treating a missing credential as a
    # problem, exactly as fleet_doctor was corrected to do for org scopes
    # (644c88d74). Only a MANAGED fleet has a designated leader; an unmanaged
    # (legacy single-node) install keeps the original serve-everywhere behavior.
    # Computed BEFORE the tunnel/sync flags because both gate on it.
    tunnel_designated = True
    try:
        from tools.network import fleet_tunnel_server
        _ts = fleet_tunnel_server.state()
        if _ts.managed and not _ts.allowed:
            tunnel_designated = False
    except Exception:
        pass

    # tunnel — is THIS dashboard reachable from outside. Only the designated
    # tunnel server runs a tunnel; a non-designated machine reaches the fleet
    # THROUGH the server and has none of its own, so a missing tunnel there is
    # expected, not a fault (same rule as the sync flag). For the designated
    # server, serving() is a deterministic control-socket handshake: a dead
    # connector reads Down, never a swallowed "unavailable". (Before
    # ServingSupervisor.serving() existed this call AttributeError'd every
    # time and the except below silently degraded the tile to "unavailable",
    # so it could never show Down — the false-green root cause.)
    if not tunnel_designated:
        flags["tunnel"] = {
            "needs": False, "value": "",
            "detail": ("Your other devices reach the fleet through its "
                       "designated tunnel server, not this machine — it runs "
                       "no tunnel of its own, which is expected."),
        }
    else:
        try:
            from tools.dashboard.link_serving_supervisor import get_supervisor
            serving = bool(get_supervisor().serving())
            flags["tunnel"] = {
                "needs": not serving,
                "value": "Up" if serving else "Down",
                "scopes": [] if serving else ["personal"],
                "detail": ("The connection your other devices use to reach "
                           "this dashboard." if serving
                           else "Your other devices can't reach this dashboard "
                           "from outside. Bringing the tunnel back needs your "
                           "root key."),
            }
        except Exception:
            flags["tunnel"] = {"needs": False, "value": "",
                               "detail": "Tunnel state is unavailable."}

    # sync — can the fleet's other machines sync WITH this one. Lit ONLY when a
    # serving connector is unarmed (holds no credential — the memory-only one
    # that dies on restart), which the root ceremony fixes. It deliberately
    # does NOT light on "the connector's git commit differs from disk": that is
    # a developer deploy-hygiene probe (fleet_doctor's STALE-CODE top line),
    # not a user sync fault — it fires on ANY code change, including a frontend
    # edit that cannot affect sync at all, and actual schema/version
    # incompatibility is guarded exactly and cross-machine by the
    # compatibility digest (the "Paused" state), not by a local commit compare.
    # The scopeless (org=None) and "personal" scopes resolve to the SAME
    # database, so a label under both names is reported once (bead auto-9yp8r).
    unarmed_scopes = sorted(set(sync_unarmed))
    # sync_stale (connector commit != disk) is intentionally NOT surfaced here;
    # it remains a fleet_doctor developer diagnostic.
    stale_scopes = sorted(set(sync_stale))
    if not tunnel_designated:
        # This machine is not the designated tunnel server, so holding no serving
        # credential is expected, not a fault. Report it quietly and never light.
        flags["sync"] = {
            "needs": False,
            "value": "",
            "detail": ("Your other machines sync through the fleet's designated "
                       "tunnel server, not directly with this one, so it holds "
                       "no serving credential — that's expected, not a problem."),
            "unarmed": [],
            "stale": [],
            "scopes": [],
            "count": 0,
            "since": None,
        }
    else:
        sync_needs = bool(unarmed_scopes)
        if sync_needs:
            parts = ["Your other machines can't sync with this one."]
            if sync_refusals:
                plural = "s" if sync_refusals != 1 else ""
                parts.append(
                    f"{sync_refusals} request{plural} have been refused"
                    + _refused_since(sync_since) + "."
                )
            parts.append(
                _oxford(unarmed_scopes)
                + (" holds" if len(unarmed_scopes) == 1 else " hold")
                + " no serving credential — unlock with your root to give "
                + ("it" if len(unarmed_scopes) == 1 else "them") + " a new one."
            )
            sync_detail = " ".join(parts)
            sync_value = "Locked"
        else:
            sync_detail = "Whether your other machines can sync with this one."
            sync_value = "Serving" if serving_setup else ""
        flags["sync"] = {
            "needs": sync_needs,
            "value": sync_value,
            "detail": sync_detail,
            "unarmed": unarmed_scopes,
            "stale": [],
            "scopes": unarmed_scopes,
            "count": sync_refusals,
            "since": sync_since,
        }

    # approvals — requests waiting on you. Not yet wired to a live pending count
    # (there is no clean count accessor); honest no-alert default until it is.
    flags["approvals"] = {"needs": False, "detail": "Nothing is waiting on you."}

    return JSONResponse(flags)


ROUTES = [
    Route("/api/identity/status", get_status, methods=["GET"]),
    Route("/api/identity/unlock-state", get_unlock_state, methods=["GET"]),
    Route("/api/identity/personal", get_personal, methods=["GET"]),
    Route("/api/identity/personal", post_personal, methods=["POST"]),
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
