"""Share state for Design Studio designs, projected from link grants.

The source of truth for "is this design shared" is the org's
``autonomy.network.link-grant`` Settings rows (written only by the
``link_publish`` approval ceremony, revoked only by ``link_revoke``).  This
module is a read model over them: nothing here mints, extends, or revokes a
grant.

A design is *shared* when at least one active, unexpired grant of type
``design`` or ``present`` targets its stable design id or any of its
revision ids.  Both types are counted because both resolve to the design's
latest revision when served (``link_serving``), so either one makes the
design reachable from outside.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable

logger = logging.getLogger(__name__)

DESIGN_TARGET_TYPES = ("design", "present")


def _expires_at(payload: dict) -> int | None:
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    ttl = meta.get("ttl")
    try:
        issued = datetime.fromisoformat(str(payload["issued_at"]).replace("Z", "+00:00"))
        if type(ttl) is int and ttl > 0:
            return int(issued.timestamp() + ttl)
    except (KeyError, TypeError, ValueError):
        pass
    return None


SHARE_TARGET_TYPES = ("design", "present", "note", "mission")


def active_grants(org: str | None, target_types: Iterable[str] = SHARE_TARGET_TYPES,
                  *, now: datetime | None = None) -> list[dict]:
    """Every unexpired grant of the given types the org holds, newest first."""
    wanted = set(target_types)
    from tools.graph import settings_ops
    from tools.graph.schemas.network_identity import (
        NETWORK_LINK_GRANT_REVISION,
        NETWORK_LINK_GRANT_SET_ID,
    )

    if not org:
        return []
    now = now or datetime.now(timezone.utc)
    try:
        members = settings_ops.read_owned_set(
            NETWORK_LINK_GRANT_SET_ID,
            org=org,
            target_revision=NETWORK_LINK_GRANT_REVISION,
        ).members
    except Exception:
        logger.debug("design-shares: no readable grant set for org %r", org, exc_info=True)
        return []
    grants = []
    for member in members:
        payload = member.payload
        if not isinstance(payload, dict):
            continue
        if payload.get("target_type") not in wanted:
            continue
        expires_at = _expires_at(payload)
        if expires_at is not None and expires_at <= int(now.timestamp()):
            continue
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        grants.append({
            "token": payload.get("token"),
            "target_uuid": str(payload.get("target_uuid") or ""),
            "target_type": payload.get("target_type"),
            "url": payload.get("url"),
            "label": meta.get("label") or "",
            "issued_at": payload.get("issued_at"),
            "expires_at": expires_at,
        })
    grants.sort(key=lambda g: str(g.get("issued_at") or ""), reverse=True)
    return grants


def active_design_grants(org: str | None, *, now: datetime | None = None) -> list[dict]:
    """Every unexpired design/present grant the org holds, newest first."""
    return active_grants(org, DESIGN_TARGET_TYPES, now=now)


def share_for_target(org: str | None, target_type: str, target_uuid: str,
                     extra_ids: Iterable[str] = ()) -> dict:
    """Share state for any shareable asset: ``{"shared", "grants"}``.

    Design and Present grants are interchangeable (both serve the design's
    latest revision), so a design or deck counts either type; other types
    match only themselves.
    """
    types = DESIGN_TARGET_TYPES if target_type in DESIGN_TARGET_TYPES else (target_type,)
    ids = {str(target_uuid)} | {str(i) for i in extra_ids if i}
    mine = [g for g in active_grants(org, types) if g.get("target_uuid") in ids]
    return {"shared": bool(mine), "grants": mine}


def shared_design_ids(org: str | None, *, now: datetime | None = None) -> set[str]:
    """The target ids (design ids or revision ids) with an active grant."""
    return {g["target_uuid"] for g in active_design_grants(org, now=now) if g["target_uuid"]}


def share_for_design(
    org: str | None,
    design_id: str,
    revision_ids: Iterable[str] = (),
    *,
    grants: list[dict] | None = None,
) -> dict:
    """The share state one design's surface shows.

    ``{"shared": bool, "grants": [...]}`` where grants are the active ones
    that reach this design (newest first).  The first grant is the one a
    "copy link" control should use.
    """
    ids = {str(design_id)} | {str(r) for r in revision_ids if r}
    if grants is None:
        grants = active_design_grants(org)
    mine = [g for g in grants if g.get("target_uuid") in ids]
    return {"shared": bool(mine), "grants": mine}
