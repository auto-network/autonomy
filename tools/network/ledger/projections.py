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

from typing import Dict, Optional

from tools.network.idkit import canonical_json

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
