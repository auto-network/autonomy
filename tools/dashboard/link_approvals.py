"""``link_publish`` / ``link_revoke`` approval kinds — the C3 share-link ceremony.

The share-link publish flow rides the generalized approval primitive
(``approvals_routes``) exactly the way commit signing does, with the roles
split the same way:

* the **requester** (``graph link publish`` / ``revoke``, see
  ``tools/graph/link_cmd.py``) posts a pending request and blocks on the
  held GET;
* the **operator's browser** reviews WHAT is being shared (the enrichment
  below resolves the target's real title from trusted local stores — the
  requesting agent cannot spoof it) and, on approve, signs the exact
  registry request with the operator's SESSION key (click only, no
  passphrase — spec §6.4) and posts the signed envelope in the decision;
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
* audience pinning (confused-deputy guard) — the registry envelope binds
  only method/path/payload, not the destination host, and the executor
  reloads the org binding at execution time. So the approve decision also
  carries the ``registry_url`` the dialog displayed and the signer saw,
  and the executor REFUSES when the current binding's ``registry_url``
  differs: a binding swap between render and approval must never silently
  redirect an operator-approved publish to a different registry.
"""

from __future__ import annotations

import re
import time

import httpx

from tools.graph import settings_ops
# Importing registers the autonomy.network.* Setting schemas (they
# self-register on import), so grant-cache writes validate.
from tools.graph.schemas.network_identity import (  # noqa: F401
    NETWORK_BINDING_SET_ID,
    NETWORK_LINK_GRANT_REVISION,
    NETWORK_LINK_GRANT_SET_ID,
    TARGET_TYPES,
)

_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")  # 128-bit CSPRNG token shape (I2)

_TYPE_LABELS = {
    "present": "Present deck",
    "design": "Design",
    "note": "Note",
    "file": "File",
}


# ── trusted lookups ───────────────────────────────────────────


def _load_binding(org: str | None) -> tuple[dict | None, str | None]:
    """The org's auto.network binding row → (payload, error).

    One binding is the common case; with several, the lexically first
    registry key wins deterministically (v1: no --registry selector).
    """
    try:
        members = sorted(
            settings_ops.read_set(NETWORK_BINDING_SET_ID, org=org).members,
            key=lambda m: m.key,
        )
    except Exception as e:
        return None, f"could not read the org's network binding: {e}"
    if not members:
        return None, (
            "this org has no auto.network binding — run the org identity "
            "ceremony (C1) to register one before publishing share-links"
        )
    payload = members[0].payload
    if not isinstance(payload, dict) or not payload.get("registry_url"):
        return None, "the org's network binding row is malformed"
    return payload, None


def _resolve_target(target_type: str, target_uuid: str, org: str | None) -> dict:
    """Resolve what is being shared from TRUSTED local stores.

    Returns ``{"title": str | None, "error": str | None}``. The title is
    what the operator sees in the dialog; a resolution failure is shown
    too — the operator can still decline, but never approves blind.
    """
    try:
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
            src = graph_ops.get_source(target_uuid, org=org)
            if not src:
                return {"title": None, "error": f"note {target_uuid} not found in the graph"}
            return {"title": src.get("title") or src.get("label") or target_uuid,
                    "error": None}
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
    return payload


# ── GET enrichment (what the operator reviews) ────────────────


def _enrich_link_publish(row: dict) -> dict:
    req = row["request"]
    org = req.get("org")
    meta = req.get("meta") or {}
    target = _resolve_target(req.get("target_type", ""), req.get("target_uuid", ""), org)
    binding, binding_error = _load_binding(org)
    out = {
        "target_title": target["title"],
        "target_error": target["error"],
        "type_label": _TYPE_LABELS.get(req.get("target_type", ""), req.get("target_type")),
        "ttl": meta.get("ttl"),
        "label": meta.get("label"),
        "binding_error": binding_error,
    }
    if binding:
        out["registry_request"] = {
            "method": "POST",
            "path": "/v1/links",
            "registry_url": binding["registry_url"],
            "payload": _registry_payload(req, binding),
        }
    return out


def _enrich_link_revoke(row: dict) -> dict:
    req = row["request"]
    org = req.get("org")
    token = req.get("token", "")
    binding, binding_error = _load_binding(org)
    # Show which grant dies: resolve the token through the local grant cache.
    grant = _cached_grant(token, org)
    target_title, type_label = None, None
    if grant:
        resolved = _resolve_target(grant.get("target_type", ""),
                                   grant.get("target_uuid", ""), org)
        target_title = resolved["title"] or grant.get("target_uuid")
        type_label = _TYPE_LABELS.get(grant.get("target_type", ""),
                                      grant.get("target_type"))
    out = {
        "target_title": target_title,
        "type_label": type_label,
        "label": (grant.get("meta") or {}).get("label") if grant else None,
        "cached": grant is not None,
        "binding_error": binding_error,
    }
    if binding:
        out["registry_request"] = {
            "method": "DELETE",
            "path": f"/v1/links/{token}",
            "registry_url": binding["registry_url"],
            "payload": {},
        }
    return out


def _cached_grant(token: str, org: str | None) -> dict | None:
    try:
        for m in settings_ops.read_set(NETWORK_LINK_GRANT_SET_ID, org=org).members:
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


def _audience_error(decision: dict, binding: dict) -> str | None:
    """The confused-deputy guard: the destination the operator approved must
    be exactly the destination this execution would forward to."""
    approved_url = decision.get("registry_url")
    if not isinstance(approved_url, str) or not approved_url:
        return (
            "decision carried no registry_url — the approved destination "
            "must ride the decision so a binding swap cannot redirect the "
            "signed request (confused-deputy guard)"
        )
    if approved_url != binding["registry_url"]:
        return (
            "the org's registry binding changed between review and approval "
            f"(operator approved {approved_url!r}, binding now points at "
            f"{binding['registry_url']!r}) — refusing to forward the signed "
            "request; re-run the publish so the operator reviews the new "
            "destination"
        )
    return None


def _envelope_and_subject(decision: dict) -> tuple[dict | None, dict | None, str | None]:
    """Validate the decision's signed envelope; return (envelope, subject, error).

    The chain itself is verified authoritatively by the registry (I4); here
    we only require the C2 shape — a cert-bearing envelope — and extract
    the subject for I6 attribution.
    """
    envelope = decision.get("envelope")
    if not isinstance(envelope, dict):
        return None, None, (
            "decision carried no signed envelope — the sign-on ceremony (C2) "
            "has not landed, so there is no operator session key to sign with"
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
    if subject["kind"] not in ("operator", "agent"):
        return None, None, (
            f"cert subject kind {subject['kind']!r} cannot issue grants "
            "(operator or agent subjects only)"
        )
    return envelope, subject, None


async def _forward_to_registry(binding: dict, envelope: dict, method: str,
                               path: str) -> tuple[httpx.Response | None, str | None]:
    try:
        async with _registry_client(binding["registry_url"]) as client:
            resp = await client.request(method, path, json=envelope)
        return resp, None
    except httpx.HTTPError as e:
        return None, f"could not reach the registry at {binding['registry_url']}: {e}"


def _registry_error(resp: httpx.Response) -> str:
    try:
        detail = resp.json().get("detail", resp.text)
    except Exception:
        detail = resp.text
    return f"registry refused the request ({resp.status_code}): {detail}"


async def _execute_link_publish(row: dict, decision: dict) -> dict:
    req = row["request"]
    org = req.get("org")
    envelope, subject, err = _envelope_and_subject(decision)
    if err:
        return _fail(err)
    binding, binding_error = _load_binding(org)
    if binding_error:
        return _fail(binding_error)
    audience_error = _audience_error(decision, binding)
    if audience_error:
        return _fail(audience_error)
    expected = _registry_payload(req, binding)
    if envelope.get("payload") != expected:
        # What was staged is exactly what an approval applies to.
        return _fail("signed payload does not match the approved request — refusing to publish")
    resp, err = await _forward_to_registry(binding, envelope, "POST", "/v1/links")
    if err:
        return _fail(err)
    if resp.status_code != 201:
        return _fail(_registry_error(resp))
    body = resp.json()
    token, url = body.get("token"), body.get("url")
    if not isinstance(token, str) or not _TOKEN_RE.match(token):
        # I2 tripwire: only a CSPRNG-shaped opaque token enters the cache.
        return _fail("registry returned a malformed grant token — not caching it")
    grant = {
        "token": token,
        "target_uuid": req["target_uuid"],
        "target_type": req["target_type"],
        "meta": req.get("meta") or {},
        "subject": subject,  # I6: the issuing cert's subject
        "issued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    settings_ops.upsert_by_key(
        NETWORK_LINK_GRANT_SET_ID, NETWORK_LINK_GRANT_REVISION,
        token, grant, org=org,
    )
    return {"ok": True, "url": url, "token": token}


async def _execute_link_revoke(row: dict, decision: dict) -> dict:
    req = row["request"]
    org = req.get("org")
    token = req.get("token", "")
    envelope, _subject, err = _envelope_and_subject(decision)
    if err:
        return _fail(err)
    binding, binding_error = _load_binding(org)
    if binding_error:
        return _fail(binding_error)
    audience_error = _audience_error(decision, binding)
    if audience_error:
        return _fail(audience_error)
    if envelope.get("payload") != {}:
        return _fail("revoke envelopes carry an empty payload — refusing to forward")
    resp, err = await _forward_to_registry(binding, envelope, "DELETE", f"/v1/links/{token}")
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
        for m in settings_ops.read_set(NETWORK_LINK_GRANT_SET_ID, org=org).members:
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
