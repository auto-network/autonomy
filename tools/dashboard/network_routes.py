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
import logging
import hashlib
import json
import os
import re
import time
import urllib.parse
from pathlib import Path

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from tools.data_paths import resolve_store
# Safe at module scope: api_auth imports only starlette, and defers its one
# unlock_routes import into the call. The reverse edge is what is circular.
from tools.dashboard.api_auth import (
    organization_scope_from_request,
    require_global_api_authority,
    resolve_scoped_org,
)
from tools.graph import schemas, settings_ops
# Importing registers the autonomy.network.* Setting schemas (they
# self-register on import).
from tools.graph.schemas.network_identity import (  # noqa: F401
    NETWORK_BINDING_REVISION,
    NETWORK_BINDING_REVISION_2,
    NETWORK_BINDING_SET_ID,
    NETWORK_ORG_KEY_REVISION,
    NETWORK_ORG_KEY_SET_ID,
    NETWORK_SERVE_CERT_REVISION,
    NETWORK_SERVE_CERT_SET_ID,
    SERVE_CERT_SCOPE,
)
from tools.graph.schemas.personal_identity import PERSONAL_IDENTITY_SET_ID

DEFAULT_REGISTRY_URL = "https://registry.auto.network"
_PERSONA_PUB_RE = re.compile(r"^[0-9a-f]{64}\Z")
_BINDING_GENERATION_RE = re.compile(r"^[0-9a-f]{64}\Z")
_BINDING_OUTCOMES = frozenset({"claimed", "already_bound_self", "reclaimed_expired"})


def _canonical_binding_policy(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    if value == {"mode": "none"}:
        return {"mode": "none"}
    if (
        set(value) == {"mode", "recovery_pub"}
        and value.get("mode") == "recovery-key"
        and isinstance(value.get("recovery_pub"), str)
        and _PERSONA_PUB_RE.fullmatch(value["recovery_pub"])
    ):
        return {"mode": "recovery-key", "recovery_pub": value["recovery_pub"]}
    return None


def _validated_binding_response(value, *, registration: bool) -> dict | None:
    if not isinstance(value, dict):
        return None
    expected = {
        "org_uuid", "root_pub", "binding_generation", "expires_at", "recovery_policy"
    }
    if registration:
        expected.add("outcome")
    if set(value) != expected:
        return None
    if (
        not isinstance(value.get("org_uuid"), str)
        or not isinstance(value.get("root_pub"), str)
        or _PERSONA_PUB_RE.fullmatch(value["root_pub"]) is None
        or not isinstance(value.get("binding_generation"), str)
        or _BINDING_GENERATION_RE.fullmatch(value["binding_generation"]) is None
        or type(value.get("expires_at")) is not int
        or value["expires_at"] < 0
        or value["expires_at"] > 9_007_199_254_740_991
        or _canonical_binding_policy(value.get("recovery_policy")) is None
    ):
        return None
    if registration and value.get("outcome") not in _BINDING_OUTCOMES:
        return None
    return dict(value)

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
        except schemas.SchemaValidationError:
            # This store CANNOT hold a serving credential: the setting
            # declares organization scope, and a machine or personal store
            # refuses it by declaration. That is not an uninspectable store,
            # it is a store where no serving child can exist, so it is no
            # evidence either way and the scan continues.
            #
            # Failing closed here refused EVERY mint as soon as such a store
            # existed locally -- reported as "child keys cannot be reused",
            # which names the one thing that was not wrong.
            continue
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


def _registration_binding_context(org: str | None):
    """Return the one exact local V1/V2 binding used by registration.

    Registration is also the root-direct refresh/reclaim transport.  Once a
    local binding exists, its coordinates and registry destination are the
    server-owned context; mere row presence is not enough authority to replace
    it.  Fail closed on partial, multiple, malformed, or key/payload-mismatched
    state instead of choosing one row and contacting the default registry.
    """
    result = settings_ops.read_owned_set(NETWORK_BINDING_SET_ID, org=org)
    if any(result.dropped.values()):
        raise ValueError("binding query returned partial or dropped state")
    if not result.members:
        return None
    if len(result.members) != 1:
        raise ValueError("registration requires exactly one local binding")
    member = result.members[0]
    if member.stored_revision not in (
        NETWORK_BINDING_REVISION,
        NETWORK_BINDING_REVISION_2,
    ):
        raise ValueError("local binding has an unsupported schema revision")
    if not isinstance(member.payload, dict):
        raise ValueError("local binding payload is malformed")
    schemas.validate_payload(
        NETWORK_BINDING_SET_ID, member.stored_revision, member.payload
    )
    registry_url = member.payload.get("registry_url")
    binding_key = (
        urllib.parse.urlsplit(registry_url).netloc or registry_url
        if isinstance(registry_url, str)
        else None
    )
    if not binding_key or member.key != binding_key:
        raise ValueError("local binding key does not match its registry URL")
    if _canonical_binding_policy(member.payload.get("recovery_policy")) is None:
        raise ValueError("local binding recovery policy is malformed")
    return member


def _same_registration_binding(left, right) -> bool:
    if left is None or right is None:
        return left is right
    return (
        left.id == right.id
        and left.key == right.key
        and left.stored_revision == right.stored_revision
        and left.payload == right.payload
    )


def _root_pub_has_stored_armor(org: str | None, root_pub: str) -> bool:
    """True when ``root_pub``'s encrypted armor is already persisted, so a
    registration binding it cannot strand the key when the page closes.

    A normal org proves recoverability with its ``NetworkOrgKeyV2`` org-key.
    The PERSONAL org (``org is None``) binds the personal root itself, whose
    armor is the personal identity row (``autonomy.identity.personal``) — the
    reachability cert the fleet mints is signed by that same personal root, so
    the personal org must bind it, and the personal identity satisfies the
    identical recoverability invariant without a redundant org-key.
    """
    org_key = _first_member(NETWORK_ORG_KEY_SET_ID, org)
    if org_key is not None and org_key.payload.get("root_pub") == root_pub:
        return True
    # Personal org: the scope resolves to the personal store (the CALLER_ORG
    # sentinel collapses to None when no org is stamped), the bound root IS the
    # personal root, and its recoverable armor is the personal identity row —
    # gate the fallback on true personal scope so a NAMED org still requires its
    # own org-key.
    if settings_ops._resolve_org_arg(org) is None:
        personal = _first_member(PERSONAL_IDENTITY_SET_ID, None)
        if (
            personal is not None
            and personal.payload.get("root_pub") == root_pub
            and personal.payload.get("armored_private_key")
        ):
            return True
    return False


async def get_org_key(request: Request) -> JSONResponse:
    """The org's armored (encrypted) network root key, or 404.

    Operator-only (auto-6ff9b). :func:`_scoped_org` refuses a caller that
    NAMES another org, which is a different question from whether the caller
    may read at all: an unauthenticated request for its OWN org passed that
    check and received the key. The blob is passphrase-encrypted or sealed
    rather than plaintext, so this is not immediate compromise — it is
    unauthenticated disclosure of offline-attackable material, which is the
    same severity argument that closed ``auto-1wwpf.2``.

    Every browser caller reaches this AFTER the session cookie exists, so the
    guard costs them nothing: unlock's serving-credential maintenance runs
    downstream of ``POST /api/identity/unlock/password`` (``unlock.js``
    awaits the cookie-issuing call before it starts maintenance), and the
    network-identity and worktrees screens are behind the human gate.
    """
    if _mock_mode():
        return JSONResponse({"error": "no network org key configured"}, status_code=404)
    refused = require_global_api_authority(request)
    if refused is not None:
        return refused
    org, refused = resolve_scoped_org(request.query_params.get("org"), request=request)
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
    org, refused = resolve_scoped_org(request.query_params.get("org"), request=request)
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
    org, refused = resolve_scoped_org(body.get("org"), request=request)
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
    _org, refused = resolve_scoped_org(requested_org, request=request)
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

    # Record WHICH member this node is. The ledger says the founder's persona
    # is a member; it cannot say that persona is US, because that depends on
    # who holds which seed -- and the seed never leaves the browser. The
    # persona's PUBLIC half is in the claim event the client just signed, so
    # this is read from the folded batch rather than derived from anything
    # secret. Without it a browser-founded organization has no record of who
    # its owner is on this node (auto-jdba4 follow-up).
    try:
        org_ops._record_persona_setting(
            requested_org,
            event_ids[0],
            events[3].payload["persona_pub"],
            source="found",
        )
    except Exception as exc:
        # The ledger is already durable and correct; failing the whole
        # founding here would leave a founded org the client believes failed.
        # Report it instead, so the gap is visible rather than silent.
        return JSONResponse(
            {
                "ok": True,
                "genesis_id": event_ids[0],
                "event_ids": event_ids,
                "persona_recorded": False,
                "warning": f"founded, but the owner persona was not recorded: {exc}",
            }
        )

    return JSONResponse(
        {
            "ok": True,
            "genesis_id": event_ids[0],
            "event_ids": event_ids,
            "persona_recorded": True,
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
    _org, refused = resolve_scoped_org(requested_org, request=request)
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
    _org, refused = resolve_scoped_org(requested_org, request=request)
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
    _org, refused = resolve_scoped_org(requested_org, request=request)
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
    _org, refused = resolve_scoped_org(requested_org, request=request)
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
    _org, refused = resolve_scoped_org(requested_org, request=request)
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
    _org, refused = resolve_scoped_org(requested_org, request=request)
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

    # org-scope: request — an invite belongs to the org whose join link it
    # carries; the SMTP install is resolved from that org. No ambient
    # fallback exists, so an unnamed org is refused by name.
    org = requested_org
    if not org:
        return JSONResponse(
            {"ok": False, "error": (
                "name the org this invite is for (body 'org') — its SMTP "
                "install is what sends the mail"
            )},
            status_code=400,
        )
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


async def post_ledger_delegate(request: Request) -> JSONResponse:
    """Append one client-signed storage delegate event (auto-pw9bs.2).

    The unattended agent delegate is minted in the BROWSER, where the persona
    key lives — this process may hold the delegate's signing key (crib §12) but
    never the persona's, so it cannot mint one itself. The browser signs the
    delegation event and posts it here to be made durable.

    Durability is the whole point. A delegate that exists only in the minting
    process resolves against that process's fold and nowhere else, so the first
    thing to re-open the ledger — a key holder, a sealer — cannot resolve the
    author to a member and every write fails with an authority error that names
    the wrong cause.

    This route contributes no signatures and inspects no secret. It verifies
    the event is a delegate, is self-consistent, and cites the current heads,
    then appends it.
    """
    if _mock_mode():
        return JSONResponse(
            {"ok": False, "error": "mock dashboard has no authority ledger"},
            status_code=502,
        )
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "body must be JSON"},
                            status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "body must be a JSON object"},
                            status_code=400)
    requested_org = body.get("org")
    if not isinstance(requested_org, str) or not requested_org:
        return JSONResponse({"ok": False, "error": "body must carry the local org slug"},
                            status_code=400)
    _org, refused = resolve_scoped_org(requested_org, request=request)
    if refused is not None:
        return refused
    wire = body.get("event")
    if not isinstance(wire, str):
        return JSONResponse({"ok": False, "error": (
            "body must carry the delegate event as a canonical wire string"
        )}, status_code=400)

    from tools.network.ledger import Event, LedgerError, LedgerStore, org_ledger_db_path

    try:
        event = Event.from_json(wire)
        if event.type != "delegate":
            raise ValueError(f"event type must be delegate, got {event.type!r}")
        event.verify_sig()
    except (LedgerError, ValueError, TypeError) as exc:
        return JSONResponse({"ok": False, "error": f"delegate rejected: {exc}"},
                            status_code=400)

    store_path = org_ledger_db_path(requested_org)
    if not store_path.exists():
        return JSONResponse({"ok": False, "error": (
            "this store has no founded ledger — a delegate is authorized by "
            "membership, and there is no roster to resolve against"
        )}, status_code=404)
    try:
        with LedgerStore(store_path) as store:
            current = store.heads()
            if tuple(event.parents) != tuple(current):
                # Refuse rather than append at a stale frontier: the fold that
                # authorizes this delegate would not be the one it cited.
                return JSONResponse({"ok": False, "error": (
                    "authority ledger advanced; re-mint the delegate at the "
                    "current heads and retry"
                )}, status_code=409)
            event_id = store.append(event)
    except LedgerError as exc:
        return JSONResponse({"ok": False, "error": f"delegate refused: {exc}"},
                            status_code=400)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": (
            f"could not append the delegate: {exc}"
        )}, status_code=500)
    return JSONResponse({"ok": True, "event_id": event_id})


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
    _org, refused = resolve_scoped_org(requested_org, request=request)
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


async def post_sealed_org_key(request: Request) -> JSONResponse:
    """Persist an org root key the BROWSER sealed (I1, auto-jdba4).

    Body: ``{org?, root_pub, sealed_root_key, owner_kem_pub, seal_purpose}``.
    The org root is generated in the operator's browser and sealed there to
    the owner's derived encapsulation key, so only sealed material arrives
    here: no passphrase and no root plaintext reaches the server. This is
    the client-driven counterpart of the server-side seal the founding
    ceremony used to perform under a password.

    Idempotent while the ledger is UN-FOUNDED -- the seal-then-fold window
    and its retries. Once founded, the ledger has committed to this root and
    the key can never be replaced (409).
    """
    if _mock_mode():
        return JSONResponse(
            {"ok": False, "error": "mock dashboard stores no org keys"},
            status_code=502,
        )
    # Operator-only (auto-6ff9b), refused BEFORE the body is read: founding an
    # organization is an operator act, and an org-bound agent session is
    # deliberately too narrow for it even when it names its own org. An agent
    # that legitimately drives a founding presents operator credentials.
    refused = require_global_api_authority(request)
    if refused is not None:
        return refused
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "body must be JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse(
            {"ok": False, "error": "body must be a JSON object"}, status_code=400,
        )

    # Refuse a cross-org write BEFORE processing the (foreign) payload.
    org, refused = resolve_scoped_org(body.get("org"), request=request)
    if refused is not None:
        return refused

    from tools.graph import org_ops

    # The sealed key lives in one organization's own database and is looked up
    # by slug, so the caller-org sentinel must collapse to a literal here; a
    # scopeless write has no org whose ledger could lock the key.
    slug = settings_ops._resolve_org_arg(org)
    if not isinstance(slug, str) or not slug:
        return JSONResponse(
            {"ok": False, "error": (
                "no organization scope resolved — name the org whose root key "
                "this is"
            )},
            status_code=400,
        )

    sealed = {
        k: body.get(k)
        for k in ("root_pub", "sealed_root_key", "owner_kem_pub", "seal_purpose")
    }
    try:
        org_ops.store_sealed_org_key(slug, sealed)
    except org_ops.OrgNotFoundError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=404)
    except org_ops.OrgExistsError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=409)
    except org_ops.OrgError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse(
            {"ok": False, "error": f"could not store the sealed org key: {e}"},
            status_code=500,
        )
    return JSONResponse({"ok": True, "root_pub": sealed["root_pub"]})


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
    org, refused = resolve_scoped_org(body.get("org"), request=request)
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
        recoverable = _root_pub_has_stored_armor(org, payload["root_pub"])
    except Exception as e:
        return JSONResponse({"ok": False,
                             "error": f"could not read the stored key armor: {e}"},
                            status_code=500)
    if not recoverable:
        return JSONResponse({"ok": False, "error": (
            "the registration's root_pub matches no stored key armor — "
            "store the encrypted armor first (an org-key for a normal org, or "
            "the personal identity for the personal org), or the identity "
            "would be unrecoverable the moment this page closes"
        )}, status_code=409)

    # A genuinely unbound organization uses the code-owned default registry.
    # Once a binding exists, the exact validated row freezes the UUID, root,
    # policy, destination and binding key for refresh/reclaim.  None of those
    # coordinates is a caller-selectable replacement operation.
    try:
        existing_binding = _registration_binding_context(org)
    except Exception as e:
        return JSONResponse(
            {"ok": False, "error": f"existing binding is unavailable: {e}"},
            status_code=409,
        )
    claim_policy: dict = {"mode": payload.get("recovery_policy")}
    if payload.get("recovery_pub") is not None:
        claim_policy["recovery_pub"] = payload["recovery_pub"]
    claim_policy = _canonical_binding_policy(claim_policy)
    if claim_policy is None:
        return JSONResponse(
            {"ok": False, "error": "registration recovery policy is malformed"},
            status_code=400,
        )
    if existing_binding is not None and (
        payload["org_uuid"] != existing_binding.payload.get("org_uuid")
        or payload["root_pub"] != existing_binding.payload.get("root_pub")
        or claim_policy != existing_binding.payload.get("recovery_policy")
    ):
        return JSONResponse(
            {
                "ok": False,
                "error": (
                    "signed registration coordinates do not match the existing "
                    "binding — refresh/reclaim cannot replace local authority"
                ),
            },
            status_code=409,
        )
    allowed_outcomes = (
        _BINDING_OUTCOMES
        if existing_binding is None
        else frozenset({"already_bound_self", "reclaimed_expired"})
    )

    registry_url = (
        _registry_url()
        if existing_binding is None
        else existing_binding.payload["registry_url"]
    )
    try:
        async with _registry_client(registry_url) as client:
            resp = await client.post("/v1/orgs", json=envelope)
    except httpx.HTTPError as e:
        return JSONResponse({"ok": False, "error": (
            f"could not reach the registry at {registry_url}: {e}"
        )}, status_code=502)
    if resp.status_code == 409:
        # The registry already holds a binding for this UUID. A registry taught
        # same-root idempotency returns 201 (claim_org's already_bound_self); a
        # registry not yet running that fix still 409s a same-root
        # re-registration. Treat the 409 as idempotent success ONLY when our own
        # persisted binding proves the org is ours (same org_uuid + root_pub) —
        # a genuine different-root conflict still fails below. This lets a
        # re-unlock sail past register to serve-cert provisioning without
        # depending on the production registry being redeployed, and it is the
        # reason registration must be safe to repeat: the personal org derives a
        # deterministic org_uuid, so every unlock re-attempts it.
        if (
            existing_binding is not None
            and existing_binding.payload.get("org_uuid") == payload["org_uuid"]
            and existing_binding.payload.get("root_pub") == payload["root_pub"]
            and isinstance(existing_binding.payload.get("binding_generation"), str)
            and _BINDING_GENERATION_RE.fullmatch(
                existing_binding.payload["binding_generation"]
            ) is not None
        ):
            return JSONResponse({
                "ok": True,
                "registry": existing_binding.key,
                "binding": existing_binding.payload,
                "outcome": "already_bound_self",
                "already_registered": True,
            })
    if resp.status_code != 201:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        return JSONResponse({"ok": False, "error": (
            f"registry refused the registration ({resp.status_code}): {detail}"
        )}, status_code=502)

    reg = _validated_binding_response(resp.json(), registration=True)
    # Persist the registry's AUTHORITATIVE 201 claim, never the caller's
    # echoed request: the binding the dashboard trusts must be what the
    # registry actually bound (org_uuid + root_pub come from the response,
    # not from payload).
    if reg is None:
        return JSONResponse({"ok": False, "error": (
            "registry returned a malformed authoritative binding "
            "— binding not persisted"
        )}, status_code=502)
    if reg["outcome"] not in allowed_outcomes:
        return JSONResponse({"ok": False, "error": (
            f"registry outcome {reg['outcome']!r} is invalid for this binding context "
            "— binding not persisted"
        )}, status_code=502)
    reg_org_uuid = reg["org_uuid"]
    reg_root_pub = reg["root_pub"]
    expires_at = reg["expires_at"]
    # The registry must bind the SAME root the operator just proved control
    # of. A different root_pub means the 201 is not an authoritative confirm
    # of that key — refuse rather than persist a binding for a foreign root.
    if reg_org_uuid != payload["org_uuid"] or reg_root_pub != payload["root_pub"]:
        return JSONResponse({"ok": False, "error": (
            "registry bound different UUID/root coordinates than the signed claim — "
            "refusing to persist authority we did not prove"
        )}, status_code=502)
    policy = claim_policy
    if reg["recovery_policy"] != policy:
        return JSONResponse({"ok": False, "error": (
            "registry returned a different recovery policy than the frozen claim "
            "— binding not persisted"
        )}, status_code=502)
    binding = {
        "org_uuid": reg_org_uuid,
        "root_pub": reg_root_pub,
        "binding_generation": reg["binding_generation"],
        "registry_url": registry_url,
        "recovery_policy": policy,
        "binding_expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                            time.gmtime(expires_at)),
        "last_renewed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    binding_key = (
        urllib.parse.urlsplit(registry_url).netloc or registry_url
        if existing_binding is None
        else existing_binding.key
    )
    # NetworkBindingV1 is @home("organization"): an org-homed set refuses a
    # scopeless (org=None) WRITE — it must name a store (operator ruling
    # 2026-08-20, no default scope). The personal org's store is the operator's
    # own — "personal" — which resolves to the same personal.db that the fleet
    # reads back via _load_binding(None). A named org keeps its own slug.
    write_org = "personal" if settings_ops._resolve_org_arg(org) is None else org
    try:
        current_binding = _registration_binding_context(org)
        if not _same_registration_binding(existing_binding, current_binding):
            return JSONResponse(
                {
                    "ok": False,
                    "error": (
                        "local binding changed during registry registration "
                        "— authoritative response not persisted"
                    ),
                },
                status_code=409,
            )
        settings_ops.upsert_by_key(
            NETWORK_BINDING_SET_ID, NETWORK_BINDING_REVISION_2, binding_key,
            binding, org=write_org,
        )
    except Exception as e:
        return JSONResponse({"ok": False, "error": (
            f"registry accepted the binding but persisting it locally failed: {e}"
        )}, status_code=500)
    return JSONResponse({
        "ok": True,
        "registry": binding_key,
        "binding": binding,
        "outcome": reg["outcome"],
    })


async def post_renew(request: Request) -> JSONResponse:
    """Forward a signed binding-renewal heartbeat to the registry (§4.2).

    Body: ``{org?, envelope}`` — the browser-signed renew envelope
    ``{payload:{requested_ttl?}, signer, [cert], signature}``, signed over
    POST and the registry's own renew path. Renewal is the weakest mutation:
    it only extends the binding's liveness and never changes the bound root or
    recovery policy. The registry destination and org UUID are taken from the
    STORED binding, frozen server-side — a heartbeat cannot redirect itself to
    a foreign registry or a UUID the caller names. An ALREADY-EXPIRED binding
    cannot be renewed (the registry returns 410); reclaiming it is a fresh
    root-direct registration, which the sign-in path does instead.

    On the registry's 200 the local binding's expiry is advanced.
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
            "body must carry the signed renewal 'envelope' as an object"
        )}, status_code=400)
    unknown = set(body) - {"org", "envelope"}
    if unknown:
        return JSONResponse({"ok": False, "error": (
            f"unexpected keys {sorted(unknown)}: renewal carries only 'org' "
            "and 'envelope' — the registry destination is fixed server-side"
        )}, status_code=400)
    org, refused = resolve_scoped_org(body.get("org"), request=request)
    if refused is not None:
        return refused
    try:
        member = _first_member(NETWORK_BINDING_SET_ID, org)
    except Exception as e:
        return JSONResponse({"ok": False,
                             "error": f"could not read the binding setting: {e}"},
                            status_code=500)
    if member is None:
        return JSONResponse({"ok": False, "error": (
            "this organization has no binding to renew — register it first"
        )}, status_code=404)
    binding = dict(member.payload)
    org_uuid = binding.get("org_uuid")
    registry_url = binding.get("registry_url")
    if not org_uuid or not registry_url:
        return JSONResponse({"ok": False, "error": "the org's binding row is malformed"},
                            status_code=500)

    try:
        async with _registry_client(registry_url) as client:
            resp = await client.post(f"/v1/orgs/{org_uuid}/renew", json=body["envelope"])
    except httpx.HTTPError as e:
        return JSONResponse({"ok": False, "error": (
            f"could not reach the registry at {registry_url}: {e}"
        )}, status_code=502)
    if resp.status_code != 200:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        # 410 is the registry saying the binding already died — the caller
        # must reclaim it with a fresh registration, not a heartbeat.
        return JSONResponse({"ok": False, "expired": resp.status_code == 410,
                             "error": (
            f"registry refused the renewal ({resp.status_code}): {detail}"
        )}, status_code=502)

    reg = _validated_binding_response(resp.json(), registration=False)
    if reg is None:
        return JSONResponse({"ok": False, "error": (
            "registry returned a malformed authoritative binding — renewal not persisted"
        )}, status_code=502)
    local_policy = _canonical_binding_policy(binding.get("recovery_policy"))
    if (
        reg["org_uuid"] != org_uuid
        or reg["root_pub"] != binding.get("root_pub")
        or local_policy is None
        or reg["recovery_policy"] != local_policy
        or (
            binding.get("binding_generation") is not None
            and reg["binding_generation"] != binding.get("binding_generation")
        )
    ):
        return JSONResponse({"ok": False, "error": (
            "registry renewal authority does not match the frozen local binding "
            "— no V2 binding was written"
        )}, status_code=502)
    expires_at = reg["expires_at"]
    binding["binding_generation"] = reg["binding_generation"]
    binding["recovery_policy"] = reg["recovery_policy"]
    binding["binding_expires_at"] = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(expires_at))
    binding["last_renewed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    # See post_register: an org-homed set refuses a scopeless write, so the
    # personal binding is written under the operator's own store ("personal").
    write_org = "personal" if settings_ops._resolve_org_arg(org) is None else org
    try:
        settings_ops.upsert_by_key(
            NETWORK_BINDING_SET_ID, NETWORK_BINDING_REVISION_2, member.key,
            binding, org=write_org,
        )
    except Exception as e:
        return JSONResponse({"ok": False, "error": (
            f"registry renewed the binding but persisting it locally failed: {e}"
        )}, status_code=500)
    return JSONResponse({"ok": True, "registry": member.key, "binding": binding})


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


#: Renew a serving credential once fewer than this many days remain. The
#: delegate is minted for 30 days, so renewing under 20 means a fresh
#: credential is left alone for its first 10 days: signing in every day still
#: mints at most once per 10 days, while any sign-in in the final third
#: replaces it well before it dies.
logger = logging.getLogger(__name__)

SERVE_CERT_RENEW_BELOW_DAYS = 20

#: Last browser-side unlock maintenance report, for reading back off-device.
_LAST_UNLOCK_MAINTENANCE: dict = {}


async def get_serve_cert_status(request: Request) -> JSONResponse:
    """Cheap pre-unlock check: does this org need a fresh serving credential?

    No certificate bytes or identity are returned. The browser uses this only
    to decide whether the organization root it is about to unlock should also
    sign the context-specific serving certificates.
    """
    org, refused = resolve_scoped_org(request.query_params.get("org"), request=request)
    if refused is not None:
        return refused
    from tools.dashboard.link_serving_supervisor import serve_cert_state

    state = serve_cert_state(org)
    status = state.get("status", "missing")

    # RENEW BEFORE IT DIES, not after. `status` is "ok" for any certificate
    # that has not already passed not_after, so keying the mint decision on it
    # alone means a credential can only ever be replaced once it is expired --
    # every renewal necessarily begins with an outage, lasting until whenever
    # the next password unlock happens to occur. Renewal is opportunistic and
    # unlocks are irregular, so the window has to be wide enough that an
    # ordinary sign-in falls inside it: half the 30-day lifetime.
    #
    # Only the browser's "should I mint?" answer changes here. `status` is
    # returned untouched because the connector and the supervisor gate serving
    # on it being "ok" -- reporting a still-valid certificate as anything else
    # would stop serving, which is a worse outage than the one this prevents.
    required = status != "ok"
    days_remaining = None
    row = state.get("row") or {}
    if not isinstance(row.get("dns01_cert"), str):
        # Existing tunnel credentials remain usable while the ordinary unlock
        # ceremony upgrades them.  Do not take the live connector down merely
        # because its new, narrower DNS authority has not been minted yet.
        required = True
    not_after = row.get("not_after")
    if isinstance(not_after, int):
        days_remaining = (not_after - int(time.time())) / 86400.0
        if days_remaining < SERVE_CERT_RENEW_BELOW_DAYS:
            required = True
    return JSONResponse({
        "required": required,
        "status": status,
        # Reported whether or not a renewal is due, so the caller can say how
        # long a credential has left instead of only that it is fine for now.
        "days_remaining": (
            None if days_remaining is None else round(days_remaining, 1)
        ),
    })


async def post_serve_cert(request: Request) -> JSONResponse:
    """Provision the org's tunnel serving delegate (§5.1).

    Body: ``{org?, cert, viewer_cert, dns01_cert, private_key}`` — two
    direct-root ``tunnel:serve`` certificates plus one exact-scope
    ``serve:dns-01`` certificate over one Ed25519 serving child,
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
            or not isinstance(body.get("dns01_cert"), str) \
            or not isinstance(body.get("private_key"), str):
        return JSONResponse({"ok": False, "error": (
            "body must carry 'cert' (registry admission cert), 'viewer_cert' "
            "(identity-neutral viewer cert), 'dns01_cert' (exact-scope DNS "
            "delegate), and 'private_key' (their shared "
            "delegate key hex)"
        )}, status_code=400)

    # Refuse a cross-org write BEFORE processing the (foreign) payload.
    org, refused = resolve_scoped_org(body.get("org"), request=request)
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
        logger.warning(
            "post_serve_cert: binding row for org=%r is malformed -- "
            "root_pub=%r org_uuid=%r",
            org, root_pub, org_uuid,
        )
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
        dns01_cert = DelegationCert.from_json(body["dns01_cert"])
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
    if (
        dns01_cert.child_pub != cert.child_pub
        or dns01_cert.org != cert.org
        or tuple(dns01_cert.scope) != ("serve:dns-01",)
        or dns01_cert.not_before != cert.not_before
        or dns01_cert.not_after != cert.not_after
        or dns01_cert.parent_cert is not None
        or dns01_cert.subject != cert.subject
    ):
        return JSONResponse({"ok": False, "error": (
            "dns01 cert must be a direct-root serve:dns-01 delegate over "
            "the same child key, organization, persona, and validity window"
        )}, status_code=400)
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
        for candidate, required_scope in (
            (cert, SERVE_CERT_SCOPE),
            (viewer_cert, SERVE_CERT_SCOPE),
            (dns01_cert, "serve:dns-01"),
        ):
            verified = verify_chain(
                candidate,
                root_pub,
                org=org_uuid,
                now=now,
                required_scope=required_scope,
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
                and previous_serve.payload.get("dns01_cert") == body["dns01_cert"]
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
        logger.warning(
            "post_serve_cert: could not write serve key file %s for org=%r: %r",
            key_path, org, e,
        )
        return JSONResponse({"ok": False, "error": f"could not write the serve key file: {e}"},
                            status_code=500)
    # NetworkServeCertV2 is @home("organization") — same as the binding, an
    # org-homed set refuses a scopeless (org=None) write. The personal org's
    # serve-cert lives in the operator's own store ("personal"), which resolves
    # to the same personal.db that serve_cert_state(None) reads. Without this the
    # personal serve-cert POST 500s ("declares no single home"), the browser's
    # best-effort provisioning swallows it, and the tunnel never comes up.
    write_org = "personal" if settings_ops._resolve_org_arg(org) is None else org
    try:
        settings_ops.upsert_by_key(
            NETWORK_SERVE_CERT_SET_ID, NETWORK_SERVE_CERT_REVISION, "default",
            {"cert": body["cert"], "viewer_cert": body["viewer_cert"],
             "dns01_cert": body["dns01_cert"],
             "key_path": key_file,
             "root_pub": root_pub, "not_after": cert.not_after},
            org=write_org,
        )
    except Exception as e:
        logger.warning(
            "post_serve_cert: could not store serve cert settings row for org=%r: %r",
            org, e,
        )
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



async def post_unlock_maintenance_report(request: Request) -> JSONResponse:
    """Record what the unlock's maintenance pass actually did, per org.

    Serving repair and the org-key migration run in the browser, so their
    outcome has until now existed only in a console. An operator on a phone
    has no console, and a failure nobody can read is the reporting defect that
    let three organizations drift to the edge of expiry unnoticed. The browser
    posts its report here so the result is on the server, where it can be read
    without the machine that produced it.

    Diagnostic only: it carries statuses and error strings, never key
    material, and is logged rather than stored.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "body must be JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse(
            {"ok": False, "error": "body must be a JSON object"}, status_code=400,
        )
    logger.warning("[unlock-maintenance] %s", json.dumps(body, default=str)[:4000])
    _LAST_UNLOCK_MAINTENANCE.clear()
    _LAST_UNLOCK_MAINTENANCE.update(body)
    _LAST_UNLOCK_MAINTENANCE["received_at"] = int(time.time())
    return JSONResponse({"ok": True})


async def get_unlock_maintenance_report(request: Request) -> JSONResponse:
    """The most recent unlock maintenance report, or an empty object."""
    return JSONResponse(dict(_LAST_UNLOCK_MAINTENANCE))


def _service_publication_error(code: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": code}, status_code=status_code)


def _service_publication_org(request: Request) -> tuple[str | None, JSONResponse | None]:
    refused = require_global_api_authority(request)
    if refused is not None:
        return None, refused
    org = organization_scope_from_request(request)
    if not isinstance(org, str) or not org:
        return None, _service_publication_error("organization_required")
    return org, None


async def get_service_reservations(request: Request) -> JSONResponse:
    org, refused = _service_publication_org(request)
    if refused is not None:
        return refused
    from tools.dashboard import service_publication

    return JSONResponse({"reservations": service_publication.list_reservations(org)})


async def post_service_reservation(request: Request) -> JSONResponse:
    org, refused = _service_publication_org(request)
    if refused is not None:
        return refused
    try:
        body = await request.json()
    except Exception:
        return _service_publication_error("invalid_json")
    if not isinstance(body, dict):
        return _service_publication_error("invalid_json")
    if set(body) != {"app_label"}:
        return _service_publication_error("unknown_fields")
    from tools.dashboard import service_publication

    try:
        projection, created = service_publication.reserve_origin(
            org, body.get("app_label")
        )
    except ValueError:
        return _service_publication_error("invalid_app_label")
    except service_publication.ServicePublicationError as exc:
        return _service_publication_error(exc.code, exc.status_code)
    return JSONResponse(
        {"reservation": projection}, status_code=201 if created else 200
    )


async def put_service_reservation_state(request: Request) -> JSONResponse:
    org, refused = _service_publication_org(request)
    if refused is not None:
        return refused
    try:
        body = await request.json()
    except Exception:
        return _service_publication_error("invalid_json")
    if not isinstance(body, dict):
        return _service_publication_error("invalid_json")
    if set(body) != {"state"}:
        return _service_publication_error("unknown_fields")
    state = body.get("state")
    if state not in {"active", "paused", "released"}:
        return _service_publication_error("invalid_state")
    from tools.dashboard import service_publication

    try:
        projection, _changed = service_publication.transition_reservation(
            org, request.path_params.get("reservation_id", ""), state
        )
    except service_publication.ServicePublicationError as exc:
        return _service_publication_error(exc.code, exc.status_code)
    return JSONResponse({"reservation": projection})


async def get_service_targets(request: Request) -> JSONResponse:
    org, refused = _service_publication_org(request)
    if refused is not None:
        return refused
    from tools.dashboard import service_publication

    return JSONResponse({"targets": service_publication.list_service_targets(org)})


async def get_service_gateway(request: Request) -> JSONResponse:
    _org, refused = _service_publication_org(request)
    if refused is not None:
        return refused
    from tools.dashboard import web_gateway_supervisor

    return JSONResponse({"gateway": web_gateway_supervisor.status()})


async def put_service_target(request: Request) -> JSONResponse:
    org, refused = _service_publication_org(request)
    if refused is not None:
        return refused
    try:
        body = await request.json()
    except Exception:
        return _service_publication_error("invalid_json")
    if not isinstance(body, dict):
        return _service_publication_error("invalid_json")
    if set(body) != {"session_id", "port"}:
        return _service_publication_error("unknown_fields")
    from tools.dashboard import service_publication

    try:
        projection, created = await service_publication.bind_service_target(
            org,
            request.path_params.get("reservation_id", ""),
            body.get("session_id"),
            body.get("port"),
        )
    except service_publication.ServicePublicationError as exc:
        return _service_publication_error(exc.code, exc.status_code)
    return JSONResponse(
        {"target": projection}, status_code=201 if created else 200
    )


async def delete_service_target(request: Request) -> Response:
    org, refused = _service_publication_org(request)
    if refused is not None:
        return refused
    if await request.body():
        return _service_publication_error("unknown_fields")
    from tools.dashboard import service_publication

    try:
        service_publication.unbind_service_target(
            org, request.path_params.get("reservation_id", "")
        )
    except service_publication.ServicePublicationError as exc:
        return _service_publication_error(exc.code, exc.status_code)
    return Response(status_code=204)


async def check_service_target(request: Request) -> JSONResponse:
    org, refused = _service_publication_org(request)
    if refused is not None:
        return refused
    from tools.dashboard import service_publication

    try:
        descriptor = await service_publication.resolve_service_target(
            org, request.path_params.get("reservation_id", "")
        )
    except service_publication.ServicePublicationError as exc:
        return _service_publication_error(exc.code, exc.status_code)
    return JSONResponse(
        {
            "ok": True,
            "target": {
                "reservation_id": request.path_params["reservation_id"],
                "session_id": descriptor.session_id,
                "port": descriptor.port,
                "checked_at": descriptor.checked_at,
                "expires_at": descriptor.expires_at,
            },
        }
    )


ROUTES = [
    Route("/api/network/service-reservations", get_service_reservations, methods=["GET"]),
    Route("/api/network/service-reservations", post_service_reservation, methods=["POST"]),
    Route(
        "/api/network/service-reservations/{reservation_id}/state",
        put_service_reservation_state,
        methods=["PUT"],
    ),
    Route("/api/network/service-targets", get_service_targets, methods=["GET"]),
    Route("/api/network/service-gateway", get_service_gateway, methods=["GET"]),
    Route(
        "/api/network/service-targets/{reservation_id}",
        put_service_target,
        methods=["PUT"],
    ),
    Route(
        "/api/network/service-targets/{reservation_id}",
        delete_service_target,
        methods=["DELETE"],
    ),
    Route(
        "/api/network/service-targets/{reservation_id}/check",
        check_service_target,
        methods=["POST"],
    ),
    Route("/api/network/org-key", get_org_key, methods=["GET"]),
    Route("/api/network/org-key/sealed", post_sealed_org_key, methods=["POST"]),
    Route("/api/network/binding", get_binding, methods=["GET"]),
    Route("/api/network/registry", get_registry, methods=["GET"]),
    Route("/api/network/ledger/found", post_ledger_found, methods=["POST"]),
    Route("/api/network/ledger/heads", get_ledger_heads, methods=["GET"]),
    Route("/api/network/ledger/delegate", post_ledger_delegate, methods=["POST"]),
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
    Route("/api/network/renew", post_renew, methods=["POST"]),
    Route("/api/network/invite/resolve", post_invite_resolve, methods=["POST"]),
    Route("/api/network/serve-cert", get_serve_cert_status, methods=["GET"]),
    Route("/api/network/unlock-report", get_unlock_maintenance_report),
    Route("/api/network/unlock-report", post_unlock_maintenance_report, methods=["POST"]),
    Route("/api/network/serve-cert", post_serve_cert, methods=["POST"]),
    Route("/api/network/revocations", post_revocation, methods=["POST"]),
]
