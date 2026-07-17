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
| `POST /v1/orgs/{org}/topics/{topic}/heads` | chain with `topic:<topic>` | F3 notification plane: announce ledger DAG heads (32-byte hints only) |
| `POST /v1/orgs/{org}/topics/{topic}/heads/poll` | chain with `topic:<topic>` | cursor-based hint fanout (`since` seq) + latest announcement |
| `POST /v1/orgs/{org}/topics/{topic}/bundles` | chain with `topic:<topic>` | F3 data plane: store-and-forward mailbox of ENCRYPTED event bundles — the broker stores topic + hashes + sizes + opaque ciphertext, nothing else (L6) |
| `POST /v1/orgs/{org}/topics/{topic}/bundles/fetch` | chain with `topic:<topic>` | by cursor (`since`), by hash (`want` — fetch-missing-by-hash), or `meta_only` manifests for anti-entropy planning |
| `POST /v1/orgs/{org}/reachability` | chain with `node:announce` | G1 fabric: self-announce direct-dial candidates + optional peer-relay URL; row keyed by the envelope SIGNER (a node can only announce itself); TTL-leased, stale rows expire |
| `POST /v1/orgs/{org}/reachability/query` | chain with `node:lookup` | live hints for the org's nodes; Tier B by construction — anonymous callers never see interior addresses |
| `POST /v1/orgs/{org}/topics/{topic}/witness` | chain with `topic:<topic>` | F4 equivocation witness: append the observed head-set to the per-topic hash-chained log; returns the tip **signed by the registry witness key** (idempotent on an unchanged head-set) |
| `POST /v1/orgs/{org}/topics/{topic}/witness/head` | chain with `topic:<topic>` | serve the current signed head-set — identical to every member |
| `POST /v1/orgs/{org}/topics/{topic}/witness/since` | chain with `topic:<topic>` | signed chain entries after `since` (the client's continuity walk) |
| `GET /v1/witness/pubkey` | none | the registry's pinnable witness verification key — public by nature; the pin is what makes split-view detection provable |
| `GET /healthz` | none | systemd/Caddy probe |
| `WS /t/{org}` | `tunnel:serve` hello (chain to bound root) | §5.1 relay tunnel — one outbound dashboard connection per org; see `tools/network/relaykit/TOOL.md` |
| `WS /v1/links/{token}/channel` | none (bootloader) | viewer end of the relay; every failure closes `4404` (anti-enumeration) |
| `GET /l/{token}` | none (bootloader shell) | §5.3 — one static page for every token; identical bytes, status mirrors envelope liveness; no identifiers before the channel is up; see `bootloader/README.md` |
| `GET /l-assets/autonet.js` | none | the WebCrypto channel client (viewer side of B2's handshake) |

`subject.kind == "persona"` on any chain → `501 rung-2` (viewer authn is
Track E). Revocation records are retained only until the revoked key's
natural expiry and purged lazily on every mutation (I7).

## Ledger-sync topics (F3, spec §6–7)

Per-org topics are the broker path for the org authority ledger
(`tools/network/ledger/` — sync/bundles/broker modules; bead
`auto-rrzrt`). The registry acts as **T0-blind pub/sub + mailbox**:
"a well-known peer that is always awake". Every topic call — publish
*and* subscribe (Tier B) — passes the I4 gate; delegated signers need
the exact per-topic scope `topic:<name>`, so a key granted only content
topics cannot touch the mandatory `authority` topic, and vice versa.
Bundle ciphertext is sealed client-side under the org sync key
(HKDF of the genesis wire — see `ledger/bundles.py`) with AAD binding
org + topic + hash manifest: the broker can deny service but can never
read, forge, or cross-topic-splice a bundle. L6 is pinned by a disk-scan
test (`tests/test_broker_sync.py`).

Two v1 boundaries, deliberate: **cert scopes are exact strings** (idkit
chains have no wildcard semantics — mint one `topic:<name>` entry per
topic; ledger scope *patterns* like `topic:*` apply to ledger events,
not to registry certs), and **no mailbox retention/quota yet** — spec
§13 Q5 ties defaults to the first real Tier-C threshold; until then the
mailbox grows monotonically (identical re-announcements are deduped
server-side, so quiet-org heartbeats cost nothing).

## Equivocation witness (F4, spec §6 role 2, L5)

The registry's **T1-neutral anti-fork role** (`witness.py` + the
`witness_log` store table; client verifier in `ledger/witness.py`, bead
`auto-12jah`). It keeps a per-`(org, topic)` **append-only, hash-chained**
log of published head-sets and serves each tip **signed by the registry
witness key** — Certificate-Transparency split-view detection reduced to
hashes, so it composes with the L6 encrypted mailbox untouched (the
witness never sees an event, only its id).

- **Provable equivocation.** Every served tip is non-repudiably signed and
  carries its chain position (`seq` + `prev`). A server that shows two
  members different histories signs two contradictory attestations; the
  pair is a self-contained proof anyone re-checks against the pinned key
  (`split-seq` — two entries at one seq; `fork-prev` — a `seq N+1` entry
  rooted on a different `seq N` entry than the member holds). The witness
  key is **pinned** (`GET /v1/witness/pubkey`, TOFU/out-of-band); a proof
  only counts when both halves verify under the same trusted key, so a
  split server cannot escape by signing each half under its own key.
- **Append-only, two layers.** Structural: the store only appends, `seq`
  is monotonic, and each entry commits to the previous by content address
  — a rewrite breaks the chain members already hold signed. Semantic
  (client-side, DAG-aware): a fresh head-set must *supersede* the last
  (`ancestry(new) ⊇ old`); a head-set that drops a non-superseded head is
  a rejected retraction; a descendant head passes.
- **Witnessed-head query (F7 seam).** `ledger.witness.witnessed_fold` folds
  "state as of witnessed head H" deterministically, and only against a
  witness-signed head-set — a forged branch was never witnessed, which is
  the L7 circularity defense for org-root recovery.
- **Blindness (L5).** The `witness_log` row is hashes, a publisher pubkey,
  and a timestamp — pinned by a disk-scan test (`tests/test_witness.py`).

A stable deployment MUST pass a persistent `witness_key` to `create_app`;
rotating it silently breaks pins and hence detection.

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
