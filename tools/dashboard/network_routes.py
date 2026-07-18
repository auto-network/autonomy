"""auto.network identity routes — the C2 sign-on ceremony's server side.

Three small routes back the browser ceremony in
``static/js/network-signon.js`` (spec §6.3):

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

The sign-on ceremony itself needs no POST: minting happens entirely in
the browser and the session key never leaves it.
"""

from __future__ import annotations

import os

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from tools.graph import settings_ops
# Importing registers the autonomy.network.* Setting schemas (they
# self-register on import).
from tools.graph.schemas.network_identity import (  # noqa: F401
    NETWORK_BINDING_SET_ID,
    NETWORK_ORG_KEY_SET_ID,
)


def _mock_mode() -> bool:
    # The mock dashboard has no settings DB and no org identity; the
    # chrome must land deterministically signed-out there.
    return bool(os.environ.get("DASHBOARD_MOCK"))


def _first_member(set_id: str, org: str | None):
    """Lexically-first member of a keyed set, or None. One row is the
    common case; with several, the lexically first key wins
    deterministically (v1: no selector, matching link_approvals)."""
    members = sorted(
        settings_ops.read_set(set_id, org=org).members, key=lambda m: m.key
    )
    for m in members:
        if isinstance(m.payload, dict):
            return m
    return None


async def get_org_key(request: Request) -> JSONResponse:
    """The org's armored (encrypted) network root key, or 404."""
    if _mock_mode():
        return JSONResponse({"error": "no network org key configured"}, status_code=404)
    org = request.query_params.get("org") or None
    try:
        member = _first_member(NETWORK_ORG_KEY_SET_ID, org)
    except Exception as e:
        return JSONResponse({"error": f"could not read the org key setting: {e}"},
                            status_code=500)
    if member is None or not member.payload.get("armored_private_key"):
        return JSONResponse({"error": (
            "no auto.network org key is stored for this org — run the org "
            "identity ceremony (C1) first"
        )}, status_code=404)
    return JSONResponse({
        "label": member.key,
        "armored_private_key": member.payload["armored_private_key"],
        "root_pub": member.payload.get("root_pub"),
    })


async def get_binding(request: Request) -> JSONResponse:
    """The org's registry binding row, or 404."""
    if _mock_mode():
        return JSONResponse({"error": "no network binding configured"}, status_code=404)
    org = request.query_params.get("org") or None
    try:
        member = _first_member(NETWORK_BINDING_SET_ID, org)
    except Exception as e:
        return JSONResponse({"error": f"could not read the binding setting: {e}"},
                            status_code=500)
    if member is None:
        return JSONResponse({"error": (
            "this org has no auto.network binding — run the org identity "
            "ceremony (C1) to register one"
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
    if not isinstance(body, dict) or not isinstance(body.get("record"), str) \
            or not isinstance(body.get("revoked_cert"), str):
        return JSONResponse({"ok": False, "error": (
            "body must carry 'record' and 'revoked_cert' as canonical wire "
            "JSON strings"
        )}, status_code=400)
    org = body.get("org") or None
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


ROUTES = [
    Route("/api/network/org-key", get_org_key, methods=["GET"]),
    Route("/api/network/binding", get_binding, methods=["GET"]),
    Route("/api/network/revocations", post_revocation, methods=["POST"]),
]
