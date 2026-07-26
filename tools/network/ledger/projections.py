"""Fold-derived read models — projections over the authority state.

Ops are the replicated primitive; projections are **derived views**,
rebuildable from the event store at any time (DR eval ``361ae60f-14c``).
Every projection carries the heads + fold fingerprint it was derived
from, and renders to canonical JSON bytes so "rebuild from zero" is
byte-identical, not merely equivalent.

Three read models (consumed by the dashboard/roster surfaces, F6):

- ``roster`` — member table with sponsor provenance (persona, current
  key, roles, sponsor, claim/invite event ids).
- ``roles`` — the role matrix: each defined role's version, policy,
  scope set, and holders (member personas + bare-key grants).
- ``live-keys`` — every key currently holding authority → its scope
  patterns (the fixpoint output; what connectors consult).

Plus the ``ledger-state`` document (heads, last witnessed head, sync
cursor) whose shape is contract-pinned by the graph Settings schema
``autonomy.network.ledger-state#1`` — built here, validated there, and a
cross-pin test keeps the two from drifting.
"""

from __future__ import annotations

import hashlib
import re
from typing import Dict, Optional

from tools.network.idkit import canonical_json

from .errors import MalformedEventError
from .fold import FoldState

PROJECTION_NAMES = ("live-keys", "roles", "roster")

LEDGER_STATE_SET_ID = "autonomy.network.ledger-state"
LEDGER_PROJECTION_SET_ID = "autonomy.network.ledger-projection"


def build_roster(state: FoldState) -> list:
    """Member rows, sorted by persona id. Sponsor provenance is permanent."""
    rows = []
    for pid in sorted(state.members):
        m = state.members[pid]
        rows.append(
            {
                "persona": m.persona,
                "current_key": m.current_key,
                "roles": list(m.roles),
                "sponsor": m.sponsor,
                "claim_id": m.claim_id,
                "invite_id": m.invite_id,
            }
        )
    return rows


def build_role_matrix(state: FoldState) -> dict:
    """Role → definition + holders (member personas and bare-key grants)."""
    holders: Dict[str, set] = {}
    for pid, m in state.members.items():
        for role in m.roles:
            holders.setdefault(role, set()).add(pid)
    for key, roles in state.bare_roles.items():
        for role in roles:
            holders.setdefault(role, set()).add(key)
    matrix = {}
    for name in sorted(state.role_defs):
        d = state.role_defs[name]
        matrix[name] = {
            "version": d.version,
            "claim_requires": d.claim_requires,
            "scope_set": list(d.scope_set),
            "event_id": d.event_id,
            "holders": sorted(holders.get(name, ())),
        }
    return matrix


def build_live_keys(state: FoldState) -> dict:
    """Key → sorted scope patterns it currently holds (the live-key set)."""
    return {key: sorted(scopes) for key, scopes in sorted(state.authority_map().items())}


def build_projections(state: FoldState) -> dict:
    """All read models, each wrapped with its derivation provenance."""
    fingerprint = state.fingerprint()
    bodies = {
        "roster": build_roster(state),
        "roles": build_role_matrix(state),
        "live-keys": build_live_keys(state),
    }
    return {
        name: {
            "projection": name,
            "org": state.org,
            "heads": list(state.heads),
            "fingerprint": fingerprint,
            "body": bodies[name],
        }
        for name in PROJECTION_NAMES
    }


def unassemblable_thresholds(state: FoldState) -> tuple:
    """Admin-ack roles whose approver threshold exceeds the members who
    hold admission authority over them — a WARNING surface, never a
    validity rule (an unassemblable threshold stays legal and dormant
    until the org grows into it, mirroring the recovery-quorum rule).

    Root is not counted: it is the cold constitutional key, not a member
    approver in routine admission.
    """
    from .fold import scope_role_grant
    from .scopes import set_covers

    candidates = {m.current_key for m in state.members.values()}
    candidates.update(state.bare_roles)
    candidates.discard(state.root)
    warnings = []
    for name in sorted(state.role_defs):
        role_def = state.role_defs[name]
        if role_def.claim_requires != "admin-ack":
            continue
        holders = sorted(
            key
            for key in candidates
            if set_covers(state.authority(key), scope_role_grant(name))
        )
        if role_def.approver_threshold > len(holders):
            warnings.append(
                {
                    "role": name,
                    "approver_threshold": role_def.approver_threshold,
                    "admission_authority_holders": holders,
                }
            )
    return tuple(warnings)


def organization_content_domain_id(genesis_id: str) -> str:
    """The version-one organization-content storage-domain identifier.

    ``SHA-256("autonomy/storage-domain/v1" || genesis_id ||
    "organization-content")`` (contract §4) — anchored to the genesis
    event id, so it is invariant across a ``key.rotate``.
    """
    if not isinstance(genesis_id, str) or not re.fullmatch("[0-9a-f]{64}", genesis_id):
        raise MalformedEventError("genesis_id must be 64 lowercase hex chars")
    return hashlib.sha256(
        b"autonomy/storage-domain/v1"
        + genesis_id.encode("ascii")
        + b"organization-content"
    ).hexdigest()


def build_loss_heads(state: FoldState) -> dict:
    """The access-loss read model: the maximal contraction events a
    writer's frontier must cover (contract §6), with the same derivation
    provenance as the other projections. Standalone — not added to
    ``PROJECTION_NAMES``, so the dashboard read-model schema is unaffected.
    """
    return {
        "projection": "loss-heads",
        "org": state.org,
        "heads": list(state.heads),
        "fingerprint": state.fingerprint(),
        "body": {
            "domain_id": organization_content_domain_id(state.genesis_id),
            "loss_heads": list(state.loss_heads),
        },
    }


def projection_bytes(projection: dict) -> bytes:
    """The canonical byte form — what byte-identical rebuilds compare."""
    return canonical_json(projection)


def ledger_state_payload(
    state: FoldState,
    *,
    org_uuid: str,
    event_count: int,
    last_witnessed_head: Optional[str] = None,
    sync_cursor: Optional[Dict[str, str]] = None,
) -> dict:
    """The ``autonomy.network.ledger-state#1`` document for this replica.

    Holds only hashes and cursors — never event content (L8 discipline
    extends to the Settings layer: authority lives in the ledger, the
    graph holds pointers into it).
    """
    return {
        "org_uuid": org_uuid,
        "genesis_id": state.genesis_id,
        "heads": list(state.heads),
        "fingerprint": state.fingerprint(),
        "root_pub": state.root,
        "event_count": event_count,
        "last_witnessed_head": last_witnessed_head,
        "sync_cursor": dict(sync_cursor) if sync_cursor else {},
    }
