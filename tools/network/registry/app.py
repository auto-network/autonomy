"""FastAPI application — auto.network registry v1 (spec §4).

Authorization model (I4): the registry holds **no permission tables**.
Every mutating endpoint parses a signed envelope (``signing.py``), then
verifies the embedded delegation chain against the org binding's root
public key with ``idkit.verify_chain`` — signature, org, time, strict
narrowing, and revocation are all re-checked per request. A request
signed by the bound root key itself ("root-direct", no cert) is also
accepted; scope requirements apply only to delegated signers, since the
root is the authority every scope narrows from.

Two anchors sit outside the chain rule by construction:

- **registration** (§4.1) is self-signed by the root key being bound —
  the binding does not exist yet (first-key-claims-UUID);
- **rebind** (§4.3) is signed by the recovery key the org pre-declared —
  policy ``none`` has no accept path at all (I3: structurally
  impossible), not a disabled one.

Rung-2 surface (viewer authn) is fenced off with ``501 rung-2``:
``subject.kind == "persona"`` signers and ``meta.require_auth`` grants
are rejected until Track E lands.
"""

from __future__ import annotations

import time
import uuid
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket

from tools.network.idkit import (
    ChainVerifyError,
    DelegationCert,
    IdkitError,
    MalformedError,
    RevocationError,
    RevocationRecord,
    generate_token,
    verify_chain,
    verify_revocation,
    verify_signature,
)
from tools.network.idkit.keys import PUBLIC_KEY_HEX_LEN, _decode_hex

from .relay import TunnelHub, tunnel_endpoint, viewer_endpoint
from .signing import ENVELOPE_VERSION, MAX_CLOCK_SKEW, request_signing_input
from .store import LinkGrant, OrgBinding, RegistryStore

DEFAULT_BINDING_TTL = 30 * 86_400  # spec §4.2: binding TTL default 30d
MIN_BINDING_TTL = 3_600
MAX_BINDING_TTL = DEFAULT_BINDING_TTL

RECOVERY_POLICIES = frozenset({"none", "recovery-key"})

_ENVELOPE_FIELDS = frozenset({"v", "signer", "ts", "payload", "cert", "sig"})
_LINK_META_FIELDS = frozenset({"ttl", "label", "require_auth"})


class AuthContext:
    """What a verified envelope authorizes (attribution per I6)."""

    def __init__(self, signer_pub: str, subject_kind: str, subject_id: str,
                 cert: Optional[DelegationCert]):
        self.signer_pub = signer_pub
        self.subject_kind = subject_kind
        self.subject_id = subject_id
        self.cert = cert


def _bad_request(msg: str) -> HTTPException:
    return HTTPException(status_code=400, detail=msg)


def _forbidden(msg: str) -> HTTPException:
    return HTTPException(status_code=403, detail=msg)


def _rung2(msg: str) -> HTTPException:
    return HTTPException(status_code=501, detail=f"rung-2: {msg}")


async def _read_json(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise _bad_request("request body is not valid JSON")
    if not isinstance(body, dict):
        raise _bad_request("request body must be a JSON object")
    return body


def _parse_envelope(body: dict) -> dict:
    unknown = set(body) - _ENVELOPE_FIELDS
    if unknown:
        raise _bad_request(f"envelope carries unknown fields: {sorted(unknown)}")
    missing = _ENVELOPE_FIELDS - set(body) - {"cert"}
    if missing:
        raise _bad_request(f"envelope is missing fields: {sorted(missing)}")
    if body["v"] != ENVELOPE_VERSION:
        raise _bad_request(f"unsupported envelope version: {body['v']!r}")
    if type(body["ts"]) is not int:
        raise _bad_request("envelope ts must be an integer unix timestamp")
    if not isinstance(body["payload"], dict):
        raise _bad_request("envelope payload must be a JSON object")
    if "cert" in body and not isinstance(body["cert"], str):
        raise _bad_request("envelope cert must be a canonical wire JSON string")
    return body


def _verify_envelope_signature(envelope: dict, method: str, path: str, now: int) -> None:
    if abs(now - envelope["ts"]) > MAX_CLOCK_SKEW:
        raise _forbidden(f"envelope ts outside ±{MAX_CLOCK_SKEW}s freshness window")
    try:
        signing_input = request_signing_input(
            method, path, envelope["ts"], envelope["signer"], envelope["payload"]
        )
        verify_signature(envelope["signer"], envelope["sig"], signing_input)
    except MalformedError as exc:
        raise _bad_request(str(exc))
    except IdkitError:
        raise _forbidden("envelope signature does not verify against signer")


def _authorize(
    envelope: dict,
    method: str,
    path: str,
    binding: OrgBinding,
    store: RegistryStore,
    now: int,
    *,
    required_scope: Optional[str] = None,
    required_target_type: Optional[str] = None,
) -> AuthContext:
    """The I4 gate: envelope signature + delegation chain to the bound root.

    Root-direct (no cert): the signer must BE the bound root key.
    Delegated: the cert must delegate to the signer and its chain must
    verify down from the bound root — including scope, narrowing, time,
    and the org's current revocation denylist.
    """
    _verify_envelope_signature(envelope, method, path, now)
    store.purge_expired_revocations(now=now)  # I7: sweep before every check

    cert_wire = envelope.get("cert")
    if cert_wire is None:
        if envelope["signer"] != binding.root_pub:
            raise _forbidden("signer does not chain to the org's bound root key")
        return AuthContext(binding.root_pub, "root", binding.root_pub, None)

    try:
        cert = DelegationCert.from_json(cert_wire)
    except MalformedError as exc:
        raise _bad_request(f"cert: {exc}")
    if cert.child_pub != envelope["signer"]:
        raise _forbidden("cert does not delegate to the envelope signer")

    try:
        result = verify_chain(
            cert,
            binding.root_pub,
            org=binding.org_uuid,
            now=now,
            revocations=store.revocation_set(binding.org_uuid),
            required_scope=required_scope,
            required_target_type=required_target_type,
        )
    except ChainVerifyError as exc:
        raise _forbidden(f"{type(exc).__name__}: {exc}")
    except MalformedError as exc:
        raise _bad_request(str(exc))

    if result.subject_kind == "persona":
        raise _rung2("persona subjects require viewer authn (Track E)")
    return AuthContext(result.leaf_pub, result.subject_kind, result.subject_id, cert)


def _require_binding(store: RegistryStore, org_uuid: str, now: int) -> OrgBinding:
    binding = store.get_org(org_uuid)
    if binding is None:
        raise HTTPException(status_code=404, detail="unknown org binding")
    if binding.expires_at < now:
        raise HTTPException(status_code=410, detail="org binding has expired")
    return binding


def _clamp_ttl(payload: dict, field: str = "requested_ttl") -> int:
    ttl = payload.get(field, DEFAULT_BINDING_TTL)
    if type(ttl) is not int or ttl <= 0:
        raise _bad_request(f"{field} must be a positive integer of seconds")
    return max(MIN_BINDING_TTL, min(ttl, MAX_BINDING_TTL))


def _require_fields(payload: dict, allowed: frozenset, required: frozenset, what: str) -> None:
    unknown = set(payload) - allowed
    if unknown:
        raise _bad_request(f"{what} carries unknown fields: {sorted(unknown)}")
    missing = required - set(payload)
    if missing:
        raise _bad_request(f"{what} is missing fields: {sorted(missing)}")


def _require_pub(value: object, what: str) -> str:
    try:
        _decode_hex(value, PUBLIC_KEY_HEX_LEN, what)
    except MalformedError as exc:
        raise _bad_request(str(exc))
    return value  # type: ignore[return-value]


def _require_uuid(value: object, what: str) -> str:
    if not isinstance(value, str):
        raise _bad_request(f"{what} must be a UUID string")
    try:
        uuid.UUID(value)
    except ValueError:
        raise _bad_request(f"{what} is not a valid UUID")
    return value


def _parse_recovery_policy(payload: dict) -> tuple:
    """Returns (policy, recovery_pub_or_None); enforces pairing rules."""
    policy = payload.get("recovery_policy")
    if policy not in RECOVERY_POLICIES:
        raise _bad_request(f"recovery_policy must be one of {sorted(RECOVERY_POLICIES)}")
    recovery_pub = payload.get("recovery_pub")
    if policy == "recovery-key":
        if recovery_pub is None:
            raise _bad_request("recovery_policy recovery-key requires recovery_pub")
        return policy, _require_pub(recovery_pub, "recovery_pub")
    if recovery_pub is not None:
        raise _bad_request("recovery_pub is only valid with recovery_policy recovery-key")
    return policy, None


def create_app(
    db_path: str = ":memory:",
    *,
    now_fn=None,
    base_url: str = "https://auto.network",
) -> FastAPI:
    """Build the registry app.

    *now_fn* is the clock (unix seconds); injectable so TTL, expiry, and
    purge behavior are deterministic under test. *base_url* prefixes the
    share-link URLs returned by ``POST /v1/links``.
    """
    app = FastAPI(title="auto.network registry", version="1")
    store = RegistryStore(db_path)
    now_fn = now_fn or (lambda: int(time.time()))
    hub = TunnelHub()
    app.state.store = store
    app.state.now_fn = now_fn
    app.state.tunnel_hub = hub

    def now() -> int:
        return int(now_fn())

    # -- §4.1 register binding ----------------------------------------------

    @app.post("/v1/orgs", status_code=201)
    async def register_org(request: Request):
        envelope = _parse_envelope(await _read_json(request))
        payload = envelope["payload"]
        _require_fields(
            payload,
            allowed=frozenset(
                {"org_uuid", "root_pub", "recovery_policy", "recovery_pub",
                 "requested_ttl", "endpoint_hints"}
            ),
            required=frozenset({"org_uuid", "root_pub", "recovery_policy"}),
            what="registration payload",
        )
        org_uuid = _require_uuid(payload["org_uuid"], "org_uuid")
        root_pub = _require_pub(payload["root_pub"], "root_pub")
        policy, recovery_pub = _parse_recovery_policy(payload)
        endpoint_hints = payload.get("endpoint_hints")
        if endpoint_hints is not None and not isinstance(endpoint_hints, list):
            raise _bad_request("endpoint_hints must be a list")
        ttl = _clamp_ttl(payload)

        # Genesis is self-signed: the key claiming the UUID must sign the
        # claim. No cert — there is nothing to chain to yet.
        if envelope.get("cert") is not None:
            raise _bad_request("registration is self-signed by root_pub; cert must be absent")
        if envelope["signer"] != root_pub:
            raise _forbidden("registration must be signed by the root_pub being bound")
        t = now()
        _verify_envelope_signature(envelope, "POST", str(request.url.path), t)

        existing = store.get_org(org_uuid)
        if existing is not None and existing.expires_at >= t:
            # First key claims the UUID; names are not authority (§4.1).
            raise HTTPException(status_code=409, detail="org UUID is already bound")
        store.create_org(
            org_uuid,
            root_pub,
            policy,
            recovery_pub,
            now=t,
            expires_at=t + ttl,
            endpoint_hints=endpoint_hints,
            replacing_expired=existing is not None,
        )
        return {"org_uuid": org_uuid, "root_pub": root_pub, "expires_at": t + ttl}

    # -- §4.2 renew (heartbeat) ----------------------------------------------

    @app.post("/v1/orgs/{org_uuid}/renew")
    async def renew_org(org_uuid: str, request: Request):
        envelope = _parse_envelope(await _read_json(request))
        _require_fields(
            envelope["payload"],
            allowed=frozenset({"requested_ttl"}),
            required=frozenset(),
            what="renew payload",
        )
        ttl = _clamp_ttl(envelope["payload"])
        t = now()
        binding = _require_binding(store, org_uuid, t)
        # Renewal is deliberately the weakest mutation: it only extends the
        # liveness of authority that already exists, so ANY key that
        # verifiably belongs to the org (root-direct or any valid chain,
        # no scope requirement) may heartbeat.
        _authorize(envelope, "POST", str(request.url.path), binding, store, t)
        store.renew_org(org_uuid, now=t, expires_at=t + ttl)
        return {"org_uuid": org_uuid, "expires_at": t + ttl}

    # -- §4.3 rebind (recovery only, I3) --------------------------------------

    @app.post("/v1/orgs/{org_uuid}/rebind")
    async def rebind_org(org_uuid: str, request: Request):
        t = now()
        binding = _require_binding(store, org_uuid, t)

        # I3: no rebind path outside the pre-declared policy. Under policy
        # "none" this endpoint has no accept branch at all — the request is
        # refused before any signature is even looked at, so no payload
        # (valid-root-signed included) can reach a rebind.
        if binding.recovery_policy != "recovery-key":
            raise _forbidden(
                "rebind is structurally unavailable: recovery policy is "
                f"{binding.recovery_policy!r}"
            )

        envelope = _parse_envelope(await _read_json(request))
        payload = envelope["payload"]
        _require_fields(
            payload,
            allowed=frozenset({"new_root_pub", "recovery_policy", "recovery_pub"}),
            required=frozenset({"new_root_pub"}),
            what="rebind payload",
        )
        new_root_pub = _require_pub(payload["new_root_pub"], "new_root_pub")
        if "recovery_policy" in payload:
            new_policy, new_recovery_pub = _parse_recovery_policy(payload)
        else:
            if "recovery_pub" in payload:
                raise _bad_request("recovery_pub requires recovery_policy")
            new_policy, new_recovery_pub = binding.recovery_policy, binding.recovery_pub

        # The ONLY key that can sign a rebind is the pre-declared cold
        # recovery key. Not the root (a stolen root must not be able to
        # rotate away the recovery path), not a delegate.
        if envelope.get("cert") is not None:
            raise _forbidden("rebind must be signed directly by the recovery key; cert must be absent")
        if envelope["signer"] != binding.recovery_pub:
            raise _forbidden("rebind must be signed by the declared recovery key")
        _verify_envelope_signature(envelope, "POST", str(request.url.path), t)

        store.rebind_org(org_uuid, binding.root_pub, new_root_pub, now=t)
        if new_policy != binding.recovery_policy or new_recovery_pub != binding.recovery_pub:
            store.update_recovery_policy(org_uuid, new_policy, new_recovery_pub)
        return {
            "org_uuid": org_uuid,
            "root_pub": new_root_pub,
            "previous_root_pub": binding.root_pub,
            "recovery_policy": new_policy,
        }

    # -- §4.4 links ------------------------------------------------------------

    @app.post("/v1/links", status_code=201)
    async def create_link(request: Request):
        envelope = _parse_envelope(await _read_json(request))
        payload = envelope["payload"]
        _require_fields(
            payload,
            allowed=frozenset({"org", "target_uuid", "target_type", "meta"}),
            required=frozenset({"org", "target_uuid", "target_type"}),
            what="link payload",
        )
        org_uuid = _require_uuid(payload["org"], "org")
        target_uuid = _require_uuid(payload["target_uuid"], "target_uuid")
        target_type = payload["target_type"]
        if not isinstance(target_type, str) or not target_type:
            raise _bad_request("target_type must be a non-empty string")
        meta = payload.get("meta", {})
        if not isinstance(meta, dict):
            raise _bad_request("meta must be a JSON object")
        _require_fields(meta, allowed=_LINK_META_FIELDS, required=frozenset(), what="link meta")
        if meta.get("require_auth"):
            raise _rung2("require_auth grants need viewer authn (Track E + ledger)")
        link_ttl = meta.get("ttl")
        if link_ttl is not None and (type(link_ttl) is not int or link_ttl <= 0):
            raise _bad_request("meta.ttl must be a positive integer of seconds")

        t = now()
        binding = _require_binding(store, org_uuid, t)
        auth = _authorize(
            envelope, "POST", str(request.url.path), binding, store, t,
            required_scope="link:publish", required_target_type=target_type,
        )

        # I2: the token is pure CSPRNG output — generate_token() takes no
        # inputs, so it cannot be derived from the target.
        token = generate_token()
        store.create_link(
            LinkGrant(
                token=token,
                org_uuid=org_uuid,
                target_uuid=target_uuid,
                target_type=target_type,
                meta=meta,
                created_at=t,
                expires_at=t + link_ttl if link_ttl is not None else None,
                revoked_at=None,
                signer_pub=auth.signer_pub,
                subject_kind=auth.subject_kind,
                subject_id=auth.subject_id,
            )
        )
        return {"token": token, "url": f"{base_url}/l/{token}"}

    @app.delete("/v1/links/{token}")
    async def revoke_link(token: str, request: Request):
        envelope = _parse_envelope(await _read_json(request))
        _require_fields(
            envelope["payload"], allowed=frozenset(), required=frozenset(), what="revoke payload"
        )
        t = now()
        link = store.get_link(token)
        if link is None:
            raise HTTPException(status_code=404, detail="unknown link")
        binding = _require_binding(store, link.org_uuid, t)
        _authorize(
            envelope, "DELETE", str(request.url.path), binding, store, t,
            required_scope="link:revoke",
        )
        store.revoke_link(token, now=t)
        return {"token": token, "revoked_at": t}

    # -- §4.5 revocations --------------------------------------------------------

    @app.post("/v1/revocations", status_code=201)
    async def add_revocation(request: Request):
        # The revocation record is self-authorizing (root- or
        # ancestor-signed), so this endpoint takes the bare record plus the
        # revoked key's cert (the I7 proof of the natural expiry horizon) —
        # no envelope. Anyone may DELIVER a valid record; only the org's
        # own keys can MINT one.
        body = await _read_json(request)
        _require_fields(
            body,
            allowed=frozenset({"org", "record", "revoked_cert"}),
            required=frozenset({"org", "record", "revoked_cert"}),
            what="revocation payload",
        )
        org_uuid = _require_uuid(body["org"], "org")
        if not isinstance(body["record"], str) or not isinstance(body["revoked_cert"], str):
            raise _bad_request("record and revoked_cert must be canonical wire JSON strings")
        t = now()
        binding = _require_binding(store, org_uuid, t)
        try:
            record = RevocationRecord.from_json(body["record"])
            revoked_cert = DelegationCert.from_json(body["revoked_cert"])
        except MalformedError as exc:
            raise _bad_request(str(exc))
        try:
            verify_revocation(record, binding.root_pub, org=org_uuid, revoked_cert=revoked_cert)
        except (RevocationError, ChainVerifyError) as exc:
            raise _forbidden(f"{type(exc).__name__}: {exc}")
        except MalformedError as exc:
            raise _bad_request(str(exc))
        store.add_revocation(org_uuid, record)
        store.purge_expired_revocations(now=t)
        return {"revoked_key_id": record.revoked_key_id, "expires_at": record.expires_at}

    # -- §4.6 grant envelope (bootloader) -------------------------------------

    @app.get("/v1/links/{token}/envelope")
    async def link_envelope(token: str):
        # Anti-enumeration (§5.3): unknown, expired, revoked, and
        # dead-binding tokens are all the SAME 404 — a prober learns
        # nothing about which failure they hit.
        t = now()
        link = store.get_link(token)
        if (
            link is None
            or link.revoked_at is not None
            or (link.expires_at is not None and link.expires_at < t)
        ):
            raise HTTPException(status_code=404, detail="unknown link")
        binding = store.get_org(link.org_uuid)
        if binding is None or binding.expires_at < t:
            raise HTTPException(status_code=404, detail="unknown link")
        return {
            "org": link.org_uuid,
            "target_uuid": link.target_uuid,
            "target_type": link.target_type,
            "meta": link.meta,
            "root_pub": binding.root_pub,
            # Direct-connect upgrade seam (§5.4): empty in v1; the
            # bootloader tries these before relay fallback once populated.
            "endpoints": [],
        }

    # -- §5.1 relay tunnel ------------------------------------------------------

    @app.websocket("/t/{org_uuid}")
    async def relay_tunnel(websocket: WebSocket, org_uuid: str):
        await tunnel_endpoint(websocket, org_uuid, hub, store, now_fn)

    @app.websocket("/v1/links/{token}/channel")
    async def relay_viewer(websocket: WebSocket, token: str):
        await viewer_endpoint(websocket, token, hub, store, now_fn)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    return app
