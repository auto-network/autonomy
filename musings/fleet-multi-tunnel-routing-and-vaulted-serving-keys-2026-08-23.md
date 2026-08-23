# Fleet multi-tunnel routing & vaulted serving keys

*2026-08-23 — design note, arising from the personal-fleet SJC sync bring-up.*

## Why this note exists

Bringing a fresh machine (SJC) into the operator's **personal fleet** and
syncing the personal database surfaced two structural gaps that are really one
architecture:

1. **The serving credential does not survive a process restart.** The fleet
   sync scheduler is armed only by a browser unlock, which mints a fresh
   ephemeral *process key* + machine→process *delegation cert* and pushes it to
   the serving connector **in memory**. Any connector restart — a hot reload of
   a watched dir, a watchdog relaunch, a manual restart to load a fix — drops it
   and demands another unlock. During bring-up this meant a dozen+ unlocks.

2. **The relay allows exactly one tunnel per organization.** A single
   `selected_machine_id` "fleet leader" flag disables serving on every other
   machine so they don't fight over the relay's one slot. That is a stopgap, not
   the model: a real fleet wants every machine reachable, addressed by its own
   machine id, and an org wants multiple tunnels with routing smart enough to
   send a generic-asset request to any tunnel and a specific-machine/persona
   request to the right one.

Both reduce to: **per-machine serving identity should be durable, vaulted, and
first-class in routing** — the way the reachability mesh already treats it.

## What exists today (code-grounded)

There are two independent layers, with very different machine-awareness.

### Discovery + steady-state sync — already machine-native, no relay tunnel

- Each machine self-announces under the **deterministic personal org uuid**
  `personal_org_uuid = uuid5(ns, personal_root_pub)`
  (`tools/network/fleet_runtime.py:41-51`). That uuid *is* the fleet's shared
  rendezvous namespace.
- `node:announce` / `node:lookup` (`tools/network/fleet_reachability.py`) store
  `(org_uuid, machine_pub) → {ws addrs, relay_url}` in the registry's
  `node_hints` table (`tools/network/registry/store.py:218-227`). **The machine
  identity is the envelope signer** — a machine can only announce itself
  (`fleet_reachability.py:9-12`). Authorized by a root→machine_key delegation
  cert scoped `node:announce`/`node:lookup` (`fleet_runtime.py:165-189`).
- `ReachabilityCache` (`fleet_reachability.py:100-181`) refreshes peers on a
  throttle, feeding `peer_addresses` → `{machine_pub: [ws urls]}`
  (`fleet_sync_scheduler.py:79`, wired at
  `fleet_enrollment_routes.py:233-260`).
- Steady-state sync dials **directly, per machine**:
  `fleet_direct_connect(addr, expected_machine_pub=…)`
  (`fleet_sync_channel.py:449-487`), mutually authenticated by `machine_pub`
  (`build_client_hello`/`accept_client`/`verify_server`,
  `fleet_sync_channel.py:265-353`). **This never touches the relay tunnel.**

**So three fleet machines already discover and address each other by
`machine_pub` through the direct reachability mesh.** The relay is not on that
path.

### The relay tunnel — org-only, exactly one per org

- The relay routing table is `TunnelHub = Dict[str, Tunnel]` keyed by org uuid,
  one slot per org (`tools/network/registry/relay.py:480`). `register()`
  overwrites the slot and boots the previous tunnel with close code
  `CLOSE_REPLACED = 4409` (`relay.py:485-489, 752-754`). "One tunnel per org" is
  a **data-structure consequence + last-writer-wins supersede**, not a validated
  invariant — two connectors for one org just fight over the slot.
- The tunnel hello carries `{v, org, signer, ts, cert, sig}`
  (`relaykit/hello.py:31,46-59`) — org + **persona** leaf only, **no
  machine_id/machine_pub**. The relay verifies the serve cert resolves to a
  *canonical organization persona* (`relay.py:558-567`).
- A viewer resolving `/l/<token>` is routed by **org only**:
  `viewer_endpoint` does `hub.get(link.org_uuid)` (`relay.py:846`) and never
  consults the link's `target_uuid`. `lookup()` even **drops** each peer's
  announced `relay_url` (`fleet_reachability.py:90-96`).
- **Bootstrap** rides this tunnel: `FleetRoute{rendezvous, origin_machine_pub}`
  (`tools/network/fleet_route.py:16-19`) points at an exact `/l/<token>` bearer
  link; `pull_checkpoint_once` connects `ViewerChannel.connect(ws, token)`
  (`fleet_relay_sync.py:288-309`) → `hub.get(org)` → whichever single machine
  holds the org tunnel; `origin_machine_pub` is pinned inside the E2E handshake
  (`fleet_relay_sync.py:361`).

**No design doc or stub for multiple tunnels per org exists yet.**

## The gap, stated precisely

- **Discovery/steady-state:** machine-native, N machines, already correct.
- **Relay tunnel:** org-only, one machine, persona-authenticated. It is used for
  (a) **bootstrap** of a machine not yet in the mesh, and (b) **fallback relay**
  for a machine that cannot be direct-dialed. Both cases benefit from — and the
  second *requires* — being able to route to a **specific machine's** tunnel,
  which the relay cannot do today.
- **The serving key** (whatever authenticates a machine's tunnel) is minted per
  unlock and, for the relay serve-cert, historically **kept on disk unvaulted
  because it predates the vault**. The fleet runtime credential is not persisted
  at all.

## Target model

### 1. Serving key material becomes a vaulted, audited setting in **machine.db**

Decision: **machine.db**, not personal.db.

The deciding property is **replication + per-machine identity**, and it only
gets stronger under multi-tunnel:

- A machine's serving/tunnel private key (and its reachability machine key) is
  **per-machine** — the whole reachability mesh, the `expected_machine_pub`
  handshake, and per-machine revocation already key on it. It must **never**
  replicate; otherwise machine B holds machine A's serving key and per-machine
  routing/revocation collapses.
- `machine.db` is node-local and never part of personal-graph replication
  (LOCAL policy, alongside `keycontrol_meta`/`keycontrol_pending` —
  `tools/network/fleet_sync_sim/policies.py`). It is the correct trust boundary.
- The vault is personal-scoped, and that is fine: the personal vault (warm after
  login) supplies the **encryption key**; the resulting ciphertext lives as an
  audited setting in machine.db. **Personal vault encrypts, machine-local
  storage confines.**
- Personal.db would only be justified by an interchangeable *shared* leader key
  — exactly the model multi-tunnel abandons. If ever needed, it would still have
  to sit under an **excluded, non-replicated** set_id (the carve-out that
  already protects `autonomy.identity.personal` / `autonomy.identity.passkey`).

Only the private key is vaulted; public certs may live anywhere.

### 2. Recovery property (the whole point)

One source of truth (the vault), three states:

- **Hot reload:** the existing ramfs hand-off
  (`save_vault_across_hot_reload` / `restore_vault_across_hot_reload`,
  `tools/dashboard/unlock_routes.py:1272-1320`, carrying the delegate signing
  key + persona KEM key today) is **extended to carry the serving key material +
  the fleet runtime credential**, so a reload keeps them alive with no recovery.
- **Cold start (ramfs destroyed):** the material is sealed in the vaulted
  machine.db setting. The **first login warms the personal vault**, which
  decrypts the machine-local serving key into ramfs and re-arms the connector —
  no re-mint unless the cert has genuinely expired (the ~30-day cadence).
- **Full shutdown with ramfs gone stays fully cold** until that first unlock —
  the fail-closed property is preserved (a crash skips the ramfs save and boots
  locked, `unlock_routes.py:1284-1290`).

This unifies the pre-vault on-disk serve-cert (tier 1) and the memory-only fleet
runtime credential (tier 3) onto the same vault-backed, ramfs-warm path.

### 3. Multiple tunnels per org, machine-tagged routing

Bring the relay tunnel up to the machine-awareness the mesh already has. This is
a **registry-side change**, not additive sugar:

- `TunnelHub` keys by **`(org_uuid, machine_pub)`** instead of `org_uuid` —
  multiple live tunnels per org, no supersede between distinct machines
  (`relay.py:480,485-489`). Supersede still applies within one machine's slot
  (reconnect).
- The tunnel hello carries **`machine_pub`** and authenticates with the
  per-machine reachability/machine key (not only the shared org persona)
  (`relaykit/hello.py:31`, verify at `relay.py:558-567`).
- `viewer_endpoint` routing gains a **target selector**: a generic-asset request
  routes to **any** live tunnel for the org; a request naming a specific
  machine (or a persona that resolves to one) routes to that machine's tunnel
  (`relay.py:819-852`). The link's `target_uuid`/`target_type` or an explicit
  machine hint carries the selector.
- `lookup()` **surfaces `relay_url`** (today dropped,
  `fleet_reachability.py:90-96`) so a peer that cannot direct-dial machine X can
  fall back through **X's own** relay tunnel rather than a single leader's.

Under this model `selected_machine_id` stops being a *serving on/off* switch and
becomes at most a *default/primary hint*; every machine can serve its own
tunnel, and bootstrap/fallback can reach a specific machine.

## Three machines, end to end (target)

- All three announce `(personal_org_uuid, machine_pub) → addrs, relay_url` and
  each holds its **own** relay tunnel keyed `(personal_org_uuid, machine_pub)`.
- Steady-state sync is the **direct mesh** (already built): lookup peers by
  machine_pub, `fleet_direct_connect(addr, expected_machine_pub=…)`.
- **Reach a specific machine** = look up its `machine_pub` → direct-dial its
  addrs, or, if undialable, route a relay channel through **its** tunnel via its
  announced `relay_url`.
- **A brand-new joiner** still bootstraps through a tunnel — but now any serving
  machine's tunnel, selected by the invite, not a forced single leader.

## Scope / status

- **Exists:** the entire machine-native discovery + direct-sync mesh; the
  ramfs warm-vault hot-reload hand-off (for delegate + KEM keys); the
  per-machine reachability delegation cert.
- **Net-new:** vaulting the serving key material into machine.db as an audited
  setting; extending the ramfs snapshot + warm-restore to re-arm the connector
  (tiers 1 & 3); registry-side multi-tunnel-per-org keying, machine-tagged
  hello, machine-selective viewer routing, and surfacing `relay_url` from
  lookup.
- **Risk notes:** the serve cert's relay-side identity is a *canonical org
  persona* today; making the tunnel machine-addressable means the relay must
  accept a machine-scoped identity on the serve path — a trust-model change to
  review carefully. The `4409` supersede semantics that make reconnect work must
  be preserved *within* a machine's slot while removed *between* machines.

## Sequencing

The vaulted-credential / ramfs-warm-restore work (tiers 1 & 3) is independent of
the multi-tunnel routing work and lands first — it removes the unlock treadmill
immediately and is a contained dashboard + vault change. The multi-tunnel
registry work is the larger, trust-sensitive piece and follows.
