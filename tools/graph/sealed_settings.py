"""SealedSettings — navigate graph settings as if they were unencrypted.

A consumer (a password manager, a notes app) calls ``get`` / ``put`` / ``list``
/ ``delete`` with logical names and plaintext. This layer transparently:

- **hashes the key** into a blind index — ``<domain>:<HMAC(K_index, name)>`` —
  so the substrate never sees the logical name;
- **seals the value** under a metadata key derived from the store's root, so
  the substrate stores only ciphertext;
- **manages the root key** lazily: on first use it is minted (a cold,
  approval-free seal) or released through the existing ``vault_open``
  rendezvous, and cached for the session.

The consumer never sees the hash, the ciphertext, the derived keys, or the
unlock ceremony. Swapping a plain settings client for a ``SealedSettings`` of
the same surface changes no call site.

## No new crypto

Keys are derived from the 32-byte root with HKDF-SHA-256 and values sealed with
ChaCha20-Poly1305 — both from ``cryptography``, the same primitives the vault
already uses. The root itself is an ordinary ``autonomy.vault.secured``
credential (``sealed-settings.<domain>.root``): minted cold on first use and
released later, per session, into the requesting session's ramfs.

## Where sealing lands, and the one audit event

The metadata rows here are sealed by THIS layer under the factor-derived key,
so listing a store is ONE factor release (opening the root), not one audited
release per row. A secret whose every reveal must be audited does NOT belong in
a metadata row — it belongs in ``autonomy.vault.audited`` under the same blind
index, where the substrate's release-is-audit fires per reveal. This module
owns the index + metadata half; the audited-secret half is the existing vault
set, addressed by :func:`blind_index`.

## Runtime seam

The substrate operations (release/mint the root, read/write/list rows) are a
:class:`SealedBackend` so the two runtime contexts — a container session over
the HTTP client, an in-process dashboard plugin — can each supply their own
without the crypto core knowing which it is. :class:`ClientBackend` is the
container/HTTP adapter; it is runtime-critical (its root release and existence
probe only exercise against a live dashboard) and must be validated on a real
run, not only unit-tested.
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
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .schemas.vault_credential import VAULT_SECURED_SET_ID
from .schemas.sealed_row import SEALED_ROW_REVISION, SEALED_ROW_SET_ID

# One fixed salt binds every derivation to this scheme + version, so a root
# reused elsewhere never yields these subkeys and a future scheme revision is a
# clean break rather than a silent key collision.
_HKDF_SALT = b"autonomy.sealed-settings.v1"
_INFO_INDEX = b"index"          # -> K_index, the blind-index HMAC key
_INFO_METADATA = b"metadata"    # -> K_meta, the metadata AEAD key
_ROOT_LEN = 32
_NONCE_LEN = 12


# --------------------------------------------------------------------------- #
# Typed failures — the ONLY thing that surfaces through the abstraction is that
# a sealed read can be unavailable, exactly as a plain settings read can.
# --------------------------------------------------------------------------- #
class SealedSettingsError(RuntimeError):
    """A sealed store operation could not complete."""


class VaultLocked(SealedSettingsError):
    """The root exists but could not be released now.

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


def blind_index(k_index: bytes, name: str) -> str:
    """The base64url HMAC-SHA-256 of a logical name — the row's hidden key.

    Exposed so the audited-secret half of a store can address the same item by
    the same blind index.
    """
    digest = hmac.new(k_index, _canonical(name), sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _seal(k_meta: bytes, plaintext: bytes, aad: bytes) -> str:
    nonce = os.urandom(_NONCE_LEN)
    blob = nonce + ChaCha20Poly1305(k_meta).encrypt(nonce, plaintext, aad)
    return base64.urlsafe_b64encode(blob).decode("ascii")


def _open(k_meta: bytes, ciphertext: str, aad: bytes) -> bytes:
    blob = base64.urlsafe_b64decode(ciphertext.encode("ascii"))
    nonce, sealed = blob[:_NONCE_LEN], blob[_NONCE_LEN:]
    return ChaCha20Poly1305(k_meta).decrypt(nonce, sealed, aad)


# --------------------------------------------------------------------------- #
# Substrate seam.
# --------------------------------------------------------------------------- #
class SealedBackend(Protocol):
    """The substrate operations SealedSettings needs, per runtime context."""

    def read_root(self, secured_key: str, *, block: bool) -> bytes | None:
        """Return the 32-byte root, or ``None`` if no such credential exists.

        Raise :class:`VaultLocked` if the credential exists but cannot be
        released now (pending / denied / cold).
        """

    def mint_root(self, secured_key: str, value_hex: str) -> None:
        """Seal a fresh root (a cold, approval-free personal-secured write)."""

    def read_row(self, row_key: str) -> str | None:
        """Return a row's ``ciphertext`` string, or ``None`` if absent."""

    def write_row(self, row_key: str, ciphertext: str) -> None:
        """Create-or-update a row's ``ciphertext``."""

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


# --------------------------------------------------------------------------- #
# The transparent surface.
# --------------------------------------------------------------------------- #
class SealedSettings:
    """A sealed key/value store over one logical ``domain``.

    ``block`` chooses what a root release does when the operator has not yet
    decided: ``False`` (default) raises ``VaultLocked("PENDING")`` immediately;
    ``True`` holds until the decision (the backend's release call waits).
    """

    def __init__(self, domain: str, backend: SealedBackend, *, block: bool = False):
        if ":" in domain:
            raise ValueError("domain must not contain ':' (it prefixes row keys)")
        self._domain = domain
        self._backend = backend
        self._block = block
        self._root: bytes | None = None

    # -- consumer-facing: identical shape to a plain settings client -------- #
    def get(self, name: str):
        _, k_meta = self._keys()
        ciphertext = self._backend.read_row(self._row_key(name))
        if ciphertext is None:
            return None
        return self._open_item(k_meta, self._row_key(name), ciphertext).metadata

    def put(self, name: str, metadata) -> None:
        _, k_meta = self._keys()
        row_key = self._row_key(name)
        payload = json.dumps(
            {"name": name, "metadata": metadata},
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        self._backend.write_row(row_key, _seal(k_meta, payload, self._aad(row_key)))

    def delete(self, name: str) -> None:
        self._keys()  # ensure the store is unlocked before we mutate it
        self._backend.delete_row(self._row_key(name))

    def list(self) -> list[SealedItem]:
        _, k_meta = self._keys()
        items: list[SealedItem] = []
        for row_key, ciphertext in self._backend.list_rows(self._domain):
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

    # -- internals ---------------------------------------------------------- #
    def _row_key(self, name: str) -> str:
        k_index, _ = self._keys()
        return f"{self._domain}:{blind_index(k_index, name)}"

    def _aad(self, row_key: str) -> bytes:
        # Bind each ciphertext to its own row so a blob cannot be lifted into
        # another key's row and still open.
        return row_key.encode("utf-8")

    def _open_item(self, k_meta: bytes, row_key: str, ciphertext: str) -> SealedItem:
        payload = json.loads(_open(k_meta, ciphertext, self._aad(row_key)))
        return SealedItem(payload["name"], payload["metadata"])

    def _keys(self) -> tuple[bytes, bytes]:
        if self._root is None:
            self._root = self._get_or_create_root()
        return _hkdf(self._root, _INFO_INDEX), _hkdf(self._root, _INFO_METADATA)

    def _secured_key(self) -> str:
        return f"sealed-settings.{self._domain}.root"

    def _get_or_create_root(self) -> bytes:
        secured_key = self._secured_key()
        root = self._backend.read_root(secured_key, block=self._block)
        if root is None:
            # First use: seed the credential (cold write, no approval), then
            # bind to the AUTHORITATIVE root via the same release path. We never
            # use the freshly generated bytes directly, so a concurrent
            # first-mint resolves to one winner both sessions converge on.
            self._backend.mint_root(secured_key, secrets.token_bytes(_ROOT_LEN).hex())
            root = self._backend.read_root(secured_key, block=self._block)
            if root is None:
                raise SealedSettingsError("root key absent immediately after mint")
        if len(root) != _ROOT_LEN:
            raise SealedSettingsError("root key is not 32 bytes")
        return root


# --------------------------------------------------------------------------- #
# Container / HTTP backend. RUNTIME-CRITICAL: the root release and the
# existence probe only exercise against a live dashboard, so this adapter's
# behavior must be confirmed on a real run, not solely by the unit tests
# (which cover the crypto core through a fake backend).
# --------------------------------------------------------------------------- #
class ClientBackend:
    """A :class:`SealedBackend` over ``tools.graph.client`` (a container session)."""

    _PERSONAL = "personal"

    def __init__(self, client, *, ttl_seconds: int = 60):
        self._client = client
        self._ttl = ttl_seconds

    def read_root(self, secured_key: str, *, block: bool) -> bytes | None:
        # Existence probe first: opening a nonexistent secured key cannot be
        # told apart from a denial, so decide absent-vs-locked WITHOUT the
        # ceremony. The row is present even while sealed.
        members = self._client.read_set(VAULT_SECURED_SET_ID, org=self._PERSONAL)
        if secured_key not in {m.key for m in members.members}:
            return None
        try:
            receipt = self._client.request_vault_open(
                VAULT_SECURED_SET_ID, secured_key,
                org=self._PERSONAL, ttl_seconds=self._ttl,
            )
        except PermissionError:
            raise VaultLocked(VaultLocked.DENIED)
        except Exception as exc:  # GraphHttpError et al.
            status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
            if status == 408:
                raise VaultLocked(VaultLocked.PENDING) from exc
            if _looks_cold(exc):
                raise VaultLocked(VaultLocked.COLD) from exc
            raise
        with open(receipt["path"], "rb") as handle:
            return bytes.fromhex(handle.read().decode("ascii").strip())

    def mint_root(self, secured_key: str, value_hex: str) -> None:
        self._client.seal_personal_setting(
            secured_key, value_hex, policy_class_id="personal-root",
        )

    def read_row(self, row_key: str) -> str | None:
        members = self._client.read_set(SEALED_ROW_SET_ID, org=self._PERSONAL)
        for member in members.members:
            if member.key == row_key:
                return (member.payload or {}).get("ciphertext")
        return None

    def write_row(self, row_key: str, ciphertext: str) -> None:
        self._client.add_setting(
            SEALED_ROW_SET_ID, SEALED_ROW_REVISION, row_key,
            {"ciphertext": ciphertext}, state="raw", org=self._PERSONAL,
        )

    def delete_row(self, row_key: str) -> None:
        # No hard-delete on the HTTP surface; a tombstone write (empty payload
        # cannot pass validation, so overwrite with a self-identifying blank the
        # layer treats as absent) is a follow-up. Left explicit rather than
        # silently no-op'd. See design note.
        raise NotImplementedError("row deletion over the HTTP client is a follow-up")

    def list_rows(self, prefix: str) -> Iterable[tuple[str, str]]:
        members = self._client.read_set(SEALED_ROW_SET_ID, org=self._PERSONAL)
        for member in members.members:
            if member.key.startswith(f"{prefix}:"):
                ciphertext = (member.payload or {}).get("ciphertext")
                if isinstance(ciphertext, str):
                    yield member.key, ciphertext


def _looks_cold(exc: Exception) -> bool:
    text = str(exc).lower()
    return "no_key_holder" in text or "vault is locked" in text or "no key" in text
