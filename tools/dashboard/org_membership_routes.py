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
from typing import Optional

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


def _link_grants(slug: str) -> dict:
    """invite_ref -> {url, label, bearer} from the org's own grant cache.

    The bearer is retained on the grant row (graph://e75ebdde-6df) so the
    screen can re-render a redeemable link rather than losing it after one
    showing; possession of a link buys only the right to ask, because
    admission still requires a countersignature. It is absent on invitations
    minted before that ruling. The label is the human name the operator gave
    the link at publish time (meta.label).
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
    grants = {}
    for member in members:
        payload = member.payload or {}
        invite_ref = payload.get("invite_ref")
        url = payload.get("url")
        if payload.get("target_type") == "org:join" and invite_ref and url:
            label = (payload.get("meta") or {}).get("label")
            bearer = payload.get("bearer")
            grants[str(invite_ref)] = {
                "url": str(url),
                "label": str(label) if label else None,
                "bearer": str(bearer) if bearer else None,
            }
    return grants


def _org_key_owner_kem_pub(slug: str) -> Optional[str]:
    """The ``owner_kem_pub`` of the org's sealed root key, or None when the
    org has no revision-2 armor (password-armored roots carry none)."""
    from tools.graph import settings_ops
    from tools.graph.schemas.network_identity import NETWORK_ORG_KEY_SET_ID

    try:
        members = settings_ops.read_owned_set(
            NETWORK_ORG_KEY_SET_ID, org=slug,
        ).members
    except Exception:
        return None
    for member in members:
        value = (member.payload or {}).get("owner_kem_pub")
        if isinstance(value, str) and value:
            return value
    return None


def _membership_view(slug: str) -> dict:
    from tools.graph import org_ops as org_ops_module
    from tools.network.ledger.fold import scope_invite, scope_role_grant
    from tools.network.ledger.projections import unassemblable_thresholds
    from tools.network.ledger.store import LedgerStore, org_ledger_db_path

    path = org_ledger_db_path(slug)
    if not path.exists():
        return {"founded": False}
    now_ms = int(time.time() * 1000)
    profiles = _member_profiles(slug)
    grants = _link_grants(slug)
    with LedgerStore(path) as store:
        state = store.fold(now=now_ms)
        invite_uses = getattr(state, "invite_uses", {}) or {}
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
        # The viewer's authority, read from the same fold as everything
        # else: which roles they may invite for or grant. A viewer who is not
        # a member (or whose persona cannot be resolved) may do neither.
        viewer_key = None
        try:
            viewer_lookup = org_ops_module.persona_pub_for_org(state.genesis_id)
        except Exception:
            viewer_lookup = None
        if viewer_lookup and viewer_lookup in state.members:
            viewer_key = state.members[viewer_lookup].current_key
        holders_by_role: dict[str, list[str]] = {}
        for persona, view in state.members.items():
            for role_name in view.roles:
                holders_by_role.setdefault(role_name, []).append(persona)
        bare_by_role: dict[str, list[str]] = {}
        for key, roles in state.bare_roles.items():
            if key in state.members:
                continue
            for role_name in roles:
                bare_by_role.setdefault(role_name, []).append(key)
        warnings = {
            warning["role"]: warning
            for warning in unassemblable_thresholds(state)
        }
        role_defs = [
            {
                "name": name,
                "version": role.version,
                "claim_requires": role.claim_requires,
                "approver_threshold": role.approver_threshold,
                "scope_set": list(role.scope_set),
                # Personas holding the role (members), and any bare keys a
                # role.grant named that never claimed membership.
                "holders": sorted(holders_by_role.get(name, [])),
                "bare_holders": sorted(bare_by_role.get(name, [])),
                # What the VIEWER may do with this role, decided by the fold.
                "minter_may_invite": bool(
                    viewer_key and state.holds(viewer_key, scope_invite(name))
                ),
                "viewer_may_grant": bool(
                    viewer_key and state.holds(viewer_key, scope_role_grant(name))
                ),
                # Present when the admission threshold exceeds the members
                # who could approve — legal and dormant, but worth a chip.
                "threshold_warning": warnings.get(name),
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
                # {max_uses, used, remaining} — same keyset as the status map;
                # used counts admitted members only (autonomy@f60c26a).
                "uses": invite_uses.get(invite_id),
                "join_url": (grants.get(invite_id) or {}).get("url"),
                # The retained bearer, so the screen can re-render a
                # redeemable link (graph://e75ebdde-6df). Absent on
                # invitations minted before bearers were retained.
                "bearer": (grants.get(invite_id) or {}).get("bearer"),
                "label": (grants.get(invite_id) or {}).get("label"),
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
                # The staged claim body is deliberately NOT served. A
                # token-bound claim carries the invitation's bearer secret in
                # ``body["token"]`` (events.py ``_v_member_claim``), and the
                # screen never reads the body: countersigning needs only
                # ``invite_ref`` and ``persona_pub``, which it signs over
                # (ceremony/claim.js ``signClaimApproval``). Serving the body
                # published a live credential to every reader of this route
                # for no consumer.
                # The claimant's persona-signed self-introduction ("I am
                # Dean" + optional avatar). Interim source: the claim body's
                # vestigial profile field; auto-zxcvr moves it to the
                # detached staging-row packet without changing this contract
                # (ledger-purity ruling, graph://9282a825-4ce comments).
                "introduction": profile if isinstance(profile, dict) else None,
                # Provenance: the human name of the link this request came in
                # on, and the invite's binding (bearer link vs key-bound —
                # crucial when an invitation was issued to a specific
                # identity; email binding joins this vocabulary later).
                "invite_label": (
                    (grants.get(record["invite_ref"]) or {}).get("label")
                ),
                "invite_binding": None,
                "granted_role": None,
                "have": None,
                "need": None,
                "ready": None,
            }
            try:
                invite_payload = store.get(record["invite_ref"]).payload
                row["granted_role"] = invite_payload.get("granted_role")
                row["invite_binding"] = (
                    "key" if invite_payload.get("invite_pub") else "bearer"
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
        org_uuid = state.org
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
        # The genesis event's org UUID — the link_publish target for
        # publishing a join route (mint ceremony, auto-aopjw).
        "org_uuid": org_uuid,
        "heads": heads,
        "members": members,
        "role_defs": role_defs,
        "invites": invites,
        "pending_claims": pending,
        "viewer_persona": viewer_persona,
        # The encapsulation key the org root is sealed to. The browser
        # compares it with the key it derives from the operator's personal
        # root to decide whether "Define role" can run here; the server
        # never decides that, because it never holds the personal root.
        "org_key_owner_kem_pub": _org_key_owner_kem_pub(slug),
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


async def put_charter(request: Request) -> JSONResponse:
    """PUT /api/orgs/{slug}/charter — write the org identity Setting.

    The Charter screen's one write (auto-bkoe6): validates the whole
    payload against ``autonomy.org#2`` (name required; byline capped at
    60; the revision-2 ``description`` capped at 4000) and upserts the
    org's own ``autonomy.org`` base row at canonical state, so the
    charter edit outranks the founding seed everywhere the identity
    cascade reads. ``GET /api/orgs/{slug}`` already returns the resolved
    identity; this is the missing half of the round trip.
    """
    refusal = api_auth.require_global_api_authority(request)
    if refusal is not None:
        return refusal
    slug = request.path_params.get("slug") or ""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "body must be JSON"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "body must be an object"}, status_code=400)

    def _write() -> str:
        from tools.graph import org_ops, settings_ops
        if org_ops.get_org(slug) is None:
            raise LookupError(slug)
        return settings_ops.upsert_by_key(
            "autonomy.org", 2, slug, payload, org=slug, state="canonical",
        )

    try:
        setting_id = await asyncio.to_thread(_write)
    except LookupError:
        return JSONResponse({"error": "not found"}, status_code=404)
    except Exception as exc:
        # SchemaValidationError carries the field-level message the form
        # renders verbatim; anything else is a 400 with its text too.
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "setting_id": setting_id})


ROUTES = [
    Route("/api/orgs/{slug}/membership", get_membership, methods=["GET"]),
    Route("/api/orgs/{slug}/charter", put_charter, methods=["PUT"]),
]
