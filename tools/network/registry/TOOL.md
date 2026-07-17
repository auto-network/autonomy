# registry — auto.network registry service v1

FastAPI + SQLite implementation of the auto.network registry (spec
`graph://a17c8657-939` §4): org bindings, share-link grants, rebind
policy, and revocations. Bead `auto-4p7bg` (B1). Builds on idkit
(`tools/network/idkit`, A1) — `cryptography`, `fastapi`, and `uvicorn`
are the only runtime dependencies.

## Authorization model (I4)

The registry holds **no permission tables**. Every mutation arrives in a
signed envelope (`signing.py`) carrying a delegation certificate chain,
and authority is re-derived per request by `idkit.verify_chain` against
the org binding's root public key — signature, org, time validity,
strict scope narrowing, and the org's revocation denylist, on every
call.

```json
{
  "v": 1,
  "signer": "<leaf pub, 64 hex>",
  "ts": 1800000000,
  "payload": { ... },
  "cert": "<canonical DelegationCert wire JSON, chain embedded>",
  "sig": "<128 hex over REQUEST_DOMAIN || canonical_json({v, method, path, ts, signer, payload})>"
}
```

The signature binds the HTTP method and path (no cross-endpoint replay)
and `ts` must sit within ±300s of server time (bounded replay window).
`cert` is parsed with idkit's anti-malleable `from_json` — exactly one
accepted byte form. Omitting `cert` means root-direct: the signer must
*be* the bound root key. Scope requirements (`link:publish`,
`link:revoke`) apply to delegated signers; the root is the authority all
scopes narrow from.

Two anchors sit outside the chain rule by construction:

- **register** — self-signed by the root key claiming the UUID (the
  binding doesn't exist yet; first key claims the name).
- **rebind** — signed by the pre-declared recovery key only. Policy
  `none` has no accept path at all (I3): the request is refused before
  any signature is inspected.

## Endpoints

| Endpoint | Auth | Notes |
|---|---|---|
| `POST /v1/orgs` | self-signed by `root_pub` | first-key-claims-UUID; live collision → 409; expired bindings are reclaimable; TTL clamped to [1h, 30d], default 30d |
| `POST /v1/orgs/{org}/renew` | root-direct or any valid chain | heartbeat; weakest mutation by design — it only extends existing authority |
| `POST /v1/orgs/{org}/rebind` | recovery key, direct signature only | policy `none` → 403 for ANY payload (I3); can rotate or burn the recovery key |
| `POST /v1/links` | chain with `link:publish` | token = 128-bit CSPRNG hex (I2); `meta.require_auth` → `501 rung-2`; grant records cert subject (I6) |
| `DELETE /v1/links/{token}` | chain with `link:revoke` | soft-revoke; envelope 404s afterwards |
| `POST /v1/revocations` | the record itself (root-/ancestor-signed) | body: `{org, record, revoked_cert}` wire strings; `revoked_cert` proves the I7 retention horizon |
| `GET /v1/links/{token}/envelope` | none (bootloader) | unknown/expired/revoked/dead-binding → one indistinguishable 404 (anti-enumeration); `endpoints: []` is the §5.4 direct-connect seam |
| `GET /healthz` | none | systemd/Caddy probe |
| `WS /t/{org}` | `tunnel:serve` hello (chain to bound root) | §5.1 relay tunnel — one outbound dashboard connection per org; see `tools/network/relaykit/TOOL.md` |
| `WS /v1/links/{token}/channel` | none (bootloader) | viewer end of the relay; every failure closes `4404` (anti-enumeration) |
| `GET /l/{token}` | none (bootloader shell) | §5.3 — one static page for every token; identical bytes, status mirrors envelope liveness; no identifiers before the channel is up; see `bootloader/README.md` |
| `GET /l-assets/autonet.js` | none | the WebCrypto channel client (viewer side of B2's handshake) |

`subject.kind == "persona"` on any chain → `501 rung-2` (viewer authn is
Track E). Revocation records are retained only until the revoked key's
natural expiry and purged lazily on every mutation (I7).

## Running

```bash
python -m tools.network.registry --db registry.db --port 8477
```

Tests (invariant-pinned: I2, I3, I4, I6, I7, anti-enumeration, replay):

```bash
pytest tools/network/registry/tests/
```

## Deploy

Target is the **scripted estate** (`graph://8cb2a39c-4bc`) — never the
legacy pet auto-ash-1; `deploy/deploy.sh` refuses it by name and IP.

```bash
tools/network/registry/deploy/deploy.sh root@<estate-host>
```

The unit (`deploy/autonomy-registry.service`) runs hardened
(DynamicUser, ProtectSystem=strict), binds loopback :8477, and expects
the estate's Caddy front to terminate TLS for `auto.network`.
