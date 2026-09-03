"""SealedSettings — navigate graph settings as if they were unencrypted.

A consumer (a password manager, a notes app) calls ``get`` / ``put`` / ``list``
/ ``delete`` with logical names and plaintext. This layer transparently:

- **hashes the key** into a blind index — ``<store-tag>:<HMAC(K_index, name)>``
  — so the substrate never sees the logical name;
- **seals the value** under a metadata key derived from the store's sealed
  index, so the substrate stores only ciphertext;
- **manages the sealed index** lazily: on first use it is minted (a
  factor-free seal) or released through the existing ``vault_open``
  rendezvous, and cached for the session.

The consumer never sees the hash, the ciphertext, the derived keys, or the
unlock ceremony. Swapping a plain settings client for a ``SealedSettings`` of
the same surface changes no call site.

## Seal-all addressing — nothing on disk names a store

Every address this layer writes is opaque (crib ``1e005d5c-c11`` §23/§24):

- The **sealed index** — the store's one 32-byte secret, from which
  ``K_index``/``K_meta`` derive — is an ordinary ``autonomy.vault.secured``
  credential whose row key is ``base64url(HMAC-SHA-256(pepper, NFC(store
  name)))``. All sealed indexes pool in the secured set with uniform opaque
  keys; a cold reader learns how many stores exist, never which is which.
- Item rows carry that same opaque address as their **store tag** in place of
  a plaintext domain prefix, so a cold dump cannot attribute a row to a store
  or app — required for secret-vault deniability, where an attributable tag
  would defeat the vault's hidden existence.
- The **pepper** is ONE shared ``autonomy.vault.audited`` row
  (``sealed-settings.pepper``): encrypted at rest, released unattended when
  the vault is warm, minted once at vault bring-up
  (:func:`ensure_pepper_minted`). It is what makes store locations
  incomputable from a cold dump. It is never re-minted — rotation would
  orphan every store address.

## Audience discovery (append-to-hashed-sets)

To enumerate items shared to an audience you cannot predict, :meth:`share`
appends a membership row under an opaque set address
``HMAC(K_aud_name, "set:"+class)`` whose value is the item's row key SEALED
under a dedicated ``K_aud_seal`` (Approach B). :meth:`discover` resolves the
set, decrypts each reference, and authorized-opens the item. Membership is a
DISCOVERY HINT, never authorization — the vault seal decides access, so a
forged or stale membership either fails to open or opens only what the reader
could already open. Sealing the reference (not storing the item key in the
clear) keeps a cold reader from joining audiences to items or to their store,
and membership rows live in the SAME sealed-row set as items, so they are
indistinguishable from items at rest. Cross-member sharing in an org (sealing
the reference to a policy class's key instead) is the documented extension.

## Per-row convergence, no central index

There is no manifest to compare-and-swap: each item is an independent row, so
concurrent edits never contend on one blob. A writer does not guess whether it
won — :meth:`SealedSettings.put` returns a :class:`WriteOutcome` built by
consuming the substrate's authoritative write-result (the value a read now
resolves to), never by comparing timestamps or merging. Because every seal
carries a fresh nonce, the authoritative ciphertext identifies the physical
write: two racing writers converge on one value and each learns whether its
own survived. A referenced row that has not yet replicated is *pending*, never
*absent* — but the referencing mechanism (audience sets) is a separate layer.

## No new crypto

Keys are derived from the 32-byte sealed index with HKDF-SHA-256 and values
sealed with AES-256-GCM — both from ``cryptography``, the same primitives the
vault already uses. AES-GCM (not ChaCha) is deliberate: the browser holds the
derived keys and only AES-GCM is in WebCrypto, so it can keep them
non-extractable. See ``_seal``. (The vault's own sealed-index body is a
separate cipher, unchanged.)

## Where sealing lands, and the one audit event

The metadata rows here are sealed by THIS layer under the factor-derived key,
so listing a store is ONE factor release (opening the sealed index), not one
audited release per row. A secret whose every reveal must be audited does NOT belong in
a metadata row — it belongs in ``autonomy.vault.audited`` under the same blind
index, where the substrate's release-is-audit fires per reveal. This module
owns the index + metadata half; the audited-secret half is the existing vault
set, addressed by :func:`blind_index`.

## Runtime seam

The substrate operations (read the pepper, release/mint the sealed index,
read/write/list rows) are a :class:`SealedBackend` so the two runtime contexts
— a container session over the HTTP client, an in-process dashboard plugin —
can each supply their own without the crypto core knowing which it is.
:class:`ClientBackend` is the container/HTTP adapter; it is runtime-critical
(its pepper read, sealed-index release, and existence probe only exercise
against a live dashboard) and must be validated on a real run, not only
unit-tested.
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import secrets
import unicodedata
from hashlib import sha256
from typing import Iterable, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .schemas.vault_credential import (
    VAULT_AUDITED_SET_ID,
    VAULT_CREDENTIAL_REVISION,
    VAULT_SECURED_SET_ID,
)
from .schemas.sealed_row import SEALED_ROW_REVISION, SEALED_ROW_SET_ID

# One fixed salt binds every derivation to this scheme + version, so a sealed
# index reused elsewhere never yields these subkeys and a future scheme
# revision is a clean break rather than a silent key collision.
_HKDF_SALT = b"autonomy.sealed-settings.v1"
_INFO_INDEX = b"index"          # -> K_index, the blind-index HMAC key
_INFO_METADATA = b"metadata"    # -> K_meta, the metadata AEAD key
# Audience discovery uses two more subkeys, fully domain-separated from the
# item keys above so an audience address can never collide with an item's
# blind index and a cold reader cannot correlate the two.
_INFO_AUD_NAME = b"audience-index"   # -> K_aud_name, HMAC key for set/member addresses
_INFO_AUD_SEAL = b"audience-seal"    # -> K_aud_seal, AEAD key for member references
_SEALED_INDEX_LEN = 32
_NONCE_LEN = 12

#: The one shared audited credential every hidden store address derives from.
#: Its NAME is deliberately plaintext (anchor layer: the row's identity is the
#: declared leakage, its value is sealed); what it protects is every OTHER
#: address. Minted once at vault bring-up; never rotated in place.
PEPPER_KEY = "sealed-settings.pepper"
_PEPPER_LEN = 32


# --------------------------------------------------------------------------- #
# Typed failures — the ONLY thing that surfaces through the abstraction is that
# a sealed read can be unavailable, exactly as a plain settings read can.
# --------------------------------------------------------------------------- #
class SealedSettingsError(RuntimeError):
    """A sealed store operation could not complete."""


class VaultLocked(SealedSettingsError):
    """The credential exists but could not be released now.

    ``state`` is one of ``PENDING`` (operator has not yet decided),
    ``DENIED`` (operator declined), or ``COLD`` (no key holder is warm — the
    browser warm ceremony is required, not this rendezvous).
    """

    PENDING = "PENDING"
    DENIED = "DENIED"
    COLD = "COLD"

    def __init__(self, state: str, *, ticket: str | None = None):
        self.state = state
        self.ticket = ticket
        super().__init__(f"vault release {state.lower()}")


# --------------------------------------------------------------------------- #
# Pure crypto core — no I/O, fully unit-testable.
# --------------------------------------------------------------------------- #
def _canonical(name: str) -> bytes:
    """Byte form a logical name hashes/seals under: NFC then UTF-8.

    NFC first so two visually identical names that differ only in Unicode
    composition map to the same row instead of two.
    """
    return unicodedata.normalize("NFC", name).encode("utf-8")


def _hkdf(root: bytes, info: bytes, length: int = 32) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(), length=length, salt=_HKDF_SALT, info=info
    ).derive(root)


def hidden_address(pepper: bytes, store_name: str) -> str:
    """The opaque row address a store's sealed index lives at.

    ``base64url(HMAC-SHA-256(pepper, NFC(store-name)))``, unpadded — the
    seal-all form (crib §23). The same string doubles as the store tag
    prefixing every item row, so nothing on disk attributes a row to a store.
    A secret vault derives its address as ``HMAC(pepper, KDF(password))``
    browser-side; colliding with it would take a store name whose NFC UTF-8
    bytes equal a 32-byte KDF output, which is negligible.
    """
    digest = hmac.new(pepper, _canonical(store_name), sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _bare_key(row_key: str) -> str:
    """Strip an optional server-derived ``<org>:`` prefix to the logical key.

    A row is stored either bare (``<tag>.<blind>``, operator/browser mint) or
    org-prefixed (``<org>:<tag>.<blind>``, session mint). Neither the org
    slug, the base64url tag, nor the blind index contains ``:``, so at most
    one ``:`` is ever present and the logical key is the final segment.
    """
    return row_key.rsplit(":", 1)[-1]


def blind_index(k_index: bytes, name: str) -> str:
    """The base64url HMAC-SHA-256 of a logical name — the row's hidden key.

    Exposed so the audited-secret half of a store can address the same item by
    the same blind index.
    """
    digest = hmac.new(k_index, _canonical(name), sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


# The layer's value seal is AES-256-GCM, NOT ChaCha, for one reason: the
# browser holds K_meta (and K_aud_seal) and decrypts these values, and only
# AES-GCM is in WebCrypto — so the browser can derive K_meta as a
# NON-EXTRACTABLE key and decrypt via `subtle.decrypt`, keeping the key
# unstealable (and holdable in IndexedDB) even under XSS. ChaCha would force
# raw key bytes into JS (noble), which are exfiltratable. This is the layer's
# OWN seal, independent of the vault body cipher (the sealed index's own body
# stays ChaCha/A-2, decrypted once at open). AES-GCM is safe here because these
# writes are single-owner and modest-volume with a fresh 96-bit nonce per seal
# — nonce-misuse resistance (GCM-SIV) is load-bearing only for the vault's
# unattended, multi-writer, fleet-synced AUDITED body, not for this layer.
def _seal(k_meta: bytes, plaintext: bytes, aad: bytes) -> str:
    nonce = os.urandom(_NONCE_LEN)
    blob = nonce + AESGCM(k_meta).encrypt(nonce, plaintext, aad)
    return base64.urlsafe_b64encode(blob).decode("ascii")


def _open(k_meta: bytes, ciphertext: str, aad: bytes) -> bytes:
    blob = base64.urlsafe_b64decode(ciphertext.encode("ascii"))
    nonce, sealed = blob[:_NONCE_LEN], blob[_NONCE_LEN:]
    return AESGCM(k_meta).decrypt(nonce, sealed, aad)


# --------------------------------------------------------------------------- #
# Substrate seam.
# --------------------------------------------------------------------------- #
class SealedBackend(Protocol):
    """The substrate operations SealedSettings needs, per runtime context."""

    def read_pepper(self) -> bytes:
        """Return the 32-byte pepper for the CALLER'S scope, minting if absent.

        Peppers are per-scope: an org session gets its org's pepper, the
        operator gets the personal one. The audited set is org-scoped on read
        (an org session only sees its own ``<org>:`` rows), so the pepper is
        stored org-writeback-keyed and this is get-or-create — read the
        caller's pepper, or mint it (the server derives the ``<org>:`` prefix)
        and read the winner. Raise :class:`VaultLocked` (``COLD``) when the
        pepper exists but the vault cannot release it now.
        """

    def read_sealed_index(self, address: str, *, block: bool) -> bytes | None:
        """Return the 32-byte sealed index for the hidden ``address``, or
        ``None`` if no such credential exists.

        ``address`` is the logical hidden address; a backend may store the
        row under a principal-derived prefix and must resolve either form.
        Raise :class:`VaultLocked` if the credential exists but cannot be
        released now (pending / denied / cold).
        """

    def mint_sealed_index(self, address: str, value_hex: str) -> None:
        """Seal a fresh sealed index (a factor-free personal-secured write).

        The substrate may store it under a principal-derived prefix; the
        layer re-reads through :meth:`read_sealed_index` and never assumes
        the stored key equals ``address``.
        """

    def read_row(self, row_key: str) -> str | None:
        """Return a row's ``ciphertext`` string, or ``None`` if absent."""

    def write_row(self, row_key: str, ciphertext: str) -> str:
        """Create-or-update a row, and return the row's AUTHORITATIVE
        ciphertext — the value a subsequent read resolves to.

        This is how a writer consumes the substrate's write-result without
        comparing timestamps or running conflict logic: when a concurrent
        writer's row is the one that resolves, that writer's ciphertext is
        returned here, not the one just passed in. In the uncontended case
        the return equals the argument.
        """

    def delete_row(self, row_key: str) -> None:
        """Remove a row (best-effort; absent is not an error)."""

    def list_rows(self, prefix: str) -> Iterable[tuple[str, str]]:
        """Yield ``(row_key, ciphertext)`` for every row under ``prefix:``."""


class SealedItem:
    """One decrypted item the consumer sees: its logical name + metadata."""

    __slots__ = ("name", "metadata")

    def __init__(self, name: str, metadata):
        self.name = name
        self.metadata = metadata

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"SealedItem(name={self.name!r})"


class WriteOutcome:
    """What a :meth:`SealedSettings.put` actually resulted in.

    ``won`` is whether the caller's own write is the one a read now resolves
    to; ``value`` is the AUTHORITATIVE metadata either way — the caller's when
    it won, a concurrent writer's when it lost. Both are derived by consuming
    the substrate's authoritative write-result (the unique per-seal nonce
    makes each physical write identifiable), never by comparing timestamps or
    merging. A loser converges simply by keeping ``value``.
    """

    __slots__ = ("won", "value")

    def __init__(self, won: bool, value):
        self.won = won
        self.value = value

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"WriteOutcome(won={self.won!r})"


class AudienceListing:
    """The result of :meth:`SealedSettings.discover`.

    ``items`` are the shared items whose rows are present and open; ``pending``
    are opaque member references that were discovered but whose rows are not
    on this replica yet — reported as PENDING, never conflated with absence
    (a referenced-but-unsynced row is a sync gap, not a deletion). Membership
    is a discovery HINT, so both lists reflect what the vault seal actually
    let the reader open: a forged or stale membership either fails to open
    (dropped) or opens an item the reader was already authorized for.
    """

    __slots__ = ("items", "pending")

    def __init__(self, items, pending):
        self.items = items
        self.pending = pending

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"AudienceListing(items={len(self.items)}, pending={len(self.pending)})"


# --------------------------------------------------------------------------- #
# The transparent surface.
# --------------------------------------------------------------------------- #
class SealedSettings:
    """A sealed key/value store over one logical ``domain``.

    The domain is the store's name INSIDE this layer only; on disk it appears
    solely as the pepper-derived opaque tag. ``block`` chooses what a
    sealed-index release does when the operator has not yet decided: ``False``
    (default) raises ``VaultLocked("PENDING")`` immediately; ``True`` holds
    until the decision (the backend's release call waits).
    """

    def __init__(self, domain: str, backend: SealedBackend, *, block: bool = False):
        self._domain = domain
        self._backend = backend
        self._block = block
        self._sealed_index: bytes | None = None
        self._tag: str | None = None

    # -- consumer-facing: identical shape to a plain settings client -------- #
    def get(self, name: str):
        _, k_meta = self._keys()
        ciphertext = self._backend.read_row(self._row_key(name))
        if ciphertext is None:
            return None
        return self._open_item(k_meta, self._row_key(name), ciphertext).metadata

    def put(self, name: str, metadata) -> WriteOutcome:
        """Store ``metadata`` under ``name`` and report the converged result.

        Returns a :class:`WriteOutcome`: ``won`` says whether this write is
        the one a read now resolves to, and ``value`` is the authoritative
        metadata regardless (a concurrent writer's if this write lost). The
        result is consumed from the substrate — the returned authoritative
        ciphertext — not derived from any clock, so two racing writers
        converge on one value and each learns whether its own write survived.
        """
        _, k_meta = self._keys()
        row_key = self._row_key(name)
        payload = json.dumps(
            {"name": name, "metadata": metadata},
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        mine = _seal(k_meta, payload, self._aad(row_key))
        authoritative = self._backend.write_row(row_key, mine)
        won = authoritative == mine
        value = self._open_item(k_meta, row_key, authoritative).metadata
        return WriteOutcome(won, value)

    def delete(self, name: str) -> None:
        self._keys()  # ensure the store is unlocked before we mutate it
        self._backend.delete_row(self._row_key(name))

    def list(self) -> list[SealedItem]:
        _, k_meta = self._keys()
        items: list[SealedItem] = []
        for row_key, ciphertext in self._backend.list_rows(self._store_tag()):
            # row_key may carry a server-derived <org>: prefix; _aad strips it.
            try:
                items.append(self._open_item(k_meta, row_key, ciphertext))
            except (InvalidTag, ValueError, KeyError):
                # Not ours, or tampered — fail closed by skipping. A row we can
                # locate but cannot open is never surfaced as plaintext.
                continue
        return items

    def blind_index(self, name: str) -> str:
        """The domain-scoped blind index for ``name`` (for the audited-secret half)."""
        return self._row_key(name)

    # -- audience discovery (append-to-hashed-sets) ------------------------- #
    def share(self, name: str, audience_class: str) -> None:
        """Make ``name`` discoverable to ``audience_class``.

        Appends a membership row under the audience set's hidden address whose
        SEALED value references the item's row. Idempotent per (class, item):
        re-sharing rewrites the same membership row rather than duplicating it.
        This is a discovery hint only — it does not grant access; the item's
        vault seal remains the authorization.
        """
        k_meta, k_seal = self._keys()[1], self._audience_keys()[1]
        row_key = self._member_key(audience_class, name)
        # The sealed value is the item's own row key, so a discoverer can find
        # the item; sealed under K_aud_seal (never K_meta) with AAD bound to
        # this membership row, so it cannot be lifted into another set.
        reference = self._row_key(name).encode("utf-8")
        self._backend.write_row(row_key, _seal(k_seal, reference, self._aad(row_key)))

    def discover(self, audience_class: str) -> AudienceListing:
        """List the items shared to ``audience_class`` that this reader can open.

        Resolves the audience set, decrypts each member reference, and
        authorized-opens the item it names. An item whose row is not present on
        this replica is reported as pending (its opaque row key), never as
        absent. Members that fail to decrypt or open are dropped (fail closed).
        """
        k_meta, k_seal = self._keys()[1], self._audience_keys()[1]
        set_name = self._audience_set_name(audience_class)
        items: list[SealedItem] = []
        pending: list[str] = []
        seen: set[str] = set()
        for member_key, ciphertext in self._backend.list_rows(set_name):
            try:
                item_key = _open(k_seal, ciphertext, self._aad(member_key)).decode("utf-8")
            except (InvalidTag, ValueError):
                continue  # not ours / tampered — a hint we cannot trust, drop it
            if item_key in seen:
                continue
            seen.add(item_key)
            item_ct = self._backend.read_row(item_key)
            if item_ct is None:
                pending.append(item_key)   # referenced but not replicated: PENDING
                continue
            try:
                items.append(self._open_item(k_meta, item_key, item_ct))
            except (InvalidTag, ValueError, KeyError):
                continue  # authorized-open failed: fail closed, never plaintext
        return AudienceListing(items, pending)

    # -- internals ---------------------------------------------------------- #
    def _row_key(self, name: str) -> str:
        # The LOGICAL (bare) row key: <store-tag>.<blind-index>. The '.'
        # separator is outside base64url, so tag and blind index stay
        # unambiguous and the whole compound is a legal org-writeback suffix.
        # A session's write is stored under a server-derived <org>: prefix;
        # this bare form is what the layer computes, seals against, and looks
        # up by.
        k_index, _ = self._keys()
        return f"{self._store_tag()}.{blind_index(k_index, name)}"

    def _aad(self, row_key: str) -> bytes:
        # Bind each ciphertext to its own row so a blob cannot be lifted into
        # another key's row and still open. Always the BARE key: the writer
        # cannot know the server-derived <org>: prefix at seal time, and the
        # cross-org lift the prefix would otherwise guard against is already
        # forbidden by the writeback namespace boundary.
        return _bare_key(row_key).encode("utf-8")

    def _open_item(self, k_meta: bytes, row_key: str, ciphertext: str) -> SealedItem:
        payload = json.loads(_open(k_meta, ciphertext, self._aad(row_key)))
        return SealedItem(payload["name"], payload["metadata"])

    def _keys(self) -> tuple[bytes, bytes]:
        if self._sealed_index is None:
            self._sealed_index = self._get_or_create_sealed_index()
        return (
            _hkdf(self._sealed_index, _INFO_INDEX),
            _hkdf(self._sealed_index, _INFO_METADATA),
        )

    def _audience_keys(self) -> tuple[bytes, bytes]:
        if self._sealed_index is None:
            self._sealed_index = self._get_or_create_sealed_index()
        return (
            _hkdf(self._sealed_index, _INFO_AUD_NAME),
            _hkdf(self._sealed_index, _INFO_AUD_SEAL),
        )

    def _audience_set_name(self, audience_class: str) -> str:
        # The opaque address prefix a class's members share. Domain-tagged
        # ('set:') under K_aud_name so it cannot equal a member address or an
        # item blind index. Members live in the shared sealed-row set, so a
        # cold reader cannot even tell membership rows from item rows.
        k_aud_name, _ = self._audience_keys()
        digest = hmac.new(
            k_aud_name, b"set:" + _canonical(audience_class), sha256
        ).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    def _member_key(self, audience_class: str, name: str) -> str:
        # <set-name>.<member-tag>, where member-tag is a SEPARATE HMAC over
        # (class, item-name) — deterministic (so re-share is idempotent) but
        # unequal to the item's own blind index, so a cold reader cannot
        # correlate this membership row with the item row it references.
        k_aud_name, _ = self._audience_keys()
        member = hmac.new(
            k_aud_name,
            b"member:" + _canonical(audience_class) + b"\x00" + _canonical(name),
            sha256,
        ).digest()
        tag = base64.urlsafe_b64encode(member).decode("ascii").rstrip("=")
        return f"{self._audience_set_name(audience_class)}.{tag}"

    def _store_tag(self) -> str:
        """The store's one opaque on-disk name: its hidden address."""
        if self._tag is None:
            pepper = self._backend.read_pepper()
            if len(pepper) != _PEPPER_LEN:
                raise SealedSettingsError("pepper is not 32 bytes")
            self._tag = hidden_address(pepper, self._domain)
        return self._tag

    def _get_or_create_sealed_index(self) -> bytes:
        address = self._store_tag()
        sealed_index = self._backend.read_sealed_index(address, block=self._block)
        if sealed_index is None:
            # First use: seed the credential (factor-free seal-only write),
            # then bind to the AUTHORITATIVE bytes via the same release path.
            # We never use the freshly generated bytes directly, so a
            # concurrent first-mint resolves to one winner both sessions
            # converge on.
            self._backend.mint_sealed_index(
                address, secrets.token_bytes(_SEALED_INDEX_LEN).hex()
            )
            sealed_index = self._backend.read_sealed_index(address, block=self._block)
            if sealed_index is None:
                raise SealedSettingsError(
                    "sealed index absent immediately after mint"
                )
        if len(sealed_index) != _SEALED_INDEX_LEN:
            raise SealedSettingsError("sealed index is not 32 bytes")
        return sealed_index


# --------------------------------------------------------------------------- #
# Container / HTTP backend. RUNTIME-CRITICAL: the pepper read, the
# sealed-index release, and the existence probe only exercise against a live
# dashboard, so this adapter's behavior must be confirmed on a real run, not
# solely by the unit tests (which cover the crypto core through a fake
# backend).
# --------------------------------------------------------------------------- #
class ClientBackend:
    """A :class:`SealedBackend` over ``tools.graph.client`` (a container session).

    Every settings call passes ``org=None``: the header is omitted and the
    dashboard derives the caller's org from the session bearer. Passing
    ``"personal"`` explicitly is REFUSED at the API boundary ("organization
    mismatch" — the bearer names a real org); routing to the operator's
    personal store comes from each set's declared home, not from the header.
    """

    def __init__(self, client, *, ttl_seconds: int = 60):
        self._client = client
        self._ttl = ttl_seconds

    def read_pepper(self) -> bytes:
        # Per-scope, get-or-create. The audited set is org-scoped on read
        # (2b09456): an org session sees ONLY its own <org>: rows, so the
        # pepper is matched by SUFFIX (its stored key is <org>:PEPPER_KEY for
        # an org session, or the bare PEPPER_KEY for the operator). If the
        # caller's scope has no pepper yet, mint one — the server derives the
        # <org>: prefix on the audited write — and read the winner. Minting
        # needs the audited delegate warm; a cold vault surfaces as COLD.
        pepper = self._read_pepper_row()
        if pepper is not None:
            return pepper
        try:
            self._client.add_setting(
                VAULT_AUDITED_SET_ID, VAULT_CREDENTIAL_REVISION, PEPPER_KEY,
                {"value": secrets.token_bytes(_PEPPER_LEN).hex()},
                state="raw", org=None,
            )
        except Exception:
            # A concurrent mint likely won the create-once race (or the
            # audited write is refused); re-read and use whatever is
            # authoritative, so peppers never rotate.
            pass
        pepper = self._read_pepper_row()
        if pepper is None:
            raise SealedSettingsError(
                "the pepper is absent immediately after mint — the audited "
                "delegate is not warm (vault bring-up / auto-rhorp A-1)"
            )
        return pepper

    def _read_pepper_row(self) -> bytes | None:
        # The audited read is org-scoped server-side, so this returns only the
        # caller's own rows; the pepper is matched by suffix.
        members = self._client.read_set(VAULT_AUDITED_SET_ID, org=None)
        member = next(
            (m for m in members.members if _bare_key(m.key) == PEPPER_KEY), None
        )
        if member is None:
            return None
        failure = getattr(member, "vault_error", None)
        if failure is not None:
            if getattr(failure, "reason", "") == "no_key_holder":
                raise VaultLocked(VaultLocked.COLD)
            raise SealedSettingsError(
                f"the pepper could not be released: {getattr(failure, 'reason', '?')}"
            )
        value = (member.payload or {}).get("value")
        if not isinstance(value, str):
            raise SealedSettingsError("the pepper row carries no value")
        return bytes.fromhex(value)

    def read_sealed_index(self, address: str, *, block: bool) -> bytes | None:
        # Existence probe first: opening a nonexistent secured key cannot be
        # told apart from a denial, so decide absent-vs-locked WITHOUT the
        # ceremony. The row is present even while sealed.
        #
        # Resolution is by SUFFIX: the vault-settings seam stores a
        # session-minted row under the bearer's org (`autonomy:<address>`)
        # while operator/browser mints are unprefixed — an operator-ruled,
        # declared leakage (which principal class minted the store). The
        # rendezvous enforces the same boundary on release: an org bearer
        # never addresses the personal store directly — it sends the BARE
        # suffix and the server derives its own prefix. So an unprefixed
        # (operator-minted) store is structurally unreachable from a session,
        # and minting a shadow index beside it would split the store — fail
        # loud instead.
        from tools.graph.settings_ops import VAULT_NO_KEY_HOLDER

        members = self._client.read_set(VAULT_SECURED_SET_ID, org=None)
        # Cold is decided on the STRUCTURED reason the substrate already
        # returned with this row — the same branch read_pepper() takes, and the
        # contract VaultReadFailure states ("reason is what to branch on;
        # message is the operator-facing detail"). It is never inferred from
        # error prose: a substring probe classified any message containing
        # "no key" as COLD, so unrelated failures were reported as a cold vault
        # and sent operators chasing a lock that was not there.
        failure = next(
            (m.vault_error for m in members.members
             if m.key.endswith(f":{address}") or m.key == address),
            None,
        )
        # A secured row awaiting its ceremony carries sealed_content_key and NO
        # vault_error, so this fires only on a genuine failure — and then a
        # ceremony would be futile: the row cannot be opened at all.
        if failure is not None:
            reason = getattr(failure, "reason", "") or "?"
            if reason == VAULT_NO_KEY_HOLDER:
                raise VaultLocked(VaultLocked.COLD)
            raise SealedSettingsError(
                f"the sealed index could not be released: {reason}"
            )
        keys = {m.key for m in members.members}
        prefixed = any(k.endswith(f":{address}") for k in keys)
        if not prefixed:
            if address in keys:
                raise SealedSettingsError(
                    "this store's sealed index was minted by the operator "
                    "(unprefixed) and cannot be released to an org session"
                )
            return None
        # Reuse an already-released sealed index within this session: a prior
        # approval materialized it at /run/secrets/<name> for the credential's
        # lifetime, and re-releasing an already-open store would ask the
        # operator to approve the same thing twice. The row exists (checked
        # above), so a present file is a genuine prior release, not a stray.
        cached = self._released_path(address)
        if cached is not None:
            return cached
        try:
            receipt = self._client.request_vault_open(
                VAULT_SECURED_SET_ID, address,
                org=None, ttl_seconds=self._ttl,
            )
        except PermissionError:
            raise VaultLocked(VaultLocked.DENIED)
        except Exception as exc:  # GraphHttpError et al.
            status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
            if status == 408:
                raise VaultLocked(VaultLocked.PENDING) from exc
            # A structured reason, if the surface carried one — never a guess
            # at the prose. Anything else raises AS ITSELF: a truthful error
            # beats a confident mislabel, which is what sent the last
            # investigation after a cold key that was actually warm.
            if isinstance(getattr(exc, "body", None), dict) and \
                    exc.body.get("reason") == VAULT_NO_KEY_HOLDER:
                raise VaultLocked(VaultLocked.COLD) from exc
            raise
        with open(receipt["path"], "rb") as handle:
            return bytes.fromhex(handle.read().decode("ascii").strip())

    @staticmethod
    def _released_path(address: str) -> bytes | None:
        """The sealed index bytes if already released to this session's ramfs.

        The rendezvous delivers to ``/run/secrets/<name>`` where ``name`` is
        the bare address. Reading it back needs no approval — the operator
        already granted this session that release. Returns ``None`` when no
        such file exists or it is not the expected 32-byte hex secret.
        """
        path = os.path.join("/run/secrets", address)
        try:
            with open(path, "rb") as handle:
                raw = bytes.fromhex(handle.read().decode("ascii").strip())
        except (OSError, ValueError):
            return None
        return raw if len(raw) == _SEALED_INDEX_LEN else None

    def mint_sealed_index(self, address: str, value_hex: str) -> None:
        self._client.seal_personal_setting(
            address, value_hex, policy_class_id="personal-root",
        )

    def read_row(self, row_key: str) -> str | None:
        # row_key is the bare logical key; the stored row may carry a
        # server-derived <org>: prefix (a session mint). Match on the bare
        # form so both resolve. The read is already org-scoped by the
        # substrate (this set declares an org-writeback namespace), so a
        # session sees only its own prefixed rows plus any bare ones.
        members = self._client.read_set(SEALED_ROW_SET_ID, org=None)
        for member in members.members:
            if _bare_key(member.key) == row_key:
                return (member.payload or {}).get("ciphertext")
        return None

    def write_row(self, row_key: str, ciphertext: str) -> str:
        # Write the BARE key; the server derives this session's <org>: prefix
        # for the personal-homed org-writeback set. The header is omitted so
        # the bearer names the org (a "personal" header is refused).
        self._client.add_setting(
            SEALED_ROW_SET_ID, SEALED_ROW_REVISION, row_key,
            {"ciphertext": ciphertext}, state="raw", org=None,
        )
        # Consume the authoritative write-result: read the row back and return
        # whatever resolves. A concurrent writer that committed after us makes
        # its ciphertext authoritative, and returning it here is how put()
        # learns it lost and converges — no timestamp compared, no merge done.
        resolved = self.read_row(row_key)
        return resolved if resolved is not None else ciphertext

    def delete_row(self, row_key: str) -> None:
        # No hard-delete on the HTTP surface; a tombstone write (empty payload
        # cannot pass validation, so overwrite with a self-identifying blank the
        # layer treats as absent) is a follow-up. Left explicit rather than
        # silently no-op'd. See design note.
        raise NotImplementedError("row deletion over the HTTP client is a follow-up")

    def list_rows(self, prefix: str) -> Iterable[tuple[str, str]]:
        members = self._client.read_set(SEALED_ROW_SET_ID, org=None)
        for member in members.members:
            if _bare_key(member.key).startswith(f"{prefix}."):
                ciphertext = (member.payload or {}).get("ciphertext")
                if isinstance(ciphertext, str):
                    yield member.key, ciphertext


# --------------------------------------------------------------------------- #
# In-process backend. For a dashboard plugin (voice-notes, PM) running INSIDE
# the dashboard process, which reaches settings_ops directly rather than over
# HTTP. It is the warm-server counterpart to ClientBackend:
#
#   - the AUDITED pepper and the sealed-row values are read/written warm via
#     settings_ops, org-scoped to the caller (write_by_key derives the
#     ``<org>:`` writeback prefix; read_set filters to it);
#   - opening the store's SECURED sealed index needs the operator's factor and
#     is NOT something the warm server can do alone — so it is INJECTED (the
#     unlock ceremony, server-broker today / browser-local under auto-rhorp
#     B-1, provides the released 32-byte index). This backend separates
#     OPENING the store (the ceremony's job) from USING it (this class), so it
#     never depends on how the open happens.
# --------------------------------------------------------------------------- #
class OpsBackend:
    """A :class:`SealedBackend` over ``tools.graph.settings_ops`` (in-process).

    ``org`` is the caller's trusted organization (from
    ``organization_scope_from_request``); it scopes every read and derives the
    writeback prefix on every write. ``sealed_index`` is the store's released
    32-byte secret from the unlock ceremony — required to USE the store; the
    open itself is external.
    """

    def __init__(self, org: str | None, *, sealed_index: bytes | None = None):
        self._org = org
        self._sealed_index = sealed_index

    def _ops(self):
        from tools.graph import settings_ops
        return settings_ops

    def read_pepper(self) -> bytes:
        ops = self._ops()
        pepper = self._read_pepper_row(ops)
        if pepper is not None:
            return pepper
        # get-or-create for this scope; write_by_key derives the <org>: prefix
        # and lands the audited cold seal in the operator's personal store.
        ops.write_by_key(
            VAULT_AUDITED_SET_ID, VAULT_CREDENTIAL_REVISION, PEPPER_KEY,
            {"value": secrets.token_bytes(_PEPPER_LEN).hex()},
            org=self._org, state="raw",
        )
        pepper = self._read_pepper_row(ops)
        if pepper is None:
            raise SealedSettingsError(
                "the pepper is absent immediately after mint — the audited "
                "delegate is not warm (vault bring-up / auto-rhorp A-1)"
            )
        return pepper

    def _read_pepper_row(self, ops) -> bytes | None:
        members = ops.read_set(VAULT_AUDITED_SET_ID, org=self._org, peers=[])
        member = next(
            (m for m in members.members if _bare_key(m.key) == PEPPER_KEY), None
        )
        if member is None:
            return None
        failure = getattr(member, "vault_error", None)
        if failure is not None:
            if getattr(failure, "reason", "") == "no_key_holder":
                raise VaultLocked(VaultLocked.COLD)
            raise SealedSettingsError(
                f"the pepper could not be released: {getattr(failure, 'reason', '?')}"
            )
        value = (member.payload or {}).get("value")
        if not isinstance(value, str):
            raise SealedSettingsError("the pepper row carries no value")
        return bytes.fromhex(value)

    def read_sealed_index(self, address: str, *, block: bool) -> bytes | None:
        # The warm server cannot open a SECURED credential without the
        # operator's factor; the released index is injected by the unlock.
        if self._sealed_index is None:
            raise VaultLocked(VaultLocked.PENDING)
        return self._sealed_index

    def mint_sealed_index(self, address: str, value_hex: str) -> None:
        # Store creation (a cold personal-secured seal + factor-gated open) is
        # the unlock ceremony's job, not the warm server's.
        raise NotImplementedError(
            "creating a store's sealed index is the unlock ceremony's job"
        )

    def read_row(self, row_key: str) -> str | None:
        ops = self._ops()
        members = ops.read_set(SEALED_ROW_SET_ID, org=self._org, peers=[])
        for m in members.members:
            if _bare_key(m.key) == row_key:
                return (m.payload or {}).get("ciphertext")
        return None

    def write_row(self, row_key: str, ciphertext: str) -> str:
        ops = self._ops()
        ops.write_by_key(
            SEALED_ROW_SET_ID, SEALED_ROW_REVISION, row_key,
            {"ciphertext": ciphertext}, org=self._org, state="raw",
        )
        resolved = self.read_row(row_key)
        return resolved if resolved is not None else ciphertext

    def delete_row(self, row_key: str) -> None:
        raise NotImplementedError("row deletion is a follow-up (tombstone)")

    def list_rows(self, prefix: str) -> Iterable[tuple[str, str]]:
        ops = self._ops()
        members = ops.read_set(SEALED_ROW_SET_ID, org=self._org, peers=[])
        for m in members.members:
            if _bare_key(m.key).startswith(f"{prefix}."):
                ciphertext = (m.payload or {}).get("ciphertext")
                if isinstance(ciphertext, str):
                    yield m.key, ciphertext


# --------------------------------------------------------------------------- #
# Vault bring-up hook (in-process, dashboard side).
# --------------------------------------------------------------------------- #
def ensure_pepper_minted() -> bool:
    """Mint the OPERATOR-PERSONAL pepper at bring-up if absent. Returns True on mint.

    This is the personal-scope pepper (org=None → unprefixed), for the
    operator's own personal sealed stores. Per-ORG peppers are minted lazily
    by :meth:`ClientBackend.read_pepper` (get-or-create) the first time an org
    session uses a sealed store — bring-up runs as the operator and cannot
    mint for orgs it does not yet know. Called once per unlock, AFTER the
    personal audited delegate recipient is published. Presence short-circuits
    without writing: re-minting would rotate the pepper and orphan every
    store's address, so there is deliberately no rotate path.
    """
    from tools.graph import settings_ops

    members = settings_ops.read_set(
        VAULT_AUDITED_SET_ID, org=None, peers=[]
    )
    if any(_bare_key(m.key) == PEPPER_KEY for m in members.members):
        return False
    # add_setting, not upsert_by_key: vault rows are encrypted object
    # revisions and the substrate refuses in-place rewrites of them. The
    # ensure-if-absent check above is what makes this a first-write; any
    # later change would be override_setting, which this function must never
    # grow — a rotated pepper orphans every store address.
    settings_ops.add_setting(
        VAULT_AUDITED_SET_ID,
        VAULT_CREDENTIAL_REVISION,
        PEPPER_KEY,
        {"value": secrets.token_bytes(_PEPPER_LEN).hex()},
        org=None,
    )
    return True
