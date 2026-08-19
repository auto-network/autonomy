"""Storage authority scopes and the three fold-based acceptance procedures.

A signed key-control record is admissible only against the authority
ledger at the frontier the record itself cites (contract §9): each
procedure folds at the record's ``authority_heads``, so a rolled-back
frontier is judged by its own projection, and staleness is caught by
frontier recency, not wall clocks.

**Membership is the sole authorization path (§8, PIN 6b).** Both storage
scopes are membership-derived exclusively: the predicate consults the
single contract §4 roster projection (``domain_member_keys`` — current
keys of role-holding claimed members) and never a generic scope holding
— so the root's universal wildcard does not reach storage, and a
delegated key authorizes only by resolving its delegation chain upward
to a member persona; a chain terminating outside the roster is void.
The projection is keyed by CURRENT keys, so a rekey-retired key stops
authorizing the moment the rekey enters the cited frontier.

Covered-loss acceptance is inclusive-ancestry SUPERSET coverage, not
equality (§6): a state is safe when every projected contraction head is
equal to or a causal ancestor of some event in its covered-loss set —
a conservative union descriptor covering both branches passes; an
omission never does.

Every procedure raises a :class:`StorageError` subclass on any failed
check and returns only when every check passes; none decapsulates or
unwraps key material.
"""

from __future__ import annotations

from tools.network.dag_tag import AUTHORITY, require_dag

from dataclasses import dataclass
from typing import Optional

from tools.network.ledger import projections
from tools.network.ledger.scopes import validate_scope

from . import capability as capability_mod
from . import object_header as object_header_mod
from . import state as state_mod
from .bridge import ParentBridge, verify_signature as verify_bridge_signature
from .credentials import (
    PersonaKemCredential,
    domain_member_keys,
    select_current_credential,
)
from .errors import StorageError
from .records import record_id


class AcceptanceError(StorageError):
    """Base class for acceptance-procedure rejections."""


class DomainError(AcceptanceError):
    """The record names a different organization or storage domain."""


class ScopeError(AcceptanceError):
    """The acting key neither is nor resolves to a domain member."""


class AuthorityError(AcceptanceError):
    """The record fails a fold-derived authority or addressing check."""


class LossCoverageError(AcceptanceError):
    """A projected contraction head is outside the covered-loss closure."""


class FrontierRecencyError(AcceptanceError):
    """The grant's cited frontier does not include the descriptor's heads."""


# -- scopes ---------------------------------------------------------------------------


def scope_storage_advance(domain_id: str) -> str:
    """``storage:state:advance:<domain_id>`` — held by every domain member."""
    return validate_scope(f"storage:state:advance:{domain_id}")


def scope_storage_grant(domain_id: str) -> str:
    """``storage:capability:grant:<domain_id>`` — held by every domain member."""
    return validate_scope(f"storage:capability:grant:{domain_id}")


# -- membership resolution -------------------------------------------------------------


def resolve_member_key(fold, key: str) -> Optional[str]:
    """Resolve *key* to a domain member's current key, or None.

    A member current key resolves to itself; any other key resolves by
    walking the fold's usable delegation edges upward (breadth-first,
    cycle-guarded) to the first member current key reached. This is the
    ONLY way a key authorizes a storage operation — no wildcard, no
    generic scope holding (§8).
    """
    members = domain_member_keys(fold)
    if key in members:
        return key
    seen = {key}
    frontier = [key]
    while frontier:
        current = frontier.pop(0)
        for parent in fold.delegation_parents.get(current, ()):
            if parent in members:
                return parent
            if parent not in seen:
                seen.add(parent)
                frontier.append(parent)
    return None


def _domain_guard(fold, genesis_id: str, domain_id: str) -> None:
    if genesis_id != fold.genesis_id:
        raise DomainError("record is bound to a different organization")
    if domain_id != projections.organization_content_domain_id(fold.genesis_id):
        raise DomainError("record names a different storage domain")


def loss_projection_digest(fold) -> str:
    """The single digest definition — the state-mint path reuses this so
    mint and accept agree byte-for-byte."""
    return record_id(projections.projection_bytes(projections.build_loss_heads(fold)))


def _require_loss_coverage(fold, covered_loss_heads, ancestry) -> None:
    covered_closure = ancestry(covered_loss_heads)
    for head in fold.loss_heads:
        if head not in covered_closure:
            raise LossCoverageError(
                f"projected contraction {head} is not covered by the record's "
                "covered-loss set"
            )


# -- results ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AcceptedState:
    descriptor: state_mod.StorageStateDescriptor
    creator_member: str
    history_complete: bool


@dataclass(frozen=True)
class AcceptedGrant:
    grant: capability_mod.CapabilityGrant
    grantor_member: str
    recipient_persona: str


@dataclass(frozen=True)
class AcceptedObject:
    header: object_header_mod.ObjectKeyHeader
    author_member: str


# -- procedures ------------------------------------------------------------------------


def accept_state(
    descriptor: state_mod.StorageStateDescriptor,
    fold_at,
    ancestry,
    *,
    bridges=(),
    parent_descriptors=None,
) -> AcceptedState:
    """Admissibility of a storage-state descriptor at its cited frontier."""
    state_mod.verify_structure(descriptor)  # forged/malformed rejected first
    fold = fold_at(descriptor.authority_heads)
    _domain_guard(fold, descriptor.genesis_id, descriptor.domain_id)
    creator = resolve_member_key(fold, descriptor.creator_persona)
    if creator is None:
        raise ScopeError("descriptor creator does not resolve to a domain member")
    _require_loss_coverage(fold, descriptor.covered_loss_heads, ancestry)
    if descriptor.loss_projection_digest != loss_projection_digest(fold):
        raise LossCoverageError(
            "descriptor loss_projection_digest does not match the projection "
            "at the cited frontier"
        )
    if parent_descriptors:
        for parent_id in descriptor.parent_state_ids:
            parent = parent_descriptors.get(parent_id)
            if parent is not None and (
                parent.genesis_id != descriptor.genesis_id
                or parent.domain_id != descriptor.domain_id
            ):
                raise DomainError(f"parent state {parent_id} is from another domain")
    history_complete = all(
        _has_one_valid_bridge(descriptor, parent_id, bridges)
        for parent_id in descriptor.parent_state_ids
    )
    return AcceptedState(
        descriptor=descriptor, creator_member=creator, history_complete=history_complete
    )


def _has_one_valid_bridge(descriptor, parent_id: str, bridges) -> bool:
    valid = 0
    for bridge in bridges:
        if not isinstance(bridge, ParentBridge):
            continue
        if (
            bridge.child_state_id != descriptor.state_id
            or bridge.parent_state_id != parent_id
            or bridge.genesis_id != descriptor.genesis_id
            or bridge.domain_id != descriptor.domain_id
        ):
            continue
        try:
            verify_bridge_signature(bridge)
        except StorageError:
            continue  # invalid bridge yields incomplete, never a raise (§9)
        valid += 1
    return valid == 1


def accept_grant(
    grant: capability_mod.CapabilityGrant,
    fold_at,
    ancestry,
    recipient_credential: PersonaKemCredential,
    state_descriptor: state_mod.StorageStateDescriptor,
    *,
    known_credentials=(),
) -> AcceptedGrant:
    """Admissibility of a capability grant. Runs before any decapsulation."""
    grant = capability_mod.verify_grant(grant)
    if (
        state_descriptor.state_id != grant.storage_state_id
        or state_descriptor.genesis_id != grant.genesis_id
        or state_descriptor.domain_id != grant.domain_id
        or grant.state_secret_commitment != state_descriptor.secret_commitment
    ):
        raise AuthorityError("grant does not match the presented state descriptor")
    # Frontier recency: the fold evaluating the grant must reflect every
    # contraction the state covers — a post-contraction state cannot be
    # granted under a pre-contraction frontier.
    grant_closure = ancestry(grant.authority_heads)
    for head in state_descriptor.authority_heads:
        if head not in grant_closure:
            raise FrontierRecencyError(
                "grant frontier does not causally include the descriptor's heads"
            )
    fold = fold_at(grant.authority_heads)
    _domain_guard(fold, grant.genesis_id, grant.domain_id)
    grantor = resolve_member_key(fold, grant.grantor_persona)
    if grantor is None:
        raise ScopeError("grantor does not resolve to a domain member")
    # Recipient checks: grants are persona-addressed — delegation
    # resolution applies to actors, never to recipients.
    if recipient_credential.kem_key_id != grant.recipient_kem_key_id:
        raise AuthorityError("grant is not addressed to the presented credential")
    if recipient_credential.genesis_id != fold.genesis_id:
        raise AuthorityError("recipient credential is bound to another organization")
    if recipient_credential.persona not in domain_member_keys(fold):
        raise AuthorityError("recipient is not a current content-holding member")
    peers = [c for c in known_credentials if c.persona == recipient_credential.persona]
    current = select_current_credential([*peers, recipient_credential], ancestry)
    if current.kem_key_id != grant.recipient_kem_key_id:
        raise AuthorityError("grant is addressed to a superseded credential")
    return AcceptedGrant(
        grant=grant, grantor_member=grantor, recipient_persona=recipient_credential.persona
    )


def accept_object(
    header: object_header_mod.ObjectKeyHeader,
    fold_at,
    ancestry,
    state_descriptor: state_mod.StorageStateDescriptor,
) -> AcceptedObject:
    """Admissibility of an object-key header at its writer's frontier."""
    header = object_header_mod.verify_structure(header)
    if (
        state_descriptor.state_id != header.storage_state_id
        or state_descriptor.genesis_id != header.genesis_id
        or state_descriptor.domain_id != header.domain_id
    ):
        raise AuthorityError("header does not reference the presented descriptor")
    fold = fold_at(header.writer_authority_heads)
    _domain_guard(fold, header.genesis_id, header.domain_id)
    author = resolve_member_key(fold, header.author_persona)
    if author is None:
        raise AuthorityError("header author does not resolve to a domain member")
    # The referenced state must cover every contraction at the writer's
    # frontier — a state minted before a contraction entered is unusable.
    _require_loss_coverage(fold, state_descriptor.covered_loss_heads, ancestry)
    return AcceptedObject(header=header, author_member=author)
