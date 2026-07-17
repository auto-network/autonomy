"""Delegation-chain verification.

:func:`verify_chain` is the security boundary for every idkit consumer
(registry endpoints, dashboard grant checks). It walks the embedded chain
from the root-signed hop down to the leaf and enforces, per hop:

1. **Signature** — the hop verifies against its parent's key (the org root
   for the first hop), over domain-separated canonical bytes.
2. **Org** — every hop names the org being verified against (a cert minted
   for another org never validates, even under the same root key).
3. **Time** — ``not_before <= now <= not_after`` for *every* hop; one
   expired ancestor kills the whole chain.
4. **Narrowing** — the child's scope is a *strict* subset of its parent's,
   its validity window sits inside the parent's with a strictly earlier
   ``not_after``, and its target_types do not escalate (spec §3: each hop
   strictly narrower in scope and shorter in TTL).
5. **Revocation** — no hop's key id appears in the revocation set
   (invariant I7 pairs this with bounded retention; see revocation.py).

The function is pure over ``(cert, root_pub, org, now, revocations)`` —
no lookups, no permission tables (invariant I4).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from .certs import DelegationCert
from .errors import (
    ExpiredError,
    MalformedError,
    NotYetValidError,
    RevokedError,
    ScopeError,
    ScopeEscalationError,
    SignatureError,
    TTLViolationError,
    WrongOrgError,
)
from .keys import PUBLIC_KEY_HEX_LEN, _decode_hex, verify_signature


@dataclass(frozen=True)
class ChainVerifyResult:
    """What a verified chain authorizes."""

    leaf_pub: str
    scope: tuple
    target_types: Optional[tuple]
    subject_kind: str
    subject_id: str
    org: str
    not_after: int
    depth: int


def _is_revoked(revocations, key_id: str) -> bool:
    if revocations is None:
        return False
    checker = getattr(revocations, "is_revoked", None)
    if callable(checker):
        return bool(checker(key_id))
    return key_id in revocations


def walk_chain(
    cert: DelegationCert,
    root_pub: str,
    *,
    org: str,
    now: Optional[int] = None,
    revocations=None,
    check_time: bool = True,
) -> list:
    """Verify every hop of *cert*'s chain; return the chain root-first.

    ``check_time=False`` skips only the absolute freshness check (step 3) —
    signatures, org, narrowing, and revocation are always enforced. It
    exists for revocation processing, where a record must remain checkable
    against a chain whose keys have since expired. All authorization paths
    use :func:`verify_chain`, which always checks time.
    """
    if not isinstance(cert, DelegationCert):
        raise MalformedError("expected a DelegationCert")
    _decode_hex(root_pub, PUBLIC_KEY_HEX_LEN, "root public key")
    if check_time and now is None:
        now = int(time.time())

    chain = cert.chain()
    signer_pub = root_pub
    parent: Optional[DelegationCert] = None

    for depth, hop in enumerate(chain, start=1):
        try:
            verify_signature(signer_pub, hop.sig, hop.signing_input())
        except SignatureError as exc:
            raise SignatureError(f"hop {depth}: signature does not verify against its parent key") from exc

        if hop.org != org:
            raise WrongOrgError(f"hop {depth}: cert org {hop.org!r} != expected org {org!r}")

        if check_time:
            if now < hop.not_before:
                raise NotYetValidError(f"hop {depth}: not valid before {hop.not_before} (now={now})")
            if now > hop.not_after:
                raise ExpiredError(f"hop {depth}: expired at {hop.not_after} (now={now})")

        if parent is not None:
            if not set(hop.scope) < set(parent.scope):
                raise ScopeEscalationError(
                    f"hop {depth}: scope must be a strict subset of the parent's "
                    f"(child={sorted(hop.scope)}, parent={sorted(parent.scope)})"
                )
            if hop.not_before < parent.not_before or hop.not_after >= parent.not_after:
                raise TTLViolationError(
                    f"hop {depth}: validity window [{hop.not_before}, {hop.not_after}] must sit "
                    f"inside the parent's [{parent.not_before}, {parent.not_after}] "
                    "with a strictly earlier not_after"
                )
            if parent.target_types is not None:
                if hop.target_types is None or not set(hop.target_types) <= set(parent.target_types):
                    raise ScopeEscalationError(
                        f"hop {depth}: target_types must not escalate past the parent's"
                    )

        if _is_revoked(revocations, hop.child_pub):
            raise RevokedError(f"hop {depth}: key {hop.child_pub} is revoked")

        signer_pub = hop.child_pub
        parent = hop

    return chain


def verify_chain(
    cert: DelegationCert,
    root_pub: str,
    *,
    org: str,
    now: Optional[int] = None,
    revocations=None,
    required_scope: Optional[str] = None,
    required_target_type: Optional[str] = None,
) -> ChainVerifyResult:
    """Verify *cert*'s full delegation chain down from *root_pub*.

    Raises a :class:`~.errors.ChainVerifyError` subclass naming the first
    failure; returns a :class:`ChainVerifyResult` describing what the leaf
    key is authorized to do.

    - *org*: the org the relying party is acting for (e.g. the org named
      in a registry binding). Every hop must match.
    - *now*: unix seconds; defaults to the current time.
    - *revocations*: a :class:`~.revocation.RevocationSet` or any container
      of revoked key ids.
    - *required_scope* / *required_target_type*: if given, the leaf must
      carry them (a leaf with no target_types restriction accepts any
      target type).
    """
    chain = walk_chain(cert, root_pub, org=org, now=now, revocations=revocations, check_time=True)
    leaf = chain[-1]

    if required_scope is not None and required_scope not in leaf.scope:
        raise ScopeError(f"leaf scope {sorted(leaf.scope)} does not include required {required_scope!r}")
    if required_target_type is not None and leaf.target_types is not None:
        if required_target_type not in leaf.target_types:
            raise ScopeError(
                f"leaf target_types {sorted(leaf.target_types)} do not include {required_target_type!r}"
            )

    return ChainVerifyResult(
        leaf_pub=leaf.child_pub,
        scope=leaf.scope,
        target_types=leaf.target_types,
        subject_kind=leaf.subject.kind,
        subject_id=leaf.subject.id,
        org=leaf.org,
        not_after=leaf.not_after,
        depth=len(chain),
    )
