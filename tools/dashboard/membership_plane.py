"""Committed-membership plane, dashboard side (auto-3bhy3).

The node-side half of graph://da0dd9fb-e75: build membership-proof riders
from the local fold's commitment, verify the registry's adopted checkpoint
against what this node's own fold says before trusting or extending it, and
re-stamp live tunnels over the re-prove control op when the checkpoint
advances.

The mismatch rule is the design's automatic-detection requirement: when the
registry's adopted ``members_root`` contradicts the root this node computes
from its own fold for the same commitment, the node REFUSES to re-prove
under it and raises an operator-visible alarm naming the contested state —
that is the "membership chain captured" signal whose remedy is a ledger
removal followed by a root-signed reset checkpoint.

Pure functions take the fold-derived pub lists; the thin ``*_for_org``
wrappers read the org ledger the same way ``org_authority`` does. Wiring
the alarm into a screen belongs to the fleet UI; ``membership_alarm(org)``
is the read point.
"""

from __future__ import annotations

import logging
import threading
from typing import Dict, Iterable, Optional

from tools.network.ledger import LedgerStore, org_ledger_db_path
from tools.network.ledger.membership_commitment import (
    MembershipCommitmentError,
    checkpointer_pubs,
    compute_root,
    inclusion_proof,
    member_pubs,
)

logger = logging.getLogger("dashboard.membership")

_alarm_lock = threading.Lock()
_alarms: Dict[str, str] = {}


class MembershipPlaneError(Exception):
    """A rider that cannot be built or a registry state that cannot be
    trusted; the message is operator-facing."""


def record_alarm(org: str, text: str) -> None:
    with _alarm_lock:
        _alarms[org] = text
    logger.error("membership alarm org=%s: %s", org, text)


def membership_alarm(org: str) -> Optional[str]:
    """The org's current membership alarm, or None. Cleared by
    :func:`clear_alarm` once the operator's recovery lands."""
    with _alarm_lock:
        return _alarms.get(org)


def clear_alarm(org: str) -> None:
    with _alarm_lock:
        _alarms.pop(org, None)


def commitment_for_org(org: str, at_head=None) -> dict:
    """This node's own commitment view: sorted member and checkpointer pub
    lists plus both roots, from the org ledger fold."""
    store = LedgerStore(org_ledger_db_path(org))
    try:
        heads = list(at_head) if at_head is not None else list(store.heads())
        state = store.fold(heads=heads)
    finally:
        store.close()
    members = member_pubs(state)
    checkpointers = checkpointer_pubs(state)
    return {
        "heads": tuple(sorted(heads)),
        "members": members,
        "checkpointers": checkpointers,
        "members_root": compute_root(members),
        "checkpointers_root": compute_root(checkpointers),
    }


def build_rider(
    persona_pub: str,
    registry_seq: int,
    registry_members_root: str,
    local_members: Iterable[str],
    *,
    org: str = "",
) -> dict:
    """The membership-proof rider for *persona_pub* at the registry's
    current checkpoint.

    Refuses — and records the operator alarm — when the registry's adopted
    ``members_root`` contradicts the root computed from this node's own
    fold: proving under a contested root would ratify a forged checkpoint.
    """
    members = sorted(set(local_members))
    local_root = compute_root(members)
    if registry_members_root != local_root:
        alarm = (
            "membership chain contested: the registry's adopted checkpoint "
            f"(seq {registry_seq}, members_root {registry_members_root[:16]}…) "
            f"contradicts this node's fold ({local_root[:16]}…). Refusing to "
            "re-prove under it. Recovery: remove the forger in the ledger, "
            "then publish a root-signed reset checkpoint."
        )
        if org:
            record_alarm(org, alarm)
        raise MembershipPlaneError(alarm)
    try:
        index, path = inclusion_proof(members, persona_pub)
    except MembershipCommitmentError as exc:
        raise MembershipPlaneError(
            f"this node's persona is not in the committed member set: {exc}"
        ) from exc
    return {"v": 1, "checkpoint_seq": registry_seq, "index": index,
            "path": path}


def rider_for_org(org: str, persona_pub: str, registry_state: dict) -> dict:
    """Rider from the live fold against the registry's GET /membership
    response (``{"seq": ..., "members_root": ...}``)."""
    commitment = commitment_for_org(org)
    return build_rider(
        persona_pub,
        registry_state["seq"],
        registry_state["members_root"],
        commitment["members"],
        org=org,
    )


def reprove_over_tunnel(org: str, persona_pub: str, registry_state: dict) -> dict:
    """Re-stamp the org's live tunnel at the registry's current checkpoint
    — the connector's answer to a checkpoint advance. Returns the control
    reply; raises :class:`MembershipPlaneError` (mismatch, non-member) or
    the supervisor's own errors (no tunnel)."""
    from tools.dashboard.link_serving_supervisor import control

    rider = rider_for_org(org, persona_pub, registry_state)
    return control(org, "re-prove-membership", rider)
