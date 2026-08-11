"""``link_publish`` / ``link_revoke`` approval kinds — the C3 share-link ceremony.

The share-link publish flow rides the generalized approval primitive
(``approvals_routes``) exactly the way commit signing does, with the roles
split the same way:

* the **requester** (``graph link publish`` / ``revoke``, see
  ``tools/graph/link_cmd.py``) posts a pending request and blocks on the
  held GET;
* the **operator's browser** reviews WHAT is being shared (the enrichment
  below resolves the target's real title and preview from trusted local
  stores — the requesting agent cannot spoof them), unlocks the existing
  browser signer on demand, and posts the signed envelope in the decision;
* the **executor** below forwards that envelope to the auto.network
  registry (``POST /v1/links`` / ``DELETE /v1/links/{token}``, spec §4.4),
  caches the issued grant to ``autonomy.network.link-grant`` (the I9
  serving cache), and returns the share URL through the approval result to
  the waiting CLI.

Session-key seam (C2 dependency): the browser-side signature comes from
``window.AutonomyNetworkSigner`` (see ``pages/worktrees.js``). Until the C2
sign-on ceremony lands and installs a real signer, approving from a live
browser yields a clean "no operator session key" error; the executor
likewise refuses a decision that carries no envelope. Nothing here ever
signs server-side — the session key must never exist outside the operator's
browser (spec §3, I1 discipline applied to the delegated key).

Invariants enforced here:

* **I2** — the grant token is minted by the registry from CSPRNG; the
  executor refuses to cache anything that is not 32 lowercase hex, so a
  misbehaving registry cannot plant a target-derived token in the cache.
* **I6** — every cached grant records the issuing delegation certificate's
  subject. Root-direct envelopes (no cert) are refused: a grant must trace
  to a *named* subject, not just to the org key.
* staged-request integrity — the envelope's payload must equal the payload
  derived from the *stored* request row; what the operator saw is exactly
  what gets published.
* audience freezing (confused-deputy guard) — the registry envelope binds
  only method/path/payload, not the destination host. So the FIRST render
  of the dialog freezes the full staged request server-side (method, path,
  payload, ``registry_url``, and a binding snapshot incl. ``root_pub``)
  onto the approval row, write-once. Execution takes its destination from
  that frozen snapshot ONLY — never from the decision body (client-
  controlled: a compromised browser could claim any audience) and never
  from a re-read of the mutable binding — and additionally REFUSES when
  the current binding has drifted from the snapshot (registry_url,
  org_uuid, or root_pub), so a swap between render and approval surfaces
  as a "re-run the publish", never as a silent redirect. What the operator
  was shown is exactly what executes.
"""

from __future__ import annotations

import asyncio
import copy
import re
import time
import uuid

import httpx

from tools.dashboard.dao import approval_requests as ar
from tools.graph import settings_ops
# Importing registers the autonomy.network.* Setting schemas (they
# self-register on import), so grant-cache writes validate.
from tools.graph.schemas.network_identity import (  # noqa: F401
    NETWORK_BINDING_SET_ID,
    NETWORK_LINK_GRANT_REVISION,
    NETWORK_LINK_GRANT_SET_ID,
    NETWORK_ORG_KEY_SET_ID,
    TARGET_TYPES,
)

_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")  # 128-bit CSPRNG token shape (I2)
MAX_LINK_TTL_S = 365 * 24 * 60 * 60

_TYPE_LABELS = {
    "present": "Present deck",
    "design": "Design",
    "mission": "Mission",
    "note": "Note",
    "file": "File",
    "org:join": "Invitation",
}


# ── trusted lookups ───────────────────────────────────────────


def _load_binding(org: str | None) -> tuple[dict | None, str | None]:
    """The org's auto.network binding row → (payload, error).

    One binding is the common case; with several, the lexically first
    registry key wins deterministically (v1: no --registry selector).
    """
    try:
        members = sorted(
            settings_ops.read_owned_set(NETWORK_BINDING_SET_ID, org=org).members,
            key=lambda m: m.key,
        )
    except Exception as e:
        return None, f"could not read the org's network binding: {e}"
    if not members:
        return None, (
            "This organization is not registered on auto.network yet."
        )
    payload = members[0].payload
    if not isinstance(payload, dict) or not payload.get("registry_url"):
        return None, "the org's network binding row is malformed"
    return payload, None


def _org_has_key(org: str | None) -> bool:
    """True when the org holds a stored signing key (armor present).

    Owning-scope read (P2): a peer-published org-key row must never make
    this org look keyed and offer inline registration on a key it does
    not own — the operator's password would not open a peer's armor.
    """
    try:
        members = settings_ops.read_owned_set(NETWORK_ORG_KEY_SET_ID, org=org).members
    except Exception:
        return False
    # The org root is stored either password-armored (armored_private_key) or
    # sealed to the owner's personal-root-derived X25519 key (sealed_root_key,
    # the B4 Option-B scheme). Recognise BOTH — matching network_routes.py's
    # keyed-check and the browser's _openOrgRoot — so an org keyed with the
    # sealed scheme is still offered inline first-publish registration.
    return any(isinstance(m.payload, dict) and
               (m.payload.get("armored_private_key") or m.payload.get("sealed_root_key"))
               for m in members)


def _is_registerable_on_first_publish(org: str | None) -> bool:
    """A keyed-but-unregistered org: it has a signing key and NO binding row
    (missing, not merely malformed). First publish can register its EXISTING
    key inline (browser-side, root-live window) instead of hard-failing.
    A malformed binding row is a real error, not a registerable state."""
    binding, _ = _load_binding(org)
    if binding is not None:
        return False
    try:
        # Owning-scope (P2): distinguish "no binding row" from "malformed
        # row" against THIS org's own DB, matching _load_binding — a peer's
        # row must not decide registerability.
        members = settings_ops.read_owned_set(NETWORK_BINDING_SET_ID, org=org).members
    except Exception:
        return False
    if members:               # a row exists but is malformed → genuine error
        return False
    return _org_has_key(org)


def _org_join_request(request: dict) -> dict:
    """Validate an org:join mint against the org's own invitation ledger."""
    from tools.network.ledger import (
        INVITE_LIVE,
        LedgerError,
        LedgerStore,
        org_ledger_db_path,
    )

    allowed = {
        "org",
        "target_uuid",
        "target_type",
        "invite_ref",
        "expires_at",
        "meta",
    }
    unknown = set(request) - allowed
    if unknown:
        raise ValueError(
            f"org:join publish carries unknown fields: {sorted(unknown)}"
        )
    org = request.get("org")
    invite_ref = request.get("invite_ref")
    target_uuid = request.get("target_uuid")
    expires_at = request.get("expires_at")
    meta = request.get("meta") or {}
    if not isinstance(org, str) or not org:
        raise ValueError("org:join publish requires the local org slug")
    if (
        not isinstance(invite_ref, str)
        or not re.fullmatch(r"[0-9a-f]{64}", invite_ref)
    ):
        raise ValueError("org:join publish requires a lowercase invite_ref")
    try:
        uuid.UUID(str(target_uuid))
    except (ValueError, AttributeError):
        raise ValueError("org:join target_uuid must be the organization UUID")
    if type(expires_at) is not int or expires_at < 0:
        raise ValueError("org:join expires_at must be a unix-ms integer")
    if not isinstance(meta, dict) or "ttl" in meta:
        raise ValueError(
            "org:join lifetime is fixed to the invite; meta.ttl is forbidden"
        )
    if set(meta) - {"label"}:
        raise ValueError("org:join meta may carry only label")

    path = org_ledger_db_path(org)
    if not path.exists():
        raise ValueError("organization authority ledger is not founded")
    try:
        with LedgerStore(path) as store:
            invite = store.get(invite_ref)
            if invite.type != "invite":
                raise ValueError("invite_ref does not name an invitation")
            state = store.fold(now=int(time.time() * 1000))
            if state.invites.get(invite_ref) != INVITE_LIVE:
                raise ValueError("the invitation is no longer live")
            org_uuid = store.get(store.ledger.genesis_id).payload["org"]
            if target_uuid != org_uuid:
                raise ValueError(
                    "org:join target_uuid does not match the founded organization"
                )
            if expires_at != invite.payload["expiry"]:
                raise ValueError(
                    "org:join expires_at must equal the invitation expiry"
                )
            if "token_hash" not in invite.payload:
                raise ValueError("org:join links require a bearer invitation")
            return {
                "title": f"Invitation to {invite.payload['granted_role']}",
                "expiry": expires_at,
                "org_uuid": org_uuid,
            }
    except (KeyError, LedgerError):
        raise ValueError("invite_ref is not in the organization ledger")


def prepare_create(_session: str, request: dict) -> tuple[dict, None]:
    """Fail closed before persisting an invalid org:join publish request."""
    if request.get("target_type") == "org:join":
        _org_join_request(request)
    return copy.deepcopy(request), None


def _resolve_target(
    target_type: str,
    target_uuid: str,
    org: str | None,
    request: dict | None = None,
) -> dict:
    """Resolve what is being shared from TRUSTED local stores.

    Returns ``{"title": str | None, "error": str | None}``. The title is
    what the operator sees in the dialog; a resolution failure is shown
    too — the operator can still decline, but never approves blind.
    """
    try:
        if target_type == "org:join":
            details = _org_join_request(request or {})
            return {
                "title": details["title"],
                "error": None,
            }
        if target_type in ("present", "design"):
            from agents.design_db import get_design
            design = get_design(target_uuid)
            if not design:
                return {"title": None, "error": (
                    f"{_TYPE_LABELS[target_type]} {target_uuid} is not in "
                    "Design Studio — nothing to share"
                )}
            return {"title": design.get("title") or target_uuid, "error": None}
        if target_type == "note":
            from tools.graph import ops as graph_ops
            data = graph_ops.read_source_full(target_uuid, max_chars=0, org=org)
            src = (data or {}).get("source") or {}
            if not src or src.get("type") != "note":
                return {"title": None, "error": f"note {target_uuid} not found in the graph"}
            content = "\n\n".join(
                entry.get("content") or "" for entry in data.get("entries") or []
            )
            title = src.get("title") or src.get("label") or target_uuid
            return {
                "title": title,
                "error": None,
                "preview": {"title": title, "content": content},
            }
        if target_type == "mission":
            from tools.dashboard.dao import mission_control_db
            mission = mission_control_db.get_mission(target_uuid)
            if not mission:
                return {"title": None, "error": f"mission {target_uuid} not found"}
            # The title names the MISSION only. Who the link is prepared for
            # is a separate fact and gets its own field (see _link_recipient)
            # -- folding a person into a target's name would make two
            # different things share one row and render as an explanation
            # rather than as an identity.
            return {"title": mission.get("name") or target_uuid, "error": None}
        if target_type == "file":
            from tools.graph import ops as graph_ops
            att = None
            get_att = getattr(graph_ops, "get_attachment", None)
            if callable(get_att):
                att = get_att(target_uuid, org=org)
            if isinstance(att, dict):
                return {"title": att.get("filename") or target_uuid, "error": None}
            return {"title": target_uuid, "error": None}  # best-effort: id shown as-is
    except Exception as e:
        return {"title": None, "error": f"target resolution failed: {e}"}
    return {"title": None, "error": f"unknown target type {target_type!r}"}


def _approval_identities(org: str | None) -> dict:
    """Trusted acting-org and personal-actor labels for Gate 2."""
    from tools.dashboard.identity_routes import _personal_member
    from tools.dashboard.org_identity import resolve_org_identity

    org_identity = resolve_org_identity(org)
    try:
        personal = _personal_member()
    except Exception:
        personal = None
    payload = personal.payload if personal and isinstance(personal.payload, dict) else {}
    return {
        "acting_identity": {
            "slug": org_identity.get("slug"),
            "name": org_identity.get("name"),
            "initial": org_identity.get("initial"),
            "color": org_identity.get("color"),
            "favicon": org_identity.get("favicon"),
        },
        "actor_identity": {
            "display_name": payload.get("display_name"),
            "root_pub": payload.get("root_pub"),
        },
    }


def _registry_payload(req: dict, binding: dict) -> dict:
    """The exact ``/v1/links`` payload the operator signs — built from the
    STORED request row + the org binding, in one place, so the enrichment
    (what gets signed) and the executor (what gets forwarded) cannot drift."""
    payload = {
        "org": binding["org_uuid"],
        "target_uuid": req["target_uuid"],
        "target_type": req["target_type"],
    }
    meta = req.get("meta") or {}
    if meta:
        payload["meta"] = meta
    if req.get("target_type") == "org:join":
        payload["invite_ref"] = req["invite_ref"]
        payload["expires_at"] = req["expires_at"]
    return payload


# ── GET enrichment (what the operator reviews) ────────────────
#
# The first render FREEZES the staged registry request onto the approval
# row (write-once, server-side). Every later render — and the execution —
# reads the frozen snapshot, so a binding change after first render can
# never move the destination out from under the operator; it can only
# surface as a drift warning here and a refusal at execute.


def _staged_registry_request(row: dict, build) -> tuple[dict | None, str | None, bool]:
    """The frozen staged request for *row* → (staged, binding_error, drift).

    Freezes via *build(binding)* on first render; afterwards returns the
    stored snapshot and flags drift against the current binding.
    """
    org = row["request"].get("org")
    binding, binding_error = _load_binding(org)
    staged = row.get("staged")
    if staged is None:
        if binding is None:
            return None, binding_error, False
        staged = build(binding)
        staged["binding"] = {
            "org_uuid": binding["org_uuid"],
            "root_pub": binding["root_pub"],
            "registry_url": binding["registry_url"],
        }
        ar.set_staged(row["id"], staged)
        # Re-read: a concurrent first render may have won the write-once.
        fresh = ar.get(row["id"])
        staged = (fresh or {}).get("staged") or staged
    drift = binding is not None and _binding_drift_error(staged, binding) is not None
    return staged, None, drift


def _link_recipient(req: dict) -> tuple[dict | None, str | None]:
    """Who a personalized link is being PREPARED FOR — resolved identity,
    not a decorated target name. Returns (recipient, error).

    A mission grant is bound to exactly one guest (``meta.participant_id``),
    and that binding decides whose name lands on every question they ask
    and whose access dies when this link is revoked. The operator is
    approving a link FOR A PERSON, so the person is a first-class field
    the dialog renders as an identity — avatar and all — beside the thing
    being shared, never concatenated into it.

    No color is computed here: ``participantColor`` is deliberately
    view-side (pitfall graph://73af2694-562), and the browser already has
    the same deterministic hash in ``surface-presence.js``. Sending
    ``participant_id`` is what lets the view render the identity, and is
    also the hook a real profile photo slots into later without changing
    this contract.
    """
    participant_id = (req.get("meta") or {}).get("participant_id")
    if not isinstance(participant_id, str) or not participant_id:
        return None, None
    from tools.dashboard.dao import mission_control_db

    visitor = mission_control_db.get_visitor_by_participant_id(participant_id)
    if not visitor:
        return None, (
            f"guest {participant_id} is not a known participant — nothing to "
            "bind this link to"
        )
    attachment_id = visitor.get("avatar_attachment_id")
    return {
        "participant_id": visitor["participant_id"],
        "display_name": visitor["display_name"],
        # A URL into the shared attachment route, never inline bytes: the
        # photo is stored once, hash-deduped, and cached by the browser
        # like any other image.
        "avatar_url": f"/api/attachment/{attachment_id}" if attachment_id else None,
    }, None


def _enrich_link_publish(row: dict) -> dict:
    req = row["request"]
    org = req.get("org")
    meta = req.get("meta") or {}
    target = _resolve_target(
        req.get("target_type", ""),
        req.get("target_uuid", ""),
        org,
        req,
    )
    staged, binding_error, drift = _staged_registry_request(
        row,
        lambda binding: {
            "method": "POST",
            "path": "/v1/links",
            "registry_url": binding["registry_url"],
            "payload": _registry_payload(req, binding),
        },
    )
    # Graceful seam: a keyed-but-unregistered org is NOT an error. The first
    # publish registers its existing key inline (browser-side) and then the
    # normal freeze/execute path runs against the now-live binding. We only
    # surface the non-blocking flag here; no staged request is frozen until a
    # binding exists, so the confused-deputy machinery is untouched.
    registration_required = False
    if staged is None and binding_error and _is_registerable_on_first_publish(org):
        registration_required = True
        binding_error = None
    # Internal precondition (NOT shown in the dialog): whether the approve step
    # must ALSO mint a serve-cert in its single root unlock. True when no usable
    # serve-cert is provisioned. The browser reads it to decide the dual-mint;
    # the operator sees nothing about serving.
    try:
        from tools.dashboard.link_serving_supervisor import serve_cert_ok
        serve_cert_required = not serve_cert_ok(org)
    except Exception:
        serve_cert_required = True  # fail toward minting; a spurious mint is safe
    recipient, recipient_error = _link_recipient(req)
    out = {
        "target_title": target["title"],
        "target_error": target["error"] or recipient_error,
        "recipient": recipient,
        "type_label": _TYPE_LABELS.get(req.get("target_type", ""), req.get("target_type")),
        "ttl": meta.get("ttl"),
        "label": meta.get("label"),
        "absolute_expiry": req.get("expires_at"),
        "binding_error": binding_error,
        "binding_drift": drift,
        "registration_required": registration_required,
        "serve_cert_required": serve_cert_required,
        **_approval_identities(org),
    }
    if target.get("preview"):
        out["target_preview"] = target["preview"]
    if staged:
        out["registry_request"] = {k: staged[k]
                                   for k in ("method", "path", "registry_url", "payload")}
    return out


def _enrich_link_revoke(row: dict) -> dict:
    req = row["request"]
    org = req.get("org")
    token = req.get("token", "")
    # Show which grant dies: resolve the token through the local grant cache.
    grant = _cached_grant(token, org)
    target_title, type_label = None, None
    if grant:
        resolved = _resolve_target(grant.get("target_type", ""),
                                   grant.get("target_uuid", ""), org)
        target_title = resolved["title"] or grant.get("target_uuid")
        type_label = _TYPE_LABELS.get(grant.get("target_type", ""),
                                      grant.get("target_type"))
    staged, binding_error, drift = _staged_registry_request(
        row,
        lambda binding: {
            "method": "DELETE",
            "path": f"/v1/links/{token}",
            "registry_url": binding["registry_url"],
            "payload": {},
        },
    )
    out = {
        "target_title": target_title,
        "type_label": type_label,
        # The raw target type routes the browser's signature: a cached
        # share link is signed over the tunnel proof-of-possession bytes;
        # org:join (and any token not positively classifiable) is signed
        # over the HTTP registry bytes — the same split the executor uses.
        "target_type": grant.get("target_type") if grant else None,
        "label": (grant.get("meta") or {}).get("label") if grant else None,
        "cached": grant is not None,
        "binding_error": binding_error,
        "binding_drift": drift,
    }
    if staged:
        # The revoke payload is empty by contract, so the browser cannot read
        # the org uuid out of it the way publish does — expose the frozen
        # binding's uuid so retained-session matching works for revoke too.
        out["org_uuid"] = (staged.get("binding") or {}).get("org_uuid")
        out["registry_request"] = {k: staged[k]
                                   for k in ("method", "path", "registry_url", "payload")}
    return out


def _cached_grant(token: str, org: str | None) -> dict | None:
    try:
        for m in settings_ops.read_owned_set(
            NETWORK_LINK_GRANT_SET_ID,
            org=org,
            target_revision=NETWORK_LINK_GRANT_REVISION,
        ).members:
            if m.key == token and isinstance(m.payload, dict):
                return m.payload
    except Exception:
        pass
    return None


# ── post-approval executors ───────────────────────────────────


def _registry_client(base_url: str) -> httpx.AsyncClient:
    """Factory seam: tests swap this for an ASGITransport-backed client
    pointed at an in-process registry app."""
    return httpx.AsyncClient(base_url=base_url, timeout=15.0)


def _fail(error: str) -> dict:
    return {"ok": False, "error": error}


def _binding_drift_error(staged: dict, binding: dict) -> str | None:
    """Confused-deputy guard, half two: the frozen snapshot is the ONLY
    execution context, and any drift of the live binding away from it
    (destination, org identity, or root key) refuses the execution rather
    than executing against state the operator never reviewed."""
    frozen = staged.get("binding")
    if not isinstance(frozen, dict):
        return "staged request carries no binding snapshot — refusing to forward"
    drifted = [
        f"{key}: approved {frozen.get(key)!r}, now {binding.get(key)!r}"
        for key in ("registry_url", "org_uuid", "root_pub")
        if frozen.get(key) != binding.get(key)
    ]
    if drifted:
        return (
            "the org's auto.network binding changed between review and "
            "approval (" + "; ".join(drifted) + ") — refusing to forward "
            "the signed request; re-run the publish so the operator reviews "
            "the new destination"
        )
    return None


def _frozen_staged(row: dict) -> tuple[dict | None, str | None]:
    """The server-frozen staged request an execution is allowed to use.

    Nothing client-supplied substitutes for it: if the request was never
    rendered (so never frozen), the execution refuses outright."""
    staged = row.get("staged")
    if not isinstance(staged, dict) or not staged.get("registry_url"):
        return None, (
            "this request was never staged — the dialog render freezes the "
            "exact registry request server-side, and execution refuses to "
            "proceed without that snapshot (confused-deputy guard)"
        )
    return staged, None


def _publish_payload_for_decision(staged: dict, decision: dict) -> tuple[dict | None, str | None]:
    """Rebuild the publish payload from frozen state plus the sole edit: TTL.

    An absent ``ttl`` decision field preserves the staged value for older
    clients. JSON null means no expiration and removes ``meta.ttl``. Every
    other request coordinate remains a deep copy of the server-frozen row.
    """
    payload = copy.deepcopy(staged.get("payload"))
    if not isinstance(payload, dict):
        return None, "staged request carries no publish payload"
    if "ttl" not in decision:
        return payload, None

    ttl = decision.get("ttl")
    if ttl is not None and (
        type(ttl) is not int or ttl <= 0 or ttl > MAX_LINK_TTL_S
    ):
        return None, (
            "link duration must be No expiration or a whole number of "
            "seconds between 1 and 365 days"
        )

    raw_meta = payload.get("meta")
    if raw_meta is not None and not isinstance(raw_meta, dict):
        return None, "staged request metadata is malformed"
    meta = copy.deepcopy(raw_meta or {})
    if ttl is None:
        meta.pop("ttl", None)
    else:
        meta["ttl"] = ttl
    if meta:
        payload["meta"] = meta
    else:
        payload.pop("meta", None)
    return payload, None


def _envelope_and_subject(decision: dict) -> tuple[dict | None, dict | None, str | None]:
    """Validate the decision's signed envelope; return (envelope, subject, error).

    The chain itself is verified authoritatively by the registry (I4); here
    we only require the C2 shape — a cert-bearing envelope — and extract
    the subject for I6 attribution.
    """
    envelope = decision.get("envelope")
    if not isinstance(envelope, dict):
        return None, None, (
            "decision carried no signed approval — unlock the organization "
            "in the browser and try again"
        )
    cert_wire = envelope.get("cert")
    if not isinstance(cert_wire, str) or not cert_wire:
        return None, None, (
            "envelope carries no delegation certificate — every grant must "
            "trace to a named certificate subject (I6); root-direct publish "
            "is not allowed from the dashboard"
        )
    try:
        from tools.network.idkit import DelegationCert
        cert = DelegationCert.from_json(cert_wire)
    except Exception as e:
        return None, None, f"envelope cert does not parse: {e}"
    subject = {"kind": cert.subject.kind, "id": cert.subject.id}
    if subject["kind"] not in ("operator", "agent", "persona"):
        return None, None, (
            f"cert subject kind {subject['kind']!r} cannot issue grants "
            "(operator, agent, or persona subjects only)"
        )
    return envelope, subject, None


#: The proof-of-possession domain a share-link approval envelope signs
#: over on the D19 tunnel path. It is NOT a destination (the tunnel is the
#: destination, register D19) — it is a fixed, non-routable pair whose only
#: job is to bind the session-key signature to these bytes so a stray cert
#: cannot be replayed. The browser signs the same pair; the dashboard
#: reconstructs and verifies it locally.
_TUNNEL_POP_METHOD = "TUNNEL"
_TUNNEL_POP_PATH = "/control/create-link"
_TUNNEL_REVOKE_POP_PATH = "/control/revoke-link"


def _verify_local_publish_authority(
    envelope: dict, subject: dict, org_slug: str, binding: dict,
    required_scope: str, pop_path: str,
) -> str | None:
    """Authenticate the acting persona LOCALLY for a tunnel control op.

    Once publish/revoke ride the authenticated tunnel, the registry no
    longer verifies the publish cert chain (register D19) — so the
    dashboard must, or ``subject.id`` would be an unauthenticated claim a
    compromised browser could forge to any persona. This mirrors the
    registry's own I4 gate (`_authorize`): the envelope signature proves
    possession of the session key over fixed proof-of-possession bytes,
    and the cert must chain to the org's OWN bound root with the required
    scope and delegate to that signer. Returns a refusal string, or None
    when the persona is authenticated AND the fold grants the scope."""
    from tools.network.idkit import (
        DelegationCert,
        IdkitError,
        MalformedError,
        verify_chain,
        verify_signature,
    )
    from tools.network.registry.signing import (
        MAX_CLOCK_SKEW,
        request_signing_input,
    )

    signer = envelope.get("signer")
    ts = envelope.get("ts")
    sig = envelope.get("sig")
    cert_wire = envelope.get("cert")
    if not (isinstance(signer, str) and isinstance(sig, str)
            and isinstance(cert_wire, str) and type(ts) is int):
        return "approval envelope is malformed — unlock the org and retry"
    now = int(time.time())
    if abs(now - ts) > MAX_CLOCK_SKEW:
        return "approval is stale (clock skew) — unlock the org and retry"
    try:
        signing_input = request_signing_input(
            _TUNNEL_POP_METHOD, pop_path, ts, signer, envelope["payload"])
        verify_signature(signer, sig, signing_input)
    except (IdkitError, MalformedError, KeyError):
        return "approval signature does not verify — unlock the org and retry"
    try:
        cert = DelegationCert.from_json(cert_wire)
        if cert.child_pub != signer:
            return "approval cert does not delegate to its signer"
        # Verify at REQUEST time (the same `now` as the freshness check).
        # An earlier version passed the cert's own midpoint, which made the
        # validity window tautologically satisfied — an expired or
        # not-yet-valid cert would pass (Codex D19 finding #1).
        # Authority revocation is enforced sovereignly by the ledger fold
        # below (role.revoke / member.rekey), not by a registry-supplied
        # denylist — the registry is untrusted for authority. The session
        # signing key is non-extractable and short-TTL, so there is no
        # extractable-key-leak threat for the registry denylist to cover.
        verify_chain(
            cert, binding["root_pub"], org=binding["org_uuid"], now=now,
            required_scope=required_scope,
        )
    except (IdkitError, MalformedError) as exc:
        return (
            f"approval cert does not chain to this org's root with "
            f"{required_scope}: {exc}")
    # Rung-1 transport pins the subject kind to 'operator' carrying the
    # persona public key in subject.id (settled D19 representation). A
    # non-operator cert (agent/persona kind) whose id happens to name an
    # authorized persona must not reach mint (Codex D19 finding #4).
    if cert.subject.kind != "operator":
        return (
            f"approval subject kind {cert.subject.kind!r} cannot publish or "
            "revoke on this transport (operator subjects only)")
    if {"kind": cert.subject.kind, "id": cert.subject.id} != subject:
        return "approval subject does not match its certificate"
    # Authenticated: subject.id is now trustworthy for the fold, which
    # authorizes by the dashboard-side org SLUG (its ledger DB), while the
    # chain above verified against the registry-side org UUID.
    return _authorization_refusal(
        org_slug, subject["id"], required_scope,
        "publish share links" if required_scope == "link:publish"
        else "revoke share links")


def _authorization_refusal(
    org: str, persona_pub: str, required_scope: str, action: str,
) -> str | None:
    """Ask the authority ledger; a non-None return is the refusal reason.

    Every path fails closed, but the reasons stay distinct: a genesis-less
    ledger is an operational state — the organization's ledger was never
    founded — not a permission denial. Collapsing it into "not authorized"
    sends the operator auditing roles instead of founding (the 2026-07-30
    autonomy incident: two sessions chased scopes and a phantom wipe
    because this except swallowed GenesisError).
    """
    try:
        from tools.dashboard.org_authority import authorize
        from tools.network.ledger import GenesisError

        authorized = authorize(org, persona_pub, required_scope, at_head=None)
    except GenesisError:
        return (
            f"the {org} authority ledger has no genesis event — the "
            f"organization was never founded, so no persona can {action} "
            f"yet; found it with `graph org retrofit-ledgers`"
        )
    except Exception as exc:
        return f"could not read the {org} authority ledger: {exc}"
    if not authorized:
        return f"{persona_pub} is not authorized to {action} in {org}"
    return None


async def _forward_to_registry(staged: dict, envelope: dict) -> tuple[httpx.Response | None, str | None]:
    """Forward the signed envelope to the FROZEN destination — method, path,
    and registry_url all come from the server-side snapshot, never from the
    decision body or a re-read binding."""
    try:
        async with _registry_client(staged["registry_url"]) as client:
            resp = await client.request(staged["method"], staged["path"], json=envelope)
        return resp, None
    except httpx.HTTPError as e:
        return None, f"could not reach the registry at {staged['registry_url']}: {e}"


def _registry_error(resp: httpx.Response) -> str:
    try:
        detail = resp.json().get("detail", resp.text)
    except Exception:
        detail = resp.text
    return f"registry refused the request ({resp.status_code}): {detail}"


async def _execute_link_publish(row: dict, decision: dict) -> dict:
    req = row["request"]
    org = req.get("org")
    # org:join invitations keep the HTTP publish path (option A): their
    # transport is a separate concern from D19's share-link tunnel move.
    if req.get("target_type") == "org:join":
        try:
            _org_join_request(req)
        except ValueError as exc:
            return _fail(str(exc))
        return await _execute_link_publish_http(row, decision)
    return await _execute_share_link_publish_tunnel(row, decision)


# Control failures that occur BEFORE any frame reaches the registry — the
# connector/listener is still coming up, or the tunnel is not yet dialed — so
# retrying create-link on these can never double-create a link.
_TUNNEL_STARTUP_RETRY_KINDS = frozenset({"no-listener", "unreachable", "no-tunnel"})


def _create_link_over_tunnel(org, args, *, timeout: float = 25.0, poll: float = 0.5):
    """Send the create-link control op, tolerating a connector/tunnel that is
    still coming up on a first publish. Retries ONLY the pre-write failures
    (see _TUNNEL_STARTUP_RETRY_KINDS), so a retry cannot double-create; any
    other failure raises immediately, and the last pre-write failure raises if
    the tunnel never comes up within *timeout*."""
    from tools.dashboard.link_serving_supervisor import TunnelUnavailable, control
    deadline = time.monotonic() + timeout
    while True:
        try:
            return control(org, "create-link", args)
        except TunnelUnavailable as exc:
            if getattr(exc, "kind", None) not in _TUNNEL_STARTUP_RETRY_KINDS \
                    or time.monotonic() >= deadline:
                raise
            time.sleep(poll)


async def _execute_share_link_publish_tunnel(row: dict, decision: dict) -> dict:
    """Publish a share link as a control frame on the org's authenticated
    tunnel (register D19). Authority is proven LOCALLY — the persona is
    authenticated against the org's bound root and the ledger fold grants
    ``link:publish`` — before any frame is emitted or grant written; the
    registry sees only the org, never the persona."""
    from tools.dashboard.link_serving_supervisor import (
        TunnelUnavailable,
        get_supervisor,
    )

    req = row["request"]
    org = req.get("org")
    envelope, subject, err = _envelope_and_subject(decision)
    if err:
        return _fail(err)
    binding, binding_error = _load_binding(org)
    if binding_error:
        return _fail(binding_error)
    refusal = _verify_local_publish_authority(
        envelope, subject, org, binding, "link:publish", _TUNNEL_POP_PATH)
    if refusal:
        return _fail(refusal)

    meta, meta_error = _tunnel_link_meta(req, decision)
    if meta_error:
        return _fail(meta_error)
    args = {
        "target_uuid": req["target_uuid"],
        "target_type": req["target_type"],
    }
    # The RELAY IS UNTRUSTED and is told only what it needs to mint and
    # route a token. participant_id is deliberately withheld from it: the
    # registry never authorizes anything with it (check_grant consults the
    # dashboard's own cache and never the registry), so sending it would
    # hand the relay operator a per-link guest identifier for no gain --
    # exactly the metadata the accepted set (token, org, timing, volume)
    # excludes. It stays in the LOCAL grant below, which is the only copy
    # serving ever reads.
    wire_meta = {k: v for k, v in meta.items() if k not in _LOCAL_ONLY_META}
    if wire_meta:
        args["meta"] = wire_meta
    # First publish is chicken-and-egg: the serving tunnel only runs while a
    # link is live, but the very first link is created BY riding the tunnel.
    # Start serving now (the approve step minted the serve-cert); the
    # supervisor's fresh-tunnel grace keeps the watchdog from reaping it before
    # this publish caches its grant.
    sup = get_supervisor()
    started = await asyncio.to_thread(sup.start, org)
    if not started.get("running"):
        return _fail(
            "could not start the serving tunnel for this publish "
            f"({started.get('reason')})")
    try:
        reply = await asyncio.to_thread(_create_link_over_tunnel, org, args)
    except TunnelUnavailable as exc:
        return _fail(f"the serving tunnel did not come up in time ({exc})")
    if not reply.get("ok"):
        return _fail(reply.get("error", "the registry refused the link"))
    token, url = reply.get("token"), reply.get("url")
    if not isinstance(token, str) or not _TOKEN_RE.match(token):
        return _fail("registry returned a malformed grant token — not caching it")

    grant = {
        "token": token,
        "url": url,
        "target_uuid": req["target_uuid"],
        "target_type": req["target_type"],
        "meta": meta or {},
        "subject": subject,  # I6: the authenticated acting persona
        "issued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    settings_ops.upsert_by_key(
        NETWORK_LINK_GRANT_SET_ID, NETWORK_LINK_GRANT_REVISION,
        token, grant, org=org,
    )
    # The frame round-tripped on the live tunnel, so serving IS live by
    # construction — no separate probe needed on this path (register D19 B10).
    return {
        "ok": True,
        "url": url,
        "token": token,
        "serving": {"live": True, "via": "tunnel-control"},
        "actor": _approval_identities(org)["actor_identity"],
    }


#: Grant meta the dashboard keeps to itself and never puts on the wire to
#: the registry. The relay is untrusted (I5) and authorizes nothing with
#: these — serving reads the LOCAL grant cache only — so shipping them
#: would leak who a link is for while buying nothing.
_LOCAL_ONLY_META = frozenset({"participant_id"})


def _tunnel_link_meta(req: dict, decision: dict) -> tuple[dict, str | None]:
    """The meta a share-link control frame carries.

    The approval sheet's duration selection rides the decision as ``ttl``
    (the same edit the HTTP path applies via _publish_payload_for_decision);
    honor it so the operator's chosen link lifetime actually takes effect.
    Absent ``ttl`` keeps the request's own value; JSON null means no
    expiration and drops meta.ttl. Returns (meta, error).

    ``participant_id`` is carried through because a mission grant is bound
    to exactly one guest identity and the schema REQUIRES it
    (NetworkLinkGrantV3). This allowlist previously named only ttl and
    label, so a mission publish silently lost its binding here and then
    failed its own validation after the operator had already approved --
    the operator saw an approval succeed and a publish fail. Adding a new
    meta key means adding it here too; that is the cost of an allowlist
    and it is the right cost, since an unknown key must never reach a
    grant."""
    base = req.get("meta")
    if base is not None and not isinstance(base, dict):
        return {}, "request metadata is malformed"
    meta = {
        k: base[k]
        for k in ("ttl", "label", "participant_id")
        if k in (base or {})
    }
    if "ttl" in decision:
        ttl = decision.get("ttl")
        if ttl is not None and (
            type(ttl) is not int or ttl <= 0 or ttl > MAX_LINK_TTL_S
        ):
            return {}, (
                "link duration must be No expiration or a whole number of "
                "seconds between 1 and 365 days"
            )
        if ttl is None:
            meta.pop("ttl", None)
        else:
            meta["ttl"] = ttl
    return meta, None


async def _execute_link_publish_http(row: dict, decision: dict) -> dict:
    """The pre-D19 HTTP publish path, retained for org:join invitations
    (option A): the signed envelope is forwarded to the registry, which
    verifies the chain and mints the grant."""
    req = row["request"]
    org = req.get("org")
    envelope, subject, err = _envelope_and_subject(decision)
    if err:
        return _fail(err)
    acting_persona_pub = subject["id"]
    refusal = _authorization_refusal(
        org, acting_persona_pub, "link:publish", "publish share links",
    )
    if refusal:
        return _fail(refusal)
    staged, err = _frozen_staged(row)
    if err:
        return _fail(err)
    binding, binding_error = _load_binding(org)
    if binding_error:
        return _fail(binding_error)
    drift_error = _binding_drift_error(staged, binding)
    if drift_error:
        return _fail(drift_error)
    final_payload, payload_error = _publish_payload_for_decision(staged, decision)
    if payload_error:
        return _fail(payload_error)
    if envelope.get("payload") != final_payload:
        # What was staged (and displayed) is exactly what an approval applies to.
        return _fail("signed payload does not match the staged request — refusing to publish")
    resp, err = await _forward_to_registry(staged, envelope)
    if err:
        return _fail(err)
    if resp.status_code != 201:
        return _fail(_registry_error(resp))
    body = resp.json()
    token, url = body.get("token"), body.get("url")
    if not isinstance(token, str) or not _TOKEN_RE.match(token):
        # I2 tripwire: only a CSPRNG-shaped opaque token enters the cache.
        return _fail("registry returned a malformed grant token — not caching it")
    if (
        req.get("target_type") == "org:join"
        and body.get("expires_at") != req.get("expires_at")
    ):
        return _fail(
            "registry did not preserve the invitation-aligned expiry — "
            "not caching the link"
        )
    grant = {
        "token": token,
        "url": url,
        "target_uuid": req["target_uuid"],
        "target_type": req["target_type"],
        "meta": final_payload.get("meta") or {},
        "subject": subject,  # I6: the issuing cert's subject
        "issued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if req.get("target_type") == "org:join":
        grant["invite_ref"] = req["invite_ref"]
    settings_ops.upsert_by_key(
        NETWORK_LINK_GRANT_SET_ID, NETWORK_LINK_GRANT_REVISION,
        token, grant, org=org,
    )
    # Post-publish trigger: reconcile the serving connector now that a live
    # grant exists (starts it if a serve-cert is provisioned and it isn't
    # already running). Non-fatal and best-effort — a launch failure is not an
    # exception into the publish; the probe below reports the real state, and
    # the watchdog keeps reconciling. Off the event loop: it reads settings,
    # verifies the key, and may spawn a process.
    try:
        from tools.dashboard.link_serving_supervisor import get_supervisor
        await asyncio.to_thread(get_supervisor().ensure, org)
    except Exception:
        pass
    # Final step: prove the link actually serves before reporting success.
    # The grant is already minted (the link exists) — the probe never
    # un-publishes it; it walks the viewer's real path (relay handshake +
    # object HEAD) so the result honestly says whether the tunnel is live,
    # the grant is dead, or the tunnel is unreachable, without transferring
    # the artifact. A down tunnel is a backend concern that self-heals; the
    # publish reports it, it does not fail on it.
    serving = await _probe_serving(binding, token)
    return {
        "ok": True,
        "url": url,
        "token": token,
        "serving": serving,
        "actor": _approval_identities(org)["actor_identity"],
    }


async def _probe_serving(binding: dict, token: str) -> dict:
    """End-to-end liveness probe of a freshly published link. Never raises —
    a probe that cannot run is reported as not-live, never an exception into
    the publish result (the grant is already cached)."""
    from tools.dashboard.link_probe import probe_link, registry_to_relay_ws
    try:
        return await probe_link(
            relay_url=registry_to_relay_ws(binding["registry_url"]),
            token=token,
            root_pub=binding["root_pub"],
            org_uuid=binding["org_uuid"],
        )
    except Exception as e:
        return {"live": False, "status": None, "content_length": None,
                "detail": f"serving probe could not run: {e}"}


async def _execute_link_revoke(row: dict, decision: dict) -> dict:
    req = row["request"]
    org = req.get("org")
    token = req.get("token", "")
    # Route by the cached grant's type: a KNOWN share-link goes over the
    # tunnel; org:join AND any token we cannot positively classify go over
    # HTTP. Defaulting the unknown/cache-miss case to the tunnel (the prior
    # behaviour) let an uncached org:join token reach the tunnel revoke,
    # violating option A's "org:join never rides the tunnel" (Codex D19
    # finding #5). HTTP is the safe default: the registry's DELETE revokes
    # any token by id, so an uncached share link still revokes correctly,
    # and an org:join token never crosses to the tunnel.
    grant = _cached_grant(token, org)
    is_share_link = bool(
        grant and grant.get("target_type")
        and grant.get("target_type") != "org:join")
    if is_share_link:
        return await _execute_share_link_revoke_tunnel(row, decision)
    return await _execute_link_revoke_http(row, decision)


async def _execute_share_link_revoke_tunnel(row: dict, decision: dict) -> dict:
    """Revoke a share link as a control frame on the org tunnel (D19).
    Authority is proven locally, exactly as publish; the registry checks
    only that the token's grant belongs to the tunnel's org."""
    from tools.dashboard.link_serving_supervisor import TunnelUnavailable, control

    req = row["request"]
    org = req.get("org")
    token = req.get("token", "")
    envelope, subject, err = _envelope_and_subject(decision)
    if err:
        return _fail(err)
    binding, binding_error = _load_binding(org)
    if binding_error:
        return _fail(binding_error)
    refusal = _verify_local_publish_authority(
        envelope, subject, org, binding, "link:revoke", _TUNNEL_REVOKE_POP_PATH)
    if refusal:
        return _fail(refusal)
    try:
        reply = await asyncio.to_thread(control, org, "revoke-link", {"token": token})
    except TunnelUnavailable as exc:
        return _fail(f"serving tunnel is not up — revoke rides the tunnel ({exc})")
    # An "unknown link" reply means the registry already has no such grant;
    # the local cache row must still die so the dashboard stops serving it.
    if not reply.get("ok") and "unknown link" not in (reply.get("error") or ""):
        return _fail(reply.get("error", "the registry refused the revoke"))
    removed = _drop_cached_grant(token, org)
    return {"ok": True, "token": token, "via": "tunnel-control",
            "cache_removed": removed}


async def _execute_link_revoke_http(row: dict, decision: dict) -> dict:
    """The pre-D19 HTTP revoke path, retained for org:join grants."""
    req = row["request"]
    org = req.get("org")
    token = req.get("token", "")
    envelope, subject, err = _envelope_and_subject(decision)
    if err:
        return _fail(err)
    acting_persona_pub = subject["id"]
    refusal = _authorization_refusal(
        org, acting_persona_pub, "link:revoke", "revoke share links",
    )
    if refusal:
        return _fail(refusal)
    staged, err = _frozen_staged(row)
    if err:
        return _fail(err)
    binding, binding_error = _load_binding(org)
    if binding_error:
        return _fail(binding_error)
    drift_error = _binding_drift_error(staged, binding)
    if drift_error:
        return _fail(drift_error)
    if envelope.get("payload") != {}:
        return _fail("revoke envelopes carry an empty payload — refusing to forward")
    resp, err = await _forward_to_registry(staged, envelope)
    if err:
        return _fail(err)
    # 404 = the registry never had (or already dropped) it; the local cache
    # row must still die so the dashboard stops serving the target (I9).
    if resp.status_code not in (200, 404):
        return _fail(_registry_error(resp))
    removed = _drop_cached_grant(token, org)
    return {"ok": True, "token": token,
            "registry_status": resp.status_code, "cache_removed": removed}


def _drop_cached_grant(token: str, org: str | None) -> bool:
    try:
        for m in settings_ops.read_owned_set(
            NETWORK_LINK_GRANT_SET_ID,
            org=org,
            target_revision=NETWORK_LINK_GRANT_REVISION,
        ).members:
            if m.key == token:
                settings_ops.remove_setting(m.id, org=org)
                return True
    except Exception:
        pass
    return False


# Consumed by approvals_routes when building its ENRICH / EXECUTORS registries.
ENRICH = {
    "link_publish": _enrich_link_publish,
    "link_revoke": _enrich_link_revoke,
}

EXECUTORS = {
    "link_publish": _execute_link_publish,
    "link_revoke": _execute_link_revoke,
}
