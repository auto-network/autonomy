"""Encrypted event bundles — what the broker mailbox stores (L6).

Spec ``graph://eb245082-b76`` §6, bead ``auto-rrzrt`` (F3). The broker is
a **T0-blind** store-and-forward mailbox: it must see the topic, the
event hashes, and the sizes — and nothing else. A bundle is therefore a
plaintext hash manifest next to an AES-256-GCM blob of the actual event
wires, sealed under the **org sync key**.

**The org sync key comes from ledger state**: HKDF-SHA256 over the
genesis event's canonical wire bytes. The genesis wire embeds the root's
Ed25519 signature, which only the root key could ever have produced — so
the wire (hence the key) is underivable from public facts (org UUID,
root pub) and unguessable to anyone who was never given the replica.
Possession of the org's event DAG ⇔ possession of the sync key, which is
exactly the v1 membership boundary: every full replica holder is a
member. The broker holds hashes only (L6), so it can never derive the
key from what it stores. *v1 boundary:* the key is static for the org's
lifetime; rotation on membership change (kicked members keep old
ciphertext, spec §13 Q4) lands with the M-track resolution.

**AAD pins org + topic + manifest.** The GCM tag covers
``{v, org, topic, hashes}``, so a bundle spliced across topics (a
content bundle replayed into the authority topic), across orgs, or with
a doctored manifest fails authentication instead of decrypting — topic
scoping holds cryptographically even against a dishonest broker. After
decryption the manifest is re-checked against the actual event ids, so
the broker-visible hashes can never lie about the ciphertext.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Iterable, List

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes as _hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from tools.network.idkit import canonical_json

from .errors import LedgerError
from .events import Event, require_hash_list

SYNC_KEY_INFO = b"autonomy.network.ledger.sync-key.v1"
BUNDLE_DOMAIN = b"autonomy.network.ledger.bundle.v1\n"
BUNDLE_VERSION = 1

SYNC_KEY_LEN = 32
_NONCE_LEN = 12

#: One mailbox deposit at most this many events; larger deltas split.
MAX_BUNDLE_EVENTS = 4_096

_BUNDLE_FIELDS = frozenset({"v", "hashes", "size", "ciphertext"})


class BundleError(LedgerError):
    """A sealed bundle is malformed, tampered, or for another org/topic."""


def derive_sync_key(genesis: Event) -> bytes:
    """The org sync key — 32 bytes, HKDF over the genesis wire (see module doc)."""
    if genesis.type != "genesis":
        raise BundleError("sync key derives from the genesis event only")
    return HKDF(
        algorithm=_hashes.SHA256(), length=SYNC_KEY_LEN, salt=None, info=SYNC_KEY_INFO
    ).derive(genesis.to_json())


def _aad(org: str, topic: str, hash_manifest: List[str]) -> bytes:
    return BUNDLE_DOMAIN + canonical_json(
        {"v": BUNDLE_VERSION, "org": org, "topic": topic, "hashes": hash_manifest}
    )


def seal_bundle(sync_key: bytes, org: str, topic: str, events: Iterable[Event]) -> dict:
    """Encrypt *events* for the broker mailbox.

    Returns ``{v, hashes, size, ciphertext}`` — the broker-visible face
    is exactly the L6 allowance: hashes and sizes; the wires live only
    inside the AEAD blob.
    """
    batch = list(events)
    if not batch or len(batch) > MAX_BUNDLE_EVENTS:
        raise BundleError(f"a bundle carries between 1 and {MAX_BUNDLE_EVENTS} events")
    manifest = sorted(e.event_id for e in batch)
    if len(set(manifest)) != len(batch):
        raise BundleError("bundle events must be unique")
    plaintext = canonical_json([e.to_json().decode("ascii") for e in batch])
    nonce = os.urandom(_NONCE_LEN)
    blob = nonce + AESGCM(sync_key).encrypt(nonce, plaintext, _aad(org, topic, manifest))
    return {
        "v": BUNDLE_VERSION,
        "hashes": manifest,
        "size": len(blob),
        "ciphertext": base64.b64encode(blob).decode("ascii"),
    }


def validate_bundle(bundle: object) -> dict:
    """Structural check of a sealed bundle (what the broker also enforces)."""
    if not isinstance(bundle, dict) or set(bundle) != _BUNDLE_FIELDS:
        raise BundleError(f"bundle must carry exactly {sorted(_BUNDLE_FIELDS)}")
    if bundle["v"] != BUNDLE_VERSION:
        raise BundleError(f"unsupported bundle version: {bundle['v']!r}")
    require_hash_list(
        bundle["hashes"], "bundle hashes", MAX_BUNDLE_EVENTS,
        allow_empty=False, exc=BundleError,
    )
    if not isinstance(bundle["ciphertext"], str) or not bundle["ciphertext"]:
        raise BundleError("bundle ciphertext must be a base64 string")
    if type(bundle["size"]) is not int or bundle["size"] <= _NONCE_LEN:
        raise BundleError("bundle size must be the integer ciphertext byte length")
    return bundle


def open_bundle(sync_key: bytes, org: str, topic: str, bundle: dict) -> List[Event]:
    """Decrypt and verify one bundle; returns its events.

    Fails closed on: wrong key, wrong org, wrong topic, doctored
    manifest, doctored ciphertext, size mismatch, or decrypted events
    whose ids do not equal the manifest.
    """
    bundle = validate_bundle(bundle)
    try:
        blob = base64.b64decode(bundle["ciphertext"], validate=True)
    except (ValueError, TypeError) as exc:
        raise BundleError("bundle ciphertext is not valid base64") from exc
    if len(blob) != bundle["size"]:
        raise BundleError("bundle size does not match its ciphertext")
    if len(blob) <= _NONCE_LEN:
        raise BundleError("bundle ciphertext is too short")
    manifest = bundle["hashes"]
    try:
        plaintext = AESGCM(sync_key).decrypt(
            blob[:_NONCE_LEN], blob[_NONCE_LEN:], _aad(org, topic, manifest)
        )
    except InvalidTag as exc:
        raise BundleError(
            "bundle failed authentication (wrong key, org, topic, or tampered bytes)"
        ) from exc
    wires = json.loads(plaintext)
    if not isinstance(wires, list) or any(not isinstance(w, str) for w in wires):
        raise BundleError("bundle plaintext must be a list of event wire strings")
    events = [Event.from_json(w.encode("ascii")) for w in wires]
    if sorted(e.event_id for e in events) != manifest:
        raise BundleError("bundle manifest does not match the decrypted events")
    return events
