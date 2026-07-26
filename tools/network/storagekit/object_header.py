"""Object-key header: per-object content-encryption-key wrap.

Each content body is authenticated-encrypted exactly once under its own
content-encryption key (CEK) and addressed by the SHA-256 of its
ciphertext (contract §1). The header is the signed record that carries
that CEK wrapped under a key derived from a storage-state secret, so
the CEK is recoverable only by a holder of the correct state secret in
the correct object context (§7).

Binding layout — header and body are inseparable:

- the body associated data binds every identifier except
  ``ciphertext_hash`` (which does not exist until the body is sealed);
- the wrap info and associated data bind ``ciphertext_hash`` too, so a
  header replayed against a different blob, object, revision, state,
  domain, or organization fails authentication at unwrap.

Fold-based write-authority and loss-coverage checks, and state-secret
recovery through bridges, belong to the acceptance-procedures and
read-path layers.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
from dataclasses import dataclass, fields, replace

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCMSIV, ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDFExpand

from tools.network.idkit import KeyPair, canonical_json
from tools.network.idkit import verify_signature as idkit_verify_signature
from tools.network.idkit.errors import IdkitError

from . import suites
from .errors import MalformedRecordError, RecordSignatureError, StorageError
from .records import parse_canonical, record_id, signing_input
from .state import STATE_SECRET_LEN, _require_hex, _require_id_tuple

OBJECT_HEADER_VERSION = 1
OBJECT_HEADER_DOMAIN = b"autonomy.storage.object-header.v1\n"
WRAP_INFO_LABEL = b"autonomy/object-wrap/v1"
WRAP_AAD_DOMAIN = b"autonomy.storage.object-wrap.aad.v1\n"
BODY_AAD_DOMAIN = b"autonomy.storage.object-body.aad.v1\n"
CEK_LEN = 32
NONCE_LEN = 12

_ID_HEX_LEN = 64
_KEY_HEX_LEN = 64
_SIG_HEX_LEN = 128
_WRAPPED_CEK_LEN = CEK_LEN + 16


class ObjectHeaderError(StorageError):
    """The wrap or body does not open — wrong secret/key or altered context."""


_BODY_AEADS = {
    suites.BODY_SUITE_DEFAULT: AESGCMSIV,
    suites.BODY_SUITE_LARGE: ChaCha20Poly1305,
}


def _require_b64(value: object, length: int, what: str) -> bytes:
    if not isinstance(value, str):
        raise MalformedRecordError(f"{what} must be a base64 string")
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MalformedRecordError(f"{what} does not base64-decode") from exc
    if len(raw) != length:
        raise MalformedRecordError(f"{what} must decode to exactly {length} bytes")
    if base64.b64encode(raw).decode("ascii") != value:
        raise MalformedRecordError(f"{what} is not canonical base64")
    return raw


def _require_bytes(value: object, length: int, what: str) -> bytes:
    if not isinstance(value, bytes) or len(value) != length:
        raise MalformedRecordError(f"{what} must be {length} raw bytes")
    return value


@dataclass(frozen=True)
class ObjectKeyHeader:
    version: int
    body_suite_id: str
    wrap_suite_id: str
    genesis_id: str
    domain_id: str
    object_id: str
    revision_id: str
    ciphertext_hash: str
    storage_state_id: str
    writer_authority_heads: tuple
    body_nonce: str
    wrap_nonce: str
    wrapped_cek: str
    author_persona: str
    signature: str

    def signed_dict(self) -> dict:
        d = {f.name: getattr(self, f.name) for f in fields(self)}
        del d["signature"]
        d["writer_authority_heads"] = list(d["writer_authority_heads"])
        return d

    def signing_input(self) -> bytes:
        return signing_input(OBJECT_HEADER_DOMAIN, self.signed_dict())

    def to_json(self) -> bytes:
        return canonical_json({**self.signed_dict(), "signature": self.signature})

    @property
    def header_id(self) -> str:
        return record_id(self.to_json())

    @classmethod
    def from_json(cls, wire: bytes) -> "ObjectKeyHeader":
        data = parse_canonical(_FIELDS, wire)
        if not isinstance(data["writer_authority_heads"], list):
            raise MalformedRecordError("writer_authority_heads must be a list")
        data["writer_authority_heads"] = tuple(data["writer_authority_heads"])
        header = cls(**data)
        _check_structure(header)
        return header


_FIELDS = tuple(f.name for f in fields(ObjectKeyHeader))


def _check_structure(header: ObjectKeyHeader) -> None:
    if header.version != OBJECT_HEADER_VERSION:
        raise MalformedRecordError(f"unsupported header version: {header.version!r}")
    suites.require_suite(header.body_suite_id, suites.BODY_SUITES)
    suites.require_suite(header.wrap_suite_id, suites.WRAP_SUITES)
    for name in (
        "genesis_id",
        "domain_id",
        "object_id",
        "revision_id",
        "ciphertext_hash",
        "storage_state_id",
    ):
        _require_hex(getattr(header, name), _ID_HEX_LEN, name)
    _require_id_tuple(header.writer_authority_heads, "writer_authority_heads")
    _require_b64(header.body_nonce, NONCE_LEN, "body_nonce")
    _require_b64(header.wrap_nonce, NONCE_LEN, "wrap_nonce")
    _require_b64(header.wrapped_cek, _WRAPPED_CEK_LEN, "wrapped_cek")
    _require_hex(header.author_persona, _KEY_HEX_LEN, "author_persona")
    _require_hex(header.signature, _SIG_HEX_LEN, "signature")


# -- derivation contexts ------------------------------------------------------------


def _body_aad(
    body_suite_id, genesis_id, domain_id, object_id, revision_id, storage_state_id
) -> bytes:
    return BODY_AAD_DOMAIN + canonical_json(
        {
            "version": OBJECT_HEADER_VERSION,
            "genesis_id": genesis_id,
            "domain_id": domain_id,
            "object_id": object_id,
            "revision_id": revision_id,
            "storage_state_id": storage_state_id,
            "body_suite_id": body_suite_id,
        }
    )


def _wrap_context(
    genesis_id, domain_id, storage_state_id, object_id, revision_id, ciphertext_hash
) -> tuple:
    info = WRAP_INFO_LABEL + canonical_json(
        [genesis_id, domain_id, storage_state_id, object_id, revision_id, ciphertext_hash]
    )
    aad = WRAP_AAD_DOMAIN + canonical_json(
        {
            "version": OBJECT_HEADER_VERSION,
            "genesis_id": genesis_id,
            "domain_id": domain_id,
            "storage_state_id": storage_state_id,
            "object_id": object_id,
            "revision_id": revision_id,
            "ciphertext_hash": ciphertext_hash,
            "wrap_suite_id": suites.WRAP_SUITE,
        }
    )
    return info, aad


def _wrap_key(info: bytes, state_secret: bytes) -> bytes:
    return HKDFExpand(algorithm=hashes.SHA256(), length=32, info=info).derive(state_secret)


# -- body ---------------------------------------------------------------------------


def seal_body(
    cek: bytes,
    plaintext: bytes,
    *,
    body_suite_id: str,
    body_nonce: bytes,
    genesis_id: str,
    domain_id: str,
    object_id: str,
    revision_id: str,
    storage_state_id: str,
) -> bytes:
    """AEAD the plaintext under the CEK with every identifier bound."""
    _require_bytes(cek, CEK_LEN, "content encryption key")
    _require_bytes(body_nonce, NONCE_LEN, "body_nonce")
    if not isinstance(plaintext, (bytes, bytearray)):
        raise MalformedRecordError("plaintext must be bytes")
    suites.require_suite(body_suite_id, suites.BODY_SUITES)
    aad = _body_aad(
        body_suite_id, genesis_id, domain_id, object_id, revision_id, storage_state_id
    )
    return _BODY_AEADS[body_suite_id](cek).encrypt(body_nonce, bytes(plaintext), aad)


def open_body(header: ObjectKeyHeader, cek: bytes, body_blob: bytes) -> bytes:
    """Verify the content address, then open the blob under the header's
    own context. Fails closed on any mismatch."""
    _require_bytes(cek, CEK_LEN, "content encryption key")
    if not isinstance(body_blob, (bytes, bytearray)):
        raise MalformedRecordError("body blob must be bytes")
    if header.version != OBJECT_HEADER_VERSION:
        raise MalformedRecordError(f"unsupported header version: {header.version!r}")
    suites.require_suite(header.body_suite_id, suites.BODY_SUITES)
    blob = bytes(body_blob)
    if hashlib.sha256(blob).hexdigest() != header.ciphertext_hash:
        raise ObjectHeaderError("body blob does not match the header content address")
    nonce = _require_b64(header.body_nonce, NONCE_LEN, "body_nonce")
    aad = _body_aad(
        header.body_suite_id,
        header.genesis_id,
        header.domain_id,
        header.object_id,
        header.revision_id,
        header.storage_state_id,
    )
    try:
        return _BODY_AEADS[header.body_suite_id](cek).decrypt(nonce, blob, aad)
    except (InvalidTag, ValueError) as exc:
        raise ObjectHeaderError("body does not open in this header's context") from exc


# -- wrap ---------------------------------------------------------------------------


def wrap_cek(
    state_secret: bytes,
    cek: bytes,
    *,
    genesis_id: str,
    domain_id: str,
    storage_state_id: str,
    object_id: str,
    revision_id: str,
    ciphertext_hash: str,
    wrap_nonce: bytes,
) -> bytes:
    """Encrypt the CEK under the state-secret-derived, context-bound key."""
    _require_bytes(state_secret, STATE_SECRET_LEN, "state secret")
    _require_bytes(cek, CEK_LEN, "content encryption key")
    _require_bytes(wrap_nonce, NONCE_LEN, "wrap_nonce")
    info, aad = _wrap_context(
        genesis_id, domain_id, storage_state_id, object_id, revision_id, ciphertext_hash
    )
    return AESGCMSIV(_wrap_key(info, state_secret)).encrypt(wrap_nonce, cek, aad)


def unwrap_cek(state_secret: bytes, header: ObjectKeyHeader) -> bytes:
    """Recover the CEK with the state secret in the header's own context."""
    _require_bytes(state_secret, STATE_SECRET_LEN, "state secret")
    if header.version != OBJECT_HEADER_VERSION:
        raise MalformedRecordError(f"unsupported header version: {header.version!r}")
    suites.require_suite(header.wrap_suite_id, suites.WRAP_SUITES)
    nonce = _require_b64(header.wrap_nonce, NONCE_LEN, "wrap_nonce")
    ct = _require_b64(header.wrapped_cek, _WRAPPED_CEK_LEN, "wrapped_cek")
    info, aad = _wrap_context(
        header.genesis_id,
        header.domain_id,
        header.storage_state_id,
        header.object_id,
        header.revision_id,
        header.ciphertext_hash,
    )
    try:
        cek = AESGCMSIV(_wrap_key(info, state_secret)).decrypt(nonce, ct, aad)
    except (InvalidTag, ValueError) as exc:
        raise ObjectHeaderError(
            "content key does not unwrap with that state secret in this context"
        ) from exc
    if len(cek) != CEK_LEN:
        raise ObjectHeaderError("unwrapped value is not a content encryption key")
    return cek


# -- record -------------------------------------------------------------------------


def build(
    author: KeyPair,
    state_secret: bytes,
    cek: bytes,
    *,
    genesis_id: str,
    domain_id: str,
    object_id: str,
    revision_id: str,
    storage_state_id: str,
    writer_authority_heads,
    body_suite_id: str,
    body_nonce: bytes,
    wrap_nonce: bytes,
    ciphertext_hash: str,
) -> ObjectKeyHeader:
    """Wrap the CEK and assemble the signed header for a sealed body."""
    suites.require_suite(body_suite_id, suites.BODY_SUITES)
    _require_bytes(body_nonce, NONCE_LEN, "body_nonce")
    wrapped = wrap_cek(
        state_secret,
        cek,
        genesis_id=_require_hex(genesis_id, _ID_HEX_LEN, "genesis_id"),
        domain_id=_require_hex(domain_id, _ID_HEX_LEN, "domain_id"),
        storage_state_id=_require_hex(storage_state_id, _ID_HEX_LEN, "storage_state_id"),
        object_id=_require_hex(object_id, _ID_HEX_LEN, "object_id"),
        revision_id=_require_hex(revision_id, _ID_HEX_LEN, "revision_id"),
        ciphertext_hash=_require_hex(ciphertext_hash, _ID_HEX_LEN, "ciphertext_hash"),
        wrap_nonce=wrap_nonce,
    )
    unsigned = ObjectKeyHeader(
        version=OBJECT_HEADER_VERSION,
        body_suite_id=body_suite_id,
        wrap_suite_id=suites.WRAP_SUITE,
        genesis_id=genesis_id,
        domain_id=domain_id,
        object_id=object_id,
        revision_id=revision_id,
        ciphertext_hash=ciphertext_hash,
        storage_state_id=storage_state_id,
        writer_authority_heads=_require_id_tuple(
            sorted(set(writer_authority_heads)), "writer_authority_heads"
        ),
        body_nonce=base64.b64encode(body_nonce).decode("ascii"),
        wrap_nonce=base64.b64encode(wrap_nonce).decode("ascii"),
        wrapped_cek=base64.b64encode(wrapped).decode("ascii"),
        author_persona=author.public_hex,
        signature="0" * _SIG_HEX_LEN,
    )
    return replace(unsigned, signature=author.sign_hex(unsigned.signing_input()))


def verify_structure(record) -> ObjectKeyHeader:
    """Foundation parse (for wire bytes) plus the author signature.

    Accepts the canonical wire or an :class:`ObjectKeyHeader`; returns
    the verified header. Fold-based write authority is checked elsewhere.
    """
    if isinstance(record, (bytes, bytearray)):
        header = ObjectKeyHeader.from_json(bytes(record))
    elif isinstance(record, ObjectKeyHeader):
        header = record
        _check_structure(header)
    else:
        raise MalformedRecordError("record must be wire bytes or an ObjectKeyHeader")
    try:
        idkit_verify_signature(
            header.author_persona, header.signature, header.signing_input()
        )
    except IdkitError as exc:
        raise RecordSignatureError("header signature does not verify") from exc
    return header
