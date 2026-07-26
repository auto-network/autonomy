"""Capability distribution and the continuity threshold (ruling D).

**Durability is acknowledged, not gated.** An object under any safe
state is created, stored, served, and replicated immediately — the only
write-halt conditions in the system are the confidentiality gates of
the lifecycle and acceptance modules (``StateAdvanceRequired``, no
superseded-state reuse). What this module computes is commit STATUS:
per-branch acknowledgment semantics (§10, §12) consulted by
irreversible or outward-facing acts — publishing a share link to
under-committed content, disposing of a local original, reporting
content as durably persisted. There is deliberately no
"write-eligibility" predicate here.

A branch's state is key-committed when the state with its reachable
ancestors is receipted by at least two domain members — the creator's
own receipt counting as one, a single-member organization satisfied by
the creator alone. Receipts are PERSONA-level: any number of devices or
duplicate receipts from one persona count once, and only a receipt
whose possession tag verifies (a proof of knowledge, not an assertion)
counts when the secret is available to check it.

Access expansion and reauthorization are one operation: grant the
current head, no state advance. Provisioning is eager
(mint-on-observation): any holder observing an unprovisioned authorized
member seals the current head secrets to that member's credential, and
admission approval fuses issuance into the admitting countersignature
(the membership approval path calls :func:`provision_missing` in the
same act).

Every function is pure over the provided fold and records, mutates
nothing, and is order-independent over its iterables. The reader set is
the single §4 roster projection — the root signing key is never a
reader (pin 6/6b); membership counts derive from the fold, never from a
caller-supplied member count.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import capability, state as state_mod
from .credentials import domain_member_keys
from .errors import StorageError

COMMITTED = "COMMITTED"
AT_RISK = "AT_RISK"
ORPHANED = "ORPHANED"


def grant_current_head(
    grantor,
    domain_id: str,
    recipient_credential,
    head_descriptor,
    head_secret: bytes,
    frontier,
) -> capability.CapabilityGrant:
    """Grant a current maximal safe head — no state advance, no fold read.

    Refuses a domain mismatch and (via the commitment) any secret that is
    not the named head's, so no grant ever delivers a mismatched secret.
    Recipient authorization, credential currency, and frontier recency
    are ``acceptance.accept_grant``'s job on every honest node.
    """
    if domain_id != head_descriptor.domain_id:
        raise StorageError("grant domain does not match the head descriptor")
    state_mod.verify_secret_commitment(head_descriptor, head_secret)
    return capability.issue(
        grantor,
        genesis_id=head_descriptor.genesis_id,
        domain_id=domain_id,
        storage_state_id=head_descriptor.state_id,
        recipient_credential=recipient_credential,
        state_secret=head_secret,
        state_secret_commitment=head_descriptor.secret_commitment,
        authority_heads=frontier,
    )


def provision_missing(
    grantor,
    domain_id: str,
    fold,
    credentials_by_persona,
    head_descriptors,
    head_secrets,
    existing_grants,
    frontier,
) -> tuple:
    """Eager provisioning: one grant per (unprovisioned member x held head).

    Mint-on-observation — any holder runs this on observing the roster;
    the admission-approval act runs it too, fusing grant issuance into
    the admitting countersignature. A member without a published current
    credential is skipped here and surfaces through commit status.
    Returns the minted grants, deterministically ordered.
    """
    provisioned = {
        (grant.storage_state_id, grant.recipient_kem_key_id)
        for grant in existing_grants
    }
    heads = sorted(
        (d for d in head_descriptors if d.state_id in head_secrets),
        key=lambda d: d.state_id,
    )
    minted = []
    for persona in sorted(domain_member_keys(fold)):
        credential = credentials_by_persona.get(persona)
        if credential is None:
            continue
        for head in heads:
            if (head.state_id, credential.kem_key_id) in provisioned:
                continue
            minted.append(
                grant_current_head(
                    grantor,
                    domain_id,
                    credential,
                    head,
                    head_secrets[head.state_id],
                    frontier,
                )
            )
    return tuple(minted)


def collect_receipts(state_id: str, receipts, fold, *, state_secret=None) -> frozenset:
    """The distinct qualifying receipt receivers for one state.

    A receipt counts only when it verifies, names this state and this
    organization, its receiver is a current domain member, and — when
    the secret is available — its possession tag proves knowledge.
    Persona-level: duplicates and multiple devices of one persona count
    once. Invalid records are skipped, never raised (counting, not
    acceptance).
    """
    members = domain_member_keys(fold)
    receivers = set()
    for record in receipts:
        try:
            receipt = capability.verify_receipt(record)
        except StorageError:
            continue
        if (
            receipt.storage_state_id != state_id
            or receipt.genesis_id != fold.genesis_id
            or receipt.receiver_persona not in members
        ):
            continue
        if state_secret is not None:
            try:
                capability.verify_possession_tag(receipt, state_secret)
            except StorageError:
                continue  # signed but unproven: does not count
        receivers.add(receipt.receiver_persona)
    return frozenset(receivers)


def _branch_states(state_id: str, descriptors) -> frozenset:
    """*state_id* and every descendant reachable via child parent-edges."""
    children: dict = {}
    for descriptor in descriptors.values():
        for parent_id in descriptor.parent_state_ids:
            children.setdefault(parent_id, []).append(descriptor.state_id)
    seen = {state_id}
    frontier = [state_id]
    while frontier:
        current = frontier.pop()
        for child in children.get(current, ()):
            if child not in seen:
                seen.add(child)
                frontier.append(child)
    return frozenset(seen)


def _holders(state_id: str, receipts, descriptors, fold, state_secrets) -> frozenset:
    members = domain_member_keys(fold)
    holders: set = set()
    for branch_state in _branch_states(state_id, descriptors):
        holders |= collect_receipts(
            branch_state, receipts, fold, state_secret=state_secrets.get(branch_state)
        )
        descriptor = descriptors.get(branch_state)
        if descriptor is not None:
            holders.add(descriptor.creator_persona)
    return frozenset(holders) & members


def branch_holders(state_id: str, receipts, descriptors, fold) -> frozenset:
    """Current-member holders of the branch headed by *state_id*.

    A descendant receipt subsumes ancestors — a holder of a descendant
    secret reaches the ancestor through bridges — and each branch
    state's creator counts as a holder. Intersected with the current
    roster, so a removed or rekeyed persona drops out.
    """
    return _holders(state_id, receipts, descriptors, fold, {})


@dataclass(frozen=True)
class CommitStatus:
    state_id: str
    holders: frozenset
    threshold: int
    status: str


def commit_status(
    state_id: str, receipts, descriptors, fold, *, state_secrets=None
) -> CommitStatus:
    """Per-branch acknowledgment status: COMMITTED / AT_RISK / ORPHANED.

    ``threshold`` is 2, or 1 for a single-member organization (the
    creator's self-receipt alone commits it). Possession tags are
    verified for every branch state whose secret ``state_secrets``
    supplies. This is status, never a gate: an AT_RISK object writes,
    stores, and replicates exactly like a committed one — callers
    consult this before irreversible or outward-facing acts only.
    """
    threshold = 1 if len(domain_member_keys(fold)) == 1 else 2
    holders = _holders(state_id, receipts, descriptors, fold, state_secrets or {})
    if len(holders) >= threshold:
        status = COMMITTED
    elif holders:
        status = AT_RISK
    else:
        status = ORPHANED
    return CommitStatus(
        state_id=state_id, holders=holders, threshold=threshold, status=status
    )
