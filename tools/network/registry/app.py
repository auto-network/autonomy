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
import contextlib
import hashlib
import hmac
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
    canonical_json,
    generate_token,
    verify_chain,
    verify_revocation,
    verify_signature,
)
from tools.network.idkit.keys import PUBLIC_KEY_HEX_LEN, _decode_hex

from .abuse import RelayAbuseLimiter
from .assertion import IDENTIFY_SCOPE, MAX_ASSERTION_TTL, parse_assertion
from .listings import parse_attestation_record, parse_listing_claim
from .relay import (
    HostRoutes,
    TunnelHub,
    _resolve_live_link,
    host_probe_endpoint,
    push_reprove_and_enforce,
    tunnel_endpoint,
    viewer_endpoint,
)
from .signing import (
    ENVELOPE_VERSION,
    MAX_CLOCK_SKEW,
    recovery_succession_input,
    link_operation_receipt_input,
    request_signing_input,
    sign_link_operation_receipt,
)
from .store import (
    LinkGrant,
    LinkOperation,
    OrgBinding,
    RegistryStore,
    validate_membership_advance,
)
from .witness import MAX_WITNESS_HEADS, sign_attestation
from tools.network.ledger.membership_commitment import (
    MembershipCommitmentError,
    verify_inclusion,
)
from tools.network.relaykit.hello import (
    HelloError as RelayHelloError,
    validate_membership_proof,
)

# Owned by tools.network.clock (validity intervals); re-exported here.
from tools.network.clock import (
    DEFAULT_BINDING_TTL,
    MAX_BINDING_TTL,
    MIN_BINDING_TTL,
)

RECOVERY_POLICIES = frozenset({"none", "recovery-key"})

_BOOTLOADER_DIR = Path(__file__).resolve().parent / "bootloader"
_RELAYKIT_CORE_PATH = (
    Path(__file__).resolve().parents[2]
    / "dashboard" / "static" / "js" / "lib" / "relaykit-core.js"
)

# Agent-first install primer content (auto-2dt9b): canonical, repo-tracked
# markdown under deploy/install/, served at /install with content
# negotiation. Read per-request so a redeploy of content needs no restart.
_INSTALL_DIR = Path(__file__).resolve().parents[3] / "deploy" / "install"

_INSTALL_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'"
)

# The org:join bridge page (auto-y7nap + r7kk4): a fixed static shell,
# rendered client-side, that performs NO ceremony and exactly ONE network
# interaction — the root-pinned E2E join channel to the org's own node
# (same-origin websocket), over which the ORG self-describes (name,
# byline, icon as a bounded data URI). No auto-detection of local nodes
# (operator ruling): connect admits only this origin's channel endpoint.
# The ledger bearer lives in the URL fragment and is never sent anywhere,
# channel included. connect-src is 'self' ONLY: modern browsers admit a
# same-origin wss upgrade under 'self', and scheme-wide wss:/ws: sources
# would permit connections to ANY host (relay review) — with fail-silent
# enrichment, a browser that disagrees simply shows the minimal display.
# The public landing page (register row 105): fully static, self-contained
# — real product screenshots ride as data URIs, the only script is the
# copy button, and nothing on the page can reach the network at all.
_LANDING_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; "
    "script-src 'unsafe-inline'; img-src data:; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)

_JOIN_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; "
    "connect-src 'self'; "
    "img-src data:; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
)


def _peer_host(request: Request) -> str:
    """Return Uvicorn's trusted peer address, with no unmetered fallback."""
    return request.client.host if request.client is not None else "unknown"


def _install_html_wrapper(markdown: str, host: str | None = None) -> bytes:
    """The browser face of /install: no CDN, no external fetches — a copy
    CTA for handing the URL to a coding agent, above the primer verbatim.

    The CTA names the host that actually served this page, so the copied
    prompt works at every deployment stage — registry.auto.network today,
    bare auto.network once its DNS lands (auto-9q7a5). A dead canonical URL
    in a copy button is worse than an interim one that resolves.
    """
    import html as _html
    import re as _re

    served_host = host if host and _re.fullmatch(r"[A-Za-z0-9.\-]+(:\d+)?", host) \
        else "auto.network"
    cta = _html.escape(f"Please install Autonomy from https://{served_host}/install")
    body = _html.escape(markdown)
    page = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Install Autonomy</title>
<style>
 body{{margin:0;background:#12100e;color:#e8e2d8;
      font:16px/1.55 system-ui,-apple-system,sans-serif}}
 main{{max-width:860px;margin:0 auto;padding:2.5rem 1.25rem 5rem}}
 h1{{font-size:1.6rem;margin:0 0 .3rem}} .sub{{color:#a89d8c}}
 .cta{{background:#1c1916;border:1px solid #3a332a;border-radius:10px;
      padding:1rem 1.2rem;margin:1.4rem 0}}
 .cta code{{display:block;background:#0d0c0a;border:1px solid #3a332a;
      border-radius:6px;padding:.7rem .9rem;margin:.6rem 0;
      font-size:.95rem;user-select:all}}
 button{{background:#b5764a;color:#12100e;border:0;border-radius:6px;
      padding:.45rem .9rem;font-weight:600;cursor:pointer}}
 pre{{background:#0d0c0a;border:1px solid #3a332a;border-radius:8px;
      padding:1rem 1.1rem;overflow-x:auto;font-size:.84rem;
      line-height:1.5;white-space:pre-wrap}}
</style></head><body><main>
<h1>Install Autonomy</h1>
<p class="sub">Sovereign, self-hosted, yours. The document below is written
for your coding agent — hand it the URL and it takes care of the rest,
with your consent at every step.</p>
<div class="cta"><b>Tell your coding agent:</b>
<code id="prompt">{cta}</code>
<button onclick="navigator.clipboard.writeText(document.getElementById('prompt').textContent)">Copy</button>
</div>
<pre>{body}</pre>
</main></body></html>"""
    return page.encode("utf-8")

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
    # base-uri about: -- not 'none'. A composed artifact carries
    # <base href="about:srcdoc"> so that in-page #fragment links resolve
    # inside the frame instead of navigating it to the relay page. Under
    # 'none' the browser IGNORES that <base> silently -- no console error,
    # no visible difference from omitting it -- and every anchor navigates
    # away. `about:` is the narrowest allowance that permits it and admits
    # no network origin.
    "img-src blob: data: https: http:; frame-src blob:; base-uri about:; "
    "form-action 'none'; frame-ancestors 'none'"
)

_ENVELOPE_FIELDS = frozenset(
    {"v", "signer", "ts", "payload", "cert", "sig", "membership_proof"})
#: Optional envelope fields — absent means root-direct (cert) / no
#: committed-membership rider (membership_proof).
_ENVELOPE_OPTIONAL = frozenset({"cert", "membership_proof"})
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
#: A canonical org-scoped persona public key (64 lowercase hex).
_PERSONA_RE = _HASH_RE

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
#: Owned by tools.network.clock (a freshness gate).
from tools.network.clock import MAX_ATTESTATION_FUTURE_TS

# -- G1 node reachability hints (spec §8) -------------------------------------
#
# Short TTLs are the point: a hint is a live-address claim, not a record.
# Nodes refresh on a heartbeat; anything that stops refreshing goes dark.
MAX_NODE_ADDRS = 8
MAX_NODE_URL_LEN = 256
# Owned by tools.network.clock (validity intervals); re-exported here.
from tools.network.clock import (
    DEFAULT_HINT_TTL,
    MAX_HINT_TTL,
    MIN_HINT_TTL,
)


# -- E1 session linking (spec §4.7, §4.8, §6.8) ------------------------------
#
# A first-party auto.network session is a single opaque cookie: the
# server holds all identity state keyed by the cookie's random id, the
# browser holds nothing but the pointer (I1-adjacent: no identity material
# in the browser beyond the session pointer). The cookie is HttpOnly so
# page scripts cannot read it, SameSite=Lax because redemption is a
# top-level POST from the auto.network origin itself.
SESSION_COOKIE = "an_link_session"
# Owned by tools.network.clock (validity intervals); re-exported here.
from tools.network.clock import (
    ANON_SESSION_TTL,
    CHALLENGE_TTL,
    SESSION_TTL,
)
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
    missing = _ENVELOPE_FIELDS - set(body) - _ENVELOPE_OPTIONAL
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
    if "membership_proof" in body:
        try:
            body["membership_proof"] = validate_membership_proof(
                body["membership_proof"])
        except RelayHelloError as exc:
            raise _bad_request(str(exc))
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
        membership_proof=envelope.get("membership_proof"),
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
    membership_proof: Optional[dict] = None,
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

    if membership_proof is not None:
        # Committed-membership path (auto-3bhy3, graph://da0dd9fb-e75): the
        # chain anchors at the ACTING PERSONA the cert names — PIN 6b, the
        # same anchor the dashboard's local verifier uses — and the rider
        # proves that persona under the members_root this registry adopted
        # by checkpoint induction. Standing comes from the proof, never
        # from a chain to the constitutional root.
        anchor = cert.subject.id
        if not isinstance(anchor, str) or _PERSONA_RE.fullmatch(anchor) is None:
            raise _forbidden(
                "membership-proof envelopes require a persona-keyed cert subject")
        if cert.subject.kind not in ("operator", "persona"):
            raise _forbidden(
                f"cert subject kind {cert.subject.kind!r} cannot ride a "
                "membership proof")
        try:
            result = verify_chain(
                cert,
                anchor,
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
        # idkit narrowing constrains scope/org/validity but not subject
        # continuity: every hop must name the anchor persona, or an
        # intermediate issuer could swap the actor under the same proof.
        for hop in cert.chain():
            if hop.subject.id != anchor:
                raise _forbidden(
                    "membership-proof cert chain names more than one actor")
        state = store.get_membership_state(binding.org_uuid)
        if state is None:
            raise _forbidden(
                "no membership state for this org — publish the root-signed "
                "seed checkpoint first")
        if membership_proof["checkpoint_seq"] != state.seq:
            raise _forbidden(
                f"membership proof is stale: proven at seq "
                f"{membership_proof['checkpoint_seq']}, registry at seq "
                f"{state.seq}")
        try:
            verify_inclusion(state.members_root, anchor,
                             membership_proof["index"],
                             membership_proof["path"])
        except MembershipCommitmentError as exc:
            raise _forbidden(f"membership proof does not verify: {exc}")
        return AuthContext(result.leaf_pub, "persona", anchor, cert)

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
        raise _rung2(
            "persona subjects require a membership_proof rider on the "
            "envelope (committed membership, graph://da0dd9fb-e75)")
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


def _require_hex_digest(value: object, what: str, *, length: int = 64) -> str:
    if not isinstance(value, str) or len(value) != length or re.fullmatch(
        rf"[0-9a-f]{{{length}}}", value
    ) is None:
        raise _bad_request(f"{what} must be {length} lowercase hexadecimal characters")
    return value


def _json_digest(value: object, *, max_bytes: int = 65_536) -> str:
    try:
        encoded = canonical_json(value)
    except Exception as exc:
        raise _bad_request("canonical JSON input is malformed") from exc
    if len(encoded) > max_bytes:
        raise _bad_request(f"canonical JSON input exceeds {max_bytes} bytes")
    return hashlib.sha256(encoded).hexdigest()


def _origin_proof_bytes(value: object) -> bytes:
    if not isinstance(value, str) or len(value) != 43 or "=" in value:
        raise _bad_request("origin_proof must be 43-character base64url without padding")
    try:
        raw = base64.urlsafe_b64decode(value + "=")
    except Exception as exc:
        raise _bad_request("origin_proof is malformed") from exc
    if len(raw) != 32 or base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") != value:
        raise _bad_request("origin_proof must decode canonically to exactly 32 bytes")
    return raw


def _origin_proof_commitment(raw: bytes) -> str:
    return hashlib.sha256(
        b"autonomy.link.origin-proof-commitment.v1\n" + raw
    ).hexdigest()


def _require_seq(payload: dict, field: str = "since") -> int:
    value = payload.get(field, 0)
    if type(value) is not int or value < 0:
        raise _bad_request(f"{field} must be a non-negative integer sequence cursor")
    return value


def _parse_recovery_policy(payload: dict, root_pub: str) -> tuple:
    """Returns (policy, recovery_pub_or_None); enforces pairing rules AND that
    the recovery factor is a key DISTINCT from ``root_pub``.

    A recovery key the root controls is no second factor -- a stolen root would
    sign both the rotation/rebind and its "recovery" co-signature, defeating the
    whole 'a stolen root alone cannot rotate' property. This is the AUTHORITATIVE
    check: the registry is the control (a non-browser client curls the envelope
    directly), so the browser's mirror of this is only a UX nicety. ``root_pub``
    is the root the recovery factor must differ from, per endpoint: the payload
    root at registration, the NEW root at rebind, the bound root at policy update.
    """
    policy = payload.get("recovery_policy")
    if policy not in RECOVERY_POLICIES:
        raise _bad_request(f"recovery_policy must be one of {sorted(RECOVERY_POLICIES)}")
    recovery_pub = payload.get("recovery_pub")
    if policy == "recovery-key":
        if recovery_pub is None:
            raise _bad_request("recovery_policy recovery-key requires recovery_pub")
        recovery_pub = _require_pub(recovery_pub, "recovery_pub")
        if recovery_pub == root_pub:
            raise _bad_request(
                "recovery_pub must differ from root_pub -- a recovery factor the "
                "root controls is no second factor"
            )
        return policy, recovery_pub
    if recovery_pub is not None:
        raise _bad_request("recovery_pub is only valid with recovery_policy recovery-key")
    return policy, None


def _binding_policy(binding: OrgBinding) -> dict:
    """Closed authoritative recovery-policy wire object."""
    if binding.recovery_policy == "none":
        return {"mode": "none"}
    if binding.recovery_policy == "recovery-key" and binding.recovery_pub:
        return {"mode": "recovery-key", "recovery_pub": binding.recovery_pub}
    # Stored registry state outside the closed vocabulary is never projected
    # as partial authority.
    raise HTTPException(status_code=503, detail="registry binding policy is unavailable")


def _binding_response(binding: OrgBinding, *, outcome: Optional[str] = None) -> dict:
    result = {
        "org_uuid": binding.org_uuid,
        "root_pub": binding.root_pub,
        "binding_generation": binding.binding_generation,
        "expires_at": binding.expires_at,
        "recovery_policy": _binding_policy(binding),
    }
    if outcome is not None:
        result["outcome"] = outcome
    return result


def create_app(
    db_path: str = ":memory:",
    *,
    now_fn=None,
    now_ms_fn=None,
    base_url: str = "https://relay.auto.network",
    witness_key: Optional[KeyPair] = None,
    secure_cookies: bool = True,
    build_info: Optional[dict] = None,
    abuse_limiter: Optional[RelayAbuseLimiter] = None,
    turn_issuer=None,
    stream_ingress_port: Optional[int] = None,
    stream_ingress_host: str = "127.0.0.1",
    stream_idle_timeout: Optional[float] = None,
    metrics_port: Optional[int] = None,
    metrics_host: str = "127.0.0.1",
    abuse_exempt_sources: frozenset = frozenset(),
) -> FastAPI:
    """Build the registry app.

    *now_fn* is the clock (unix seconds); *now_ms_fn* is the independent
    trusted millisecond clock used for source deadlines. Both are injectable
    so expiry behavior is deterministic under test. *base_url* prefixes the
    share-link URLs minted by the tunnel create-link op. *witness_key* is the
    Ed25519 key the equivocation witness (F4) signs its head-set
    attestations with; a fresh one is generated when omitted, but a
    persistent deployment must pass a stable key — clients *pin* the
    witness public key, and rotating it silently would break split-view
    detection. Discover/pin it via ``GET /v1/witness/pubkey``. *secure_cookies*
    marks session cookies ``Secure`` (production default); tests over
    plain-http ``testserver`` set it False so the client keeps the cookie.
    *build_info* is deploy provenance exposed by ``GET /versionz`` for
    diagnostics only; it is never used for protocol or trust decisions.
    """
    app = FastAPI(title="auto.network registry", version="1")
    store = RegistryStore(db_path)
    now_fn = now_fn or (lambda: int(time.time()))
    now_ms_fn = now_ms_fn or (lambda: int(time.time() * 1000))
    hub = TunnelHub()
    from .metrics import RegistryMetrics
    metrics = RegistryMetrics()
    host_routes = HostRoutes(store, now_fn, metrics=metrics)
    metrics.bind_state(hub=hub, host_routes=host_routes, store=store)
    if abuse_limiter is None:
        abuse_limiter = RelayAbuseLimiter(
            exempt_sources=frozenset(abuse_exempt_sources)
        )
    witness_key = witness_key or KeyPair.generate()
    challenge_hub = ChallengeHub()
    build_info = dict(build_info or {
        "commit": "unknown", "dirty": None, "built_at": None,
    })
    app.state.store = store
    app.state.now_fn = now_fn
    app.state.now_ms_fn = now_ms_fn
    app.state.tunnel_hub = hub
    app.state.host_routes = host_routes
    app.state.metrics = metrics
    app.state.witness_key = witness_key
    app.state.challenge_hub = challenge_hub
    app.state.abuse_limiter = abuse_limiter
    app.state.turn_issuer = turn_issuer

    def now() -> int:
        return int(now_fn())

    def now_ms() -> int:
        value = now_ms_fn()
        if type(value) is not int or value < 0 or value > 9_007_199_254_740_991:
            raise HTTPException(status_code=503, detail="registry millisecond clock is unavailable")
        return value

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
        policy, recovery_pub = _parse_recovery_policy(payload, root_pub)
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
        # "claimed", "reclaimed_expired", and "already_bound_self" all succeed:
        # the last is a same-root re-registration (idempotent), whose liveness
        # claim_org already refreshed to t + ttl, so the response below reports
        # the correct expiry for every accepted outcome.
        accepted = store.get_org(org_uuid)
        if accepted is None:
            raise HTTPException(status_code=503, detail="accepted binding is unavailable")
        return _binding_response(accepted, outcome=outcome)

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
        renewed = store.get_org(org_uuid)
        if renewed is None:
            raise HTTPException(status_code=503, detail="renewed binding is unavailable")
        return _binding_response(renewed)

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
            new_policy, new_recovery_pub = _parse_recovery_policy(payload, new_root_pub)
        else:
            if "recovery_pub" in payload:
                raise _bad_request("recovery_pub requires recovery_policy")
            new_policy, new_recovery_pub = binding.recovery_policy, binding.recovery_pub
        # After the rebind the bound root is new_root_pub, so the recovery factor
        # must differ from IT -- this also covers the carried-over policy path,
        # where _parse_recovery_policy was not re-run (rebinding onto the
        # recovery key itself, keeping it as recovery, would otherwise
        # self-defeat on the new root).
        if new_policy == "recovery-key" and new_recovery_pub == new_root_pub:
            raise _bad_request(
                "recovery_pub must differ from the new root_pub after a rebind"
            )

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
        rebound = store.get_org(org_uuid)
        if rebound is None:
            raise HTTPException(status_code=503, detail="rebound binding is unavailable")
        result = _binding_response(rebound)
        result["previous_root_pub"] = binding.root_pub
        return result

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
            allowed=frozenset(
                {"recovery_policy", "recovery_pub", "policy_epoch",
                 "recovery_succession_sig"}
            ),
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

        new_policy, new_recovery_pub = _parse_recovery_policy(payload, binding.root_pub)

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

        # Succession/removal gate: a policy update is sovereign ONLY when it is
        # not changing or removing an EXISTING recovery key. A stolen root
        # alone must not be able to swap the recovery factor — otherwise it
        # sets itself (or an accomplice) as recovery, then that key signs a
        # rebind, hijacking the org's registry-bound root: the relay-visible
        # pin new joiners trust (existing members derive recovery from the
        # immutable ledger genesis and are unaffected). So:
        #   ADD (none -> recovery-key):        SOVEREIGN, root-signed alone.
        #   SUCCESSION (recovery-key -> other): requires the OLD recovery key's
        #                                       co-signature over this exact,
        #                                       epoch-bound transition.
        #   REMOVAL (recovery-key -> none):     same — removing recovery is a
        #                                       subset-authorized transition too.
        # The announced-windowed-vetoable path (the lattice's alternative to a
        # complete authorization) is unbuilt; until that substrate exists a
        # succession/removal without the co-signature is REFUSED, not deferred.
        changes_existing_recovery = binding.recovery_policy == "recovery-key" and (
            new_policy != "recovery-key" or new_recovery_pub != binding.recovery_pub
        )
        succession_sig = payload.get("recovery_succession_sig")
        if changes_existing_recovery:
            if not isinstance(succession_sig, str):
                raise _forbidden(
                    "changing or removing an existing recovery key requires the "
                    "old recovery key's co-signature (recovery_succession_sig); a "
                    "root signature alone cannot succeed or remove the recovery factor"
                )
            succession_input = recovery_succession_input(
                org_uuid, binding.recovery_pub, new_policy, new_recovery_pub, epoch,
            )
            try:
                verify_signature(binding.recovery_pub, succession_sig, succession_input)
            except MalformedError as exc:
                raise _bad_request(f"recovery_succession_sig: {exc}")
            except IdkitError:
                raise _forbidden(
                    "recovery_succession_sig does not verify against the current "
                    "recovery key over this transition"
                )
        elif succession_sig is not None:
            # ADD or no-op: no existing recovery key is being changed, so a
            # succession co-signature authorizes nothing. Reject it rather than
            # accept-and-ignore — no unverifiable bytes ride along (mirrors the
            # ledger refusing a recovery-continuity co-sig under policy "none").
            raise _bad_request(
                "recovery_succession_sig is only valid when changing or removing "
                "an existing recovery key"
            )

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

    # -- committed membership (graph://da0dd9fb-e75, auto-1wxet) ----------------

    @app.post("/v1/orgs/{org_uuid}/membership-checkpoints", status_code=201)
    async def submit_membership_checkpoint(org_uuid: str, request: Request):
        """Adopt one membership checkpoint by induction.

        The body IS the signed record — no request envelope, because the
        record is self-authenticating: a root-signed seed/reset verifies
        against the bound root, and a member-signed record verifies against
        the checkpointer commitment of the state this registry has already
        adopted (``validate_membership_advance``). Whoever delivers it
        changes nothing about whether it is true.
        """
        t = now()
        binding = _require_binding(store, org_uuid, t)
        record = await _read_json(request)
        if record.get("org") != org_uuid:
            raise _bad_request("checkpoint org must match the path org")
        stored = store.get_membership_state(org_uuid)
        try:
            validate_membership_advance(
                stored.checkpoint if stored is not None else None,
                record, binding.root_pub,
            )
        except MembershipCommitmentError as exc:
            raise _forbidden(str(exc))
        store.advance_membership_state(org_uuid, record, now=t)
        # Push a reprove-required message down each live tunnel of the org and
        # close, after the silence budget, any that has not re-proven under the
        # new set (relay.push_reprove_and_enforce). A removed member's tunnel
        # fails to re-prove and is dropped; honest members are re-stamped in
        # milliseconds without interruption. Runs in the background so the
        # checkpoint submitter is not held for the deadline.
        asyncio.create_task(
            push_reprove_and_enforce(hub, store, org_uuid, record["seq"]))
        return {
            "org_uuid": org_uuid,
            "seq": record["seq"],
            "members_root": record["members_root"],
            "checkpointers_root": record["checkpointers_root"],
        }

    @app.get("/v1/orgs/{org_uuid}/membership")
    async def get_membership_state(org_uuid: str):
        """The registry's current verified membership tuple — what a member
        node reads to build a proof rider (checkpoint_seq) and what the
        sign-on ceremony probes to learn whether a seed exists yet."""
        t = now()
        _require_binding(store, org_uuid, t)
        state = store.get_membership_state(org_uuid)
        if state is None:
            raise HTTPException(
                status_code=404,
                detail="no membership state — a root-signed seed checkpoint "
                       "has not been adopted for this org",
            )
        return {
            "org_uuid": state.org_uuid,
            "seq": state.seq,
            "members_root": state.members_root,
            "checkpointers_root": state.checkpointers_root,
            "ledger_head": state.ledger_head,
            "verified_at": state.verified_at,
        }

    # -- §4.4 links ------------------------------------------------------------

    @app.post("/v1/link-operation-receipts", status_code=201)
    async def accept_link_operation_receipt(request: Request, response: Response):
        """Consume fresh Link authority into one immutable witness receipt.

        The bounded public registry input is transport context outside the
        signed envelope.  Its digest is inside that envelope, so it cannot be
        substituted, but the raw object does not become part of the public
        receipt-acceptance envelope later retained by Central.
        """
        response.headers["Cache-Control"] = "no-store"
        receipt_request = await _read_json(request)
        _require_fields(
            receipt_request,
            allowed=frozenset({"envelope", "registry_input"}),
            required=frozenset({"envelope"}),
            what="Link operation receipt request",
        )
        if not isinstance(receipt_request["envelope"], dict):
            raise _bad_request("Link operation receipt envelope must be an object")
        envelope = _parse_envelope(receipt_request["envelope"])
        transport_registry_input = receipt_request.get("registry_input")
        payload = envelope["payload"]
        _require_fields(
            payload,
            allowed=frozenset(
                {
                    "org_uuid",
                    "operation",
                    "operation_id",
                    "target_type",
                    "binding_root_pub",
                    "binding_generation",
                    "registry_input_digest",
                    "local_intent_digest",
                    "operand_digest",
                    "origin_proof_commitment",
                    "source_expires_at_ms",
                }
            ),
            required=frozenset(
                {
                    "org_uuid",
                    "operation",
                    "operation_id",
                    "binding_root_pub",
                    "binding_generation",
                    "registry_input_digest",
                    "local_intent_digest",
                    "origin_proof_commitment",
                }
            ),
            what="Link operation receipt payload",
        )
        org_uuid = _require_uuid(payload["org_uuid"], "org_uuid")
        operation = payload.get("operation")
        if operation not in ("publish", "revoke"):
            raise _bad_request("operation must be publish or revoke")
        target_type = payload.get("target_type")
        if operation == "publish":
            if not isinstance(target_type, str) or not target_type:
                raise _bad_request("publish receipt requires target_type")
        elif target_type is not None:
            raise _bad_request("target_type is only valid for publish")
        operation_id = _require_hex_digest(payload.get("operation_id"), "operation_id")
        root_pub = _require_pub(payload.get("binding_root_pub"), "binding_root_pub")
        generation = _require_hex_digest(
            payload.get("binding_generation"), "binding_generation"
        )
        registry_digest = _require_hex_digest(
            payload.get("registry_input_digest"), "registry_input_digest"
        )
        local_digest = _require_hex_digest(
            payload.get("local_intent_digest"), "local_intent_digest"
        )
        commitment = _require_hex_digest(
            payload.get("origin_proof_commitment"), "origin_proof_commitment"
        )
        operand_digest = payload.get("operand_digest")
        if operation == "revoke":
            operand_digest = _require_hex_digest(operand_digest, "operand_digest")
        elif operand_digest is not None:
            raise _bad_request("operand_digest is only valid for revoke")
        source_expires_at_ms = payload.get("source_expires_at_ms")
        if source_expires_at_ms is not None and (
            type(source_expires_at_ms) is not int
            or source_expires_at_ms < 0
            or source_expires_at_ms > 9_007_199_254_740_991
        ):
            raise _bad_request("source_expires_at_ms must be a non-negative safe integer")
        if source_expires_at_ms is not None and (
            operation != "publish" or target_type != "org:join"
        ):
            raise _bad_request("source_expires_at_ms is only valid for org:join publish")
        if operation == "publish" and target_type == "org:join" and source_expires_at_ms is None:
            raise _bad_request("org:join receipt requires source_expires_at_ms")

        registry_input = transport_registry_input
        if operation == "publish":
            if not isinstance(registry_input, dict):
                raise _bad_request("publish receipt requires registry_input")
            _require_fields(
                registry_input,
                allowed=frozenset(
                    {
                        "operation_id",
                        "target_uuid",
                        "target_type",
                        "invite_ref",
                        "source_expires_at_ms",
                        "meta",
                    }
                ),
                required=frozenset(
                    {"operation_id", "target_uuid", "target_type"}
                ),
                what="publish registry input",
            )
            if registry_input.get("operation_id") != operation_id:
                raise _bad_request("registry_input.operation_id does not match")
            if registry_input.get("target_type") != target_type:
                raise _bad_request("registry_input.target_type does not match")
            if not hmac.compare_digest(
                _json_digest(registry_input), registry_digest
            ):
                raise _bad_request("registry_input does not match its digest")
            target_uuid = _require_uuid(
                registry_input.get("target_uuid"), "registry_input.target_uuid"
            )
            meta = registry_input.get("meta", {})
            if not isinstance(meta, dict):
                raise _bad_request("registry_input.meta must be an object")
            _require_fields(
                meta,
                allowed=frozenset({"ttl", "label"}),
                required=frozenset(),
                what="registry_input.meta",
            )
            ttl = meta.get("ttl")
            if ttl is not None and (
                type(ttl) is not int
                or ttl <= 0
                or ttl > 365 * 24 * 60 * 60
            ):
                raise _bad_request(
                    "registry_input.meta.ttl must be between 1 second and 365 days"
                )
            label = meta.get("label")
            if label is not None:
                try:
                    label_bytes = label.encode("utf-8") if isinstance(label, str) else b""
                except UnicodeError as exc:
                    raise _bad_request(
                        "registry_input.meta.label must be valid UTF-8 text"
                    ) from exc
                if not isinstance(label, str) or len(label_bytes) > 256:
                    raise _bad_request(
                        "registry_input.meta.label must be at most 256 UTF-8 bytes"
                    )
            invite_ref = registry_input.get("invite_ref")
            registry_deadline = registry_input.get("source_expires_at_ms")
            if target_type == "org:join":
                if (
                    target_uuid != org_uuid
                    or not isinstance(invite_ref, str)
                    or _HASH_RE.fullmatch(invite_ref) is None
                    or registry_deadline != source_expires_at_ms
                ):
                    raise _bad_request(
                        "org:join registry input must match UUID, invite, and deadline"
                    )
                if ttl is not None:
                    raise _bad_request(
                        "org:join registry input must not carry meta.ttl"
                    )
            elif invite_ref is not None or registry_deadline is not None:
                raise _bad_request(
                    "invite_ref/source deadline are only valid for org:join"
                )
        elif registry_input is not None:
            raise _bad_request("registry_input is only valid for publish receipt")

        t = now()
        binding = _require_binding(store, org_uuid, t)
        if binding.root_pub != root_pub or binding.binding_generation != generation:
            raise HTTPException(status_code=409, detail="binding root or generation changed")
        auth = _authorize(
            envelope,
            "POST",
            str(request.url.path),
            binding,
            store,
            t,
            required_scope=f"link:{operation}",
            required_target_type=target_type,
        )
        request_digest = _json_digest(payload)
        receipt = {
            "v": 1,
            "org_uuid": org_uuid,
            "binding_root_pub": root_pub,
            "binding_generation": generation,
            "operation_id": operation_id,
            "operation": operation,
            "receipt_request_digest": request_digest,
            "registry_input_digest": registry_digest,
            "local_intent_digest": local_digest,
            "operand_digest": operand_digest,
            "origin_proof_commitment": commitment,
            "source_expires_at_ms": source_expires_at_ms,
            "accepting_signer_pub": auth.signer_pub,
            "accepting_subject_kind": auth.subject_kind,
            "accepting_subject_id": auth.subject_id,
            "accepted_at": t,
        }
        receipt_json = canonical_json(receipt).decode("utf-8")
        signature = sign_link_operation_receipt(witness_key, receipt)
        candidate = LinkOperation(
            org_uuid=org_uuid,
            operation_id=operation_id,
            operation=operation,
            binding_root_pub=root_pub,
            binding_generation=generation,
            receipt_request_digest=request_digest,
            acceptance_envelope_digest=_json_digest(envelope),
            registry_input_digest=registry_digest,
            local_intent_digest=local_digest,
            operand_digest=operand_digest,
            origin_proof_commitment=commitment,
            source_expires_at_ms=source_expires_at_ms,
            accepted_at=t,
            signer_pub=auth.signer_pub,
            subject_kind=auth.subject_kind,
            subject_id=auth.subject_id,
            receipt_json=receipt_json,
            receipt_signature=signature,
        )
        status, accepted = store.claim_link_operation(
            candidate,
            now=t,
            trusted_now_ms=now_ms,
        )
        if status == "source_expired":
            raise HTTPException(status_code=410, detail="Link operation source has expired")
        if status == "conflict":
            raise HTTPException(status_code=409, detail="operation_id names different Link input")
        if status == "binding_mismatch" or accepted is None:
            raise HTTPException(status_code=409, detail="binding changed before receipt acceptance")
        return {
            "receipt": json.loads(accepted.receipt_json),
            "signature": accepted.receipt_signature,
        }

    @app.post("/v1/link-operations/{operation_id}/execute")
    async def execute_link_operation(
        operation_id: str, request: Request, response: Response
    ):
        response.headers["Cache-Control"] = "no-store"
        operation_id = _require_hex_digest(operation_id, "operation_id")
        body = await _read_json(request)
        _require_fields(
            body,
            allowed=frozenset(
                {"receipt", "signature", "origin_proof", "registry_input", "operand"}
            ),
            required=frozenset(
                {"receipt", "signature", "origin_proof", "registry_input"}
            ),
            what="Link operation execution",
        )
        receipt = body.get("receipt")
        signature = body.get("signature")
        registry_input = body.get("registry_input")
        if not isinstance(receipt, dict) or not isinstance(signature, str):
            raise _bad_request("receipt and signature are required")
        if not isinstance(registry_input, dict):
            raise _bad_request("registry_input must be an object")
        if receipt.get("operation_id") != operation_id:
            raise _bad_request("receipt operation_id does not match the route")
        try:
            receipt_json = canonical_json(receipt).decode("utf-8")
            verify_signature(
                witness_key.public_hex,
                signature,
                link_operation_receipt_input(receipt),
            )
        except (
            IdkitError,
            MalformedError,
            TypeError,
            ValueError,
            UnicodeError,
            OverflowError,
            RecursionError,
        ):
            raise _forbidden("receipt witness signature is invalid")
        org_uuid = _require_uuid(receipt.get("org_uuid"), "receipt.org_uuid")
        stored = store.get_link_operation(org_uuid, operation_id)
        if stored is None:
            raise HTTPException(status_code=404, detail="unknown Link operation")
        if not hmac.compare_digest(
            receipt_json, stored.receipt_json
        ) or not hmac.compare_digest(signature, stored.receipt_signature):
            raise _forbidden("receipt does not match the accepted operation")
        proof = _origin_proof_bytes(body.get("origin_proof"))
        if not hmac.compare_digest(
            _origin_proof_commitment(proof), stored.origin_proof_commitment
        ):
            raise _forbidden("origin proof does not match the accepted commitment")
        if not hmac.compare_digest(_json_digest(registry_input), stored.registry_input_digest):
            raise HTTPException(status_code=409, detail="registry input differs from receipt")
        if registry_input.get("operation_id") != operation_id:
            raise _bad_request("registry_input.operation_id does not match the route")

        t = now()
        if stored.operation == "publish":
            if "operand" in body:
                raise _bad_request("publish execution has no sensitive operand")
            _require_fields(
                registry_input,
                allowed=frozenset(
                    {"operation_id", "target_uuid", "target_type", "invite_ref", "source_expires_at_ms", "meta"}
                ),
                required=frozenset({"operation_id", "target_uuid", "target_type"}),
                what="publish registry input",
            )
            target_uuid = _require_uuid(registry_input["target_uuid"], "target_uuid")
            target_type = registry_input.get("target_type")
            if not isinstance(target_type, str) or not target_type:
                raise _bad_request("target_type must be non-empty text")
            meta = registry_input.get("meta", {})
            if not isinstance(meta, dict):
                raise _bad_request("meta must be an object")
            _require_fields(meta, frozenset({"ttl", "label"}), frozenset(), "link meta")
            ttl = meta.get("ttl")
            if ttl is not None and (
                type(ttl) is not int
                or ttl <= 0
                or ttl > 365 * 24 * 60 * 60
            ):
                raise _bad_request("meta.ttl must be between 1 second and 365 days")
            label = meta.get("label")
            if label is not None and (
                not isinstance(label, str)
                or len(label.encode("utf-8")) > 256
            ):
                raise _bad_request("meta.label must be at most 256 UTF-8 bytes")
            source_deadline = registry_input.get("source_expires_at_ms")
            if source_deadline != stored.source_expires_at_ms:
                raise HTTPException(status_code=409, detail="source deadline differs from receipt")
            invite_ref = registry_input.get("invite_ref")
            if target_type == "org:join":
                if target_uuid != org_uuid or not isinstance(invite_ref, str) or not _HASH_RE.fullmatch(invite_ref):
                    raise _bad_request("org:join input requires matching org UUID and invite_ref")
                if source_deadline is None:
                    raise _bad_request("org:join input requires its fixed source deadline")
                if ttl is not None:
                    raise _bad_request("org:join input must not carry meta.ttl")
            elif invite_ref is not None or source_deadline is not None:
                raise _bad_request("invite_ref/source deadline are only valid for org:join")
            token = generate_token()
            grant = LinkGrant(
                token=token,
                org_uuid=org_uuid,
                target_uuid=target_uuid,
                target_type=target_type,
                invite_ref=invite_ref,
                meta=meta,
                created_at=t,
                expires_at=t + ttl if ttl is not None else None,
                expires_at_ms=source_deadline,
                revoked_at=None,
                signer_pub=stored.signer_pub,
                subject_kind=stored.subject_kind,
                subject_id=stored.subject_id,
                operation_id=operation_id,
            )
            status, completed = store.execute_publish_operation(
                org_uuid, operation_id, grant, now=t
            )
            if status in ("binding_mismatch", "conflict", "inconsistent") or completed is None:
                raise HTTPException(status_code=409, detail="Link operation cannot execute")
            return {
                "state": "succeeded",
                "token": completed.result_token,
                "url": f"{base_url}/l/{completed.result_token}",
                "completed_at": completed.completed_at,
            }

        operand = body.get("operand")
        token = _require_hex_digest(operand, "operand", length=32)
        expected_operand = _json_digest(["autonomy.link.operand", 1, "revoke", token])
        if stored.operand_digest is None or not hmac.compare_digest(
            expected_operand, stored.operand_digest
        ):
            raise HTTPException(status_code=409, detail="revoke operand differs from receipt")
        status, completed = store.execute_revoke_operation(
            org_uuid, operation_id, token, now=t
        )
        if status in ("binding_mismatch", "conflict") or completed is None:
            raise HTTPException(status_code=409, detail="Link operation cannot execute")
        return {
            "state": completed.state,
            "completed_at": completed.completed_at,
            "revoked_at": completed.completed_at if completed.state == "succeeded" else None,
        }

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
        # Revocation is effective on the standing connection, not merely on
        # its next reconnect. The hub keeps only connection-memory signer
        # attribution; no persona/address history is created.
        await hub.close_revoked(
            org_uuid, record.revoked_key_id, host_routes=host_routes
        )
        return {"revoked_key_id": record.revoked_key_id, "expires_at": record.expires_at}

    # -- §4.6 grant envelope (bootloader) -------------------------------------

    @app.get("/v1/links/{token}/envelope")
    async def link_envelope(token: str, request: Request):
        # Anti-enumeration (§5.3): unknown, expired, revoked, and
        # dead-binding tokens are all the SAME 404 — a prober learns
        # nothing about which failure they hit.
        admission = abuse_limiter.begin(_peer_host(request))
        if admission is None:
            raise HTTPException(status_code=404, detail="unknown link")
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
        if abuse_limiter.resolve(admission, token, link.org_uuid) is None:
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
    relaykit_core_bytes = _RELAYKIT_CORE_PATH.read_bytes()
    join_shell_bytes = (_BOOTLOADER_DIR / "join.html").read_bytes()
    join_js_bytes = (_BOOTLOADER_DIR / "join.js").read_bytes()

    @app.get("/l/{token}")
    async def bootloader_page(token: str, request: Request):
        # ONE static byte sequence for every token — live, expired,
        # revoked, or invented. The token is read client-side from the
        # URL; nothing org- or target-identifying is in these bytes
        # (§5.3: the URL is a pure network pointer). Only the STATUS
        # differs, and it mirrors the envelope endpoint's liveness rule
        # exactly, so it opens no oracle the envelope doesn't already.
        admission = abuse_limiter.begin(_peer_host(request))
        link = (
            _resolve_live_link(store, token, now())
            if admission is not None
            else None
        )
        live = (
            link is not None
            and abuse_limiter.resolve(admission, token, link.org_uuid) is not None
        )
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

    landing_bytes = (_BOOTLOADER_DIR / "landing.html").read_bytes()

    @app.get("/")
    async def landing_page():
        # The public front door (register row 105): what Autonomy is, the
        # agent-install CTA, and the REAL product shown through real
        # captures — one static byte sequence, no state, no lookups.
        return Response(
            content=landing_bytes,
            media_type="text/html",
            headers={
                "Content-Security-Policy": _LANDING_CSP,
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
            },
        )

    @app.get("/network/join")
    async def join_bridge_page():
        # ONE static byte sequence regardless of query — the server never
        # reads the invitation context (org, channel token) and the ledger
        # bearer never reaches it at all (fragment-only). Rendering,
        # validation, and the local-node handoff are entirely client-side
        # in join.js; this page performs no ceremony (auto-y7nap).
        return Response(
            content=join_shell_bytes,
            media_type="text/html",
            headers={
                "Content-Security-Policy": _JOIN_CSP,
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
            },
        )

    @app.get("/l-assets/join.js")
    async def join_bridge_js():
        return Response(
            content=join_js_bytes,
            media_type="text/javascript",
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
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

    @app.get("/l-assets/relaykit-core.js")
    async def relaykit_core_js():
        # The Dashboard static path and this Relay path serve one source file,
        # not generated/copy-maintained siblings. A browser gets byte-identical
        # channel crypto and operation framing from either origin.
        return Response(
            content=relaykit_core_bytes,
            media_type="text/javascript",
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    # -- agent-first install primer (auto-2dt9b) ------------------------------
    #
    # Content contract (packaging pillar): GET /install serves the
    # repo-tracked primer — text/markdown to agents and curl, a
    # self-contained HTML wrapper to browsers; GET /install/<doc> serves
    # the fetchable sub-documents. The canonical bytes live in
    # deploy/install/ in the checkout; this route is convenience
    # distribution, never a control point. Deploying/operating the service
    # is the relay-network pillar's half of the ruled boundary.

    def _install_doc(rel: str) -> Path | None:
        """Resolve a repo-tracked install document; fail closed on escape."""
        root = _INSTALL_DIR.resolve()
        try:
            candidate = (root / rel).resolve()
            candidate.relative_to(root)
        except (ValueError, OSError):
            return None
        if candidate.suffix != ".md" or not candidate.is_file():
            return None
        return candidate

    _install_headers = {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }

    @app.get("/install")
    async def install_primer(request: Request):
        doc = _install_doc("INSTALL.md")
        if doc is None:
            raise HTTPException(404, "install primer is not present in this deployment")
        markdown = doc.read_text(encoding="utf-8")
        if "text/html" in request.headers.get("accept", ""):
            return Response(
                content=_install_html_wrapper(
                    markdown, host=request.headers.get("host"),
                ),
                media_type="text/html",
                headers={
                    **_install_headers,
                    "Content-Security-Policy": _INSTALL_CSP,
                },
            )
        return Response(
            content=markdown,
            media_type="text/markdown; charset=utf-8",
            headers=_install_headers,
        )

    @app.get("/install/{doc_path:path}")
    async def install_document(doc_path: str):
        doc = _install_doc(doc_path)
        if doc is None:
            raise HTTPException(404, "no such install document")
        return Response(
            content=doc.read_text(encoding="utf-8"),
            media_type="text/markdown; charset=utf-8",
            headers=_install_headers,
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
                              base_url=base_url, turn_issuer=turn_issuer,
                              witness_key=witness_key,
                              host_routes=host_routes)

    @app.websocket("/v1/links/{token}/channel")
    async def relay_viewer(websocket: WebSocket, token: str):
        await viewer_endpoint(
            websocket, token, hub, store, now_fn, abuse_limiter=abuse_limiter
        )

    @app.websocket("/v1/hosts/{host}/probe")
    async def relay_host_probe(websocket: WebSocket, host: str):
        await host_probe_endpoint(
            websocket, host, host_routes, now_fn,
            abuse_limiter=abuse_limiter,
        )

    if stream_ingress_port is not None:
        # Raw-stream ingress (auto-9z1xh): started on the app's own event
        # loop. In production it binds the serve floating IP's :443 directly
        # and is itself the public serve edge — the socket peer is the native
        # client, no forward and no PROXY header.
        from tools.network.relaykit.stream_wire import (
            STREAM_IDLE_TIMEOUT, STREAM_INGRESS_REBIND_SECONDS,
        )
        from .relay import _ops
        from .stream_ingress import start_stream_ingress

        idle = (
            stream_idle_timeout
            if stream_idle_timeout is not None
            else STREAM_IDLE_TIMEOUT
        )

        async def _bind_ingress():
            return await start_stream_ingress(
                stream_ingress_host, stream_ingress_port,
                host_routes=host_routes, abuse_limiter=abuse_limiter,
                idle_timeout=idle, metrics=metrics,
            )

        async def _retry_bind_ingress():
            """Keep trying until the serve address exists, then serve."""
            while True:
                await asyncio.sleep(STREAM_INGRESS_REBIND_SECONDS)
                try:
                    app.state.stream_ingress = await _bind_ingress()
                except OSError:
                    continue
                return

        @app.on_event("startup")
        async def _start_stream_ingress():
            # The serve edge is a FEATURE of this process, not a precondition
            # for it: the registry API and the relay have NO functional
            # dependency on the serve address. So a bind failure — floating IP
            # detached, netplan recycled without it, a boot race — degrades
            # instead of killing the process. Previously this raised out of
            # startup, systemd restarted, and an absent serve IP took the whole
            # registry and relay down with the serve edge (outage 2026-09-03).
            app.state.stream_ingress = None
            app.state.stream_ingress_rebind = None
            try:
                app.state.stream_ingress = await _bind_ingress()
            except OSError as exc:
                _ops("stream.ingress.bind-failed",
                     host=stream_ingress_host, port=stream_ingress_port,
                     error=type(exc).__name__,
                     detail="serving degraded; registry API and relay "
                            "unaffected; retrying")
                app.state.stream_ingress_rebind = asyncio.create_task(
                    _retry_bind_ingress()
                )

        @app.on_event("shutdown")
        async def _stop_stream_ingress():
            task = getattr(app.state, "stream_ingress_rebind", None)
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            server = getattr(app.state, "stream_ingress", None)
            if server is not None:
                server.close()
                await server.wait_closed()

    if metrics_port is not None:
        # Private metrics exposition (auto-albp6.9) on its OWN loopback
        # listener — never the public app. Bounded cardinality: org + reason
        # enums only.
        from .metrics import start_metrics_listener

        from .metrics import render_readout

        @app.on_event("startup")
        async def _start_metrics_listener():
            app.state.metrics_server = await start_metrics_listener(
                metrics_host, metrics_port, metrics,
                readout=lambda: render_readout(hub, build_info),
            )

        @app.on_event("shutdown")
        async def _stop_metrics_listener():
            server = getattr(app.state, "metrics_server", None)
            if server is not None:
                server.close()
                await server.wait_closed()

    @app.get("/v1/dns/zone-state")
    async def dns_zone_state():
        # Read path for the co-located DNS process (auto-g1jxw). Contents
        # are public by nature — every value here is published in DNS.
        # A token-bound zone is delegated to <org>.ns1/ns2.auto.network at its
        # parent; the child must answer the same NS set (and SOA MNAME).
        from .relay import zone_token_names
        zone_ns = {}
        for row in store.list_serve_zones():
            if row.get("state") == "active" and row.get("binding_kind") == "ns-token":
                zone_ns[row["zone"]] = [
                    name + "." for name in zone_token_names(row["org_uuid"])
                ]
        return {
            "challenges": store.live_serve_challenges(now=now()),
            "zones": sorted(store.active_serve_zones()),
            "zone_ns": zone_ns,
        }

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/versionz")
    async def versionz(response: Response):
        response.headers["Cache-Control"] = "no-store"
        return {"service": "auto.network-registry", **build_info}

    return app
