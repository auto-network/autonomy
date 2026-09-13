"""The install seed a joiner keeps from its sponsor, machine-local
(``autonomy.machine.org-install-seed#1``; the schema module says why).

One writer, :func:`install`, at the moment the organization is installed
on this machine. Two readers, one per served kind, each returning only
what a reader should overlay where it has NO replicated row:

* :func:`seed_profiles` — persona -> presentation, for the member listing.
* :func:`seed_reachability` — machine -> verified addresses, for the org
  peer candidates (the row is re-verified here, as a replicated one is).

Nothing here writes to an organization-homed set. A seed row is never
this machine's claim about another member; it is a note to self.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from tools.graph import settings_ops
from tools.graph.schemas.org_install_seed import (
    KIND_MEMBER_PROFILE,
    KIND_REACHABILITY,
    ORG_INSTALL_SEED_REVISION as REVISION,
    ORG_INSTALL_SEED_SET_ID as SET_ID,
    seed_key,
    seed_prefix,
)

#: The machine store is addressed by this literal org name in settings_ops.
MACHINE = "machine"


def _write(slug: str, kind: str, row_key: str, row: dict, *, now: int) -> None:
    payload = {"organization": slug, "kind": kind, "row_key": row_key,
               "row": row, "installed_at": now}
    settings_ops.upsert_by_key(SET_ID, REVISION, seed_key(slug, kind, row_key),
                               payload, org=MACHINE)


def install(slug: str, genesis_id: str, *, member_profiles: Any,
            reachability_rows: Any, skip_persona: str = "",
            now: int | None = None) -> dict[str, int]:
    """Keep the served rows locally. Directory rows are bounded here again
    (name required, avatar bounded); reachability rows are verified against
    the org's genesis before they are kept, exactly as a reader would.
    The joiner's own persona is never seeded: their row is theirs to write."""
    from tools.dashboard import member_directory
    from tools.network.fleet_org_reachability import verify_row

    now = int(time.time()) if now is None else int(now)
    counts = {KIND_MEMBER_PROFILE: 0, KIND_REACHABILITY: 0}
    for entry in member_profiles if isinstance(member_profiles, list) else ():
        if not isinstance(entry, dict):
            continue
        persona = entry.get("persona_pub")
        if not isinstance(persona, str) or len(persona) != 64 or persona == skip_persona:
            continue
        name = member_directory._clean(entry.get("display_name"), member_directory.NAME_MAX)
        if not name:
            continue
        row = {
            "display_name": name,
            "byline": member_directory._clean(entry.get("byline"), member_directory.BYLINE_MAX),
            "avatar": member_directory.avatar_ref(entry.get("avatar")),
            "color": member_directory._clean(entry.get("color"), 32),
        }
        try:
            _write(slug, KIND_MEMBER_PROFILE, persona, row, now=now)
            counts[KIND_MEMBER_PROFILE] += 1
        except Exception:
            continue
    for entry in reachability_rows if isinstance(reachability_rows, list) else ():
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        row = {k: v for k, v in entry.items() if k != "key"}
        if not isinstance(key, str) or verify_row(key, row, org=genesis_id) is None:
            continue
        try:
            _write(slug, KIND_REACHABILITY, key, row, now=now)
            counts[KIND_REACHABILITY] += 1
        except Exception:
            continue
    return counts


def _rows(slug: str, kind: str) -> dict[str, dict]:
    """row_key -> served row of one kind for one org. Unreadable store: {}."""
    try:
        members = settings_ops.read_owned_set(
            SET_ID, org=MACHINE, target_revision=REVISION,
            prefix=seed_prefix(slug, kind),
        ).members
    except Exception:
        return {}
    out: dict[str, dict] = {}
    for member in members:
        payload = member.payload or {}
        if payload.get("organization") != slug or payload.get("kind") != kind:
            continue
        row_key, row = payload.get("row_key"), payload.get("row")
        if isinstance(row_key, str) and isinstance(row, dict):
            out[row_key] = row
    return out


def seed_profiles(slug: str) -> dict[str, dict]:
    """persona -> presentation the sponsor served, for personas the
    replicated directory does not name yet."""
    out: dict[str, dict] = {}
    for persona, row in _rows(slug, KIND_MEMBER_PROFILE).items():
        name = row.get("display_name")
        if isinstance(name, str) and name:
            out[persona] = {
                "display_name": name,
                "avatar": row.get("avatar") or None,
                "color": row.get("color") or None,
                "byline": row.get("byline") or None,
            }
    return out


def seed_reachability(
    slug: str, *, org: str, own_machine_pub: str = "",
    is_member: Callable[[str], bool | None] | None = None,
    now: int | None = None,
) -> dict[str, list[str]]:
    """machine -> addresses for every seeded row that still verifies
    against *org* (the genesis id), other than this machine's own. The
    reader lives with the other reachability readers so the sync scheduler
    consults it without a dashboard import."""
    from tools.network.fleet_org_reachability import seeded_co_member_addresses

    return seeded_co_member_addresses(
        slug, org=org, own_machine_pub=own_machine_pub, is_member=is_member, now=now,
    )
