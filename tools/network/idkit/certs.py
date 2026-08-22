"""Delegation certificates.

A delegation certificate is a canonical-JSON-signed statement by a *parent*
key that a *child* public key may act with a given scope, for a given org
and subject, within a validity window. The parent's own certificate is
embedded (``parent_cert``), so a leaf cert carries its entire chain — the
verifier needs nothing but the org's root public key (invariant I4: no
permission tables; authority travels with the request).

Signature input is domain-separated::

    sig = Ed25519_sign(parent_priv, CERT_DOMAIN || canonical_json(payload))

where ``payload`` is the cert dict *without* the ``sig`` field. Domain
separation prevents a signature minted for one object kind (cert vs
revocation) from being replayed as the other.

Wire form is the canonical JSON of the full dict including ``sig``.
Parsing is strict: unknown fields, wrong types, unsorted or duplicate
scope entries, and out-of-range timestamps are all rejected — everything
a signature covers has exactly one accepted byte form.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from .canonical import canonical_json
from .errors import (
    MalformedError,
    ScopeEscalationError,
    TTLViolationError,
)
from .keys import KeyPair, SIGNATURE_HEX_LEN, PUBLIC_KEY_HEX_LEN, _decode_hex

CERT_DOMAIN = b"autonomy.idkit.cert.v1\n"
CERT_VERSION = 1

#: Hard ceiling on embedded chain depth (root-signed hop = depth 1).
#: Real chains are 2–3 hops (root -> session -> agent); the cap exists so a
#: hostile blob cannot make the verifier recurse or loop unboundedly.
MAX_CHAIN_DEPTH = 16

SUBJECT_KINDS = frozenset({"operator", "agent", "persona", "machine"})

_MAX_TS = 2**63 - 1
_MAX_SCOPE_ENTRIES = 64
_MAX_STR = 256

_CERT_FIELDS = frozenset(
    {
        "v",
        "child_pub",
        "scope",
        "target_types",
        "org",
        "subject",
        "not_before",
        "not_after",
        "parent_cert",
        "sig",
    }
)


def _require_str(value: object, what: str, max_len: int = _MAX_STR) -> str:
    if not isinstance(value, str) or not value or len(value) > max_len:
        raise MalformedError(f"{what} must be a non-empty string of at most {max_len} chars")
    return value


def _require_ts(value: object, what: str) -> int:
    # bool is an int subclass; reject it explicitly.
    if type(value) is not int or value < 0 or value > _MAX_TS:
        raise MalformedError(f"{what} must be an integer unix timestamp in [0, 2**63)")
    return value


def _require_scope_list(value: object, what: str) -> tuple:
    if not isinstance(value, list) or not value or len(value) > _MAX_SCOPE_ENTRIES:
        raise MalformedError(f"{what} must be a non-empty list of at most {_MAX_SCOPE_ENTRIES} entries")
    for entry in value:
        _require_str(entry, f"{what} entry", max_len=128)
    if value != sorted(set(value)):
        raise MalformedError(f"{what} must be sorted and free of duplicates")
    return tuple(value)


@dataclass(frozen=True)
class Subject:
    kind: str
    id: str

    def to_dict(self) -> dict:
        return {"kind": self.kind, "id": self.id}

    @classmethod
    def from_dict(cls, data: object) -> "Subject":
        if not isinstance(data, dict) or set(data) != {"kind", "id"}:
            raise MalformedError("subject must be an object with exactly {kind, id}")
        kind = _require_str(data["kind"], "subject.kind", max_len=32)
        if kind not in SUBJECT_KINDS:
            raise MalformedError(f"subject.kind must be one of {sorted(SUBJECT_KINDS)}")
        return cls(kind=kind, id=_require_str(data["id"], "subject.id"))


@dataclass(frozen=True)
class DelegationCert:
    """One hop of a delegation chain, with its ancestry embedded."""

    child_pub: str
    scope: tuple
    org: str
    subject: Subject
    not_before: int
    not_after: int
    sig: str
    target_types: Optional[tuple] = None
    parent_cert: Optional["DelegationCert"] = None
    v: int = CERT_VERSION

    # -- serialization -----------------------------------------------------

    def payload_dict(self) -> dict:
        """The signed portion: everything except ``sig``."""
        payload = {
            "v": self.v,
            "child_pub": self.child_pub,
            "scope": list(self.scope),
            "org": self.org,
            "subject": self.subject.to_dict(),
            "not_before": self.not_before,
            "not_after": self.not_after,
        }
        if self.target_types is not None:
            payload["target_types"] = list(self.target_types)
        if self.parent_cert is not None:
            payload["parent_cert"] = self.parent_cert.to_dict()
        return payload

    def payload_bytes(self) -> bytes:
        return canonical_json(self.payload_dict())

    def signing_input(self) -> bytes:
        return CERT_DOMAIN + self.payload_bytes()

    def to_dict(self) -> dict:
        data = self.payload_dict()
        data["sig"] = self.sig
        return data

    def to_json(self) -> bytes:
        """Canonical wire bytes (full cert including signature)."""
        return canonical_json(self.to_dict())

    # -- parsing -----------------------------------------------------------

    @classmethod
    def from_dict(cls, data: object, _depth: int = 1) -> "DelegationCert":
        if _depth > MAX_CHAIN_DEPTH:
            raise MalformedError(f"delegation chain exceeds MAX_CHAIN_DEPTH={MAX_CHAIN_DEPTH}")
        if not isinstance(data, dict):
            raise MalformedError("certificate must be a JSON object")
        unknown = set(data) - _CERT_FIELDS
        if unknown:
            raise MalformedError(f"certificate carries unknown fields: {sorted(unknown)}")
        missing = _CERT_FIELDS - set(data) - {"target_types", "parent_cert"}
        if missing:
            raise MalformedError(f"certificate is missing fields: {sorted(missing)}")

        if data["v"] != CERT_VERSION:
            raise MalformedError(f"unsupported certificate version: {data['v']!r}")

        child_pub = data["child_pub"]
        _decode_hex(child_pub, PUBLIC_KEY_HEX_LEN, "child_pub")
        sig = data["sig"]
        _decode_hex(sig, SIGNATURE_HEX_LEN, "sig")

        scope = _require_scope_list(data["scope"], "scope")
        target_types = None
        if "target_types" in data:
            target_types = _require_scope_list(data["target_types"], "target_types")

        org = _require_str(data["org"], "org", max_len=128)
        subject = Subject.from_dict(data["subject"])
        not_before = _require_ts(data["not_before"], "not_before")
        not_after = _require_ts(data["not_after"], "not_after")
        if not_before >= not_after:
            raise MalformedError("not_before must be strictly before not_after")

        parent_cert = None
        if "parent_cert" in data:
            parent_cert = cls.from_dict(data["parent_cert"], _depth=_depth + 1)

        return cls(
            child_pub=child_pub,
            scope=scope,
            org=org,
            subject=subject,
            not_before=not_before,
            not_after=not_after,
            sig=sig,
            target_types=target_types,
            parent_cert=parent_cert,
        )

    @classmethod
    def from_json(cls, raw) -> "DelegationCert":
        """Parse canonical wire bytes. Strictly anti-malleable: any byte
        form other than the one :meth:`to_json` produces — reordered keys,
        whitespace, unicode escapes, duplicate keys — is rejected, so a
        given certificate has exactly one accepted wire encoding."""
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise MalformedError("certificate bytes are not valid UTF-8") from exc
        if not isinstance(raw, str):
            raise MalformedError("certificate JSON must be str or bytes")
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise MalformedError("certificate is not valid JSON") from exc
        cert = cls.from_dict(data)
        if cert.to_json() != raw.encode("utf-8"):
            raise MalformedError("certificate is not in canonical wire form")
        return cert

    # -- chain access --------------------------------------------------------

    def chain(self) -> list:
        """Certs from the root-signed hop down to (and including) this leaf."""
        certs = []
        cursor: Optional[DelegationCert] = self
        while cursor is not None:
            certs.append(cursor)
            cursor = cursor.parent_cert
            if len(certs) > MAX_CHAIN_DEPTH:
                raise MalformedError(f"delegation chain exceeds MAX_CHAIN_DEPTH={MAX_CHAIN_DEPTH}")
        certs.reverse()
        return certs

    @property
    def key_id(self) -> str:
        """Key id of the key this cert delegates TO."""
        return self.child_pub


def issue_cert(
    parent: KeyPair,
    child_pub: str,
    *,
    scope,
    org: str,
    subject: Subject,
    not_before: int,
    not_after: int,
    target_types=None,
    parent_cert: Optional[DelegationCert] = None,
) -> DelegationCert:
    """Mint a delegation certificate signed by *parent*.

    If *parent_cert* is None, *parent* is the org root key. Otherwise
    *parent_cert* must be the certificate that delegated to *parent*
    (``parent_cert.child_pub == parent.public_hex``), and the narrowing
    rules are enforced at issuance too — a well-behaved issuer never mints
    a cert that verification would reject:

    - scope must be a strict subset of the parent's scope
    - the validity window must sit inside the parent's, with a strictly
      earlier ``not_after``
    - target_types must not escalate past the parent's

    Issuance-time checks are a convenience; :func:`~.verify.verify_chain`
    re-enforces all of them and is the security boundary.
    """
    scope = _require_scope_list(sorted(set(scope)), "scope")
    if target_types is not None:
        target_types = _require_scope_list(sorted(set(target_types)), "target_types")
    _require_str(org, "org", max_len=128)
    _decode_hex(child_pub, PUBLIC_KEY_HEX_LEN, "child_pub")
    not_before = _require_ts(not_before, "not_before")
    not_after = _require_ts(not_after, "not_after")
    if not_before >= not_after:
        raise MalformedError("not_before must be strictly before not_after")

    if parent_cert is not None:
        if parent_cert.child_pub != parent.public_hex:
            raise MalformedError("parent_cert does not delegate to the signing key")
        if len(parent_cert.chain()) >= MAX_CHAIN_DEPTH:
            raise MalformedError(f"delegation chain would exceed MAX_CHAIN_DEPTH={MAX_CHAIN_DEPTH}")
        if org != parent_cert.org:
            raise MalformedError("child org must match parent org")
        if not set(scope) < set(parent_cert.scope):
            raise ScopeEscalationError("child scope must be a strict subset of parent scope")
        if not_before < parent_cert.not_before or not_after >= parent_cert.not_after:
            raise TTLViolationError(
                "child validity window must sit inside the parent's with a strictly earlier not_after"
            )
        if parent_cert.target_types is not None:
            if target_types is None or not set(target_types) <= set(parent_cert.target_types):
                raise ScopeEscalationError("child target_types must not escalate past parent target_types")

    unsigned = DelegationCert(
        child_pub=child_pub,
        scope=scope,
        org=org,
        subject=subject,
        not_before=not_before,
        not_after=not_after,
        sig="0" * SIGNATURE_HEX_LEN,
        target_types=target_types,
        parent_cert=parent_cert,
    )
    sig = parent.sign_hex(unsigned.signing_input())
    return DelegationCert(
        child_pub=child_pub,
        scope=scope,
        org=org,
        subject=subject,
        not_before=not_before,
        not_after=not_after,
        sig=sig,
        target_types=target_types,
        parent_cert=parent_cert,
    )
