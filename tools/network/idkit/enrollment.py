"""Passkey enrollment statements — the root's signed claim about a credential.

The settings store is agent-writable by design: the adversary in scope is a
coopted local agent, and security comes from VERIFICATION AT USE rather than
from the store being trustworthy (crib B1). So a row in
``autonomy.identity.passkey`` proves nothing on its own. This module is what
turns it into evidence.

A statement is one root-signed record binding, together:

* which credential this is (``credential_id``, ``credential_public_key``),
* where it may be asserted (``rp_id``, ``origin``),
* and — only when the authenticator supports the WebAuthn PRF extension —
  ``provisioning_public_key``: the X25519 encapsulation public half derived
  from that credential's PRF output.

## Why one record rather than loose columns

``PersonaKemCredential.binding_dict()`` exists because a verifier handed
separate fields can check a PARTIAL binding and believe it checked the whole
thing. The same failure is available here and is worse: the field that matters
is an address secrets get sealed to. So there is exactly one signed object, its
signature covers every field, and there is no way to verify half of it.

## The rule this exists to enforce

**An unverified provisioning public key must never receive a seal.**

Every ceremony that seals to a passkey — master-KEK re-wrap on revocation,
cross-device key provisioning — must call :func:`verify` and compare the
statement's ``provisioning_public_key`` against the row it is about to use.
Reading the row's convenience copy alone is the vulnerability, not a shortcut:
an agent can write any public key it likes into the row, and material sealed to
it would open for the agent instead of the operator.

## Absence is bound, not merely permitted

``provisioning_public_key`` is omitted from the signed payload entirely when the
credential has no PRF support, rather than being signed as null. That is what
makes adding one an attack the signature catches: a statement minted without the
field commits to a payload that has no such key, so a row that grows one no
longer matches anything the root said.

There is no path that adds PRF to an already-enrolled credential (``auto-oox5r``,
operator 2026-08-03). A device that wants it enrolls a NEW passkey — which mints
its own statement at the ceremony — and the old row is deleted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields

from .canonical import canonical_json
from .errors import MalformedError, SignatureError
from .keys import KeyPair, load_public_key, verify_signature

#: Domain prefix. Distinct from every other signed record, so a statement can
#: never be replayed as one and vice versa.
ENROLLMENT_DOMAIN = b"autonomy.identity.passkey-enrollment.v1\n"
ENROLLMENT_VERSION = 1

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX128 = re.compile(r"^[0-9a-f]{128}$")

#: Fields that are always present and always signed.
_REQUIRED = (
    "version",
    "credential_id",
    "credential_public_key",
    "rp_id",
    "origin",
    "nonce",
    "created_hlc",
    "signer",
)


class EnrollmentError(MalformedError):
    """A statement is structurally wrong, or does not bind what was claimed."""


def _require_hex(value, pattern, length, what: str) -> None:
    if not isinstance(value, str) or not pattern.match(value):
        raise EnrollmentError(f"{what} must be {length} lowercase hex characters")


def _require_text(value, what: str) -> None:
    if not isinstance(value, str) or not value:
        raise EnrollmentError(f"{what} must be a non-empty string")


@dataclass(frozen=True)
class PasskeyEnrollmentStatement:
    """One credential's enrollment, as the personal root attested it."""

    version: int
    credential_id: str  # base64url of the raw credential id, as WebAuthn gives it
    credential_public_key: str  # COSE key, hex
    rp_id: str
    origin: str
    nonce: str  # 64 hex — single-use, frozen at options time
    created_hlc: tuple  # (ts_ms, count), advisory
    signer: str  # WHICH root key signed. An identifier for selection after
    #              personal-root rotation — never itself an authority claim.
    signature: str
    provisioning_public_key: str | None = None  # absent for non-PRF passkeys

    # ── the signed payload ──

    def binding_dict(self) -> dict:
        """Everything the signature covers.

        ``provisioning_public_key`` is OMITTED when absent rather than carried
        as null, so a statement minted for a non-PRF credential commits to a
        payload in which no such key exists.
        """
        payload = {
            "version": self.version,
            "credential_id": self.credential_id,
            "credential_public_key": self.credential_public_key,
            "rp_id": self.rp_id,
            "origin": self.origin,
            "nonce": self.nonce,
            "created_hlc": list(self.created_hlc),
            "signer": self.signer,
        }
        if self.provisioning_public_key is not None:
            payload["provisioning_public_key"] = self.provisioning_public_key
        return payload

    def signing_input(self) -> bytes:
        return ENROLLMENT_DOMAIN + canonical_json(self.binding_dict())

    def to_dict(self) -> dict:
        return {**self.binding_dict(), "signature": self.signature}

    def to_json(self) -> bytes:
        return canonical_json(self.to_dict())

    @property
    def prf_capable(self) -> bool:
        """Whether this credential can hold key material at all."""
        return self.provisioning_public_key is not None

    @classmethod
    def from_dict(cls, data: dict) -> "PasskeyEnrollmentStatement":
        if not isinstance(data, dict):
            raise EnrollmentError("statement must be a dict")
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise EnrollmentError(f"unknown statement fields: {unknown}")
        missing = sorted((set(_REQUIRED) | {"signature"}) - set(data))
        if missing:
            raise EnrollmentError(f"statement is missing fields: {missing}")
        hlc = data["created_hlc"]
        if not isinstance(hlc, (list, tuple)) or len(hlc) != 2:
            raise EnrollmentError("created_hlc must be a two-element list")
        return cls(
            version=data["version"],
            credential_id=data["credential_id"],
            credential_public_key=data["credential_public_key"],
            rp_id=data["rp_id"],
            origin=data["origin"],
            nonce=data["nonce"],
            created_hlc=tuple(hlc),
            signer=data["signer"],
            signature=data["signature"],
            provisioning_public_key=data.get("provisioning_public_key"),
        )

    @classmethod
    def from_json(cls, wire: bytes) -> "PasskeyEnrollmentStatement":
        import json

        try:
            data = json.loads(bytes(wire).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise EnrollmentError(f"statement is not valid JSON: {exc}") from exc
        return cls.from_dict(data)


def _validate(statement: PasskeyEnrollmentStatement) -> None:
    if statement.version != ENROLLMENT_VERSION:
        raise EnrollmentError(f"unsupported statement version: {statement.version!r}")
    _require_text(statement.credential_id, "credential_id")
    _require_text(statement.credential_public_key, "credential_public_key")
    _require_text(statement.rp_id, "rp_id")
    _require_text(statement.origin, "origin")
    _require_hex(statement.nonce, _HEX64, 64, "nonce")
    _require_hex(statement.signer, _HEX64, 64, "signer")
    _require_hex(statement.signature, _HEX128, 128, "signature")
    if statement.provisioning_public_key is not None:
        _require_hex(
            statement.provisioning_public_key, _HEX64, 64, "provisioning_public_key"
        )
    ts, count = statement.created_hlc
    if not isinstance(ts, int) or not isinstance(count, int) or ts < 0 or count < 0:
        raise EnrollmentError("created_hlc must be two non-negative integers")
    try:
        load_public_key(statement.signer)
    except Exception as exc:  # noqa: BLE001 — surfaced as our own error type
        raise EnrollmentError(f"signer is not a usable public key: {exc}") from exc


def mint(
    *,
    root: KeyPair,
    credential_id: str,
    credential_public_key: str,
    rp_id: str,
    origin: str,
    nonce: str,
    created_hlc: tuple,
    provisioning_public_key: str | None = None,
) -> PasskeyEnrollmentStatement:
    """Sign a statement with the personal root. One static signature, at the
    enrollment ceremony — there is no re-signing and no counter."""
    draft = PasskeyEnrollmentStatement(
        version=ENROLLMENT_VERSION,
        credential_id=credential_id,
        credential_public_key=credential_public_key,
        rp_id=rp_id,
        origin=origin,
        nonce=nonce,
        created_hlc=tuple(created_hlc),
        signer=root.public_hex,
        signature="0" * 128,  # placeholder; not part of binding_dict
        provisioning_public_key=provisioning_public_key,
    )
    _validate(draft)
    signature = root.sign_hex(draft.signing_input())
    return PasskeyEnrollmentStatement(
        **{**{f.name: getattr(draft, f.name) for f in fields(draft)},
           "signature": signature}
    )


def verify(record, *, root_pub: str) -> PasskeyEnrollmentStatement:
    """Structurally validate, then check the signature against *root_pub*.

    *root_pub* is supplied by the caller, which resolved it from the identity it
    trusts. ``signer`` selects WHICH root key after a rotation; it is never
    itself the reason to believe a statement, so a self-consistent statement
    signed by some other key is refused here rather than accepted as valid.
    """
    if isinstance(record, PasskeyEnrollmentStatement):
        statement = record
    elif isinstance(record, (bytes, bytearray)):
        statement = PasskeyEnrollmentStatement.from_json(bytes(record))
    elif isinstance(record, dict):
        statement = PasskeyEnrollmentStatement.from_dict(record)
    else:
        raise EnrollmentError("statement must be wire bytes, a dict, or a statement")

    _validate(statement)
    _require_hex(root_pub, _HEX64, 64, "root_pub")
    if statement.signer != root_pub:
        raise SignatureError(
            "statement was signed by a key that is not this identity's root"
        )
    # raises SignatureError itself; ordering is (public, signature, data)
    verify_signature(statement.signer, statement.signature, statement.signing_input())
    return statement


def verified_provisioning_key(record, *, root_pub: str, row_key: str | None) -> str:
    """The ONLY safe way to obtain an address to seal to.

    Verifies the statement, then requires the row's convenience copy to match
    it. Returns the key, or raises — there is no path through this function that
    yields an unverified address.

    Callers must use this at every sealing site. Reading ``row["provisioning_
    public_key"]`` directly is the vulnerability this module exists to remove.
    """
    statement = verify(record, root_pub=root_pub)
    if statement.provisioning_public_key is None:
        raise EnrollmentError(
            "this credential has no provisioning key: it cannot receive a seal"
        )
    if row_key is not None and row_key != statement.provisioning_public_key:
        raise EnrollmentError(
            "the stored provisioning key does not match the signed statement"
        )
    return statement.provisioning_public_key
