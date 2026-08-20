"""Passkey enrollment statements — the root's signed claim about a credential.

Security comes from VERIFICATION AT USE rather than from the store being
trustworthy (crib B1), so a row in ``autonomy.identity.passkey`` proves nothing
on its own. This module is what turns it into evidence.

Note on the threat model, because an earlier version of this docstring stated it
too broadly. A coopted LOCAL agent cannot edit a passkey row today, on three
independent guards: both identity sets are in
``settings_ops.PROTECTED_IDENTITY_SET_IDS`` so the generic settings API refuses
them without the identity-route capability; both passkey registration routes are
in ``unlock_routes._GATED_API_PATHS`` so they sit behind the human unlock rather
than the open agent surface; and ``POST /api/identity/personal`` refuses
overwrite with 409, so ``root_pub`` cannot be swapped once it exists.

So this module's live value is NOT the local agent. It is:

* **ingest** — a row arriving from another machine enters through none of those
  three guards, and
* **use** — a signature checked at the moment of sealing survives a compromise
  of whatever wrote the row, whenever that happens and however it got there.

Entry validation and use validation are different jobs. A design that verifies
only on the way in is precisely a design that trusts the store afterwards.

A statement is one root-signed record binding, together:

* which credential this is (``credential_id``, ``credential_public_key``),
* where it may be asserted (``rp_id``, ``origin``),
* what the operator SEES when choosing it (``label``, ``transports``,
  ``aaguid``) — see below,
* its counter at enrollment (``initial_sign_count``),
* and — only when the authenticator supports the WebAuthn PRF extension —
  ``provisioning_public_key``: the X25519 encapsulation public half derived
  from that credential's PRF output.

## Why the label is signed

``label`` is the text a human reads when deciding which device to trust,
promote or revoke. Signing the address while leaving the NAME forgeable
protects the wrong half of the decision: relabel an attacker's credential
"Jeremy's YubiKey" and the operator authorises the following ceremony against
it, correctly, having been told a lie by the only part of the record they can
read.

Consequence worth stating plainly: renaming a device therefore mints a NEW
statement, and that is a root ceremony. This is not the periodic re-signing the
design rejects — it is a deliberate act, and a rename genuinely does change what
the operator will consent to.

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

## Wire compatibility

There is none to preserve, deliberately. Statements minted before
``initial_sign_count`` became part of the binding do not parse, and that is the
correct outcome rather than an oversight: accepting one would mean accepting a
counter floor nobody signed, which is exactly the guarantee the field exists to
give. No such statement ever reached production. A parser default here would
have bought compatibility with a handful of throwaway test artifacts at the
price of a silent hole.
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
    "initial_sign_count",
)


class EnrollmentError(MalformedError):
    """A statement is structurally wrong, or does not bind what was claimed."""


def _require_hex(value, pattern, length, what: str) -> None:
    if not isinstance(value, str) or not pattern.match(value):
        raise EnrollmentError(f"{what} must be {length} lowercase hex characters")


def _require_int(value, what: str, *, minimum: int = 0) -> None:
    """A strict integer. ``bool`` is deliberately excluded: Python makes
    ``True == 1`` and ``isinstance(True, int)`` true, so a naive check accepts a
    record whose wire form is ``true`` where a number belongs. The root would
    have signed it quite happily, and a second implementation reading the same
    bytes is free to reject it — which is a divergence between two verifiers
    that both believe they agree.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise EnrollmentError(f"{what} must be an integer >= {minimum}, not a boolean")


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
    #: What the authenticator's counter read AT ENROLLMENT. A static statement
    #: cannot bind the LIVE counter — that would need re-signing per assertion,
    #: which this record deliberately does not do. What it gives is a signed
    #: floor: a row claiming a counter below this one is lying about its own
    #: history. Anything that later checks monotonicity must compare against
    #: this, not against a previous row read.
    initial_sign_count: int = 0
    label: str | None = None  # what a HUMAN reads when choosing this device
    transports: tuple = ()
    aaguid: str | None = None

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
        payload["initial_sign_count"] = self.initial_sign_count
        if self.provisioning_public_key is not None:
            payload["provisioning_public_key"] = self.provisioning_public_key
        # Omitted rather than nulled, on the same discipline as the
        # provisioning key: absence is part of what the root committed to, so a
        # row that grows one of these no longer matches the statement.
        if self.label is not None:
            payload["label"] = self.label
        if self.transports:
            payload["transports"] = list(self.transports)
        if self.aaguid is not None:
            payload["aaguid"] = self.aaguid
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
            initial_sign_count=data["initial_sign_count"],
            label=data.get("label"),
            transports=tuple(data.get("transports") or ()),
            aaguid=data.get("aaguid"),
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
    _require_int(statement.version, "version")
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
    _require_int(statement.initial_sign_count, "initial_sign_count")
    if statement.label is not None:
        _require_text(statement.label, "label")
    if not isinstance(statement.transports, (tuple, list)):
        raise EnrollmentError("transports must be a list")
    for i, t in enumerate(statement.transports):
        _require_text(t, f"transports[{i}]")
    if statement.aaguid is not None:
        _require_text(statement.aaguid, "aaguid")
    ts, count = statement.created_hlc
    _require_int(ts, "created_hlc[0]")
    _require_int(count, "created_hlc[1]")
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
    initial_sign_count: int,
    provisioning_public_key: str | None = None,
    label: str | None = None,
    transports: tuple = (),
    aaguid: str | None = None,
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
        initial_sign_count=initial_sign_count,
        label=label,
        transports=tuple(transports),
        aaguid=aaguid,
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

    .. warning::

       **This function is exactly as strong as where you got ``root_pub``.**
       The fatal call is one line and reads as verification::

           verify(statement, root_pub=statement.signer)   # signer == signer

       That accepts anything, signed by anybody, addressed anywhere — and it
       passes every test in this suite, because the tests supply a correct
       ``root_pub`` and are asking a different question. Reading it from the row
       under verification, or from any settings row a caller can influence, is
       the same mistake by a longer route.

       It must come from ``autonomy.identity.personal``: that set is protected
       in ``settings_ops`` and the create route refuses overwrite with 409, so
       it is the one value a caller cannot substitute.

       Identified by the Test & Automation pillar as a FORWARD seam: it cannot
       be reached today only because the passkey arm has no non-test caller,
       which means it gets created at the moment somebody wires the route. See
       ``graph://e9de411a-366``.
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
