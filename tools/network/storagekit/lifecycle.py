"""Storage-state lifecycle: safety, selection, advancement, union.

A state is **safe** for a writer when every required contraction is
equal to or a causal ancestor of some head in the state's covered-loss
set (contract §6) — a superset is safe, an omission is not. Advancement
is **lazy** (§10): nothing advances on the contraction itself; the next
content write selects a safe state or, when none is available, mints a
fresh state that coalesces every outstanding contraction and bridges
the prior heads. Concurrent safe states have **no total order and no
retroactive winner**: selection breaks ties by ascending ``state_id``
(the same ascending-identifier discipline the fold applies), and a
multi-parent union joins branches so every branch history stays
reachable — no parent is invalidated.

Pure over its arguments: causal reasoning enters only through the
injected ``ancestry(ids) -> frozenset`` inclusive-closure seam; no wall
clock, no transport, no new cryptographic construction — this module
only composes the state and bridge records.
"""

from __future__ import annotations

from tools.network.dag_tag import AUTHORITY, require_dag

from . import bridge as bridge_mod
from . import state as state_mod
from .errors import StorageError


class StateAdvanceRequired(StorageError):
    """No available state covers the required contractions — the writer
    must advance before writing; falling back to a stale state is never
    an option (fail closed)."""


def state_covers(descriptor, required_loss_heads, ancestry) -> bool:
    """Contract §6 safety predicate. An empty required set is covered.

    One of the two leaves where an ``ancestry`` is actually APPLIED to loss
    heads, so the DAG check lives here rather than at the call sites: every
    caller — ``select_safe_state``, and through it ``create_object`` and
    ``seal_revision`` — funnels through this line, including callers not
    written yet.
    """
    require_dag(ancestry, AUTHORITY, "state_covers")
    covered = ancestry(descriptor.covered_loss_heads)
    return all(head in covered for head in required_loss_heads)


def select_safe_state(
    domain_id: str, required_loss_heads, available_states, ancestry
):
    """The safe available state with the smallest ``state_id``.

    Order-independent by construction (min over a total order on
    identifiers, matching the fold's ascending-identifier tie-break for
    concurrent events). Other-domain descriptors are ignored. Raises
    :class:`StateAdvanceRequired` when nothing safe is available.
    """
    safe = [
        d
        for d in available_states
        if d.domain_id == domain_id and state_covers(d, required_loss_heads, ancestry)
    ]
    if not safe:
        raise StateAdvanceRequired(
            "no available state covers the required contractions"
        )
    return min(safe, key=lambda d: d.state_id)


def _checked_parents(parent_descriptors, parent_secrets) -> list:
    parents = list(parent_descriptors)
    ids = [d.state_id for d in parents]
    if len(set(ids)) != len(ids):
        raise StorageError("duplicate parent states")
    for state_id in ids:
        secret = parent_secrets.get(state_id)
        if not isinstance(secret, bytes) or len(secret) != state_mod.STATE_SECRET_LEN:
            raise StorageError(f"missing or malformed secret for parent {state_id}")
    return parents


def _mint(
    creator,
    genesis_id,
    domain_id,
    parents,
    parent_secrets,
    authority_heads,
    covered_loss_heads,
    loss_projection_digest,
) -> tuple:
    descriptor, secret = state_mod.generate(
        creator,
        genesis_id,
        domain_id,
        [d.state_id for d in parents],
        authority_heads,
        covered_loss_heads,
        loss_projection_digest,
    )
    bridges = tuple(
        bridge_mod.create(
            creator,
            genesis_id=genesis_id,
            domain_id=domain_id,
            child_state_id=descriptor.state_id,
            parent_state_id=parent.state_id,
            child_state_secret=secret,
            parent_state_secret=parent_secrets[parent.state_id],
            authority_heads=authority_heads,
        )
        for parent in parents
    )
    return descriptor, secret, bridges


def advance_state(
    creator,
    domain_id: str,
    genesis_id: str,
    required_loss_heads,
    loss_projection_digest: str,
    parent_descriptors,
    parent_secrets,
    authority_heads,
) -> tuple:
    """Coalesce the outstanding contractions into one fresh state.

    Invoked only at a content write. Covers exactly the required set;
    bridges every prior head so held history stays reachable. Returns
    ``(descriptor, secret, bridges)``.
    """
    required = sorted(set(required_loss_heads))
    if not required:
        raise StorageError("advancement requires a non-empty contraction set")
    parents = _checked_parents(parent_descriptors, parent_secrets)
    return _mint(
        creator,
        genesis_id,
        domain_id,
        parents,
        parent_secrets,
        authority_heads,
        required,
        loss_projection_digest,
    )


def union_state(
    creator,
    domain_id: str,
    genesis_id: str,
    parent_descriptors,
    parent_secrets,
    required_loss_heads,
    loss_projection_digest: str,
    authority_heads,
) -> tuple:
    """Join concurrent safe states into one multi-parent union.

    The covered-loss set is the union of the parents' covered sets and
    the required set — a permitted superset (§6). One bridge per parent:
    no branch is invalidated, every branch history stays reachable.
    Returns ``(descriptor, secret, bridges)``.
    """
    parents = _checked_parents(parent_descriptors, parent_secrets)
    if len(parents) < 2:
        raise StorageError("a union requires at least two parent states")
    covered = set(required_loss_heads)
    for parent in parents:
        covered.update(parent.covered_loss_heads)
    return _mint(
        creator,
        genesis_id,
        domain_id,
        parents,
        parent_secrets,
        authority_heads,
        sorted(covered),
        loss_projection_digest,
    )
