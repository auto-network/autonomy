# Fleet member status screen + status API — the join/sync health dashboard

*2026-08-23 — design spec, to build after the first personal-fleet sync is
proven. Companion to the failure catalog in
`musings/fleet-new-machine-lifecycle-sequence-2026-08-23.md`: every failure mode
in that catalog becomes one live status flag here.*

## The problem this fixes

A machine booted with a fleet invite that is **enrolled-but-not-synchronizing**
(or freshly rebooted so its in-memory runtime credential is gone) is in a broken
state that the current UI actively hides:

- The operator opens the dashboard on that machine. Because the browser still
  holds a **valid session cookie** from an earlier attempt, the dashboard decides
  "already signed in" and drops them on the **"Set up your assistant"** page
  (the *Set up Claude / Set up Codex* buttons).
- That page is **wrong and useless here**: the machine is a fleet member whose
  sync is down and which needs a **fresh unlock to re-mint keys** — not an
  assistant to configure.
- Today the only way to learn *why* sync is down is to SSH in and tail logs /
  grep files. Over two days of bring-up, the real state was always knowable — it
  was spread across a dozen probes (roster, serve-cert, connector-status, relay
  envelope, catalog integrity, the 4404-overload) that no human or remote agent
  could see from the UI.

**The deeper routing bug — "accepted" is mistaken for "done."** The moment the
invitation is *accepted*, the system assumes onboarding is complete and hands
the operator the assistant-setup page — but acceptance is only step 1 of five.
The machine is not usable until its **personal database has actually
synchronized**. Between acceptance and a finished first sync (which can be
**many gigabytes** and take a long time) the operator has no honest view: even
after re-logging in a fresh incognito window (the only way in, because the
stale cookie path is broken), they land on *install Claude / install Codex* —
useless, because there is nothing to configure until sync lands. **The status
screen must persist for the entire onboarding lifecycle — from
invitation-in-progress until the first sync completes and the operator can log
directly into a working dashboard — never dismissed early on mere acceptance.**

**What's needed:** for a machine **in the invitation / joining / member state**,
the homepage is a **fleet status screen** — a catalogue of every check we learned
to run, as human-readable status flags — backed by a **JSON status API** that a
**remote configuring agent reads programmatically, with no SSH and no log
tailing.** Fast, complete, machine- and human-consumable.

## Three deliverables

1. **`GET /api/fleet/status` → JSON** — the single source of truth. Every check
   below as a structured field. This is what a remote agent (or `fleet_doctor`,
   or the screen) reads. No SSH, no log-grep — every probe already exists
   server-side; this endpoint just runs and returns them.
2. **The status screen** — renders that JSON as a list of **status flags**, each
   a one-line label + state chip, **expandable** to a plain-language explanation
   of what the status means and what to do about it. Never a wall of text.
3. **Routing** — a machine in the joining/member-degraded state lands here, not
   on assistant-setup (see "When this screen shows").

## The onboarding view: five steps

The **default view for a machine mid-join** is not the raw flag catalogue — it
is a **five-step linear checklist**, the operator's honest picture of "how far
along is this machine becoming a working fleet member." Each step is one line
with a big state chip (done / active / pending / failed) and expands to the
underlying flags from the catalogue below. The steps are strictly ordered; the
screen highlights the first non-`done` step as the current one. The exhaustive
flag catalogue is the *detail* underneath — this checklist is the summary a
human reads in two seconds.

| # | Step (operator-facing) | Means | Backed by flags |
|---|---|---|---|
| 1 | **Invitation accepted** | The operator on the home fleet approved this machine's join and its personal armor was delivered. | `enroll.admission`, `enroll.armor_delivered` |
| 2 | **Personal armor unlocked** | The delivered armor was unlocked on this machine — the personal root decrypted, the machine key derived, the vault warm. | `enroll.completed`, `vault.warm`, `cred.runtime_present`/`cred.runtime_valid` |
| 3 | **Fleet connection established (roster)** | This machine is a signed member of the root-signed roster and can resolve its peers. *(This is where two days of bring-up got stuck.)* | `enroll.completed` (roster `machine_id`), `reach.announced`, `reach.peers_resolved` |
| 4 | **Secure tunnel created** | A live path up through the relay into the parent fleet's serving node — the channel dials, is admitted, and stays open. | `relay.reachable`, `relay.link_live`, `relay.hub_tunnel`, `sync.last_error` clean |
| 5 | **Personal database synchronizing** | The first full checkpoint is transferring and installing. **Shows a progress bar** (see below). Done when the frontier reaches the origin's and the catalog installs. | `sync.transfer` (bytes/%), `sync.frontier`, `catalog.integrity` |

Steps 1–2 are the **prerequisites**; 3–4 are **establishing the path**; 5 is
the **payload**. Only when step 5 reaches `done` — first sync complete, member
fully live — does onboarding close out and the operator get the normal
dashboard. Until then, this is the homepage.

### Step 5 progress — a machine-db sync-progress setting

Step 5 must show a **progress bar**, and for a multi-gigabyte personal database
that bar has to be fed by something the sync path writes as it goes. Add a
**graph Setting in the machine database** (local, `machine.db` — never
replicated) that the fleet sync scheduler updates as the checkpoint streams and
materializes:

```json
// autonomy.fleet.sync_progress  (machine.db, keyed by origin org_uuid + machine_id)
{
  "phase": "transfer",              // idle | transfer | materialize | done | failed
  "bytes_total": 601931776,          // checkpoint size when known (574 MiB here)
  "bytes_transferred": 247463936,
  "records_total": 812443,           // catalog total_records
  "records_applied": 318902,
  "started_at": 1755975000,
  "updated_at": 1755975041,
  "last_error": null                 // distinct cause on failure (throttled / fk_orphan / …)
}
```

- The scheduler/relay-sync writes `bytes_transferred` during the stream and
  `records_applied` during materialize; the screen renders
  `bytes_transferred/bytes_total` then `records_applied/records_total`, and the
  bar survives a browser refresh because it reads persisted state, not a live
  socket.
- `GET /api/fleet/status` returns this object verbatim under the `sync.transfer`
  flag's `evidence`, so a **remote agent watches a multi-GB first sync advance
  with no SSH** — the same observability the operator gets, programmatically.
- It lives in `machine.db` precisely because it is **per-machine observation of
  this machine's own download**, not fleet-shared truth; it must not ride the
  replicated personal graph.

## Status-flag model

Each flag is one object, identical in the API and the screen:

```json
{
  "id": "serving.tunnel_registered",
  "label": "Serving tunnel registered on the relay",
  "state": "fail",                     // ok | warn | fail | pending | n/a
  "summary": "Home is configured to serve but no tunnel is registered on the relay for this org.",
  "detail": "The connector process is alive and holds a valid serve-cert, but the relay's TunnelHub has no live tunnel for personal_org_uuid 02d833fd… — its tunnel-serve WS is down or was replaced (4409). A viewer only routes if the RELAY has the tunnel; the local 'serving' flag is not the same fact.",
  "remediation": "Restart/kick the duplicate connector; ensure exactly one serving connector for this org.",
  "evidence": { "hub_has_tunnel": false, "local_serving": true, "org_uuid": "02d833fd…" }
}
```

- **`state`**: `ok` (green), `warn` (amber — degraded but working), `fail` (red —
  broken), `pending` (in progress, e.g. awaiting admission), `n/a` (not
  applicable to this machine's role).
- **`summary`** is the collapsed one-liner a human reads. **`detail`** is the
  expandable explanation (what it means + why it matters). **`remediation`** is
  the fix. **`evidence`** is the raw values the flag was computed from (for the
  remote agent / deep dive).
- The screen groups flags by the **phases** below and shows a single top-line
  verdict (the worst state, plus a headline like *"Needs unlock to re-mint keys"*
  or *"Synchronizing — 41% of first checkpoint"*).

## The check catalogue (grounded in the failure catalog)

Every one of these is a `fail`/`warn` we actually hit. Group = screen section.
"Probe" = the existing server-side source, so none of this needs SSH.

### A. Enrollment / identity
| Flag | Probe | Fail means |
|---|---|---|
| `enroll.invite_present` | `machine.fleet-joining` marker; `AUTONOMY_FLEET_INVITE` | booted with an invite but no marker written |
| `enroll.request_submitted` | `fleet_enrollment_join_state` (recovery) | request never reached the origin |
| `enroll.sas` | pending row `verification_code` | show the SAS to compare (pending state) |
| `enroll.admission` | approval decision status | awaiting operator approval on home |
| `enroll.armor_delivered` | `autonomy.identity.personal` present | approved but armor not delivered |
| `enroll.completed` | `autonomy.machine.identity` (`machine_id`) | delivered but completion (unlock) not done → **not yet a member** |

### B. Vault / credential  ← *the "you need to log in again" state*
| Flag | Probe | Fail means |
|---|---|---|
| `vault.warm` | vault open state | **cold — needs unlock** (the exact state that today mis-routes to assistant-setup) |
| `cred.runtime_present` | `dashboard_relay_sync_service` / scheduler config | no fleet runtime credential — **unlock to mint one** |
| `cred.runtime_valid` | cert `not_after` vs now; TTL bound | expired (the 12h→30d issue) or exceeds this machine's TTL ceiling (version skew) |
| `cred.days_remaining` | `not_after - now` | warn under the renew threshold |
| `cred.persisted` | (future `auto-oj5pt`) | memory-only → dies on restart |
| `cred.reachability` | reachability cert present | discovery disabled |

### C. Serving (this machine, if designated tunnel server)
| Flag | Probe | Fail means |
|---|---|---|
| `serving.designated` | `fleet_tunnel_server.state()` | not this machine's job (`n/a`) or unassigned |
| `serving.serve_cert` | `serve_cert_state(org)` + `days_remaining` | missing/expired/key-missing |
| `serving.gate` | `_has_live_grant(org)` OR fleet-has-members | not serving (no live grant and no members) |
| `serving.connector_running` | supervisor `serving()` / control socket | connector dead/wedged |
| `serving.runtime_configured` | connector-status `fleet_runtime_configured` | scheduler `None` → "locked for Fleet sync" (needs unlock/arming) |
| `serving.tunnel_registered` | **relay hub**, not the local flag | local-serving true but **relay hub has no tunnel** (the WS dropped / 4409 flap) |

### D. Reachability / routing
| Flag | Probe | Fail means |
|---|---|---|
| `reach.announced` | `node:announce` under `personal_org_uuid` | this machine not discoverable |
| `reach.peers_resolved` | `ReachabilityCache.peers()` | can't find roster peers by `machine_pub` |
| `route.mode` | FleetRoute vs `peer_addresses` in use | still on the **transient invite link** vs the stable direct-dial mesh |
| `relay.reachable` | envelope `GET /l/<token>/envelope` | relay down/unreachable |
| `relay.link_live` | envelope 200 (grant not revoked/expired) | grant expired or explicitly revoked |

### E. Sync / pull
| Flag | Probe | Fail means |
|---|---|---|
| `sync.last_pull` | `fleet_sync_peer_state.last_success_ns` | never synced / stale frontier |
| `sync.last_error` | last pull outcome + **distinct** close code | surfaces the real cause (not an overloaded 4404): `throttled` (byte cap), `no_tunnel`, `unknown_link`, `tls`, `handshake` |
| `sync.frontier` | earned watermark vs peers | how far behind |
| `sync.transfer` | bytes / % of first checkpoint (machine-db `sync_progress` setting) | in-progress checkpoint (e.g. throttled at N MB of 574 MiB) |
| `sync.install` | last `install_checkpoint`/materialize outcome | the transfer landed but **materialize aborted** — e.g. an **FK-orphan** (a `thoughts` row whose `sources` parent was deleted long ago without cascade — hit live: 16.6k orphans on home's personal.db) failing the whole checkpoint. A robust receiver **skips/quarantines** the offending rows and advances the frontier rather than aborting a multi-GB sync over origin-side referential debris. |

### F. Checkpoint / catalog (server side of a pull)
| Flag | Probe | Fail means |
|---|---|---|
| `catalog.activated` | trigger presence | capture not active |
| `catalog.integrity` | `tracked_live == base.total_records`; deprecated orphans; dups | the AlphaError family (dedup, orphan, RETURNING gap) |

### G. Relay-side (queryable from the member, no SSH)
| Flag | Probe | Fail means |
|---|---|---|
| `relay.hub_tunnel` | relay reports a tunnel for this org | the definitive "is home actually serving" answer |
| `relay.byte_budget` | (after the fleet:join exemption) | a bulk checkpoint exceeding the anti-abuse buckets |

### H. System / startup
| Flag | Probe | Fail means |
|---|---|---|
| `sys.no_stub_dbs` | no empty `orgs/<local-store>.db` | the migration-crash stub class |
| `sys.version` | running build vs peers | version skew (e.g. the 12h-vs-30d TTL ceiling) |

## When this screen shows (routing)

The homepage router must distinguish **role**, not just "logged in":

- Machine has a **fleet-joining marker** or a **roster identity** (`machine_id`)
  → this is a **fleet member**. Its homepage is the **fleet status screen**,
  regardless of an existing session cookie. Never assistant-setup.
- **Acceptance is not completion.** The screen persists from
  invitation-in-progress until **step 5 (first sync) reaches `done`** — not when
  the invitation is merely accepted. The current bug is exactly this: the router
  treats "invitation accepted" as "onboarding complete" and hands over
  assistant-setup while the personal database has not yet transferred. The gate
  to the normal dashboard is *first-sync-complete*, tracked by the machine-db
  sync-progress setting's `phase == "done"`.
- **The re-login path must land here too.** Because the stale-cookie path is
  broken, the operator's real way back in is a fresh (incognito) login — and
  today that *still* drops them on assistant-setup because they are "part of the
  fleet." Post-login role resolution must route a not-yet-synced member to this
  screen. If `vault.warm`/`cred.runtime_present` is `fail`, the headline is an
  explicit **"Unlock to re-mint sync keys"** call to action.
- A genuinely fresh, non-fleet machine keeps the assistant-setup homepage.

So the fix has two halves: (1) **detect the member/joining role AND whether
first sync is complete**, and route to the status screen until it is; (2)
**build the screen + API**. Half (1) alone already ends the "useless homepage on
a broken node" trap.

## Build notes / reuse

- **`fleet_doctor` already implements most probes** (serve-cert, live-grant,
  connector-status, catalog canary, `--verify-catalog`, `--ssh`). `GET
  /api/fleet/status` should be `fleet_doctor`'s checks refactored into a
  library the route calls — one implementation, three consumers (CLI, API,
  screen).
- Existing endpoints to fold in: `/api/fleet/enrollment/local-sync-status`,
  connector-status control op, `serve_cert_state`, `fleet_tunnel_server.state()`,
  the relay envelope fetch, the fleet plugin projection
  (`plugins/fleet/entrypoints/projection.py`).
- The **relay-side** flags (`relay.hub_tunnel`, `relay.byte_budget`) need the
  relay to expose a small **authenticated status endpoint** (does the hub hold a
  tunnel for org X; what are the current byte budgets) so a member can ask
  without the operator SSH-ing the registry. Depends on the distinct-close-code
  work (so `sync.last_error` can name the cause).
- **Anti-enumeration:** the member queries its **own** org's relay status with
  its roster credential; keep the public/unauthenticated relay surface uniform.

## Acceptance

- A rebooted, unsynced member opens the dashboard → lands on the status screen
  with a red headline "Unlock to re-mint sync keys" and a green/amber/red flag
  for every row above — **not** the assistant-setup page.
- A machine mid-join shows the **five-step checklist** with the current step
  highlighted; the screen **persists until step 5 (first sync) is `done`**, and
  a fresh (incognito) re-login during onboarding returns to it, never to
  assistant-setup.
- Step 5 shows a **progress bar** driven by the machine-db `sync_progress`
  setting; a **multi-gigabyte** first sync is observable advancing, both in the
  UI and via `GET /api/fleet/status` (so a remote agent watches it too).
- `curl -s /api/fleet/status | jq` returns the same flags; a remote agent
  diagnoses and remediates the machine **without a single SSH or log tail**.
- Every failure we hit over the two-day bring-up maps to exactly one flag whose
  `detail` explains it in plain language.
