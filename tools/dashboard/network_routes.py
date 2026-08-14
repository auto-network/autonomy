"""auto.network identity routes — C1/C2 ceremonies' server side.

Read routes backing the C2 sign-on ceremony in
``static/js/network-signon.mjs`` (spec §6.3):

* ``GET /api/network/org-key`` — the org root key as its passphrase-
  encrypted armor (``autonomy.network.org-key``). Same discipline as
  ``/api/sign-key``: the server stores and serves the ENCRYPTED blob
  only; decryption happens in the operator's browser (invariant I1).
* ``GET /api/network/binding`` — the org's registry binding row
  (``autonomy.network.binding``): org UUID, root pub, registry URL. The
  ceremony pins the minted cert's org to this and refuses an org key
  whose public half disagrees with the bound root.
* ``POST /api/network/revocations`` — forwards a browser-minted,
  root-signed revocation record to the registry (``POST
  /v1/revocations``, spec §4.5). The record is self-authorizing; this
  route adds no authority, it only bridges the browser to the registry
  so revocation works without cross-origin registry access. Nothing here
  ever signs anything (I1 applied to the root, §6.3 to the session key).

Write routes backing the C1 create-org-identity ceremony in
``static/js/network-identity.js`` (spec §6.2–6.3, bead auto-40fob):

* ``GET /api/network/registry`` — the registry destination this
  deployment registers against. Server-configured
  (``AUTONOMY_NETWORK_REGISTRY_URL``), never taken from the browser —
  the same frozen-destination discipline C3's approval executor uses,
  so a compromised page cannot point the ceremony at a hostile registry.
* ``POST /api/network/org-key`` — stores the armored root key. The body
  must parse as the canonical idkit armor (``tools/network/idkit/
  armor.py``); anything plaintext-shaped is refused (I1 tested
  rejection). Refuses to overwrite an existing identity (409).
* ``POST /api/network/register`` — forwards the browser's ROOT-DIRECT
  registration envelope to the configured registry (``POST /v1/orgs``,
  §4.1) and, on 201, persists ``autonomy.network.binding``. The
  envelope must be self-signed by the root whose encrypted armor is
  ALREADY stored — registering an unstorable identity would strand the
  org. This route holds no keys and signs nothing (I1).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import time
import urllib.parse
from pathlib import Path

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from tools.data_paths import resolve_store
from tools.graph import settings_ops
# Importing registers the autonomy.network.* Setting schemas (they
# self-register on import).
from tools.graph.schemas.network_identity import (  # noqa: F401
    NETWORK_BINDING_REVISION,
    NETWORK_BINDING_SET_ID,
    NETWORK_ORG_KEY_REVISION,
    NETWORK_ORG_KEY_SET_ID,
    NETWORK_SERVE_CERT_REVISION,
    NETWORK_SERVE_CERT_SET_ID,
    SERVE_CERT_SCOPE,
)

DEFAULT_REGISTRY_URL = "https://registry.auto.network"
_PERSONA_PUB_RE = re.compile(r"^[0-9a-f]{64}\Z")

# -- invite resolve (auto-yw5gz) ------------------------------------------------
# The dashboard-origin half of the paste-into-your-own-dashboard flow: the page
# hands its own dashboard {relay_host, channel_token} (TRANSPORT credentials
# only — registry-visible by design), the dashboard opens the root-pinned E2E
# join channel to the org and returns the org-served invite context + identity.
# The ledger bearer (#t=) is NEVER part of this — it stays in the browser.
_CHANNEL_TOKEN_RE = re.compile(r"^[0-9a-f]{32}\Z")
_ORG_UUID_RE = re.compile(r"^[0-9a-f-]{32,36}\Z")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}\Z")
# The org-identity guards MATCH the relay bridge's (join.js safeColor/safeIcon):
# a bare hex color and a bounded data:image/*;base64 URI — never arbitrary CSS,
# never a remote URL. Re-validated here serve-side even though the org already
# bounds them, and re-validated again client-side (defence at every hop).
_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}\Z")
_ICON_RE = re.compile(
    r"^data:image/(png|jpeg|gif|webp|svg\+xml);base64,[A-Za-z0-9+/=]+\Z"
)
_ICON_MAX_LEN = 300000
# The whole request vocabulary — transport credentials, plus the already-public
# org/root_pub/invite_ref a /network/join handoff carries in its own query. A
# 't'/'bearer' or any other key is refused, not ignored, so the bearer can
# never ride in even by accident.
_RESOLVE_KEYS = frozenset({"relay_host", "channel_token", "org", "root_pub",
                           "invite_ref"})


def _serve_child_used_by_another_local_org(
    child_pub: str, current_org
) -> bool:
    """Refuse cross-org child-key reuse using only this dashboard's stores.

    This is the client-side A1 check. The registry deliberately keeps no
    cross-org key index because such an index would itself be an identity
    correlation mechanism. A pinned GRAPH_DB represents a single settings
    scope and therefore has no other local org databases to compare.
    """
    if os.environ.get("GRAPH_DB"):
        return False
    current_slug = settings_ops._resolve_org_arg(current_org)
    try:
        from tools.graph import org_ops

        scopes: list[str | None] = [None]
        scopes.extend(ref.slug for ref in org_ops.list_orgs())
    except Exception:
        return True  # no enumeration is not proof of uniqueness
    for scope in scopes:
        if scope == current_slug:
            continue
        try:
            members = settings_ops.read_owned_set(
                NETWORK_SERVE_CERT_SET_ID, org=scope
            ).members
        except Exception:
            # Fresh-per-org serving children are a privacy boundary. A local
            # store we cannot inspect is not evidence that the child is new.
            return True
        for member in members:
            if not isinstance(member.payload, dict):
                continue
            try:
                from tools.network.idkit import DelegationCert

                other = DelegationCert.from_json(member.payload.get("cert"))
            except Exception:
                continue
            if other.child_pub == child_pub:
                return True
    return False


def _registry_url() -> str:
    """The registry destination — server config only, never the request."""
    return os.environ.get("AUTONOMY_NETWORK_REGISTRY_URL") or DEFAULT_REGISTRY_URL


def _mock_mode() -> bool:
    # The mock dashboard has no settings DB and no org identity; the
    # chrome must land deterministically signed-out there.
    return bool(os.environ.get("DASHBOARD_MOCK"))


def _scoped_org(requested_org, *, request=None):
    """Resolve the org a network route is scoped to, refusing cross-org access.

    A network route reads/writes another org's ENCRYPTED root key + registry
    binding — an org-key blob is offline-attackable, so a cross-org read is a
    real leak. The caller's OWN org is taken from the authenticated session
    token when a bearer is present (auto-h4kzx, derive-when-present): the
    caller can no longer name its own org, closing the surface where a
    consistent caller passed a spoofed ``?org=`` + a matching caller-controlled
    ``X-Graph-Org``. With no bearer (host / old client) the caller org falls
    back to the env-cascade resolution (per-request ``X-Graph-Org`` contextvar
    → ``GRAPH_ORG`` env → scopeless), unchanged and never refused.

    An explicit ``?org=`` / body ``org`` is honored ONLY when it names the
    caller's OWN org; any other value is a cross-org attempt and is refused.

    Returns ``(org, None)`` on success — the token slug when a bearer resolved
    it (so the route reads the token's own DB), else
    :data:`settings_ops.CALLER_ORG`, the env-cascade sentinel — or
    ``(None, JSONResponse)`` (403) when an unauthorized override was passed.
    """
    token_org = None
    if request is not None:
        # Lazy import: network_routes is imported BY server, so a top-level
        # import would be circular; at request time server is fully loaded.
        from tools.dashboard.server import _token_org_or_none
        token_org = _token_org_or_none(request)
    caller = token_org if token_org is not None else settings_ops._resolve_settings_caller(None)
    if requested_org and requested_org != caller:
        return None, JSONResponse({"error": (
            "cross-org access to another org's network identity is not "
            "permitted"
        )}, status_code=403)
    return (token_org if token_org is not None else settings_ops.CALLER_ORG), None


def _first_member(set_id: str, org: str | None):
    """Lexically-first member of a keyed set, or None. One row is the
    common case; with several, the lexically first key wins
    deterministically (v1: no selector, matching link_approvals)."""
    members = sorted(
        settings_ops.read_owned_set(set_id, org=org).members, key=lambda m: m.key
    )
    for m in members:
        if isinstance(m.payload, dict):
            return m
    return None


async def get_org_key(request: Request) -> JSONResponse:
    """The org's armored (encrypted) network root key, or 404."""
    if _mock_mode():
        return JSONResponse({"error": "no network org key configured"}, status_code=404)
    org, refused = _scoped_org(request.query_params.get("org"), request=request)
    if refused is not None:
        return refused
    try:
        member = _first_member(NETWORK_ORG_KEY_SET_ID, org)
    except Exception as e:
        return JSONResponse({"error": f"could not read the org key setting: {e}"},
                            status_code=500)
    payload = member.payload if member is not None else {}
    # Two armor generations: revision-1 password armor and the revision-2
    # Option-B seal the founding ceremony writes. The browser opens either
    # with the one personal password; refusing to serve the sealed shape
    # made every ceremony-created org unable to sign on at all (auto-05tom).
    if not (payload.get("armored_private_key") or payload.get("sealed_root_key")):
        return JSONResponse({"error": (
            "This organization has no signing key yet."
        )}, status_code=404)
    out = {"label": member.key, "root_pub": payload.get("root_pub")}
    for field in ("armored_private_key", "sealed_root_key",
                  "owner_kem_pub", "seal_purpose"):
        if payload.get(field):
            out[field] = payload[field]
    return JSONResponse(out)


async def get_binding(request: Request) -> JSONResponse:
    """The org's registry binding row, or 404."""
    if _mock_mode():
        return JSONResponse({"error": "no network binding configured"}, status_code=404)
    org, refused = _scoped_org(request.query_params.get("org"), request=request)
    if refused is not None:
        return refused
    try:
        member = _first_member(NETWORK_BINDING_SET_ID, org)
    except Exception as e:
        return JSONResponse({"error": f"could not read the binding setting: {e}"},
                            status_code=500)
    if member is None:
        return JSONResponse({"error": (
            "This organization is not registered on auto.network yet."
        )}, status_code=404)
    payload = member.payload
    if not payload.get("org_uuid") or not payload.get("root_pub") \
            or not payload.get("registry_url"):
        return JSONResponse({"error": "the org's network binding row is malformed"},
                            status_code=500)
    return JSONResponse({"registry": member.key, **payload})


def _registry_client(base_url: str) -> httpx.AsyncClient:
    """Factory seam: tests swap this for an ASGITransport-backed client
    pointed at an in-process registry app."""
    return httpx.AsyncClient(base_url=base_url, timeout=15.0)


async def post_revocation(request: Request) -> JSONResponse:
    """Forward a root-signed revocation record to the registry (§4.5).

    Body: ``{org?: <graph org slug>, record: <RevocationRecord wire JSON>,
    revoked_cert: <DelegationCert wire JSON>}``. The registry verifies the
    record against the bound root; a bad record is ITS 403, not ours.
    """
    if _mock_mode():
        return JSONResponse({"ok": False, "error": "mock dashboard has no registry"},
                            status_code=502)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "body must be JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "body must be a JSON object"},
                            status_code=400)
    # Refuse a cross-org revocation before touching the (foreign) binding.
    org, refused = _scoped_org(body.get("org"), request=request)
    if refused is not None:
        return refused
    if not isinstance(body.get("record"), str) \
            or not isinstance(body.get("revoked_cert"), str):
        return JSONResponse({"ok": False, "error": (
            "body must carry 'record' and 'revoked_cert' as canonical wire "
            "JSON strings"
        )}, status_code=400)
    try:
        member = _first_member(NETWORK_BINDING_SET_ID, org)
    except Exception as e:
        return JSONResponse({"ok": False,
                             "error": f"could not read the binding setting: {e}"},
                            status_code=500)
    if member is None:
        return JSONResponse({"ok": False,
                             "error": "this org has no auto.network binding"},
                            status_code=404)
    binding = member.payload
    forward = {
        "org": binding["org_uuid"],
        "record": body["record"],
        "revoked_cert": body["revoked_cert"],
    }
    try:
        async with _registry_client(binding["registry_url"]) as client:
            resp = await client.post("/v1/revocations", json=forward)
    except httpx.HTTPError as e:
        return JSONResponse({"ok": False, "error": (
            f"could not reach the registry at {binding['registry_url']}: {e}"
        )}, status_code=502)
    if resp.status_code != 201:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        return JSONResponse({"ok": False, "error": (
            f"registry refused the revocation ({resp.status_code}): {detail}"
        )}, status_code=502)
    return JSONResponse({"ok": True, **resp.json()})


async def get_registry(request: Request) -> JSONResponse:
    """The server-configured registry destination for the C1 ceremony."""
    return JSONResponse({"registry_url": _registry_url()})


async def post_ledger_found(request: Request) -> JSONResponse:
    """Atomically install one client-signed organization founding batch.

    The client supplies four canonical wire events; this route contributes
    no signatures and sees no root plaintext. The complete batch is verified
    in an isolated LedgerStore before a temporary durable store is atomically
    promoted into place, so a bad later event cannot strand a partial genesis.
    """
    if _mock_mode():
        return JSONResponse(
            {"ok": False, "error": "mock dashboard has no authority ledger"},
            status_code=502,
        )
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"ok": False, "error": "body must be JSON"},
            status_code=400,
        )
    if not isinstance(body, dict):
        return JSONResponse(
            {"ok": False, "error": "body must be a JSON object"},
            status_code=400,
        )
    requested_org = body.get("org")
    if not isinstance(requested_org, str) or not requested_org:
        return JSONResponse(
            {"ok": False, "error": "body must carry the local org slug"},
            status_code=400,
        )
    _org, refused = _scoped_org(requested_org, request=request)
    if refused is not None:
        return refused
    wires = body.get("events")
    if (
        not isinstance(wires, list)
        or len(wires) != 4
        or any(not isinstance(wire, str) for wire in wires)
    ):
        return JSONResponse(
            {
                "ok": False,
                "error": "events must be exactly four canonical wire strings",
            },
            status_code=400,
        )

    from tools.graph import org_ops
    from tools.network.ledger import LedgerError
    from tools.network.ledger.events import Event
    from tools.network.ledger.store import LedgerStore, org_ledger_db_path

    org_ref = org_ops.get_org(requested_org)
    if org_ref is None:
        return JSONResponse(
            {"ok": False, "error": "local organization does not exist"},
            status_code=404,
        )
    store_path = org_ledger_db_path(requested_org)
    if store_path.exists():
        try:
            with LedgerStore(store_path) as existing:
                if len(existing) > 0:
                    return JSONResponse(
                        {
                            "ok": False,
                            "error": "organization ledger is already founded",
                        },
                        status_code=409,
                    )
        except LedgerError as exc:
            return JSONResponse(
                {"ok": False, "error": f"could not open ledger: {exc}"},
                status_code=500,
            )

    try:
        events = [Event.from_json(wire) for wire in wires]
        expected_types = [
            "genesis",
            "role.define",
            "invite",
            "member.claim",
        ]
        if [event.type for event in events] != expected_types:
            raise ValueError(
                "founding events must be genesis, role.define, invite, "
                "member.claim in that order"
            )
        if events[0].payload.get("org") != org_ref.id:
            raise ValueError(
                "genesis org must equal the local organization's stable id"
            )
        if events[0].parents:
            raise ValueError("genesis must have no parents")
        for previous, event in zip(events, events[1:]):
            if event.parents != (previous.event_id,):
                raise ValueError(
                    "each founding event must name only its predecessor"
                )
        with LedgerStore() as candidate:
            for event in events:
                candidate.append(event)
            candidate.refresh_projections(now=events[-1].hlc.ts)
    except (LedgerError, ValueError, TypeError) as exc:
        return JSONResponse(
            {"ok": False, "error": f"founding batch rejected: {exc}"},
            status_code=400,
        )

    try:
        # The ledger tables are co-located inside the existing organization
        # database. Never replace that file: doing so would erase the orgs
        # row, graph content, and schema stamp. The complete batch has already
        # passed isolated validation above, so only verified events reach this
        # durable append path.
        with LedgerStore(store_path) as durable:
            if len(durable) > 0:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": "organization ledger is already founded",
                    },
                    status_code=409,
                )
            event_ids = [durable.append(event) for event in events]
            durable.refresh_projections(now=events[-1].hlc.ts)
    except (LedgerError, OSError) as exc:
        return JSONResponse(
            {"ok": False, "error": f"could not persist founding batch: {exc}"},
            status_code=500,
        )

    return JSONResponse(
        {
            "ok": True,
            "genesis_id": event_ids[0],
            "event_ids": event_ids,
        }
    )


async def get_ledger_heads(request: Request) -> JSONResponse:
    """Return the founded ledger's identity and current signing frontier."""
    if _mock_mode():
        return JSONResponse(
            {"ok": False, "error": "mock dashboard has no authority ledger"},
            status_code=502,
        )
    requested_org = request.query_params.get("org")
    if not isinstance(requested_org, str) or not requested_org:
        return JSONResponse(
            {"ok": False, "error": "query must carry the local org slug"},
            status_code=400,
        )
    _org, refused = _scoped_org(requested_org, request=request)
    if refused is not None:
        return refused

    from tools.network.ledger import LedgerError, LedgerStore, org_ledger_db_path

    store_path = org_ledger_db_path(requested_org)
    if not store_path.exists():
        return JSONResponse(
            {"ok": False, "error": "organization ledger is not founded"},
            status_code=404,
        )
    try:
        with LedgerStore(store_path) as store:
            genesis_id = store.ledger.genesis_id
            if genesis_id is None:
                return JSONResponse(
                    {"ok": False, "error": "organization ledger is not founded"},
                    status_code=409,
                )
            return JSONResponse(
                {
                    "genesis_id": genesis_id,
                    "heads": list(store.heads()),
                }
            )
    except (LedgerError, OSError) as exc:
        return JSONResponse(
            {"ok": False, "error": f"could not read authority ledger: {exc}"},
            status_code=500,
        )


def _claim_http_response(envelope: dict) -> JSONResponse:
    """Map one claim-service outcome to HTTP without changing its body."""
    discriminator = envelope.get("status")
    if discriminator in {"ok", "admitted", "pending", "ready", "absent"}:
        code = 200
    elif discriminator == "gone":
        code = {
            "invite-expired": 410,
            "invite-already-claimed": 409,
        }.get(envelope.get("reason"), 404)
    elif discriminator == "rejected":
        reason = envelope.get("reason")
        if reason in {
            "claim-bad-token",
            "claim-wrong-key",
            "approver-not-authorized",
        }:
            code = 403
        elif reason in {
            "invite-already-claimed",
            "persona-exists",
            "stale-heads",
        }:
            code = 409
        else:
            code = 400
    else:
        code = 500
    return JSONResponse(envelope, status_code=code)


def _claim_http_fault(exc: Exception) -> JSONResponse:
    """Malformed/internal exceptions are outside normal protocol outcomes."""
    from tools.network.ledger import LedgerError

    if isinstance(exc, FileNotFoundError):
        return JSONResponse(
            {"status": "gone", "reason": "unknown-org"},
            status_code=404,
        )
    if isinstance(exc, (LedgerError, TypeError, ValueError)):
        return JSONResponse(
            {
                "status": "rejected",
                "reason": "malformed-input",
                "detail": str(exc),
            },
            status_code=400,
        )
    return JSONResponse(
        {"status": "rejected", "reason": "internal-error"},
        status_code=500,
    )


def _claim_key_matches(
    claim_key: str,
    invite_ref: object,
    persona_pub: object,
) -> bool:
    if not isinstance(invite_ref, str) or not isinstance(persona_pub, str):
        return False
    try:
        material = (invite_ref + persona_pub).encode("ascii")
    except UnicodeEncodeError:
        return False
    return hashlib.sha256(material).hexdigest() == claim_key


async def post_ledger_claim(request: Request) -> JSONResponse:
    """Thin HTTP adapter for transport-neutral claim submit/finalize."""
    if _mock_mode():
        return JSONResponse(
            {"ok": False, "error": "mock dashboard has no authority ledger"},
            status_code=502,
        )
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "body must be JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
    requested_org = body.get("org")
    if not isinstance(requested_org, str) or not requested_org:
        return JSONResponse(
            {"error": "body must carry the local org slug"},
            status_code=400,
        )
    _org, refused = _scoped_org(requested_org, request=request)
    if refused is not None:
        return refused
    wire = body.get("event")
    if not isinstance(wire, str):
        return JSONResponse(
            {"error": "body must carry an event canonical wire string"},
            status_code=400,
        )
    from tools.dashboard import claim_service
    try:
        return _claim_http_response(claim_service.submit(requested_org, wire))
    except Exception as exc:
        return _claim_http_fault(exc)


async def get_ledger_claim_context(request: Request) -> JSONResponse:
    """Thin HTTP adapter for public invite/authority minting context."""
    if _mock_mode():
        return JSONResponse(
            {"error": "mock dashboard has no authority ledger"},
            status_code=502,
        )
    requested_org = request.query_params.get("org")
    invite_ref = request.query_params.get("invite_ref")
    if not isinstance(requested_org, str) or not requested_org:
        return JSONResponse(
            {"error": "query must carry the local org slug"},
            status_code=400,
        )
    _org, refused = _scoped_org(requested_org, request=request)
    if refused is not None:
        return refused
    from tools.dashboard import claim_service
    try:
        return _claim_http_response(
            claim_service.context(requested_org, invite_ref)
        )
    except Exception as exc:
        return _claim_http_fault(exc)


async def get_ledger_claim(request: Request) -> JSONResponse:
    """Thin HTTP adapter for pending/admitted/absent claim status."""
    if _mock_mode():
        return JSONResponse(
            {"error": "mock dashboard has no authority ledger"},
            status_code=502,
        )
    claim_key = request.path_params["claim_key"]
    requested_org = request.query_params.get("org")
    invite_ref = request.query_params.get("invite_ref")
    persona_pub = request.query_params.get("persona_pub")
    if not isinstance(requested_org, str) or not requested_org:
        return JSONResponse(
            {"error": "query must carry the local org slug"},
            status_code=400,
        )
    _org, refused = _scoped_org(requested_org, request=request)
    if refused is not None:
        return refused
    if not _claim_key_matches(claim_key, invite_ref, persona_pub):
        return JSONResponse(
            {"status": "rejected", "reason": "claim-key-mismatch"},
            status_code=400,
        )
    from tools.dashboard import claim_service
    try:
        return _claim_http_response(
            claim_service.status(requested_org, invite_ref, persona_pub)
        )
    except Exception as exc:
        return _claim_http_fault(exc)


async def post_ledger_claim_approval(request: Request) -> JSONResponse:
    """Thin HTTP adapter for signature-verified countersignature merge."""
    if _mock_mode():
        return JSONResponse(
            {"error": "mock dashboard has no authority ledger"},
            status_code=502,
        )
    claim_key = request.path_params["claim_key"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "body must be JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
    requested_org = body.get("org")
    if not isinstance(requested_org, str) or not requested_org:
        return JSONResponse(
            {"error": "body must carry the local org slug"},
            status_code=400,
        )
    _org, refused = _scoped_org(requested_org, request=request)
    if refused is not None:
        return refused
    invite_ref = body.get("invite_ref")
    persona_pub = body.get("persona_pub")
    if not _claim_key_matches(claim_key, invite_ref, persona_pub):
        return JSONResponse(
            {"status": "rejected", "reason": "claim-key-mismatch"},
            status_code=400,
        )
    entry = body.get("approval")
    if not isinstance(entry, dict):
        return JSONResponse(
            {"error": "body must carry one approval object"},
            status_code=400,
        )
    from tools.dashboard import claim_service
    try:
        return _claim_http_response(
            claim_service.countersign(
                requested_org,
                invite_ref,
                persona_pub,
                entry,
            )
        )
    except Exception as exc:
        return _claim_http_fault(exc)


async def post_invite_email(request: Request) -> JSONResponse:
    """Send one already-minted secret org:join link through host SMTP."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"ok": False, "error": "body must be JSON"},
            status_code=400,
        )
    if not isinstance(body, dict):
        return JSONResponse(
            {"ok": False, "error": "body must be a JSON object"},
            status_code=400,
        )

    requested_org = body.get("org")
    if requested_org is not None and (
        not isinstance(requested_org, str) or not requested_org
    ):
        return JSONResponse(
            {"ok": False, "error": "org must be a non-empty slug"},
            status_code=400,
        )
    _org, refused = _scoped_org(requested_org, request=request)
    if refused is not None:
        return refused

    to_addr = body.get("to")
    join_link = body.get("join_link")
    expiry = body.get("expiry")
    from tools.dashboard.invite_email import (
        InviteEmailError,
        send_invite_email,
        validate_delivery,
    )

    try:
        validate_delivery(to_addr, join_link, expiry)
    except InviteEmailError as exc:
        return JSONResponse(
            {"ok": False, "error": str(exc)},
            status_code=400,
        )

    org = requested_org or settings_ops._resolve_settings_caller(None)
    try:
        receipt = await asyncio.to_thread(
            send_invite_email,
            to_addr,
            join_link,
            expiry,
            org,
        )
    except InviteEmailError as exc:
        return JSONResponse(
            {"ok": False, "error": str(exc)},
            status_code=502,
        )
    return JSONResponse({"ok": True, **receipt})


async def post_ledger_invite(request: Request) -> JSONResponse:
    """Authorize and append one client-signed routine invitation."""
    if _mock_mode():
        return JSONResponse(
            {"ok": False, "error": "mock dashboard has no authority ledger"},
            status_code=502,
        )
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"ok": False, "error": "body must be JSON"},
            status_code=400,
        )
    if not isinstance(body, dict):
        return JSONResponse(
            {"ok": False, "error": "body must be a JSON object"},
            status_code=400,
        )
    requested_org = body.get("org")
    if not isinstance(requested_org, str) or not requested_org:
        return JSONResponse(
            {"ok": False, "error": "body must carry the local org slug"},
            status_code=400,
        )
    _org, refused = _scoped_org(requested_org, request=request)
    if refused is not None:
        return refused
    wire = body.get("event")
    if not isinstance(wire, str):
        return JSONResponse(
            {
                "ok": False,
                "error": "body must carry an event canonical wire string",
            },
            status_code=400,
        )

    from tools.dashboard.org_authority import authorize
    from tools.network.ledger import (
        Event,
        LedgerError,
        LedgerStore,
        org_ledger_db_path,
        scope_invite,
    )

    try:
        event = Event.from_json(wire)
        if event.type != "invite":
            raise ValueError("event type must be invite")
        if event.payload["sponsor"] != event.author_key:
            raise ValueError("invite sponsor must equal its author")
        event.verify_sig()
    except (LedgerError, ValueError, TypeError) as exc:
        return JSONResponse(
            {"ok": False, "error": f"invitation rejected: {exc}"},
            status_code=400,
        )

    store_path = org_ledger_db_path(requested_org)
    if not store_path.exists():
        return JSONResponse(
            {"ok": False, "error": "organization ledger is not founded"},
            status_code=404,
        )
    try:
        with LedgerStore(store_path) as store:
            current_heads = store.heads()
            if event.parents != current_heads:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": "authority ledger advanced; refresh and retry",
                    },
                    status_code=409,
                )
            state = store.fold(heads=current_heads)
            if event.payload["granted_role"] not in state.role_defs:
                return JSONResponse(
                    {"ok": False, "error": "invitation role is not defined"},
                    status_code=400,
                )
            try:
                permitted = authorize(
                    requested_org,
                    event.author_key,
                    scope_invite(event.payload["granted_role"]),
                    at_head=current_heads,
                )
            except Exception:
                permitted = False
            if not permitted:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": "sponsor lacks authority to issue this invitation",
                    },
                    status_code=403,
                )
            invite_id = store.append(event)
            store.refresh_projections()
    except (LedgerError, OSError) as exc:
        return JSONResponse(
            {"ok": False, "error": f"could not append invitation: {exc}"},
            status_code=400,
        )
    return JSONResponse({"ok": True, "invite_id": invite_id})


async def put_org_key(request: Request) -> JSONResponse:
    """Store the org root key's passphrase-encrypted armor (C1 step 2).

    Body: ``{org?, label?, armored_private_key}``. The armor must parse
    with the canonical implementation (``tools/network/idkit/armor.py``)
    — that check is the I1 gate: a raw seed, a bare hex string, or any
    non-armor blob is refused before it can touch storage. An org that
    already holds a network identity gets 409; the create ceremony is
    for orgs without one (replacing a root key is a D2 rebind concern).
    """
    if _mock_mode():
        return JSONResponse({"ok": False, "error": "mock dashboard stores no org keys"},
                            status_code=502)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "body must be JSON"}, status_code=400)
    if not isinstance(body, dict) or not isinstance(body.get("armored_private_key"), str):
        return JSONResponse({"ok": False, "error": (
            "body must carry 'armored_private_key' as the armor text"
        )}, status_code=400)

    # Refuse a cross-org write BEFORE processing the (foreign) payload.
    org, refused = _scoped_org(body.get("org"), request=request)
    if refused is not None:
        return refused

    # I1 gate: only the canonical passphrase-encrypted armor is storable.
    # parse_armor is STRICT (exact field sets, formats, lengths — unknown
    # fields refused so nothing can be smuggled inside the body), and the
    # armor is RE-SERIALIZED from the parsed fields before storage, so the
    # persisted bytes can only ever carry the canonical armor fields.
    from tools.network.idkit.armor import ArmorError, canonicalize_armor, parse_armor
    try:
        canonical_armor = canonicalize_armor(body["armored_private_key"])
        armor_data = parse_armor(canonical_armor)
    except ArmorError as e:
        return JSONResponse({"ok": False, "error": (
            f"refusing to store: not a canonical passphrase-encrypted org "
            f"key armor (I1 — plaintext key material must never be "
            f"persisted): {e}"
        )}, status_code=400)
    root_pub = armor_data["root_pub"]
    if body.get("root_pub") is not None and body["root_pub"] != root_pub:
        return JSONResponse({"ok": False, "error": (
            "root_pub does not match the armor's enclosed public key"
        )}, status_code=400)

    label = body.get("label") or "default"
    if not isinstance(label, str) or len(label) > 64:
        return JSONResponse({"ok": False, "error": "label must be a short string"},
                            status_code=400)
    try:
        existing = _first_member(NETWORK_ORG_KEY_SET_ID, org)
    except Exception as e:
        return JSONResponse({"ok": False,
                             "error": f"could not read the org key setting: {e}"},
                            status_code=500)
    if existing is not None:
        return JSONResponse({"ok": False, "error": (
            "this org already holds a network identity — the create "
            "ceremony never overwrites a stored root key"
        )}, status_code=409)
    try:
        settings_ops.upsert_by_key(
            NETWORK_ORG_KEY_SET_ID, NETWORK_ORG_KEY_REVISION, label,
            {"armored_private_key": canonical_armor, "root_pub": root_pub},
            org=org,
        )
    except Exception as e:
        return JSONResponse({"ok": False,
                             "error": f"could not store the org key: {e}"},
                            status_code=500)
    return JSONResponse({"ok": True, "label": label, "root_pub": root_pub})


async def post_register(request: Request) -> JSONResponse:
    """Forward the C1 registration envelope to the registry (§4.1).

    Body: ``{org?, envelope}`` — nothing else; in particular the request
    can NOT name a registry destination (frozen server-side, C3
    discipline). The envelope must be root-direct (no cert — the binding
    does not exist yet) and self-signed by the root_pub it binds, and
    that root's encrypted armor must already be stored for the org: a
    binding whose private key was never persisted is a stranded org.
    On the registry's 201 the binding row is persisted locally.
    """
    if _mock_mode():
        return JSONResponse({"ok": False, "error": "mock dashboard has no registry"},
                            status_code=502)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "body must be JSON"}, status_code=400)
    if not isinstance(body, dict) or not isinstance(body.get("envelope"), dict):
        return JSONResponse({"ok": False, "error": (
            "body must carry the signed registration 'envelope' as an object"
        )}, status_code=400)
    unknown = set(body) - {"org", "envelope"}
    if unknown:
        # Tested rejection: the destination (or anything else) cannot ride
        # in on the request — {org, envelope} is the whole vocabulary.
        return JSONResponse({"ok": False, "error": (
            f"unexpected keys {sorted(unknown)}: the registration request "
            "carries only 'org' and 'envelope' — the registry destination "
            "is fixed server-side"
        )}, status_code=400)

    # Refuse a cross-org registration BEFORE processing the foreign envelope.
    org, refused = _scoped_org(body.get("org"), request=request)
    if refused is not None:
        return refused

    envelope = body["envelope"]
    payload = envelope.get("payload")
    if not isinstance(payload, dict) or not isinstance(payload.get("root_pub"), str) \
            or not isinstance(payload.get("org_uuid"), str):
        return JSONResponse({"ok": False, "error": (
            "envelope.payload must carry org_uuid and root_pub"
        )}, status_code=400)
    if envelope.get("cert") is not None:
        return JSONResponse({"ok": False, "error": (
            "registration is root-direct: the envelope must carry no cert"
        )}, status_code=400)
    if envelope.get("signer") != payload["root_pub"]:
        return JSONResponse({"ok": False, "error": (
            "registration must be self-signed by the root_pub being bound"
        )}, status_code=403)

    try:
        stored = _first_member(NETWORK_ORG_KEY_SET_ID, org)
    except Exception as e:
        return JSONResponse({"ok": False,
                             "error": f"could not read the org key setting: {e}"},
                            status_code=500)
    if stored is None or stored.payload.get("root_pub") != payload["root_pub"]:
        return JSONResponse({"ok": False, "error": (
            "the registration's root_pub does not match a stored org key — "
            "store the encrypted armor first, or the identity would be "
            "unrecoverable the moment this page closes"
        )}, status_code=409)

    registry_url = _registry_url()
    try:
        async with _registry_client(registry_url) as client:
            resp = await client.post("/v1/orgs", json=envelope)
    except httpx.HTTPError as e:
        return JSONResponse({"ok": False, "error": (
            f"could not reach the registry at {registry_url}: {e}"
        )}, status_code=502)
    if resp.status_code != 201:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        return JSONResponse({"ok": False, "error": (
            f"registry refused the registration ({resp.status_code}): {detail}"
        )}, status_code=502)

    reg = resp.json()
    # Persist the registry's AUTHORITATIVE 201 claim, never the caller's
    # echoed request: the binding the dashboard trusts must be what the
    # registry actually bound (org_uuid + root_pub come from the response,
    # not from payload).
    reg_org_uuid = reg.get("org_uuid")
    reg_root_pub = reg.get("root_pub")
    expires_at = reg.get("expires_at")
    if not isinstance(reg_org_uuid, str) or not isinstance(reg_root_pub, str) \
            or type(expires_at) is not int:
        return JSONResponse({"ok": False, "error": (
            "registry returned an incomplete binding (org_uuid/root_pub/expiry) "
            "— binding not persisted"
        )}, status_code=502)
    # The registry must bind the SAME root the operator just proved control
    # of. A different root_pub means the 201 is not an authoritative confirm
    # of that key — refuse rather than persist a binding for a foreign root.
    if reg_root_pub != payload["root_pub"]:
        return JSONResponse({"ok": False, "error": (
            "registry bound a different root key than the one signed — refusing "
            "to persist a binding for a key we did not prove control of"
        )}, status_code=502)
    policy: dict = {"mode": payload.get("recovery_policy")}
    if payload.get("recovery_pub") is not None:
        policy["recovery_pub"] = payload["recovery_pub"]
    binding = {
        "org_uuid": reg_org_uuid,
        "root_pub": reg_root_pub,
        "registry_url": registry_url,
        "recovery_policy": policy,
        "binding_expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                            time.gmtime(expires_at)),
        "last_renewed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    binding_key = urllib.parse.urlsplit(registry_url).netloc or registry_url
    try:
        settings_ops.upsert_by_key(
            NETWORK_BINDING_SET_ID, NETWORK_BINDING_REVISION, binding_key,
            binding, org=org,
        )
    except Exception as e:
        return JSONResponse({"ok": False, "error": (
            f"registry accepted the binding but persisting it locally failed: {e}"
        )}, status_code=500)
    return JSONResponse({"ok": True, "registry": binding_key, "binding": binding})


def _write_serve_key(path: Path, private_key_hex: str) -> None:
    """Write the delegate key to *path* as a mode-0600 file, atomically.

    The directory is 0700 and the file 0600 — the serving key is a
    process-user-only filesystem credential, the same trust boundary as the
    TLS key. Atomic replace so a concurrent read never sees a partial key.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(private_key_hex.strip())
    except BaseException:
        try:
            os.remove(tmp)
        finally:
            raise
    os.replace(tmp, path)


async def get_serve_cert_status(request: Request) -> JSONResponse:
    """Cheap pre-unlock check: does this org need a fresh serving credential?

    No certificate bytes or identity are returned. The browser uses this only
    to decide whether the organization root it is about to unlock should also
    sign the two context-specific serving certificates.
    """
    org, refused = _scoped_org(request.query_params.get("org"))
    if refused is not None:
        return refused
    from tools.dashboard.link_serving_supervisor import serve_cert_state

    status = serve_cert_state(org).get("status", "missing")
    return JSONResponse({"required": status != "ok", "status": status})


async def post_serve_cert(request: Request) -> JSONResponse:
    """Provision the org's tunnel serving delegate (§5.1).

    Body: ``{org?, cert, viewer_cert, private_key}`` — two direct-root
    ``tunnel:serve`` delegation certificates over one Ed25519 serving child,
    minted in the operator's browser during ordinary organization sign-on when
    the cheap status check says repair is required. The persona certificate is
    for registry admission; the identity-neutral certificate is for viewer
    handshakes. This route writes the shared key to its mode-0600 file, records
    the serving row (pointing at that file — never the key itself), and
    reconciles the serving connector.

    The delegate must chain to the org's OWN bound root (authoritative, from
    the binding — never a caller-supplied root) with ``tunnel:serve`` scope,
    and the supplied key must match the cert's ``child_pub``, or nothing is
    written. Registering the org must have happened first (serving needs a
    binding's registry_url + org_uuid); otherwise 409.
    """
    if _mock_mode():
        return JSONResponse({"ok": False, "error": "mock dashboard stores no serve certs"},
                            status_code=502)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "body must be JSON"}, status_code=400)
    if not isinstance(body, dict) or not isinstance(body.get("cert"), str) \
            or not isinstance(body.get("viewer_cert"), str) \
            or not isinstance(body.get("private_key"), str):
        return JSONResponse({"ok": False, "error": (
            "body must carry 'cert' (registry admission cert), 'viewer_cert' "
            "(identity-neutral viewer cert), and 'private_key' (their shared "
            "delegate key hex)"
        )}, status_code=400)

    # Refuse a cross-org write BEFORE processing the (foreign) payload.
    org, refused = _scoped_org(body.get("org"), request=request)
    if refused is not None:
        return refused

    # Serving needs the org's binding — its authoritative root (the pin the
    # viewer handshake verifies) and the registry_url/org_uuid the connector
    # dials. Register first; a serve-cert without a binding would strand.
    binding_member = _first_member(NETWORK_BINDING_SET_ID, org)
    if binding_member is None:
        return JSONResponse({"ok": False, "error": (
            "this org is not registered on auto.network yet — register before "
            "provisioning a serving delegate"
        )}, status_code=409)
    binding = binding_member.payload
    root_pub = binding.get("root_pub")
    org_uuid = binding.get("org_uuid")
    if not isinstance(root_pub, str) or not isinstance(org_uuid, str):
        return JSONResponse({"ok": False, "error": "the org's binding row is malformed"},
                            status_code=500)

    from tools.network.idkit import (
        DelegationCert,
        IdkitError,
        KeyPair,
        verify_chain,
    )
    try:
        cert = DelegationCert.from_json(body["cert"])
        viewer_cert = DelegationCert.from_json(body["viewer_cert"])
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"serving cert does not parse: {e}"},
                            status_code=400)
    if tuple(cert.scope) != (SERVE_CERT_SCOPE,):
        return JSONResponse({"ok": False, "error": (
            f"cert scope must be exactly [{SERVE_CERT_SCOPE!r}]"
        )}, status_code=400)
    if cert.subject.kind != "persona" or not _PERSONA_PUB_RE.match(cert.subject.id):
        return JSONResponse({"ok": False, "error": (
            "cert subject must be the canonical organization-scoped persona"
        )}, status_code=400)
    if (
        viewer_cert.subject.kind != "operator"
        or viewer_cert.subject.id != viewer_cert.child_pub
    ):
        return JSONResponse({"ok": False, "error": (
            "viewer cert must be identity-neutral: subject must be exactly "
            "{kind: operator, id: child_pub}"
        )}, status_code=400)
    if (
        viewer_cert.child_pub != cert.child_pub
        or viewer_cert.org != cert.org
        or tuple(viewer_cert.scope) != tuple(cert.scope)
        or viewer_cert.not_before != cert.not_before
        or viewer_cert.not_after != cert.not_after
    ):
        return JSONResponse({"ok": False, "error": (
            "registry and viewer certs must name the same child key, "
            "organization, scope, and validity window"
        )}, status_code=400)
    if viewer_cert.parent_cert is not None:
        return JSONResponse({"ok": False, "error": (
            "viewer serving delegates must be issued directly by the org root"
        )}, status_code=400)
    now = int(time.time())
    if cert.not_after <= now:
        return JSONResponse({"ok": False, "error": "cert is already expired"},
                            status_code=400)
    if cert.org != org_uuid:
        return JSONResponse({"ok": False, "error": (
            "cert org does not match the org's binding"
        )}, status_code=400)
    # The supplied key must be the one the cert delegates to.
    try:
        if KeyPair.from_private_hex(body["private_key"].strip()).public_hex != cert.child_pub:
            raise ValueError("public half does not match cert child_pub")
    except Exception as e:
        return JSONResponse({"ok": False, "error": (
            f"private_key does not match the cert's delegate key: {e}"
        )}, status_code=400)
    if _serve_child_used_by_another_local_org(cert.child_pub, org):
        return JSONResponse({"ok": False, "error": (
            "serving child keys are organization-scoped and cannot be reused "
            "across local organizations"
        )}, status_code=409)
    # Chain to the org's OWN root (not a caller-supplied one) with tunnel:serve.
    try:
        for candidate in (cert, viewer_cert):
            verified = verify_chain(
                candidate,
                root_pub,
                org=org_uuid,
                now=now,
                required_scope=SERVE_CERT_SCOPE,
            )
            if verified.depth != 1:
                raise IdkitError(
                    "serving delegates must be issued directly by the org root"
                )
    except IdkitError as e:
        return JSONResponse({"ok": False, "error": (
            f"cert does not chain to this org's root with {SERVE_CERT_SCOPE} scope: {e}"
        )}, status_code=400)

    # Use a child-keyed filename so replacing a credential is transactional:
    # a failed Settings write can remove only the new file and can never leave
    # the previous row pointing at overwritten key material.
    # Persist only the portable basename in Settings. The current node's
    # manifest-rooted serving-key directory is resolved at every read, so a
    # restored volume does not retain the source node's absolute path.
    previous_serve = _first_member(NETWORK_SERVE_CERT_SET_ID, org)
    if previous_serve is not None:
        try:
            previous_cert = DelegationCert.from_json(
                previous_serve.payload.get("cert")
            )
        except Exception:
            previous_cert = None
        if previous_cert is not None and previous_cert.child_pub == cert.child_pub:
            # A network retry of the exact successful request is idempotent;
            # reissuing a different certificate over the same child violates
            # the fresh-key-per-provisioning privacy contract.
            if (
                previous_serve.payload.get("cert") == body["cert"]
                and previous_serve.payload.get("viewer_cert") == body["viewer_cert"]
            ):
                from tools.dashboard.link_serving_supervisor import serve_cert_state

                if serve_cert_state(org, now=now).get("status") == "ok":
                    return JSONResponse({
                        "ok": True,
                        "child_pub": cert.child_pub,
                        "not_after": cert.not_after,
                    })
            return JSONResponse({"ok": False, "error": (
                "every serving credential provisioning must use a fresh "
                "organization-scoped child key"
            )}, status_code=409)
    key_file = f"serve-{org_uuid}-{cert.child_pub}.key"
    key_path = resolve_store("serving_keys") / key_file
    try:
        _write_serve_key(key_path, body["private_key"])
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"could not write the serve key file: {e}"},
                            status_code=500)
    try:
        settings_ops.upsert_by_key(
            NETWORK_SERVE_CERT_SET_ID, NETWORK_SERVE_CERT_REVISION, "default",
            {"cert": body["cert"], "viewer_cert": body["viewer_cert"],
             "key_path": key_file,
             "root_pub": root_pub, "not_after": cert.not_after},
            org=org,
        )
    except Exception as e:
        with contextlib.suppress(OSError):
            key_path.unlink()
        return JSONResponse({"ok": False, "error": f"could not store the serve cert: {e}"},
                            status_code=500)

    # The new row is durable. Remove a superseded managed key only when its
    # locator is a bare filename inside the current serving-key store. An
    # absolute locator may identify an operator-managed file outside this
    # contract and is therefore never deleted here.
    if previous_serve is not None:
        old_file = previous_serve.payload.get("key_path")
        if (
            isinstance(old_file, str)
            and old_file != key_file
            and Path(old_file).name == old_file
            and old_file not in {".", ".."}
        ):
            with contextlib.suppress(OSError):
                (resolve_store("serving_keys") / old_file).unlink()

    # Reconcile serving now (a publish's grant may already be cached, or the
    # publish that triggered this will cache one and re-ensure). Best-effort:
    # a launch failure is not a provisioning failure — the watchdog retries.
    try:
        from tools.dashboard.link_serving_supervisor import get_supervisor
        await asyncio.to_thread(get_supervisor().ensure, org)
    except Exception:
        pass

    return JSONResponse({"ok": True, "child_pub": cert.child_pub,
                         "not_after": cert.not_after})


def _relay_bases(relay_host: str):
    """Split a pasted relay origin into its ``(https base, wss base)``.

    Accepts a full origin (``https://relay.host``) or a bare host; a bare
    host defaults to https/wss. Returns ``(http_base, ws_base, None)`` or
    ``(None, None, JSONResponse)`` on a malformed value.
    """
    raw = relay_host.strip()
    parsed = urllib.parse.urlsplit(raw if "//" in raw else "https://" + raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None, None, JSONResponse(
            {"ok": False, "error": "relay_host must be an http(s) origin"},
            status_code=400,
        )
    http_base = f"{parsed.scheme}://{parsed.netloc}"
    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    return http_base, f"{ws_scheme}://{parsed.netloc}", None


def _relay_client(base_url: str) -> httpx.AsyncClient:
    """Factory seam for the link-envelope GET (tests swap it for an
    in-process registry app)."""
    return httpx.AsyncClient(base_url=base_url, timeout=10.0)


async def _fetch_link_envelope(http_base: str, token: str) -> dict | None:
    """The link's PUBLIC transport envelope from its own relay host —
    ``{org, root_pub, invite_ref, …}`` (§5.3). This is the same public
    source the relay bridge pins from; nothing secret rides here. Returns
    None on any transport/shape failure (the org step degrades to paste)."""
    try:
        async with _relay_client(http_base) as client:
            resp = await client.get(f"/v1/links/{token}/envelope")
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    try:
        body = resp.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


async def _read_org_context(ws_base: str, token: str, *, root_pub: str,
                            org: str) -> dict | None:
    """Open the root-pinned E2E join channel and read the org's own invite
    context (the ``op: context`` reply). Reuses relaykit's viewer client —
    the Python twin of the browser handshake, so ``root_pub`` is the I5 pin
    established BEFORE any channel byte and the bearer never participates.
    Returns the reply dict, or None if the org is unreachable / an older
    node that does not serve it (the caller then renders the minimal form)."""
    from tools.network.relaykit.viewer import ViewerChannel

    try:
        channel = await ViewerChannel.connect(
            ws_base, token, root_pub=root_pub, org=org
        )
    except Exception:
        return None
    try:
        await channel.send_message(json.dumps({"v": 1, "op": "context"}).encode())
        raw = await channel.recv_message()
    except Exception:
        return None
    finally:
        with contextlib.suppress(Exception):
            await channel.close()
    try:
        reply = json.loads(raw.split(b"\n", 1)[0])
    except Exception:
        return None
    if not isinstance(reply, dict) or reply.get("status") != "ok":
        return None
    return reply


def _safe_identity(reply: dict) -> dict:
    """The r7kk4 identity fields, re-validated with the bridge's guards.

    Only a non-empty name, a bare hex color, a bounded description byline,
    and a bounded ``data:image/*;base64`` icon survive. A remote-URL icon or
    arbitrary-CSS color is dropped (never emitted), matching join.js."""
    out: dict = {}
    name = reply.get("org_name")
    if isinstance(name, str) and name:
        out["org_name"] = name[:120]
    color = reply.get("org_color")
    if isinstance(color, str) and _HEX_COLOR_RE.match(color):
        out["org_color"] = color
    desc = reply.get("org_description")
    if isinstance(desc, str) and desc:
        out["org_description"] = desc[:300]
    icon = reply.get("org_icon")
    if isinstance(icon, str) and len(icon) <= _ICON_MAX_LEN and _ICON_RE.match(icon):
        out["org_icon"] = icon
    return out


async def post_invite_resolve(request: Request) -> JSONResponse:
    """Resolve a pasted invite link on THIS dashboard's own origin (auto-yw5gz).

    Body: ``{relay_host, channel_token}`` — transport credentials only — and,
    for a ``/network/join`` handoff link, its already-public
    ``{org, root_pub, invite_ref}``. The ledger bearer (#t=) is NEVER accepted:
    an unexpected key (``t``, ``bearer``, anything) is a hard 400, so the
    secret can never ride in even by mistake.

    Returns ``{ok: true, org, invite_ref?, org_name?, org_color?,
    org_description?, org_icon?}``. When the relay is unreachable or the org
    node is older, the identity fields are simply absent and the caller renders
    the minimal verified-org step — never an error page.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "body must be JSON"},
                            status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "body must be a JSON object"},
                            status_code=400)
    unknown = set(body) - _RESOLVE_KEYS
    if unknown:
        return JSONResponse({"ok": False, "error": (
            f"unexpected keys {sorted(unknown)}: invite resolve carries only "
            "transport credentials (relay_host, channel_token) and, for a "
            "handoff link, its public org/root_pub/invite_ref — never the "
            "bearer, which must stay in the browser"
        )}, status_code=400)

    relay_host = body.get("relay_host")
    channel_token = body.get("channel_token")
    if not isinstance(relay_host, str) or not relay_host:
        return JSONResponse({"ok": False, "error": "relay_host is required"},
                            status_code=400)
    if not isinstance(channel_token, str) or not _CHANNEL_TOKEN_RE.match(channel_token):
        return JSONResponse({"ok": False, "error": (
            "channel_token must be 32 hex chars"
        )}, status_code=400)
    http_base, ws_base, refused = _relay_bases(relay_host)
    if refused is not None:
        return refused

    # Root pin source per link kind (respec after dry-run 8dbf730d): a handoff
    # link already carries org/root_pub/invite_ref in its query; a bare /l/
    # paste has only transport creds, so pin from the PASTED LINK'S OWN host's
    # public envelope. That host is chosen by whoever minted the link — the
    # pin's trust root is the act of pasting, exactly the relay-bridge model,
    # NOT an independent registry cross-check. Either way the pin exists
    # before any channel byte.
    org = body.get("org")
    root_pub = body.get("root_pub")
    invite_ref = body.get("invite_ref")
    if org is not None or root_pub is not None or invite_ref is not None:
        if not (isinstance(org, str) and _ORG_UUID_RE.match(org)
                and isinstance(root_pub, str) and _HEX64_RE.match(root_pub)
                and isinstance(invite_ref, str) and _HEX64_RE.match(invite_ref)):
            return JSONResponse({"ok": False, "error": (
                "org, root_pub and invite_ref must all be supplied together "
                "and well-formed"
            )}, status_code=400)
    else:
        envelope = await _fetch_link_envelope(http_base, channel_token)
        if envelope is None:
            # Relay unreachable / unknown link: no public context to pin or
            # dial. Report the honest miss; the page keeps its paste step.
            return JSONResponse({"ok": False, "reason": "unreachable"})
        org = envelope.get("org")
        root_pub = envelope.get("root_pub")
        invite_ref = envelope.get("invite_ref")
        if not (isinstance(org, str) and _ORG_UUID_RE.match(org)
                and isinstance(root_pub, str) and _HEX64_RE.match(root_pub)):
            return JSONResponse({"ok": False, "reason": "unreachable"})

    # The minimal verified-org step stands on the pinned PUBLIC context alone
    # (id + invite ref), so an older org node that cannot serve identity still
    # renders — never an error page.
    out: dict = {"ok": True, "org": org}
    if isinstance(invite_ref, str) and invite_ref:
        out["invite_ref"] = invite_ref

    identity = await _read_org_context(ws_base, channel_token,
                                       root_pub=root_pub, org=org)
    if identity is not None:
        out.update(_safe_identity(identity))
        env_ref = identity.get("invite_ref")
        # The org's own reply is authoritative over the public envelope for
        # the invite ref it is serving.
        if isinstance(env_ref, str) and env_ref:
            out["invite_ref"] = env_ref
    return JSONResponse(out)


ROUTES = [
    Route("/api/network/org-key", get_org_key, methods=["GET"]),
    Route("/api/network/org-key", put_org_key, methods=["POST"]),
    Route("/api/network/binding", get_binding, methods=["GET"]),
    Route("/api/network/registry", get_registry, methods=["GET"]),
    Route("/api/network/ledger/found", post_ledger_found, methods=["POST"]),
    Route("/api/network/ledger/heads", get_ledger_heads, methods=["GET"]),
    Route("/api/network/ledger/invite", post_ledger_invite, methods=["POST"]),
    Route("/api/network/ledger/claim", post_ledger_claim, methods=["POST"]),
    Route(
        "/api/network/ledger/claim/context",
        get_ledger_claim_context,
        methods=["GET"],
    ),
    Route("/api/network/ledger/claim/{claim_key}", get_ledger_claim, methods=["GET"]),
    Route(
        "/api/network/ledger/claim/{claim_key}/approval",
        post_ledger_claim_approval,
        methods=["POST"],
    ),
    Route("/api/network/invite/email", post_invite_email, methods=["POST"]),
    Route("/api/network/register", post_register, methods=["POST"]),
    Route("/api/network/invite/resolve", post_invite_resolve, methods=["POST"]),
    Route("/api/network/serve-cert", get_serve_cert_status, methods=["GET"]),
    Route("/api/network/serve-cert", post_serve_cert, methods=["POST"]),
    Route("/api/network/revocations", post_revocation, methods=["POST"]),
]
