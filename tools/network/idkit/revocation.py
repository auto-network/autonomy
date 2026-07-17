"""Revocation records.

A revocation record is a signed denylist entry for one key id. Two issuer
shapes exist (spec §2, §4.5):

- **root-signed**: the org root key revokes any key in its org.
- **parent-signed**: a delegated key revokes *its own descendants only* —
  the record carries the issuer's delegation chain, and verification
  demands proof of descent: the issuer's key id must appear as an
  ancestor delegator inside the revoked key's own certificate chain.

Retention is bounded (invariant I7): every record carries ``expires_at``,
which must not outlive the revoked key's natural ``not_after`` — once the
key would have expired anyway, the record is dead weight and
:meth:`RevocationSet.purge_expired` drops it.

Signature input is domain-separated (``REVOCATION_DOMAIN``), so a cert
signature can never be replayed as a revocation or vice versa.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Optional

from .canonical import canonical_json
from .certs import DelegationCert, _require_str, _require_ts
from .errors import (
    MalformedError,
    RevocationAuthorityError,
    RevocationError,
    SignatureError,
)
from .keys import (
    KeyPair,
    PUBLIC_KEY_HEX_LEN,
    SIGNATURE_HEX_LEN,
    _decode_hex,
    verify_signature,
)

REVOCATION_DOMAIN = b"autonomy.idkit.revocation.v1\n"
REVOCATION_VERSION = 1

_REV_FIELDS = frozenset(
    {"v", "revoked_key_id", "org", "reason", "revoked_at", "expires_at", "issuer_pub", "issuer_cert", "sig"}
)


@dataclass(frozen=True)
class RevocationRecord:
    revoked_key_id: str
    org: str
    revoked_at: int
    expires_at: int
    issuer_pub: str
    sig: str
    reason: Optional[str] = None
    issuer_cert: Optional[DelegationCert] = None
    v: int = REVOCATION_VERSION

    def payload_dict(self) -> dict:
        payload = {
            "v": self.v,
            "revoked_key_id": self.revoked_key_id,
            "org": self.org,
            "revoked_at": self.revoked_at,
            "expires_at": self.expires_at,
            "issuer_pub": self.issuer_pub,
        }
        if self.reason is not None:
            payload["reason"] = self.reason
        if self.issuer_cert is not None:
            payload["issuer_cert"] = self.issuer_cert.to_dict()
        return payload

    def signing_input(self) -> bytes:
        return REVOCATION_DOMAIN + canonical_json(self.payload_dict())

    def to_dict(self) -> dict:
        data = self.payload_dict()
        data["sig"] = self.sig
        return data

    def to_json(self) -> bytes:
        return canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, data: object) -> "RevocationRecord":
        if not isinstance(data, dict):
            raise MalformedError("revocation record must be a JSON object")
        unknown = set(data) - _REV_FIELDS
        if unknown:
            raise MalformedError(f"revocation record carries unknown fields: {sorted(unknown)}")
        missing = _REV_FIELDS - set(data) - {"reason", "issuer_cert"}
        if missing:
            raise MalformedError(f"revocation record is missing fields: {sorted(missing)}")
        if data["v"] != REVOCATION_VERSION:
            raise MalformedError(f"unsupported revocation version: {data['v']!r}")

        revoked_key_id = data["revoked_key_id"]
        _decode_hex(revoked_key_id, PUBLIC_KEY_HEX_LEN, "revoked_key_id")
        issuer_pub = data["issuer_pub"]
        _decode_hex(issuer_pub, PUBLIC_KEY_HEX_LEN, "issuer_pub")
        sig = data["sig"]
        _decode_hex(sig, SIGNATURE_HEX_LEN, "sig")

        org = _require_str(data["org"], "org", max_len=128)
        revoked_at = _require_ts(data["revoked_at"], "revoked_at")
        expires_at = _require_ts(data["expires_at"], "expires_at")
        if expires_at <= revoked_at:
            raise MalformedError("expires_at must be strictly after revoked_at")

        reason = None
        if "reason" in data:
            reason = _require_str(data["reason"], "reason", max_len=512)

        issuer_cert = None
        if "issuer_cert" in data:
            issuer_cert = DelegationCert.from_dict(data["issuer_cert"])

        return cls(
            revoked_key_id=revoked_key_id,
            org=org,
            revoked_at=revoked_at,
            expires_at=expires_at,
            issuer_pub=issuer_pub,
            sig=sig,
            reason=reason,
            issuer_cert=issuer_cert,
        )

    @classmethod
    def from_json(cls, raw) -> "RevocationRecord":
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise MalformedError("revocation bytes are not valid UTF-8") from exc
        if not isinstance(raw, str):
            raise MalformedError("revocation JSON must be str or bytes")
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise MalformedError("revocation record is not valid JSON") from exc
        return cls.from_dict(data)


def issue_revocation(
    issuer: KeyPair,
    revoked_key_id: str,
    *,
    org: str,
    revoked_at: int,
    expires_at: int,
    reason: Optional[str] = None,
    issuer_cert: Optional[DelegationCert] = None,
) -> RevocationRecord:
    """Mint a revocation record signed by *issuer*.

    Pass ``issuer_cert=None`` when *issuer* is the org root; otherwise
    *issuer_cert* must be the delegation cert whose ``child_pub`` is the
    issuer's key.
    """
    _decode_hex(revoked_key_id, PUBLIC_KEY_HEX_LEN, "revoked_key_id")
    _require_str(org, "org", max_len=128)
    revoked_at = _require_ts(revoked_at, "revoked_at")
    expires_at = _require_ts(expires_at, "expires_at")
    if expires_at <= revoked_at:
        raise MalformedError("expires_at must be strictly after revoked_at")
    if issuer_cert is not None and issuer_cert.child_pub != issuer.public_hex:
        raise MalformedError("issuer_cert does not delegate to the signing key")

    unsigned = RevocationRecord(
        revoked_key_id=revoked_key_id,
        org=org,
        revoked_at=revoked_at,
        expires_at=expires_at,
        issuer_pub=issuer.public_hex,
        sig="0" * SIGNATURE_HEX_LEN,
        reason=reason,
        issuer_cert=issuer_cert,
    )
    sig = issuer.sign_hex(unsigned.signing_input())
    return RevocationRecord(
        revoked_key_id=revoked_key_id,
        org=org,
        revoked_at=revoked_at,
        expires_at=expires_at,
        issuer_pub=issuer.public_hex,
        sig=sig,
        reason=reason,
        issuer_cert=issuer_cert,
    )


def verify_revocation(
    record: RevocationRecord,
    root_pub: str,
    *,
    org: str,
    revoked_cert: Optional[DelegationCert] = None,
) -> None:
    """Verify *record*'s signature and the issuer's authority to revoke.

    - The record signature must verify against ``record.issuer_pub``.
    - The record org must match *org*.
    - **Root issuer** (``issuer_pub == root_pub``): may revoke any key in
      the org; ``issuer_cert`` must be absent.
    - **Delegated issuer**: ``issuer_cert`` must chain to *root_pub* (the
      chain is checked for validity as of ``record.revoked_at``), and
      *revoked_cert* — the certificate of the key being revoked — is
      REQUIRED as proof of descent: the issuer's key id must appear as an
      ancestor delegator in it. A delegated key can only revoke its own
      descendants.
    - When *revoked_cert* is supplied (either issuer shape), it must be a
      signature-valid chain under *root_pub* whose leaf is the revoked key,
      and ``expires_at`` must not outlive its ``not_after`` (invariant I7:
      retention bounded by the key's natural expiry).

    Raises :class:`~.errors.RevocationError` (or a
    :class:`~.errors.ChainVerifyError` from embedded chain checks) on any
    failure; returns None on success.

    Note: ``revoked_at`` is the issuer's claim. Backdating only widens a
    key's ability to *deny* its own descendants — a griefing surface
    bounded to the issuer's own subtree, accepted for v1.
    """
    from .verify import walk_chain  # local import: verify.py imports nothing from here

    if not isinstance(record, RevocationRecord):
        raise MalformedError("expected a RevocationRecord")
    _decode_hex(root_pub, PUBLIC_KEY_HEX_LEN, "root public key")

    try:
        verify_signature(record.issuer_pub, record.sig, record.signing_input())
    except SignatureError as exc:
        raise RevocationError("revocation signature does not verify against issuer_pub") from exc

    if record.org != org:
        raise RevocationError(f"revocation org {record.org!r} != expected org {org!r}")

    revoked_chain = None
    if revoked_cert is not None:
        if revoked_cert.child_pub != record.revoked_key_id:
            raise RevocationError("revoked_cert leaf key does not match revoked_key_id")
        # Structural walk: signatures/org/narrowing enforced, absolute time
        # skipped — a record must stay checkable while the registry retains
        # it, even minutes before the revoked key's natural expiry.
        revoked_chain = walk_chain(revoked_cert, root_pub, org=org, check_time=False)
        if record.expires_at > revoked_cert.not_after:
            raise RevocationError(
                "expires_at outlives the revoked key's natural expiry (I7: retention bounded)"
            )

    if record.issuer_pub == root_pub:
        if record.issuer_cert is not None:
            raise RevocationError("root-signed revocation must not carry an issuer_cert")
        return

    # Delegated issuer: prove authority (chain to root) and descent.
    if record.issuer_cert is None:
        raise RevocationAuthorityError("delegated revocation requires issuer_cert")
    if record.issuer_cert.child_pub != record.issuer_pub:
        raise RevocationAuthorityError("issuer_cert does not delegate to issuer_pub")
    walk_chain(record.issuer_cert, root_pub, org=org, now=record.revoked_at, check_time=True)

    if revoked_chain is None:
        raise RevocationAuthorityError(
            "delegated revocation requires revoked_cert as proof of descent"
        )
    ancestors = {hop.child_pub for hop in revoked_chain[:-1]}
    if record.issuer_pub not in ancestors:
        raise RevocationAuthorityError(
            "issuer is not an ancestor of the revoked key: a parent may revoke its own descendants only"
        )


class RevocationSet:
    """In-memory denylist keyed by revoked key id.

    The set stores *verified* records — call :func:`verify_revocation`
    before :meth:`add`; the set itself only enforces structural sanity.
    ``purge_expired`` implements the I7 retention bound.
    """

    def __init__(self):
        self._records: dict = {}

    def add(self, record: RevocationRecord) -> None:
        if not isinstance(record, RevocationRecord):
            raise MalformedError("RevocationSet stores RevocationRecord objects")
        existing = self._records.get(record.revoked_key_id)
        if existing is None or record.expires_at > existing.expires_at:
            self._records[record.revoked_key_id] = record

    def is_revoked(self, key_id: str) -> bool:
        return key_id in self._records

    def get(self, key_id: str) -> Optional[RevocationRecord]:
        return self._records.get(key_id)

    def purge_expired(self, now: Optional[int] = None) -> int:
        """Drop records whose retention horizon has passed; return count."""
        if now is None:
            now = int(time.time())
        dead = [key_id for key_id, rec in self._records.items() if rec.expires_at < now]
        for key_id in dead:
            del self._records[key_id]
        return len(dead)

    def __len__(self) -> int:
        return len(self._records)

    def __contains__(self, key_id: str) -> bool:
        return self.is_revoked(key_id)
