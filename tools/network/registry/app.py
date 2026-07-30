"""FastAPI application — auto.network registry v1 (spec §4).

Authorization model (I4): the registry holds **no permission tables**.
Every mutating endpoint parses a signed envelope (``signing.py``), then
verifies the embedded delegation chain against the org binding's root
public key with ``idkit.verify_chain`` — signature, org, time, strict
narrowing, and revocation are all re-checked per request. A request
signed by the bound root key itself ("root-direct", no cert) is also
accepted; scope requirements apply only to delegated signers, since the
root is the authority every scope narrows from.

Two anchors sit outside the chain rule by construction:

- **registration** (§4.1) is self-signed by the root key being bound —
  the binding does not exist yet (first-key-claims-UUID);
- **rebind** (§4.3) is signed by the recovery key the org pre-declared —
  policy ``none`` has no accept path at all (I3: structurally
  impossible), not a disabled one.

Rung-2 surface (viewer authn) is fenced off with ``501 rung-2``:
``subject.kind == "persona"`` signers and ``meta.require_auth`` grants
are rejected until Track E lands.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request, Response, WebSocket
from fastapi.responses import StreamingResponse

from tools.network.idkit import (
    ChainVerifyError,
    DelegationCert,
    IdkitError,
    KeyPair,
    MalformedError,
    RevocationError,
    RevocationRecord,
    generate_token,
    verify_chain,
    verify_revocation,
    verify_signature,
)
from tools.network.idkit.keys import PUBLIC_KEY_HEX_LEN, _decode_hex

from .assertion import IDENTIFY_SCOPE, MAX_ASSERTION_TTL, parse_assertion
from .listings import parse_attestation_record, parse_listing_claim
from .relay import TunnelHub, _resolve_live_link, tunnel_endpoint, viewer_endpoint
from .signing import ENVELOPE_VERSION, MAX_CLOCK_SKEW, request_signing_input
from .store import LinkGrant, OrgBinding, RegistryStore
from .witness import MAX_WITNESS_HEADS, sign_attestation

DEFAULT_BINDING_TTL = 30 * 86_400  # spec §4.2: binding TTL default 30d
MIN_BINDING_TTL = 3_600
MAX_BINDING_TTL = DEFAULT_BINDING_TTL

RECOVERY_POLICIES = frozenset({"none", "recovery-key"})

_BOOTLOADER_DIR = Path(__file__).resolve().parent / "bootloader"

# Bootloader shell CSP. Two facts shape it:
#
# 1. The shell is a FIXED static byte string — no target/user data is ever
#    interpolated into it (that IS the anti-enumeration property, §5.3), so
#    it has no injection surface of its own. Its only script is the external
#    autonet.js ('self').
# 2. HTML artifacts (Present decks, microsites) are INTERACTIVE and must run
#    their own scripts to render. They load into a sandboxed iframe via a
#    blob: URL — an OPAQUE origin with no same-origin access, top navigation,
#    or forms. Explicit popup capabilities let shared external links open in
#    a separate browsing context. The sandbox, not CSP, is the isolation
#    boundary between the untrusted artifact and this origin.
#
# blob: iframes inherit the embedder's CSP in Chromium, so script-src must
# admit the artifact's inline scripts ('unsafe-inline' blob:) for decks to
# work. Because the shell itself carries no inline script and no dynamic
# HTML, 'unsafe-inline' opens no vector on the shell. Shared designs and
# remote note images deliberately retain ordinary public-network access;
# the null-origin sandbox isolates them from the parent, and no-referrer
# prevents a remote request from carrying the bearer URL.
_BOOTLOADER_CSP = (
    "default-src 'none'; script-src 'self' 'unsafe-inline' blob:; "
    "style-src 'unsafe-inline'; connect-src 'self' https: http: wss: ws:; "
    "img-src blob: data: https: http:; frame-src blob:; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)

_ENVELOPE_FIELDS = frozenset({"v", "signer", "ts", "payload", "cert", "sig"})
_LINK_META_FIELDS = frozenset({"ttl", "label", "require_auth"})

# -- F3 ledger-sync topics (spec §6–7, L6) -----------------------------------
#
# Per-org topics carry 32-byte head hints (notification plane) and a
# store-and-forward mailbox of ENCRYPTED event bundles (data plane). The
# broker's whole disclosure is topic + hashes + sizes; ciphertext is
# opaque. Topic access is Tier B: every call is a signed envelope through
# the I4 gate, scoped per topic (`topic:<name>`) for delegated signers.

# \Z, not $ — $ would admit a trailing newline (a smuggled distinct
# topic/scope string and un-ledgerable 65-char "hashes")
_TOPIC_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}\Z")
_HASH_RE = re.compile(r"^[0-9a-f]{64}\Z")

BUNDLE_WIRE_VERSION = 1
MAX_HINT_HEADS = 64
MAX_BUNDLE_HASHES = 4_096
MAX_BUNDLE_BYTES = 4 * 1024 * 1024
#: nonce + GCM tag: no valid AEAD blob is smaller; garbage this short
#: would otherwise poison honest pullers' mailbox drains
MIN_BUNDLE_BYTES = 12 + 16
MAX_TOPIC_PAGE = 256
MAX_LISTING_PAGE = 256
#: An attestation minted further in the future than this is junk, not skew.
MAX_ATTESTATION_FUTURE_TS = MAX_CLOCK_SKEW

# -- G1 node reachability hints (spec §8) -------------------------------------
#
# Short TTLs are the point: a hint is a live-address claim, not a record.
# Nodes refresh on a heartbeat; anything that stops refreshing goes dark.
MAX_NODE_ADDRS = 8
MAX_NODE_URL_LEN = 256
DEFAULT_HINT_TTL = 3_600
MIN_HINT_TTL = 60
MAX_HINT_TTL = 86_400


# -- E1 session linking (spec §4.7, §4.8, §6.8) ------------------------------
#
# A first-party auto.network session is a single opaque cookie: the
# server holds all identity state keyed by the cookie's random id, the
# browser holds nothing but the pointer (I1-adjacent: no identity material
# in the browser beyond the session pointer). The cookie is HttpOnly so
# page scripts cannot read it, SameSite=Lax because redemption is a
# top-level POST from the auto.network origin itself.
SESSION_COOKIE = "an_link_session"
SESSION_TTL = 24 * 3600          # identified session lifetime (matches §6.3)
ANON_SESSION_TTL = 3600          # an anonymous QR-minting session
CHALLENGE_TTL = 60               # §4.8: "~60s" cross-device challenge
SSE_MAX_WAIT = 30                # hard real-time cap on one SSE hold


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


class ChallengeHub:
    """In-memory SSE wakeups: challenge nonce → asyncio.Event.

    Purely an optimization — the store is the source of truth for whether
    a challenge was redeemed. A waiter that registers after ``notify`` (or
    misses the event entirely) still resolves correctly by re-reading the
    store when its wait ends.
    """

    def __init__(self):
        self._events: dict = {}

    def waiter(self, nonce: str) -> asyncio.Event:
        event = self._events.get(nonce)
        if event is None:
            event = asyncio.Event()
            self._events[nonce] = event
        return event

    def notify(self, nonce: str) -> None:
        event = self._events.pop(nonce, None)
        if event is not None:
            event.set()


class AuthContext:
    """What a verified envelope authorizes (attribution per I6)."""

    def __init__(self, signer_pub: str, subject_kind: str, subject_id: str,
                 cert: Optional[DelegationCert]):
        self.signer_pub = signer_pub
        self.subject_kind = subject_kind
        self.subject_id = subject_id
        self.cert = cert


def _bad_request(msg: str) -> HTTPException:
    return HTTPException(status_code=400, detail=msg)


def _forbidden(msg: str) -> HTTPException:
    return HTTPException(status_code=403, detail=msg)


def _rung2(msg: str) -> HTTPException:
    return HTTPException(status_code=501, detail=f"rung-2: {msg}")


async def _read_json(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise _bad_request("request body is not valid JSON")
    if not isinstance(body, dict):
        raise _bad_request("request body must be a JSON object")
    return body


def _parse_envelope(body: dict) -> dict:
    unknown = set(body) - _ENVELOPE_FIELDS
    if unknown:
        raise _bad_request(f"envelope carries unknown fields: {sorted(unknown)}")
    missing = _ENVELOPE_FIELDS - set(body) - {"cert"}
    if missing:
        raise _bad_request(f"envelope is missing fields: {sorted(missing)}")
    if body["v"] != ENVELOPE_VERSION:
        raise _bad_request(f"unsupported envelope version: {body['v']!r}")
    if type(body["ts"]) is not int:
        raise _bad_request("envelope ts must be an integer unix timestamp")
    if not isinstance(body["payload"], dict):
        raise _bad_request("envelope payload must be a JSON object")
    if "cert" in body and not isinstance(body["cert"], str):
        raise _bad_request("envelope cert must be a canonical wire JSON string")
    return body


def _verify_envelope_signature(envelope: dict, method: str, path: str, now: int) -> None:
    if abs(now - envelope["ts"]) > MAX_CLOCK_SKEW:
        raise _forbidden(f"envelope ts outside ±{MAX_CLOCK_SKEW}s freshness window")
    try:
        signing_input = request_signing_input(
            method, path, envelope["ts"], envelope["signer"], envelope["payload"]
        )
        verify_signature(envelope["signer"], envelope["sig"], signing_input)
    except MalformedError as exc:
        raise _bad_request(str(exc))
    except IdkitError:
        raise _forbidden("envelope signature does not verify against signer")


def _authorize(
    envelope: dict,
    method: str,
    path: str,
    binding: OrgBinding,
    store: RegistryStore,
    now: int,
    *,
    required_scope: Optional[str] = None,
    required_target_type: Optional[str] = None,
) -> AuthContext:
    """The I4 gate: envelope signature + delegation chain to the bound root.

    Root-direct (no cert): the signer must BE the bound root key.
    Delegated: the cert must delegate to the signer and its chain must
    verify down from the bound root — including scope, narrowing, time,
    and the org's current revocation denylist.
    """
    _verify_envelope_signature(envelope, method, path, now)
    store.purge_expired_revocations(now=now)  # I7: sweep before every check
    return _verify_signer_chain(
        envelope["signer"], envelope.get("cert"), binding, store, now,
        required_scope=required_scope, required_target_type=required_target_type,
    )


def _verify_signer_chain(
    signer: str,
    cert_wire: Optional[str],
    binding: OrgBinding,
    store: RegistryStore,
    now: int,
    *,
    required_scope: Optional[str] = None,
    required_target_type: Optional[str] = None,
) -> AuthContext:
    """Chain a signer to the org's bound root — the shared half of the I4
    gate, used both for request envelopes (:func:`_authorize`) and for
    durable signed artifacts that must verify on their own chain (listing
    claims). One implementation so the two gates cannot drift."""
    if cert_wire is None:
        if signer != binding.root_pub:
            raise _forbidden("signer does not chain to the org's bound root key")
        return AuthContext(binding.root_pub, "root", binding.root_pub, None)

    try:
        cert = DelegationCert.from_json(cert_wire)
    except MalformedError as exc:
        raise _bad_request(f"cert: {exc}")
    if cert.child_pub != signer:
        raise _forbidden("cert does not delegate to the signer")

    try:
        result = verify_chain(
            cert,
            binding.root_pub,
            org=binding.org_uuid,
            now=now,
            revocations=store.revocation_set(binding.org_uuid),
            required_scope=required_scope,
            required_target_type=required_target_type,
        )
    except ChainVerifyError as exc:
        raise _forbidden(f"{type(exc).__name__}: {exc}")
    except MalformedError as exc:
        raise _bad_request(str(exc))

    if result.subject_kind == "persona":
        raise _rung2("persona subjects require viewer authn (Track E)")
    return AuthContext(result.leaf_pub, result.subject_kind, result.subject_id, cert)


def _require_binding(store: RegistryStore, org_uuid: str, now: int) -> OrgBinding:
    binding = store.get_org(org_uuid)
    if binding is None:
        raise HTTPException(status_code=404, detail="unknown org binding")
    if binding.expires_at < now:
        raise HTTPException(status_code=410, detail="org binding has expired")
    return binding


def _clamp_ttl(payload: dict, field: str = "requested_ttl") -> int:
    ttl = payload.get(field, DEFAULT_BINDING_TTL)
    if type(ttl) is not int or ttl <= 0:
        raise _bad_request(f"{field} must be a positive integer of seconds")
    return max(MIN_BINDING_TTL, min(ttl, MAX_BINDING_TTL))


def _require_fields(payload: dict, allowed: frozenset, required: frozenset, what: str) -> None:
    unknown = set(payload) - allowed
    if unknown:
        raise _bad_request(f"{what} carries unknown fields: {sorted(unknown)}")
    missing = required - set(payload)
    if missing:
        raise _bad_request(f"{what} is missing fields: {sorted(missing)}")


def _require_pub(value: object, what: str) -> str:
    try:
        _decode_hex(value, PUBLIC_KEY_HEX_LEN, what)
    except MalformedError as exc:
        raise _bad_request(str(exc))
    return value  # type: ignore[return-value]


def _require_uuid(value: object, what: str) -> str:
    if not isinstance(value, str):
        raise _bad_request(f"{what} must be a UUID string")
    try:
        uuid.UUID(value)
    except ValueError:
        raise _bad_request(f"{what} is not a valid UUID")
    return value


def _require_ws_url(value: object, what: str) -> str:
    """A reachability candidate: a ws:// or wss:// URL, bounded length.

    Deliberately shallow — the registry stores hints, it never dials
    them; a candidate that turns out to be garbage just fails the
    dialer's connection attempt like any dead address."""
    if (
        not isinstance(value, str)
        or not value.startswith(("ws://", "wss://"))
        or len(value) > MAX_NODE_URL_LEN
        or any(c.isspace() for c in value)
    ):
        raise _bad_request(
            f"{what} must be a ws:// or wss:// URL of at most {MAX_NODE_URL_LEN} chars"
        )
    return value


def _require_topic(topic: str) -> str:
    if not _TOPIC_RE.match(topic):
        raise _bad_request(
            "topic must be 1-64 chars of [a-z0-9._-] starting alphanumeric"
        )
    return topic


def _require_hashes(value: object, what: str, max_len: int) -> list:
    if not isinstance(value, list) or not value or len(value) > max_len:
        raise _bad_request(f"{what} must be a non-empty list of at most {max_len} event ids")
    for entry in value:
        if not isinstance(entry, str) or not _HASH_RE.match(entry):
            raise _bad_request(f"{what} entries must be 64-char lowercase hex event ids")
    if value != sorted(set(value)):
        raise _bad_request(f"{what} must be sorted and free of duplicates")
    return value


def _require_seq(payload: dict, field: str = "since") -> int:
    value = payload.get(field, 0)
    if type(value) is not int or value < 0:
        raise _bad_request(f"{field} must be a non-negative integer sequence cursor")
    return value


def _parse_recovery_policy(payload: dict) -> tuple:
    """Returns (policy, recovery_pub_or_None); enforces pairing rules."""
    policy = payload.get("recovery_policy")
    if policy not in RECOVERY_POLICIES:
        raise _bad_request(f"recovery_policy must be one of {sorted(RECOVERY_POLICIES)}")
    recovery_pub = payload.get("recovery_pub")
    if policy == "recovery-key":
        if recovery_pub is None:
            raise _bad_request("recovery_policy recovery-key requires recovery_pub")
        return policy, _require_pub(recovery_pub, "recovery_pub")
    if recovery_pub is not None:
        raise _bad_request("recovery_pub is only valid with recovery_policy recovery-key")
    return policy, None


def create_app(
    db_path: str = ":memory:",
    *,
    now_fn=None,
    base_url: str = "https://relay.auto.network",
    witness_key: Optional[KeyPair] = None,
    secure_cookies: bool = True,
) -> FastAPI:
    """Build the registry app.

    *now_fn* is the clock (unix seconds); injectable so TTL, expiry, and
    purge behavior are deterministic under test. *base_url* prefixes the
    share-link URLs returned by ``POST /v1/links``. *witness_key* is the
    Ed25519 key the equivocation witness (F4) signs its head-set
    attestations with; a fresh one is generated when omitted, but a
    persistent deployment must pass a stable key — clients *pin* the
    witness public key, and rotating it silently would break split-view
    detection. Discover/pin it via ``GET /v1/witness/pubkey``. *secure_cookies*
    marks session cookies ``Secure`` (production default); tests over
    plain-http ``testserver`` set it False so the client keeps the cookie.
    """
    app = FastAPI(title="auto.network registry", version="1")
    store = RegistryStore(db_path)
    now_fn = now_fn or (lambda: int(time.time()))
    hub = TunnelHub()
    witness_key = witness_key or KeyPair.generate()
    challenge_hub = ChallengeHub()
    app.state.store = store
    app.state.now_fn = now_fn
    app.state.tunnel_hub = hub
    app.state.witness_key = witness_key
    app.state.challenge_hub = challenge_hub

    def now() -> int:
        return int(now_fn())

    def _set_session_cookie(response: Response, session_id: str, max_age: int) -> None:
        response.set_cookie(
            key=SESSION_COOKIE,
            value=session_id,
            max_age=max_age,
            httponly=True,
            secure=secure_cookies,
            samesite="lax",
            path="/",
        )

    def _current_session(request: Request, t: int):
        """The caller's live session, or None (missing/expired cookie)."""
        session_id = request.cookies.get(SESSION_COOKIE)
        if not session_id:
            return None
        session = store.get_session(session_id)
        if session is None or session.expires_at < t:
            return None
        return session

    # -- §4.1 register binding ----------------------------------------------

    @app.post("/v1/orgs", status_code=201)
    async def register_org(request: Request):
        envelope = _parse_envelope(await _read_json(request))
        payload = envelope["payload"]
        _require_fields(
            payload,
            allowed=frozenset(
                {"org_uuid", "root_pub", "recovery_policy", "recovery_pub",
                 "requested_ttl", "endpoint_hints"}
            ),
            required=frozenset({"org_uuid", "root_pub", "recovery_policy"}),
            what="registration payload",
        )
        org_uuid = _require_uuid(payload["org_uuid"], "org_uuid")
        root_pub = _require_pub(payload["root_pub"], "root_pub")
        policy, recovery_pub = _parse_recovery_policy(payload)
        endpoint_hints = payload.get("endpoint_hints")
        if endpoint_hints is not None and not isinstance(endpoint_hints, list):
            raise _bad_request("endpoint_hints must be a list")
        ttl = _clamp_ttl(payload)

        # Genesis is self-signed: the key claiming the UUID must sign the
        # claim. No cert — there is nothing to chain to yet.
        if envelope.get("cert") is not None:
            raise _bad_request("registration is self-signed by root_pub; cert must be absent")
        if envelope["signer"] != root_pub:
            raise _forbidden("registration must be signed by the root_pub being bound")
        t = now()
        _verify_envelope_signature(envelope, "POST", str(request.url.path), t)

        # Atomic first-claim (F1): the existence check and the INSERT happen
        # under one held lock, so two concurrent registrations cannot both
        # pass the check. A live binding on the UUID → 409; an expired one is
        # atomically reclaimed. Names are not authority (§4.1).
        outcome = store.claim_org(
            org_uuid,
            root_pub,
            policy,
            recovery_pub,
            now=t,
            expires_at=t + ttl,
            endpoint_hints=endpoint_hints,
        )
        if outcome == "conflict_live":
            raise HTTPException(status_code=409, detail="org UUID is already bound")
        return {"org_uuid": org_uuid, "root_pub": root_pub, "expires_at": t + ttl}

    # -- §4.2 renew (heartbeat) ----------------------------------------------

    @app.post("/v1/orgs/{org_uuid}/renew")
    async def renew_org(org_uuid: str, request: Request):
        envelope = _parse_envelope(await _read_json(request))
        _require_fields(
            envelope["payload"],
            allowed=frozenset({"requested_ttl"}),
            required=frozenset(),
            what="renew payload",
        )
        ttl = _clamp_ttl(envelope["payload"])
        t = now()
        binding = _require_binding(store, org_uuid, t)
        # Renewal is deliberately the weakest mutation: it only extends the
        # liveness of authority that already exists, so ANY key that
        # verifiably belongs to the org (root-direct or any valid chain,
        # no scope requirement) may heartbeat.
        _authorize(envelope, "POST", str(request.url.path), binding, store, t)
        store.renew_org(org_uuid, now=t, expires_at=t + ttl)
        return {"org_uuid": org_uuid, "expires_at": t + ttl}

    # -- §4.3 rebind (recovery only, I3) --------------------------------------

    @app.post("/v1/orgs/{org_uuid}/rebind")
    async def rebind_org(org_uuid: str, request: Request):
        t = now()
        binding = _require_binding(store, org_uuid, t)

        # I3: no rebind path outside the pre-declared policy. Under policy
        # "none" this endpoint has no accept branch at all — the request is
        # refused before any signature is even looked at, so no payload
        # (valid-root-signed included) can reach a rebind.
        if binding.recovery_policy != "recovery-key":
            raise _forbidden(
                "rebind is structurally unavailable: recovery policy is "
                f"{binding.recovery_policy!r}"
            )

        envelope = _parse_envelope(await _read_json(request))
        payload = envelope["payload"]
        _require_fields(
            payload,
            allowed=frozenset({"new_root_pub", "recovery_policy", "recovery_pub"}),
            required=frozenset({"new_root_pub"}),
            what="rebind payload",
        )
        new_root_pub = _require_pub(payload["new_root_pub"], "new_root_pub")
        if "recovery_policy" in payload:
            new_policy, new_recovery_pub = _parse_recovery_policy(payload)
        else:
            if "recovery_pub" in payload:
                raise _bad_request("recovery_pub requires recovery_policy")
            new_policy, new_recovery_pub = binding.recovery_policy, binding.recovery_pub

        # The ONLY key that can sign a rebind is the pre-declared cold
        # recovery key. Not the root (a stolen root must not be able to
        # rotate away the recovery path), not a delegate.
        if envelope.get("cert") is not None:
            raise _forbidden("rebind must be signed directly by the recovery key; cert must be absent")
        if envelope["signer"] != binding.recovery_pub:
            raise _forbidden("rebind must be signed by the declared recovery key")
        _verify_envelope_signature(envelope, "POST", str(request.url.path), t)

        store.rebind_org(org_uuid, binding.root_pub, new_root_pub, now=t)
        if new_policy != binding.recovery_policy or new_recovery_pub != binding.recovery_pub:
            # Shared monotonic counter across policy-update and rebind (F1):
            # bump the epoch so a concurrent policy envelope's CAS fails.
            store.update_recovery_policy(
                org_uuid, new_policy, new_recovery_pub,
                expected_epoch=binding.policy_epoch,
                new_epoch=binding.policy_epoch + 1,
            )
        return {
            "org_uuid": org_uuid,
            "root_pub": new_root_pub,
            "previous_root_pub": binding.root_pub,
            "recovery_policy": new_policy,
        }

    # -- recovery-policy update (sovereign, root-signed) ----------------------

    @app.post("/v1/orgs/{org_uuid}/policy")
    async def update_policy(org_uuid: str, request: Request):
        """Root-signed recovery-policy update (SOVEREIGN model): the current
        root sets / changes / REMOVES recovery freely, proven by a root-direct
        signature — no third party constrains it. This is NOT a rebind:
        root_pub is unchanged (rebind, which recovers to a NEW root for a lost
        key, stays recovery-key-signed; I3). A monotonic ``policy_epoch``
        (compare-and-swap) blocks replay/downgrade of an old policy envelope.
        """
        t = now()
        binding = _require_binding(store, org_uuid, t)

        envelope = _parse_envelope(await _read_json(request))
        payload = envelope["payload"]
        _require_fields(
            payload,
            allowed=frozenset({"recovery_policy", "recovery_pub", "policy_epoch"}),
            required=frozenset({"recovery_policy", "policy_epoch"}),
            what="policy payload",
        )
        # Root-direct, signed by the CURRENT bound root — not a delegate, not
        # the recovery key. Possession of the root IS the authority.
        if envelope.get("cert") is not None:
            raise _forbidden("policy update is root-direct: the envelope must carry no cert")
        if envelope["signer"] != binding.root_pub:
            raise _forbidden("policy update must be signed by the org's current bound root")
        _verify_envelope_signature(envelope, "POST", str(request.url.path), t)

        new_policy, new_recovery_pub = _parse_recovery_policy(payload)

        epoch = payload["policy_epoch"]
        if type(epoch) is not int:
            raise _bad_request("policy_epoch must be an integer")
        # Strict monotonic +1: rejects replay of an old envelope and any
        # downgrade to a stale epoch. The client reads the current epoch from
        # the binding and submits exactly current+1.
        if epoch != binding.policy_epoch + 1:
            raise HTTPException(status_code=409, detail=(
                f"stale policy_epoch: expected {binding.policy_epoch + 1}, got {epoch} "
                "— re-read the binding and retry; an old policy envelope cannot replay"
            ))

        ok = store.update_recovery_policy(
            org_uuid, new_policy, new_recovery_pub,
            expected_epoch=binding.policy_epoch, new_epoch=epoch,
        )
        if not ok:
            # The CAS lost a race: a concurrent policy update or rebind bumped
            # the epoch between our read and our write.
            raise HTTPException(status_code=409, detail=(
                "policy update lost a concurrent race — re-read the binding and retry"
            ))
        return {
            "org_uuid": org_uuid,
            "recovery_policy": new_policy,
            "recovery_pub": new_recovery_pub,
            "policy_epoch": epoch,
        }

    # -- §4.4 links ------------------------------------------------------------

    @app.post("/v1/links", status_code=201)
    async def create_link(request: Request):
        envelope = _parse_envelope(await _read_json(request))
        payload = envelope["payload"]
        _require_fields(
            payload,
            allowed=frozenset(
                {
                    "org",
                    "target_uuid",
                    "target_type",
                    "invite_ref",
                    "expires_at",
                    "meta",
                }
            ),
            required=frozenset({"org", "target_uuid", "target_type"}),
            what="link payload",
        )
        org_uuid = _require_uuid(payload["org"], "org")
        target_uuid = _require_uuid(payload["target_uuid"], "target_uuid")
        target_type = payload["target_type"]
        if not isinstance(target_type, str) or not target_type:
            raise _bad_request("target_type must be a non-empty string")
        invite_ref = payload.get("invite_ref")
        absolute_expires_at = payload.get("expires_at")
        if target_type == "org:join":
            if (
                not isinstance(invite_ref, str)
                or not _HASH_RE.fullmatch(invite_ref)
            ):
                raise _bad_request(
                    "org:join invite_ref must be a 64-char lowercase hex event id"
                )
            if target_uuid != org_uuid:
                raise _bad_request(
                    "org:join target_uuid must equal the organization UUID"
                )
            if absolute_expires_at is not None and (
                type(absolute_expires_at) is not int
                or absolute_expires_at < 0
                or absolute_expires_at > 9_007_199_254_740_991
            ):
                raise _bad_request(
                    "org:join expires_at must be a non-negative safe "
                    "unix-ms integer"
                )
        elif invite_ref is not None or absolute_expires_at is not None:
            raise _bad_request(
                "invite_ref and expires_at are only valid for target_type org:join"
            )
        meta = payload.get("meta", {})
        if not isinstance(meta, dict):
            raise _bad_request("meta must be a JSON object")
        _require_fields(meta, allowed=_LINK_META_FIELDS, required=frozenset(), what="link meta")
        if meta.get("require_auth"):
            raise _rung2("require_auth grants need viewer authn (Track E + ledger)")
        link_ttl = meta.get("ttl")
        if link_ttl is not None and (type(link_ttl) is not int or link_ttl <= 0):
            raise _bad_request("meta.ttl must be a positive integer of seconds")
        if absolute_expires_at is not None and link_ttl is not None:
            raise _bad_request(
                "org:join expires_at and meta.ttl are mutually exclusive"
            )

        t = now()
        binding = _require_binding(store, org_uuid, t)
        auth = _authorize(
            envelope, "POST", str(request.url.path), binding, store, t,
            required_scope="link:publish", required_target_type=target_type,
        )

        # I2: the token is pure CSPRNG output — generate_token() takes no
        # inputs, so it cannot be derived from the target.
        token = generate_token()
        store.create_link(
            LinkGrant(
                token=token,
                org_uuid=org_uuid,
                target_uuid=target_uuid,
                target_type=target_type,
                invite_ref=invite_ref,
                meta=meta,
                created_at=t,
                expires_at=t + link_ttl if link_ttl is not None else None,
                expires_at_ms=absolute_expires_at,
                revoked_at=None,
                signer_pub=auth.signer_pub,
                subject_kind=auth.subject_kind,
                subject_id=auth.subject_id,
            )
        )
        result = {"token": token, "url": f"{base_url}/l/{token}"}
        if absolute_expires_at is not None:
            result["expires_at"] = absolute_expires_at
        return result

    @app.delete("/v1/links/{token}")
    async def revoke_link(token: str, request: Request):
        envelope = _parse_envelope(await _read_json(request))
        _require_fields(
            envelope["payload"], allowed=frozenset(), required=frozenset(), what="revoke payload"
        )
        t = now()
        link = store.get_link(token)
        if link is None:
            raise HTTPException(status_code=404, detail="unknown link")
        binding = _require_binding(store, link.org_uuid, t)
        _authorize(
            envelope, "DELETE", str(request.url.path), binding, store, t,
            required_scope="link:revoke",
        )
        store.revoke_link(token, now=t)
        return {"token": token, "revoked_at": t}

    # -- §4.5 revocations --------------------------------------------------------

    @app.post("/v1/revocations", status_code=201)
    async def add_revocation(request: Request):
        # The revocation record is self-authorizing (root- or
        # ancestor-signed), so this endpoint takes the bare record plus the
        # revoked key's cert (the I7 proof of the natural expiry horizon) —
        # no envelope. Anyone may DELIVER a valid record; only the org's
        # own keys can MINT one.
        body = await _read_json(request)
        _require_fields(
            body,
            allowed=frozenset({"org", "record", "revoked_cert"}),
            required=frozenset({"org", "record", "revoked_cert"}),
            what="revocation payload",
        )
        org_uuid = _require_uuid(body["org"], "org")
        if not isinstance(body["record"], str) or not isinstance(body["revoked_cert"], str):
            raise _bad_request("record and revoked_cert must be canonical wire JSON strings")
        t = now()
        binding = _require_binding(store, org_uuid, t)
        try:
            record = RevocationRecord.from_json(body["record"])
            revoked_cert = DelegationCert.from_json(body["revoked_cert"])
        except MalformedError as exc:
            raise _bad_request(str(exc))
        try:
            verify_revocation(record, binding.root_pub, org=org_uuid, revoked_cert=revoked_cert)
        except (RevocationError, ChainVerifyError) as exc:
            raise _forbidden(f"{type(exc).__name__}: {exc}")
        except MalformedError as exc:
            raise _bad_request(str(exc))
        store.add_revocation(org_uuid, record)
        store.purge_expired_revocations(now=t)
        return {"revoked_key_id": record.revoked_key_id, "expires_at": record.expires_at}

    @app.get("/v1/orgs/{org_uuid}/revocations")
    async def list_revocations(org_uuid: str):
        # The org's live key denylist. Read-only and non-secret: the
        # registry already enforces it publicly on every chain verification,
        # and a revoked key id reveals nothing usable. Under D19 the
        # dashboard authenticates the acting persona LOCALLY, so it must be
        # able to consult the same denylist the registry's own chain gate
        # uses — this endpoint is that source. Expired entries are purged
        # first so the list is exactly what verify_chain would honor now.
        t = now()
        uuid_str = _require_uuid(org_uuid, "org")
        store.purge_expired_revocations(now=t)
        return {"revoked": sorted(store.revoked_key_ids(uuid_str))}

    # -- §4.6 grant envelope (bootloader) -------------------------------------

    @app.get("/v1/links/{token}/envelope")
    async def link_envelope(token: str, request: Request):
        # Anti-enumeration (§5.3): unknown, expired, revoked, and
        # dead-binding tokens are all the SAME 404 — a prober learns
        # nothing about which failure they hit.
        t = now()
        link = store.get_link(token)
        if (
            link is None
            or link.revoked_at is not None
            or link.is_expired_at(t)
        ):
            raise HTTPException(status_code=404, detail="unknown link")
        binding = store.get_org(link.org_uuid)
        if binding is None or binding.expires_at < t:
            raise HTTPException(status_code=404, detail="unknown link")

        # I12: identity attaches to a VIEW only when the grant requires it.
        # A plain bearer (no-auth) grant carries no identity attribution
        # even when the fetching browser holds a fully identified session —
        # we do not so much as look up its identity here, and nothing is
        # written. When require_auth grants land (rung 2) this is the seam
        # where an identified session's attribution gets recorded.
        if link.meta.get("require_auth"):
            session = _current_session(request, t)
            if session is not None and session.identified:
                store.record_view_attribution(
                    session.session_id, token,
                    subject_kind=session.subject_kind,
                    subject_id=session.subject_id,
                    org_uuid=session.org_uuid, now=t,
                )
        return {
            "org": link.org_uuid,
            "target_uuid": link.target_uuid,
            "target_type": link.target_type,
            "invite_ref": link.invite_ref,
            "meta": link.meta,
            "root_pub": binding.root_pub,
            # Direct-connect upgrade seam (§5.4): empty in v1; the
            # bootloader tries these before relay fallback once populated.
            "endpoints": [],
        }

    # -- §5.3 bootloader --------------------------------------------------------

    shell_bytes = (_BOOTLOADER_DIR / "bootloader.html").read_bytes()
    js_bytes = (_BOOTLOADER_DIR / "autonet.js").read_bytes()

    @app.get("/l/{token}")
    async def bootloader_page(token: str):
        # ONE static byte sequence for every token — live, expired,
        # revoked, or invented. The token is read client-side from the
        # URL; nothing org- or target-identifying is in these bytes
        # (§5.3: the URL is a pure network pointer). Only the STATUS
        # differs, and it mirrors the envelope endpoint's liveness rule
        # exactly, so it opens no oracle the envelope doesn't already.
        live = _resolve_live_link(store, token, now()) is not None
        return Response(
            content=shell_bytes,
            status_code=200 if live else 404,
            media_type="text/html",
            headers={
                "Content-Security-Policy": _BOOTLOADER_CSP,
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
            },
        )

    @app.get("/l-assets/autonet.js")
    async def bootloader_js():
        return Response(
            content=js_bytes,
            media_type="text/javascript",
            headers={
                # The shell and script are one protocol unit. Caching this
                # path across a deploy can pair new HTML with old JavaScript
                # and leave every view hidden after an error.
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    # -- §4.7 bootstrap-assertion redemption ------------------------------------

    @app.post("/v1/link")
    async def redeem_assertion(request: Request, response: Response):
        """Redeem a single-use ``viewer:identify`` assertion for a session.

        The person's own dashboard signed this; here we only VERIFY. If it
        chains to a bound org root, is fresh, identify-scoped, and unspent,
        the caller (or, in the QR case, the challenge-bound browser) walks
        away identified — no login page. Personas are first-class (this is
        the rung-2 surface the mutation gate fences off).
        """
        try:
            assertion = parse_assertion(await _read_json(request))
        except MalformedError as exc:
            raise _bad_request(str(exc))
        org_uuid = _require_uuid(assertion.org, "org")

        t = now()
        # I7 hygiene: sweep stale replay/session/challenge state each redeem.
        store.purge_expired_assertions(now=t)
        store.purge_expired_challenges(now=t)
        store.purge_expired_sessions(now=t)

        # Identify-only (§7A): any other scope is out of bounds, full stop.
        if assertion.scope != IDENTIFY_SCOPE:
            raise _forbidden(f"assertion scope must be {IDENTIFY_SCOPE!r}")

        # Seconds-scale TTL, and currently within its window.
        if assertion.not_after - assertion.not_before > MAX_ASSERTION_TTL:
            raise _bad_request(f"assertion validity window exceeds {MAX_ASSERTION_TTL}s")
        if not (assertion.not_before <= t <= assertion.not_after):
            raise HTTPException(status_code=401, detail="assertion is expired or not yet valid")

        # The assertion's own signature over its identity claim. This is
        # self-contained (no binding needed), so it is checked BEFORE the
        # binding lookup: a bad signature is 401 whether or not the org is
        # bound, so 401-vs-403 never leaks which org UUIDs are registered.
        try:
            verify_signature(assertion.signer, assertion.sig, assertion.signing_input())
        except MalformedError as exc:
            raise _bad_request(str(exc))
        except IdkitError:
            raise HTTPException(status_code=401, detail="assertion signature does not verify")

        # A missing/dead binding means there is no registered root to chain
        # to — the same verdict (403) as a chain reaching an unbound root.
        binding = store.get_org(org_uuid)
        if binding is None or binding.expires_at < t:
            raise _forbidden("assertion does not chain to a registered org root")

        # Delegation chain signer → org root, identify-scoped. verify_chain
        # accepts persona subjects; only the mutation gate rejects them.
        try:
            cert = DelegationCert.from_json(assertion.cert)
        except MalformedError as exc:
            raise _bad_request(f"cert: {exc}")
        if cert.child_pub != assertion.signer:
            raise _forbidden("cert does not delegate to the assertion signer")
        store.purge_expired_revocations(now=t)
        try:
            result = verify_chain(
                cert, binding.root_pub, org=org_uuid, now=t,
                revocations=store.revocation_set(org_uuid),
                required_scope=IDENTIFY_SCOPE,
            )
        except ChainVerifyError as exc:
            raise _forbidden(f"{type(exc).__name__}: {exc}")
        except MalformedError as exc:
            raise _bad_request(str(exc))

        # Single-use (fast path; consume_assertion below is the atomic guard).
        if store.assertion_consumed(assertion.nonce):
            raise HTTPException(status_code=401, detail="assertion already redeemed")

        qr = assertion.challenge is not None
        if qr:
            # Cross-device (§4.8): upgrade the challenge-BOUND anonymous
            # session, never the submitter's. Missing / expired / already
            # redeemed challenge is one indistinguishable 404 — the QR
            # nonce must not be an enumeration oracle.
            challenge = store.get_challenge(assertion.challenge)
            if challenge is None or challenge.redeemed_at is not None or challenge.expires_at < t:
                raise HTTPException(status_code=404, detail="unknown challenge")
            target_session = challenge.session_id
        else:
            # Same-browser: always mint a FRESH session id (rotate on
            # identify), so a pre-seeded cookie cannot fixate the session
            # that walks away identified. Only the QR path reuses an id —
            # and there the reused id is the anonymous browser's own,
            # chosen by that browser, never an attacker's.
            target_session = generate_token()

        # Atomic commit: spend the single-use nonce, redeem the QR challenge
        # (if any), and upgrade the target session — all in ONE transaction
        # under the store lock. Under concurrent duplicate redemptions this
        # yields exactly one winner and clean deterministic verdicts for the
        # rest, never a torn write or a 500.
        outcome = store.commit_redemption(
            nonce=assertion.nonce,
            org_uuid=org_uuid,
            assertion_expires_at=assertion.not_after,
            challenge=assertion.challenge,
            target_session=target_session,
            subject_kind=result.subject_kind,
            subject_id=result.subject_id,
            dashboard_origin=assertion.dashboard_origin,
            now=t,
            session_expires_at=t + SESSION_TTL,
        )
        if outcome == "replay":
            raise HTTPException(status_code=401, detail="assertion already redeemed")
        if outcome == "challenge":
            # Single-use challenge gone/expired/already redeemed — one
            # indistinguishable 404, nothing committed (nonce rolled back).
            raise HTTPException(status_code=404, detail="unknown challenge")

        if qr:
            # Wake the anonymous browser's SSE waiter. Deliberately set NO
            # cookie on this response: the submitter (the PWA) is not the
            # browser that got identity.
            challenge_hub.notify(assertion.challenge)
            return {"linked": True}

        _set_session_cookie(response, target_session, SESSION_TTL)
        return {
            "linked": True,
            "org": org_uuid,
            "subject": {"kind": result.subject_kind, "id": result.subject_id},
        }

    # -- §4.8 QR cross-device challenge -----------------------------------------

    @app.post("/v1/link/challenge")
    async def mint_challenge(request: Request, response: Response):
        """An anonymous browser mints a ~60s challenge bound to ITS session.

        The nonce is rendered as a QR code; the operator's PWA scans it,
        signs an assertion carrying the nonce, and submits it to ``/v1/link``
        from its OWN channel — which upgrades this browser's session (§4.8).
        """
        t = now()
        store.purge_expired_challenges(now=t)
        store.purge_expired_sessions(now=t)

        session = _current_session(request, t)
        if session is None:
            session_id = generate_token()
            store.create_anonymous_session(session_id, now=t, expires_at=t + ANON_SESSION_TTL)
            _set_session_cookie(response, session_id, ANON_SESSION_TTL)
        else:
            session_id = session.session_id

        nonce = generate_token()
        store.create_challenge(nonce, session_id, now=t, expires_at=t + CHALLENGE_TTL)
        return {"challenge": nonce, "expires_at": t + CHALLENGE_TTL}

    @app.get("/v1/link/challenge/{nonce}")
    async def challenge_status(nonce: str):
        """Poll fallback (and the deterministic contract behind the SSE
        wait): whether the challenge has been redeemed. Missing or expired
        is one 404 — same anti-enumeration verdict as the wait stream."""
        t = now()
        challenge = store.get_challenge(nonce)
        if challenge is None or challenge.expires_at < t:
            raise HTTPException(status_code=404, detail="unknown challenge")
        return {"linked": challenge.redeemed_at is not None}

    @app.get("/v1/link/challenge/{nonce}/wait")
    async def challenge_wait(nonce: str):
        """SSE: emit ``linked`` once the challenge is redeemed, or
        ``expired`` when its window closes. A push optimization over the
        poll endpoint above — same cursorless contract, store-authoritative."""
        t = now()
        challenge = store.get_challenge(nonce)
        if challenge is None or challenge.expires_at < t:
            raise HTTPException(status_code=404, detail="unknown challenge")

        async def stream():
            ch = store.get_challenge(nonce)
            if ch is not None and ch.redeemed_at is not None:
                yield _sse("linked", {"linked": True})
                return
            event = challenge_hub.waiter(nonce)
            remaining = max(0, ch.expires_at - now()) if ch is not None else 0
            try:
                await asyncio.wait_for(event.wait(), timeout=min(remaining, SSE_MAX_WAIT))
            except asyncio.TimeoutError:
                pass
            ch = store.get_challenge(nonce)
            if ch is not None and ch.redeemed_at is not None:
                yield _sse("linked", {"linked": True})
            else:
                yield _sse("expired", {"linked": False})

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    # -- §6.8 signed endpoint hints (identified sessions only) ------------------

    @app.get("/v1/link/hints")
    async def link_hints(request: Request):
        """Serve the org's signed endpoint hints — ONLY to an identified
        session (§6.8). An anonymous or unlinked browser learns nothing:
        strangers get no auto-discovery of a dashboard's whereabouts."""
        t = now()
        session = _current_session(request, t)
        if session is None or not session.identified:
            raise _forbidden("endpoint hints require an identified session")
        binding = store.get_org(session.org_uuid)
        hints = binding.endpoint_hints if binding is not None else None
        return {"endpoint_hints": hints or []}

    # -- §6 ledger-sync topics (F3): hints + encrypted mailbox ------------------

    async def _topic_gate(request: Request, org_uuid: str, topic: str) -> tuple:
        """Shared prologue: envelope + I4 chain with per-topic scope.

        Every topic operation — publish and subscribe alike — is Tier B:
        it must chain to the org's bound root. Delegated signers need the
        exact scope ``topic:<name>``, so an attenuated key granted only
        content topics can neither publish to nor read the authority
        topic. Root-direct signers hold every scope by definition.
        """
        _require_topic(topic)
        envelope = _parse_envelope(await _read_json(request))
        t = now()
        binding = _require_binding(store, org_uuid, t)
        auth = _authorize(
            envelope, "POST", str(request.url.path), binding, store, t,
            required_scope=f"topic:{topic}",
        )
        return envelope["payload"], auth, t

    @app.post("/v1/orgs/{org_uuid}/topics/{topic}/heads", status_code=201)
    async def publish_heads(org_uuid: str, topic: str, request: Request):
        """Notification plane: announce new DAG heads (32-byte hints only)."""
        payload, auth, t = await _topic_gate(request, org_uuid, topic)
        _require_fields(
            payload, allowed=frozenset({"heads"}), required=frozenset({"heads"}),
            what="heads payload",
        )
        heads = _require_hashes(payload["heads"], "heads", MAX_HINT_HEADS)
        seq = store.publish_hint(org_uuid, topic, heads, auth.signer_pub, now=t)
        return {"topic": topic, "seq": seq}

    @app.post("/v1/orgs/{org_uuid}/topics/{topic}/heads/poll")
    async def poll_heads(org_uuid: str, topic: str, request: Request):
        """Fanout: hints after a cursor, plus the latest announcement.

        Poll-based fanout keeps the broker a plain mailbox ("a well-known
        peer that is always awake", §7); an SSE/WS push upgrade is a
        transport optimization over this same cursor, not a new contract.
        """
        payload, _auth, _t = await _topic_gate(request, org_uuid, topic)
        _require_fields(
            payload, allowed=frozenset({"since"}), required=frozenset(), what="poll payload"
        )
        since = _require_seq(payload)
        hints = store.hints_since(org_uuid, topic, since, limit=MAX_TOPIC_PAGE)
        latest = store.latest_hint(org_uuid, topic)
        next_since = hints[-1]["seq"] if hints else since
        return {"topic": topic, "hints": hints, "latest": latest, "next_since": next_since}

    @app.post("/v1/orgs/{org_uuid}/topics/{topic}/bundles", status_code=201)
    async def deposit_bundle(org_uuid: str, topic: str, request: Request):
        """Data plane: store-and-forward one ENCRYPTED event bundle.

        L6 by construction: the accepted fields are a hash manifest and a
        base64 blob. The broker records topic, hashes, and size; it never
        holds a decryption key, and unknown fields (anywhere plaintext
        could hide) are rejected by the strict field check.
        """
        payload, auth, t = await _topic_gate(request, org_uuid, topic)
        _require_fields(
            payload, allowed=frozenset({"v", "hashes", "ciphertext"}),
            required=frozenset({"v", "hashes", "ciphertext"}), what="bundle payload",
        )
        if payload["v"] != BUNDLE_WIRE_VERSION:
            raise _bad_request(f"unsupported bundle version: {payload['v']!r}")
        hashes = _require_hashes(payload["hashes"], "hashes", MAX_BUNDLE_HASHES)
        if not isinstance(payload["ciphertext"], str) or not payload["ciphertext"]:
            raise _bad_request("ciphertext must be a non-empty base64 string")
        try:
            blob = base64.b64decode(payload["ciphertext"], validate=True)
        except (ValueError, TypeError):
            raise _bad_request("ciphertext is not valid base64")
        if not MIN_BUNDLE_BYTES <= len(blob) <= MAX_BUNDLE_BYTES:
            raise _bad_request(
                f"ciphertext must be {MIN_BUNDLE_BYTES}..{MAX_BUNDLE_BYTES} bytes"
            )
        seq = store.deposit_bundle(
            org_uuid, topic, payload["v"], hashes, blob, auth.signer_pub, now=t
        )
        return {"topic": topic, "seq": seq, "size": len(blob)}

    @app.post("/v1/orgs/{org_uuid}/topics/{topic}/bundles/fetch")
    async def fetch_bundles(org_uuid: str, topic: str, request: Request):
        """Mailbox read: by cursor (store-and-forward catch-up) or by hash
        (fetch-missing-by-hash), optionally metadata-only (hashes + sizes)
        for anti-entropy planning without moving ciphertext."""
        payload, _auth, _t = await _topic_gate(request, org_uuid, topic)
        _require_fields(
            payload, allowed=frozenset({"since", "want", "meta_only"}),
            required=frozenset(), what="fetch payload",
        )
        meta_only = payload.get("meta_only", False)
        if not isinstance(meta_only, bool):
            raise _bad_request("meta_only must be a boolean")
        if "want" in payload:
            if "since" in payload:
                raise _bad_request("fetch takes since or want, not both")
            want = _require_hashes(payload["want"], "want", MAX_BUNDLE_HASHES)
            rows = store.bundles_with(org_uuid, topic, want, limit=MAX_TOPIC_PAGE)
        else:
            since = _require_seq(payload)
            rows = store.bundles_since(
                org_uuid, topic, since, limit=MAX_TOPIC_PAGE, meta_only=meta_only
            )
        bundles = []
        for row in rows:
            entry = {"seq": row["seq"], "v": row["v"], "hashes": row["hashes"],
                     "size": row["size"]}
            if not meta_only and "ciphertext" in row:
                entry["ciphertext"] = base64.b64encode(row["ciphertext"]).decode("ascii")
            bundles.append(entry)
        body = {"topic": topic, "bundles": bundles}
        if "want" not in payload:
            # a want-mode match set is not a mailbox position — no cursor
            body["next_since"] = rows[-1]["seq"] if rows else payload.get("since", 0)
        return body

    # -- §8 node reachability hints (G1 fabric path) -----------------------------
    #
    # The org roster IS the tracker: nodes self-announce direct-dial
    # candidates (the ICE-style seed) and, when they hold relay:serve,
    # the dial URL of their peer relay. Node identity is the envelope
    # signer — a node can only announce ITSELF; there is no way to plant
    # an address under someone else's key. Reads are Tier B (signed
    # envelope, scope node:lookup): an org's interior addresses are never
    # served to anonymous callers (E1 continuity).

    async def _hint_gate(request: Request, org_uuid: str, scope: str) -> tuple:
        envelope = _parse_envelope(await _read_json(request))
        t = now()
        binding = _require_binding(store, org_uuid, t)
        auth = _authorize(
            envelope, "POST", str(request.url.path), binding, store, t,
            required_scope=scope,
        )
        return envelope["payload"], auth, t

    @app.post("/v1/orgs/{org_uuid}/reachability", status_code=201)
    async def announce_reachability(org_uuid: str, request: Request):
        """Announce/refresh the SIGNER's own reachability hints."""
        payload, auth, t = await _hint_gate(request, org_uuid, "node:announce")
        _require_fields(
            payload, allowed=frozenset({"addrs", "relay_url", "ttl"}),
            required=frozenset({"addrs"}), what="reachability payload",
        )
        addrs = payload["addrs"]
        if not isinstance(addrs, list) or len(addrs) > MAX_NODE_ADDRS:
            raise _bad_request(
                f"addrs must be a list of at most {MAX_NODE_ADDRS} candidate URLs"
            )
        for addr in addrs:
            _require_ws_url(addr, "addrs entry")
        relay_url = payload.get("relay_url")
        if relay_url is not None:
            _require_ws_url(relay_url, "relay_url")
        ttl = payload.get("ttl", DEFAULT_HINT_TTL)
        if type(ttl) is not int or ttl <= 0:
            raise _bad_request("ttl must be a positive integer of seconds")
        ttl = max(MIN_HINT_TTL, min(ttl, MAX_HINT_TTL))
        store.purge_expired_node_hints(now=t)
        store.upsert_node_hint(
            org_uuid, auth.signer_pub, addrs, relay_url, now=t, expires_at=t + ttl
        )
        return {"node": auth.signer_pub, "expires_at": t + ttl}

    @app.post("/v1/orgs/{org_uuid}/reachability/query")
    async def query_reachability(org_uuid: str, request: Request):
        """Live hints for the org's nodes (Tier B — never anonymous)."""
        payload, _auth, t = await _hint_gate(request, org_uuid, "node:lookup")
        _require_fields(
            payload, allowed=frozenset({"node"}), required=frozenset(),
            what="reachability query",
        )
        node = payload.get("node")
        if node is not None:
            _require_pub(node, "node")
        return {"org": org_uuid, "hints": store.node_hints(org_uuid, now=t, node_pub=node)}

    # -- §6 equivocation witness (F4): signed, append-only head-set log ---------
    #
    # The anti-fork role (L5). Members publish the authority head-set they
    # observe; the witness appends it to a per-(org, topic) hash-chained log
    # and serves each tip SIGNED by the registry witness key. A server that
    # shows two members different histories signs two contradictory
    # attestations — a self-contained proof anyone can check. Hashes only,
    # so it composes with the L6 encrypted mailbox untouched.

    @app.get("/v1/witness/pubkey")
    async def witness_pubkey():
        """The registry's witness verification key — clients pin this.

        Public by nature (it only *verifies* signatures) and unauthenticated
        so a fresh member can pin it before it holds any org credential. The
        pin is load-bearing: equivocation is provable only between two
        attestations that verify under the SAME trusted key, never the key a
        response advertises.
        """
        return {"witness_pub": witness_key.public_hex, "v": 1}

    @app.post("/v1/orgs/{org_uuid}/topics/{topic}/witness", status_code=201)
    async def publish_witness(org_uuid: str, topic: str, request: Request):
        """Append the observed head-set to the log; return the signed tip."""
        payload, auth, t = await _topic_gate(request, org_uuid, topic)
        _require_fields(
            payload, allowed=frozenset({"heads"}), required=frozenset({"heads"}),
            what="witness payload",
        )
        heads = _require_hashes(payload["heads"], "heads", MAX_WITNESS_HEADS)
        row = store.append_witness(org_uuid, topic, heads, auth.signer_pub, now=t)
        return sign_attestation(witness_key, row["entry"])

    @app.post("/v1/orgs/{org_uuid}/topics/{topic}/witness/head")
    async def witness_head(org_uuid: str, topic: str, request: Request):
        """Serve the current signed head-set — identical to every member."""
        _payload, _auth, _t = await _topic_gate(request, org_uuid, topic)
        tip = store.witness_tip(org_uuid, topic)
        if tip is None:
            return {"topic": topic, "attestation": None}
        return {"topic": topic, "attestation": sign_attestation(witness_key, tip["entry"])}

    @app.post("/v1/orgs/{org_uuid}/topics/{topic}/witness/since")
    async def witness_since(org_uuid: str, topic: str, request: Request):
        """Serve signed chain entries after a cursor (the client's walk).

        Lets a member verify a fresh tip actually *extends* the chain it
        last witnessed, entry by entry — a served chain whose entry at an
        already-witnessed seq differs from the one the member holds signed
        is provable equivocation.
        """
        payload, _auth, _t = await _topic_gate(request, org_uuid, topic)
        _require_fields(
            payload, allowed=frozenset({"since"}), required=frozenset(), what="witness since",
        )
        since = _require_seq(payload)
        rows = store.witness_since(org_uuid, topic, since, limit=MAX_TOPIC_PAGE)
        entries = [sign_attestation(witness_key, r["entry"]) for r in rows]
        next_since = rows[-1]["entry"]["seq"] if rows else since
        return {"topic": topic, "entries": entries, "next_since": next_since}

    # -- L1 listing directory (graph://29ff28a8-b39) ----------------------------
    #
    # The registry stores signed CARDS, never bundles: a listing is a
    # portable claim (listings.py) anyone can re-verify offline. Publishing
    # is Tier B (I4 envelope + the claim's own chain to the publisher's
    # bound root, scope listing:publish); browsing is Tier A (anonymous
    # public index — a directory that hid itself would be no directory).
    # Names are labels keyed (publisher, name): two orgs may claim the same
    # name and neither is privileged; display naming is the attestation
    # layer's problem, evaluated CLIENT-side.

    def _require_continuity(head: dict, binding: OrgBinding, action: str) -> None:
        """The hijack rule: touching an existing chain — extending it,
        restarting it, or delisting it — requires the CURRENT bound root
        to be a rebind-trail successor of the root that accepted the
        chain's head. A root that reclaimed the UUID after expiry
        verifies against the binding just fine, but has no rebind edge to
        the old continuity, so the chain stays out of its reach."""
        if head["root_pub"] not in store.root_continuity(binding.org_uuid, binding.root_pub):
            raise _forbidden(
                f"{action} does not chain to the publisher key-continuity"
                " that owns this listing"
            )

    @app.post("/v1/listings", status_code=201)
    async def publish_listing(request: Request):
        envelope = _parse_envelope(await _read_json(request))
        _require_fields(
            envelope["payload"], allowed=frozenset({"claim"}),
            required=frozenset({"claim"}), what="listing payload",
        )
        try:
            claim = parse_listing_claim(envelope["payload"]["claim"])
        except MalformedError as exc:
            raise _bad_request(str(exc))
        except IdkitError:
            raise _forbidden("listing claim signature does not verify against its signer")
        payload = claim["payload"]
        publisher, name = payload["publisher"], payload["name"]

        t = now()
        binding = _require_binding(store, publisher, t)
        _authorize(
            envelope, "POST", str(request.url.path), binding, store, t,
            required_scope="listing:publish",
        )
        # The claim-layer gate, distinct from the envelope gate on purpose:
        # the envelope authorizes the HTTP write and dies with the request,
        # while the claim is the durable artifact third parties re-verify,
        # so its signature must stand on its own chain.
        claim_auth = _verify_signer_chain(
            payload["signer"], claim.get("cert"), binding, store, t,
            required_scope="listing:publish",
        )

        lid = claim["listing_id"]
        if store.get_listing(lid) is not None:
            raise HTTPException(status_code=409, detail="listing is already published")
        head = store.listing_head(publisher, name)
        if head is not None:
            _require_continuity(head, binding, "publish")
        if payload["prev"] is None:
            # A fresh chain start is allowed only where nothing ACTIVE
            # stands — over empty ground or a chain this same continuity
            # revoked. (The continuity check above already bars a UUID
            # reclaimer from restarting over a dead continuity's revoked
            # name.)
            if head is not None and head["revoked_at"] is None:
                raise HTTPException(
                    status_code=409,
                    detail="an active listing exists for this publisher/name;"
                           " an update must reference its predecessor",
                )
        else:
            if head is None or head["listing_id"] != payload["prev"]:
                raise HTTPException(
                    status_code=409,
                    detail="prev does not reference the current head listing"
                           " for this publisher/name",
                )
        seq = store.insert_listing(
            publisher=publisher,
            name=name,
            listing_id=lid,
            prev_id=payload["prev"],
            version=payload["version"],
            bundle_hash=payload["bundle_hash"],
            claim=envelope["payload"]["claim"],
            root_pub=binding.root_pub,
            signer_pub=payload["signer"],
            subject_kind=claim_auth.subject_kind,
            subject_id=claim_auth.subject_id,
            now=t,
        )
        return {"publisher": publisher, "name": name, "listing_id": lid,
                "version": payload["version"], "seq": seq}

    @app.get("/v1/listings")
    async def list_listings(
        name: Optional[str] = None,
        publisher: Optional[str] = None,
        limit: int = Query(100, ge=1, le=MAX_LISTING_PAGE),
    ):
        """Directory index (Tier A, anonymous): unrevoked chain heads."""
        rows = store.list_active_listings(publisher=publisher, name=name, limit=limit)
        return {
            "listings": [
                {
                    "publisher": r["publisher"],
                    "name": r["name"],
                    "version": r["version"],
                    "listing_id": r["listing_id"],
                    "seq": r["seq"],
                    "bundle_hash": r["bundle_hash"],
                    "created_at": r["created_at"],
                    "claim": r["claim"],
                }
                for r in rows
            ]
        }

    @app.get("/v1/listings/{org_uuid}/{name}")
    async def get_listing(org_uuid: str, name: str):
        """One chain in full: head card + history (revoked rows included,
        with their status visible — an installer must be able to see that
        a version it holds was withdrawn)."""
        head = store.listing_head(org_uuid, name)
        if head is None:
            raise HTTPException(status_code=404, detail="unknown listing")
        history = store.listing_history(org_uuid, name)
        return {
            "publisher": org_uuid,
            "name": name,
            "head": head,
            "history": [
                {
                    "listing_id": r["listing_id"],
                    "seq": r["seq"],
                    "prev": r["prev_id"],
                    "version": r["version"],
                    "bundle_hash": r["bundle_hash"],
                    "created_at": r["created_at"],
                    "revoked_at": r["revoked_at"],
                }
                for r in history
            ],
        }

    @app.delete("/v1/listings/{org_uuid}/{name}")
    async def revoke_listing(org_uuid: str, name: str, request: Request):
        envelope = _parse_envelope(await _read_json(request))
        _require_fields(
            envelope["payload"], allowed=frozenset(), required=frozenset(),
            what="listing revoke payload",
        )
        t = now()
        # No name-grammar check needed: a name that could not have been
        # published cannot have a head, so it falls out here as a 404.
        head = store.listing_head(org_uuid, name)
        if head is None:
            raise HTTPException(status_code=404, detail="unknown listing")
        binding = _require_binding(store, org_uuid, t)
        _authorize(
            envelope, "DELETE", str(request.url.path), binding, store, t,
            required_scope="listing:revoke",
        )
        _require_continuity(head, binding, "revocation")
        revoked = store.revoke_listing(org_uuid, name, now=t)
        return {"publisher": org_uuid, "name": name, "revoked_at": t, "revoked": revoked}

    # -- L1 attestations ---------------------------------------------------------
    #
    # Same delivery model as revocations: the record is self-authorizing
    # (signed by the attestor key it names), so anyone may DELIVER one;
    # only the attestor key can MINT one. The registry verifies the
    # signature — a record is authentic to its key — and stores it. It
    # never evaluates the claim: which attestors to believe is the
    # client's display policy, not the venue's verdict.

    @app.post("/v1/attestations", status_code=201)
    async def add_attestation(request: Request):
        body = await _read_json(request)
        _require_fields(
            body, allowed=frozenset({"record"}), required=frozenset({"record"}),
            what="attestation payload",
        )
        try:
            record = parse_attestation_record(body["record"])
        except MalformedError as exc:
            raise _bad_request(str(exc))
        except IdkitError:
            raise _forbidden("attestation signature does not verify against its attestor")
        payload = record["payload"]
        t = now()
        if payload["ts"] > t + MAX_ATTESTATION_FUTURE_TS:
            raise _bad_request("attestation ts is in the future")
        expires_at = payload["ts"] + payload["ttl"]
        if expires_at <= t:
            raise _bad_request("attestation is already expired")
        store.purge_expired_attestations(now=t)
        aid = record["attestation_id"]
        store.add_attestation(aid, payload, body["record"], now=t)
        return {"attestation_id": aid, "subject": payload["subject"],
                "expires_at": expires_at}

    @app.get("/v1/attestations/{subject_pub}")
    async def attestations_for_subject(subject_pub: str):
        """Live attestations about a subject key (Tier A, anonymous)."""
        _require_pub(subject_pub, "subject_pub")
        rows = store.attestations_for_subject(
            subject_pub, now=now(), limit=MAX_LISTING_PAGE
        )
        return {"subject": subject_pub, "attestations": rows}

    # -- §5.1 relay tunnel ------------------------------------------------------

    @app.websocket("/t/{org_uuid}")
    async def relay_tunnel(websocket: WebSocket, org_uuid: str):
        await tunnel_endpoint(websocket, org_uuid, hub, store, now_fn,
                              base_url=base_url)

    @app.websocket("/v1/links/{token}/channel")
    async def relay_viewer(websocket: WebSocket, token: str):
        await viewer_endpoint(websocket, token, hub, store, now_fn)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    return app
