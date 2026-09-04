"""The Membership screen's read model: one projection over the org ledger.

The browser never folds events or reads staging rows itself — this route
returns everything the Organization Settings Membership screen renders:
members, role definitions, invitations (status plus the payload facts the
fold's status map deliberately omits), staged pending claims with their
countersign progress, and the viewer's own persona. Decision note
graph://9282a825-4ce; epic auto-6v1l3.

Every field derives from a real store: the ledger fold (run with the server
clock so invite expiry is honest), the pending-claim staging table, the org's
own link-grant cache (which never holds a bearer), and the self-authored
member-profile directory. A fact without a source is null, never a guess.
"""
from __future__ import annotations

import asyncio
import logging
import time

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from tools.dashboard import api_auth

logger = logging.getLogger(__name__)


def _member_profiles(slug: str) -> dict:
    """persona_pub -> chosen presentation from the self-authored directory set
    (display_name, avatar — an attachment id or absolute URL — and color)."""
    from tools.graph import settings_ops
    from tools.graph.schemas.org_member_profile import MEMBER_PROFILE_SET_ID

    try:
        members = settings_ops.read_owned_set(
            MEMBER_PROFILE_SET_ID, org=slug
        ).members
    except Exception:
        return {}
    profiles = {}
    for member in members:
        payload = member.payload or {}
        name = payload.get("display_name")
        if isinstance(name, str) and name:
            profiles[str(member.key)] = {
                "display_name": name,
                "avatar": payload.get("avatar") or None,
                "color": payload.get("color") or None,
            }
    return profiles


def _join_urls(slug: str) -> dict:
    """invite_ref -> published join URL from the org's own grant cache.

    The cache row never contains the bearer (it rides only the minting
    browser's URL fragment), so this join is safe to serve.
    """
    from tools.graph import settings_ops
    from tools.graph.schemas.network_identity import (
        NETWORK_LINK_GRANT_REVISION,
        NETWORK_LINK_GRANT_SET_ID,
    )

    try:
        members = settings_ops.read_owned_set(
            NETWORK_LINK_GRANT_SET_ID,
            org=slug,
            target_revision=NETWORK_LINK_GRANT_REVISION,
        ).members
    except Exception:
        return {}
    urls = {}
    for member in members:
        payload = member.payload or {}
        invite_ref = payload.get("invite_ref")
        url = payload.get("url")
        if payload.get("target_type") == "org:join" and invite_ref and url:
            urls[str(invite_ref)] = str(url)
    return urls


def _membership_view(slug: str) -> dict:
    from tools.network.ledger.store import LedgerStore, org_ledger_db_path

    path = org_ledger_db_path(slug)
    if not path.exists():
        return {"founded": False}
    now_ms = int(time.time() * 1000)
    profiles = _member_profiles(slug)
    join_urls = _join_urls(slug)
    with LedgerStore(path) as store:
        state = store.fold(now=now_ms)
        members = [
            {
                "persona": persona,
                "display_name": (profiles.get(persona) or {}).get("display_name"),
                "avatar": (profiles.get(persona) or {}).get("avatar"),
                "color": (profiles.get(persona) or {}).get("color"),
                "roles": list(view.roles),
                "sponsor": view.sponsor,
                "current_key": view.current_key,
            }
            for persona, view in sorted(state.members.items())
        ]
        role_defs = [
            {
                "name": name,
                "claim_requires": role.claim_requires,
                "approver_threshold": role.approver_threshold,
                "scope_set": list(role.scope_set),
            }
            for name, role in sorted(state.role_defs.items())
        ]
        invites = []
        for invite_id, status in sorted(state.invites.items()):
            try:
                payload = store.get(invite_id).payload
            except Exception:
                # An invite the fold reports but the store cannot read is a
                # replica defect; surface the status honestly with no facts.
                payload = {}
            invites.append({
                "invite_id": invite_id,
                "status": status,
                "granted_role": payload.get("granted_role"),
                "expiry": payload.get("expiry"),
                "sponsor": payload.get("sponsor"),
                "binding": (
                    None if not payload
                    else "key" if payload.get("invite_pub") else "bearer"
                ),
                "join_url": join_urls.get(invite_id),
            })
        pending = []
        for record in store.list_pending_claims():
            body = record["body"]
            profile = body.get("profile") if isinstance(body, dict) else None
            row = {
                "claim_key": record["claim_key"],
                "persona_pub": record["persona_pub"],
                "invite_ref": record["invite_ref"],
                "submitted_at": record["staged_at"],
                "body": body,
                # The claimant's persona-SIGNED self-description ("I am
                # Dean"). Self-reported: vouched only by possession of the
                # invite the operator sent them.
                "profile": profile if isinstance(profile, dict) else None,
                "granted_role": None,
                "have": None,
                "need": None,
                "ready": None,
            }
            try:
                row["granted_role"] = (
                    store.get(record["invite_ref"]).payload.get("granted_role")
                )
            except Exception:
                pass
            try:
                readiness = store.evaluate_pending_claim(
                    record["claim_key"], now=now_ms
                )
            except Exception:
                readiness = None
            if isinstance(readiness, dict):
                if readiness.get("reason") in ("claim-expired", "legacy-staging"):
                    # Terminal staging rows are not requests anyone can act
                    # on; they age out of the table on their own.
                    continue
                row["have"] = readiness.get("have")
                row["need"] = readiness.get("need")
                row["ready"] = readiness.get("ready")
            pending.append(row)
        genesis_id = state.genesis_id
        heads = list(state.heads)
    from tools.graph import org_ops

    viewer_persona = None
    try:
        viewer_persona = org_ops.persona_pub_for_org(genesis_id)
    except Exception:
        viewer_persona = None
    return {
        "founded": True,
        "genesis_id": genesis_id,
        "heads": heads,
        "members": members,
        "role_defs": role_defs,
        "invites": invites,
        "pending_claims": pending,
        "viewer_persona": viewer_persona,
    }


async def get_membership(request: Request) -> JSONResponse:
    refusal = api_auth.require_global_api_authority(request)
    if refusal is not None:
        return refusal
    slug = request.path_params.get("slug") or ""
    try:
        view = await asyncio.to_thread(_membership_view, slug)
    except Exception:
        logger.exception("membership projection failed for %s", slug)
        return JSONResponse(
            {"error": "membership state is unavailable"}, status_code=503
        )
    return JSONResponse(view)


ROUTES = [
    Route("/api/orgs/{slug}/membership", get_membership, methods=["GET"]),
]
