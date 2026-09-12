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
import json
import logging
import os
import tempfile
import time
from typing import Optional

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from tools.dashboard import api_auth
from tools.graph.schemas.org import ORG_REVISION, ORG_SET_ID

logger = logging.getLogger(__name__)

#: The Charter screen owns only the org's text/color identity. Icon fields are
#: server-owned and set exclusively through the icon routes below; the Charter
#: writer allowlists these and never reads an icon reference from its body.
_CHARTER_ALLOWED_FIELDS = ("name", "byline", "color", "description", "type")

#: Fields the icon routes own. ``favicon`` is the legacy path/URL kept
#: read-compatible during migration; a new upload clears it and writes the two
#: portable fields, and remove clears all three (auto-j1y0z).
_SERVER_OWNED_ICON_FIELDS = ("icon_attachment_id", "icon_data_uri", "favicon")


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
            channel_pub = payload.get("channel_pub")
            grants[str(invite_ref)] = {
                "url": str(url),
                "label": str(label) if label else None,
                "bearer": str(bearer) if bearer else None,
                # The per-link channel PUBLIC key (NetworkLinkGrantV6). The
                # screen combines it with the bearer, via the shared
                # serializer, into the complete #k=..&t=.. viewer URL; absent
                # on legacy keyless links.
                "channel_pub": str(channel_pub) if channel_pub else None,
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
                # The per-link channel public key; the screen needs BOTH it and
                # the bearer to build a complete invitation URL
                # (graph://4f9e881c-a9 §3). Absent on legacy keyless links.
                "channel_pub": (grants.get(invite_id) or {}).get("channel_pub"),
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


def _current_identity_payload(slug: str) -> dict:
    """The org's current resolved ``autonomy.org`` payload, or ``{}``.

    Reads through :func:`org_ops.show_org`, which selects the highest-precedence
    identity row (canonical first, then higher schema revision) — the same row
    ``GET /api/orgs/{slug}`` returns. Icon and Charter writes both read-merge
    against this so neither erases what the other owns.
    """
    from tools.graph import org_ops
    detail = org_ops.show_org(slug)
    identity = (detail or {}).get("identity") or {}
    payload = identity.get("payload")
    return dict(payload) if isinstance(payload, dict) else {}


async def put_charter(request: Request) -> JSONResponse:
    """PUT /api/orgs/{slug}/charter — write the org's text/color identity.

    The Charter screen's one write (auto-bkoe6): it owns the org's text and
    color identity ONLY. It allowlists ``name`` (required), ``byline`` (capped
    at 60), ``color``, the ``description`` (capped at 4000), and ``type``,
    read-merges the current row so it preserves the server-owned icon fields
    (``icon_attachment_id``/``icon_data_uri``/``favicon``), and upserts the
    org's own ``autonomy.org`` base row at the current revision + canonical
    state, so the charter edit outranks the founding seed everywhere the
    identity cascade reads. Icon references are set only through the icon
    routes; the Charter body cannot set OR erase them (auto-j1y0z).
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
    # The Charter never accepts an icon reference from its JSON body — those
    # are server-owned. Refuse rather than silently drop, so a client that
    # tries learns the field is not theirs to set.
    forbidden = [k for k in _SERVER_OWNED_ICON_FIELDS if k in payload]
    if forbidden:
        return JSONResponse(
            {
                "error": (
                    "the charter cannot set icon fields "
                    f"({', '.join(sorted(forbidden))}); use the icon routes"
                ),
                "code": "icon_field_forbidden",
            },
            status_code=400,
        )

    def _write() -> str:
        from tools.graph import org_ops, settings_ops
        if org_ops.get_org(slug) is None:
            raise LookupError(slug)
        current = _current_identity_payload(slug)
        merged: dict = {}
        # Preserve server-owned icon fields the icon routes wrote.
        for field in _SERVER_OWNED_ICON_FIELDS:
            value = current.get(field)
            if value not in (None, ""):
                merged[field] = value
        # Overlay only the allowlisted text/color fields from the body.
        for field in _CHARTER_ALLOWED_FIELDS:
            if field in payload:
                merged[field] = payload[field]
        return settings_ops.upsert_by_key(
            ORG_SET_ID, ORG_REVISION, slug, merged,
            org=slug, state="canonical",
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


def _safe_unlink(path: str | None) -> None:
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


async def _parse_icon_form(request: Request):
    """Return ``(icon_bytes, crop_text)`` from the multipart request.

    Uses Starlette's parser when ``python-multipart`` is present, and a small
    stdlib fallback otherwise (some host/test environments omit the optional
    package). Raises :class:`ValueError` for a non-multipart or malformed body;
    a missing ``icon`` or ``crop`` part comes back as ``None`` for the caller
    to turn into a specific 400.
    """
    icon_bytes: bytes | None = None
    crop_text: str | None = None
    try:
        form = await request.form()
    except AssertionError as exc:
        if "python-multipart" not in str(exc):
            raise
        return await _parse_icon_form_fallback(request)

    upload = form.get("icon")
    if upload is not None and hasattr(upload, "read"):
        icon_bytes = await upload.read()
    crop_value = form.get("crop")
    if isinstance(crop_value, str):
        crop_text = crop_value
    return icon_bytes, crop_text


async def _parse_icon_form_fallback(request: Request):
    from email.parser import BytesParser
    from email.policy import default as email_policy

    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" not in content_type:
        raise ValueError("invalid multipart form")
    body = await request.body()
    envelope = (
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
        + body
    )
    message = BytesParser(policy=email_policy).parsebytes(envelope)
    if not message.is_multipart():
        raise ValueError("invalid multipart form")
    icon_bytes: bytes | None = None
    crop_text: str | None = None
    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data":
            continue
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        content = part.get_payload(decode=True) or b""
        if str(name) == "icon" and part.get_filename() is not None:
            icon_bytes = content
        elif str(name) == "crop":
            charset = part.get_content_charset() or "utf-8"
            try:
                crop_text = content.decode(charset)
            except UnicodeDecodeError:
                crop_text = content.decode("utf-8", errors="replace")
    return icon_bytes, crop_text


async def post_org_icon(request: Request) -> JSONResponse:
    """POST /api/orgs/{slug}/icon — normalize + store the org's portable icon.

    Multipart with a required ``icon`` file and a required ``crop`` JSON part
    (``{x, y, size}`` normalized coordinates in the EXIF-oriented image). The
    bytes are normalized through the shared :mod:`profile_image` seam to the
    canonical 512x512 WebP plus a bounded 64x64 data-URI derivative; the
    canonical bytes are stored in THIS org's own attachment store and the
    org's ``autonomy.org`` row is updated in place with the two icon fields,
    clearing the legacy ``favicon``. Every other field is preserved.

    Returns ``{ok, icon_attachment_id, icon_data_uri}`` on success or
    ``{error, code}`` on failure — never a filename or raw bytes. A processing
    or storage failure leaves the prior active icon untouched; an orphaned
    immutable attachment after a final-write failure is acceptable.
    """
    refusal = api_auth.require_global_api_authority(request)
    if refusal is not None:
        return refusal
    slug = request.path_params.get("slug") or ""

    from tools.dashboard import profile_image

    try:
        icon_bytes, crop_text = await _parse_icon_form(request)
    except Exception:
        return JSONResponse(
            {"error": "the request must be multipart/form-data",
             "code": "bad_request"},
            status_code=400,
        )
    if not icon_bytes:
        return JSONResponse(
            {"error": "an icon file is required", "code": "missing_icon"},
            status_code=400,
        )
    if crop_text is None:
        return JSONResponse(
            {"error": "a crop is required", "code": "missing_crop"},
            status_code=400,
        )
    try:
        crop_obj = json.loads(crop_text)
    except Exception:
        return JSONResponse(
            {"error": "crop must be a JSON object", "code": "invalid_crop"},
            status_code=400,
        )

    def _process_and_store() -> tuple[str, str]:
        from tools.graph import org_ops, ops as graph_ops, settings_ops
        if org_ops.get_org(slug) is None:
            raise LookupError(slug)
        # Normalize FIRST — a bad image or crop never creates an attachment.
        processed = profile_image.process_profile_image(icon_bytes, crop_obj)
        current = _current_identity_payload(slug)
        alt = f"Icon of {current.get('name') or slug}"
        tmp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                delete=False, suffix=".webp"
            ) as tmp:
                tmp.write(processed.canonical_webp)
                tmp_path = tmp.name
            att = graph_ops.attach_file(
                tmp_path, org=slug, alt_text=alt,
                original_filename="org-icon.webp",
            )
        finally:
            _safe_unlink(tmp_path)
        attachment_id = att["id"]
        # Read-merge: keep every non-icon field, set the two portable icon
        # fields, and clear the legacy favicon path/URL.
        payload = {
            k: v for k, v in current.items()
            if k not in _SERVER_OWNED_ICON_FIELDS
        }
        payload["icon_attachment_id"] = attachment_id
        payload["icon_data_uri"] = processed.compact_data_uri
        settings_ops.upsert_by_key(
            ORG_SET_ID, ORG_REVISION, slug, payload,
            org=slug, state="canonical",
        )
        return attachment_id, processed.compact_data_uri

    try:
        attachment_id, data_uri = await asyncio.to_thread(_process_and_store)
    except LookupError:
        return JSONResponse({"error": "not found"}, status_code=404)
    except profile_image.ProfileImageError as exc:
        return JSONResponse(
            {"error": str(exc), "code": exc.code}, status_code=400
        )
    except Exception:
        logger.exception("org icon upload failed for %s", slug)
        return JSONResponse(
            {"error": "the icon could not be stored", "code": "store_failed"},
            status_code=500,
        )
    return JSONResponse({
        "ok": True,
        "icon_attachment_id": attachment_id,
        "icon_data_uri": data_uri,
    })


async def delete_org_icon(request: Request) -> JSONResponse:
    """DELETE /api/orgs/{slug}/icon — clear the org's active icon references.

    Clears ``icon_attachment_id``, ``icon_data_uri``, and the legacy
    ``favicon`` from the org's ``autonomy.org`` row, preserving every other
    field. The immutable attachment bytes are NOT deleted — a stored icon is
    content-addressed and may be shared; only the active references are
    dropped. Idempotent: an org with no icon returns ``{ok}`` unchanged.
    """
    refusal = api_auth.require_global_api_authority(request)
    if refusal is not None:
        return refusal
    slug = request.path_params.get("slug") or ""

    def _clear() -> None:
        from tools.graph import org_ops, settings_ops
        if org_ops.get_org(slug) is None:
            raise LookupError(slug)
        current = _current_identity_payload(slug)
        if not any(current.get(f) for f in _SERVER_OWNED_ICON_FIELDS):
            return
        payload = {
            k: v for k, v in current.items()
            if k not in _SERVER_OWNED_ICON_FIELDS
        }
        settings_ops.upsert_by_key(
            ORG_SET_ID, ORG_REVISION, slug, payload,
            org=slug, state="canonical",
        )

    try:
        await asyncio.to_thread(_clear)
    except LookupError:
        return JSONResponse({"error": "not found"}, status_code=404)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True})


ROUTES = [
    Route("/api/orgs/{slug}/membership", get_membership, methods=["GET"]),
    Route("/api/orgs/{slug}/charter", put_charter, methods=["PUT"]),
    Route("/api/orgs/{slug}/icon", post_org_icon, methods=["POST"]),
    Route("/api/orgs/{slug}/icon", delete_org_icon, methods=["DELETE"]),
]
