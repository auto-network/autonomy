"""Signed request envelope for registry mutations.

Every mutating registry call carries a JSON envelope::

    {
      "v": 1,
      "signer": "<leaf public key, 64 hex>",
      "ts": <unix seconds>,
      "payload": { ...endpoint-specific body... },
      "cert": "<canonical DelegationCert wire JSON>",   # absent for root-direct
      "sig": "<128 hex>"
    }

The signature covers domain-separated canonical bytes binding the HTTP
method and path, so an envelope minted for one endpoint can never be
replayed against another::

    sig = Ed25519_sign(signer_priv,
                       REQUEST_DOMAIN || canonical_json(
                           {v, method, path, ts, signer, payload}))

``ts`` freshness (±MAX_CLOCK_SKEW) bounds replay in time. The ``cert``
field is the leaf delegation certificate with its full chain embedded,
passed as a canonical wire *string* and parsed with the anti-malleable
``DelegationCert.from_json`` — one accepted byte form. When ``cert`` is
absent the signer must be the org's bound root key itself (root-direct;
registration is always root-direct because the binding does not exist
yet).
"""

from __future__ import annotations

from typing import Optional

from tools.network.idkit import DelegationCert, KeyPair, canonical_json

REQUEST_DOMAIN = b"autonomy.network.registry.request.v1\n"
ENVELOPE_VERSION = 1

#: Domain for the OLD recovery key's co-signature authorizing its own
#: replacement or removal as the org's recovery factor (the succession
#: co-sig). Distinct from REQUEST_DOMAIN so a request envelope signature
#: can never be substituted for a succession authorization and vice versa.
RECOVERY_SUCCESSION_DOMAIN = b"autonomy.network.registry.recovery-succession.v1\n"

#: Maximum tolerated |server now - envelope ts|, seconds. Owned by
#: tools.network.clock (a freshness gate); re-exported here for the
#: registry-side call sites that always read it from this module.
from tools.network.clock import MAX_CLOCK_SKEW


def request_signing_input(method: str, path: str, ts: int, signer: str, payload: dict) -> bytes:
    """The exact bytes an envelope signature covers."""
    return REQUEST_DOMAIN + canonical_json(
        {
            "v": ENVELOPE_VERSION,
            "method": method.upper(),
            "path": path,
            "ts": ts,
            "signer": signer,
            "payload": payload,
        }
    )


def recovery_succession_input(
    org_uuid: str,
    old_recovery_pub: str,
    new_policy: str,
    new_recovery_pub: Optional[str],
    policy_epoch: int,
) -> bytes:
    """The exact bytes the OLD recovery key co-signs to authorize replacing or
    removing itself as the org's recovery factor.

    Bound to ``policy_epoch`` — the epoch the update TRANSITIONS INTO (current
    + 1) — so an old-recovery co-signature is single-use for exactly one
    succession and cannot be replayed against a later one. ``org_uuid`` and
    ``old_recovery_pub`` pin it to this org and this outgoing key;
    ``new_policy`` / ``new_recovery_pub`` pin the successor it authorizes (or
    ``None`` on removal), so the co-signer approves a specific transition, not a
    blank cheque.
    """
    return RECOVERY_SUCCESSION_DOMAIN + canonical_json(
        {
            "org_uuid": org_uuid,
            "old_recovery_pub": old_recovery_pub,
            "new_policy": new_policy,
            "new_recovery_pub": new_recovery_pub,
            "policy_epoch": policy_epoch,
        }
    )


def sign_recovery_succession(
    recovery_key: KeyPair,
    org_uuid: str,
    new_policy: str,
    new_recovery_pub: Optional[str],
    policy_epoch: int,
) -> str:
    """Client-side counterpart: the outgoing recovery key co-signs its own
    succession/removal. The signer's own public key is the ``old_recovery_pub``
    the server checks against, so it is derived here rather than passed."""
    return recovery_key.sign_hex(
        recovery_succession_input(
            org_uuid,
            recovery_key.public_hex,
            new_policy,
            new_recovery_pub,
            policy_epoch,
        )
    )


def sign_request(
    key: KeyPair,
    method: str,
    path: str,
    payload: dict,
    *,
    ts: int,
    cert: Optional[DelegationCert] = None,
) -> dict:
    """Build a signed envelope for ``method path`` with *payload*.

    Pass *cert* when *key* is a delegated key (the cert must delegate to
    it); omit it for root-direct requests. This helper is the client-side
    counterpart of the server's envelope verification and is what the
    dashboard, CLI, and tests all use.
    """
    if cert is not None and cert.child_pub != key.public_hex:
        raise ValueError("cert does not delegate to the signing key")
    envelope = {
        "v": ENVELOPE_VERSION,
        "signer": key.public_hex,
        "ts": ts,
        "payload": payload,
        "sig": key.sign_hex(request_signing_input(method, path, ts, key.public_hex, payload)),
    }
    if cert is not None:
        envelope["cert"] = cert.to_json().decode("ascii")
    return envelope
