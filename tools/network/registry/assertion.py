"""Bootstrap assertion — the rung-1 → rung-2 identity bridge (spec §7A).

A **bootstrap assertion** is a short-lived, single-use, signed statement
minted by an operator's own dashboard that says, in effect, "the browser
holding this may act as identity <subject> for org <org>." A person's
browser redeems it once at ``POST /v1/link`` (§4.7) and, if it chains to
a registered org root, walks away with a first-party auto.network
session — with no login page, no password, and no identity arbitration on
the auto.network side (it only verifies signatures).

Shape (JSON, ``build_assertion`` is the client-side minter)::

    {
      "v": 1,
      "signer": "<leaf public key, 64 hex>",   # the key that signs this
      "org": "<org uuid>",
      "nonce": "<128-bit hex>",                 # single-use anti-replay id
      "dashboard_origin": "https://dash.example",  # §6.8 announced origin
      "scope": "viewer:identify",               # identify-only (§7A)
      "not_before": <unix seconds>,
      "not_after": <unix seconds>,              # TTL is SECONDS (§7A)
      "challenge": "<hex>",                     # optional: QR cross-device bind
      "cert": "<canonical DelegationCert wire JSON>",  # signer → root
      "sig": "<128 hex>"
    }

The signature covers the domain-separated canonical bytes of the semantic
fields (everything but ``cert`` and ``sig``) — exactly the request-envelope
pattern (``signing.py``). ``cert`` is NOT in the signing input: it is
self-verifying (its own chain of signatures binds ``signer`` to the org
root), and any cert that genuinely delegates to ``signer`` already
authorizes ``signer``, so there is nothing a cert-swap could forge.

Why a distinct object rather than reusing the request envelope:

- it is **identify-only** — the redemption endpoint pins ``scope`` to
  ``viewer:identify`` and refuses anything else (§7A threat bound: a
  spoofed picker or a tricked click can mint nothing but an identify cert
  with a seconds-long TTL);
- it carries **anti-replay state of its own** (``nonce``) and a
  **seconds-scale TTL** independent of the (long-lived) session cert that
  signs it;
- it announces the **dashboard origin** (§6.8) so auto.network can cache
  "your dashboard is at X" per browser without ever discovering it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from tools.network.idkit import MalformedError, canonical_json
from tools.network.idkit.keys import PUBLIC_KEY_HEX_LEN, SIGNATURE_HEX_LEN, _decode_hex

ASSERTION_DOMAIN = b"autonomy.network.viewer.identify.v1\n"
ASSERTION_VERSION = 1

#: The only scope a bootstrap assertion may carry (§7A: identify-only).
IDENTIFY_SCOPE = "viewer:identify"

#: Hard ceiling on the assertion validity window, seconds. Owned by
#: tools.network.clock (a validity-interval gate); re-exported here for the
#: assertion call sites that always read it from this module.
from tools.network.clock import MAX_ASSERTION_TTL

#: A nonce/challenge is a 128-bit CSPRNG value, hex-encoded (32 chars).
_NONCE_HEX_LEN = 32

_ASSERTION_FIELDS = frozenset(
    {"v", "signer", "org", "nonce", "dashboard_origin", "scope",
     "not_before", "not_after", "challenge", "cert", "sig"}
)
# Fields the signature covers — order-independent (canonical_json sorts),
# but listed here to document the signed surface.
_SIGNED_FIELDS = frozenset(
    {"v", "signer", "org", "nonce", "dashboard_origin", "scope",
     "not_before", "not_after", "challenge"}
)

_MAX_ORIGIN_LEN = 256
_MAX_TS = 2**63 - 1


def _require_nonce(value: object, what: str) -> str:
    if not isinstance(value, str):
        raise MalformedError(f"{what} must be a hex string")
    try:
        _decode_hex(value, _NONCE_HEX_LEN, what)
    except MalformedError:
        raise MalformedError(f"{what} must be {_NONCE_HEX_LEN} lowercase hex chars (128-bit)")
    return value


def _require_ts(value: object, what: str) -> int:
    if type(value) is not int or value < 0 or value > _MAX_TS:
        raise MalformedError(f"{what} must be an integer unix timestamp in [0, 2**63)")
    return value


def _require_origin(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_ORIGIN_LEN:
        raise MalformedError(
            f"dashboard_origin must be a non-empty string of at most {_MAX_ORIGIN_LEN} chars"
        )
    # An origin is scheme + authority with NO path/query/fragment (§6.8: it
    # is an identifier, never a secret and never a capability URL).
    if not (value.startswith("https://") or value.startswith("http://")):
        raise MalformedError("dashboard_origin must be an http(s) origin")
    rest = value.split("://", 1)[1]
    if not rest or "/" in rest or "?" in rest or "#" in rest:
        raise MalformedError("dashboard_origin must be a bare origin (scheme://host[:port])")
    return value


@dataclass(frozen=True)
class Assertion:
    """A parsed bootstrap assertion. ``cert`` is still the wire string —
    the redemption endpoint parses and chain-verifies it against the bound
    org root (personas are first-class here, unlike the mutation gate)."""

    v: int
    signer: str
    org: str
    nonce: str
    dashboard_origin: str
    scope: str
    not_before: int
    not_after: int
    cert: str
    sig: str
    challenge: Optional[str] = None

    def signed_dict(self) -> dict:
        """The exact semantic fields the signature covers."""
        fields = {
            "v": self.v,
            "signer": self.signer,
            "org": self.org,
            "nonce": self.nonce,
            "dashboard_origin": self.dashboard_origin,
            "scope": self.scope,
            "not_before": self.not_before,
            "not_after": self.not_after,
        }
        if self.challenge is not None:
            fields["challenge"] = self.challenge
        return fields

    def signing_input(self) -> bytes:
        return ASSERTION_DOMAIN + canonical_json(self.signed_dict())


def parse_assertion(body: object) -> Assertion:
    """Strictly parse an assertion body into an :class:`Assertion`.

    Rejects unknown/missing fields and wrong types. Does NOT check the
    signature, the chain, freshness, or single-use — those are the
    redemption endpoint's job, where the org binding (root key,
    revocations, clock) is in scope.
    """
    if not isinstance(body, dict):
        raise MalformedError("assertion must be a JSON object")
    unknown = set(body) - _ASSERTION_FIELDS
    if unknown:
        raise MalformedError(f"assertion carries unknown fields: {sorted(unknown)}")
    missing = _ASSERTION_FIELDS - set(body) - {"challenge"}
    if missing:
        raise MalformedError(f"assertion is missing fields: {sorted(missing)}")

    if body["v"] != ASSERTION_VERSION:
        raise MalformedError(f"unsupported assertion version: {body['v']!r}")

    signer = body["signer"]
    _decode_hex(signer, PUBLIC_KEY_HEX_LEN, "signer")
    sig = body["sig"]
    _decode_hex(sig, SIGNATURE_HEX_LEN, "sig")

    if not isinstance(body["org"], str) or not body["org"]:
        raise MalformedError("org must be a non-empty string")
    if not isinstance(body["scope"], str) or not body["scope"]:
        raise MalformedError("scope must be a non-empty string")
    if not isinstance(body["cert"], str) or not body["cert"]:
        raise MalformedError("cert must be a canonical wire JSON string")

    nonce = _require_nonce(body["nonce"], "nonce")
    dashboard_origin = _require_origin(body["dashboard_origin"])
    not_before = _require_ts(body["not_before"], "not_before")
    not_after = _require_ts(body["not_after"], "not_after")
    if not_before >= not_after:
        raise MalformedError("not_before must be strictly before not_after")

    challenge = None
    if "challenge" in body:
        challenge = _require_nonce(body["challenge"], "challenge")

    return Assertion(
        v=ASSERTION_VERSION,
        signer=signer,
        org=body["org"],
        nonce=nonce,
        dashboard_origin=dashboard_origin,
        scope=body["scope"],
        not_before=not_before,
        not_after=not_after,
        cert=body["cert"],
        sig=sig,
        challenge=challenge,
    )


def build_assertion(
    key,
    *,
    org: str,
    nonce: str,
    dashboard_origin: str,
    not_before: int,
    not_after: int,
    cert,
    scope: str = IDENTIFY_SCOPE,
    challenge: Optional[str] = None,
) -> dict:
    """Mint a signed assertion wire dict (client-side: dashboard picker/PWA).

    *key* is the leaf key (session or persona key); *cert* is the
    :class:`~tools.network.idkit.DelegationCert` that delegates to it and
    chains to the org root. This is the server's counterpart of
    :func:`parse_assertion` + the redemption verification, and what the
    tests, the dashboard picker, and the PWA QR ceremony all use.
    """
    if cert.child_pub != key.public_hex:
        raise ValueError("cert does not delegate to the signing key")
    assertion = Assertion(
        v=ASSERTION_VERSION,
        signer=key.public_hex,
        org=org,
        nonce=nonce,
        dashboard_origin=dashboard_origin,
        scope=scope,
        not_before=not_before,
        not_after=not_after,
        cert=cert.to_json().decode("ascii"),
        sig="0" * SIGNATURE_HEX_LEN,
        challenge=challenge,
    )
    sig = key.sign_hex(assertion.signing_input())
    wire = assertion.signed_dict()
    wire["cert"] = assertion.cert
    wire["sig"] = sig
    return wire
