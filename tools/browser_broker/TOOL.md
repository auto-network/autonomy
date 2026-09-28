# Browser broker

Lends an agent session a real, visible web browser: one container per lease,
driven through structured commands, with sign-in performed on the caller's
behalf so the caller never sees a credential. Design of record:
graph://c330323d-986; epic `auto-8q7oe`.

## Modules

- `profiles.py` — persistent browser profiles (`auto-0skxh`). A profile's path
  is `<browser_profiles store>/<org>/<workspace>/<name>`, built from the
  caller's authenticated organization and workspace plus a short validated
  name; the caller never supplies a path. Also the profile's container name
  (`brw-p-<16 hex>`, which gives a profile a single owner) and its `/profile`
  mount.

- `lease_agent.py` — the program inside each lease container (`auto-8q7oe.5`).
  Port 7300, `X-Lease-Secret` on every request. `GET /health`, `POST /command`
  (`goto snapshot screenshot click fill type press wait url title text
  download`; no operation runs caller-supplied script), `POST /abort`
  (returns once the command has stopped), `POST /expiry`. Targets are
  `{"ref"}` from `snapshot`, `{"role","name"}`, `{"label"}`, `{"text"}` or
  `{"css"}`. Extends `BrowserController` from `tools/connectors/stealth_repl.py`.
- `lease_watchdog.py` — ends the container at the lease's expiry
  (`BROWSER_LEASE_EXPIRES_AT`, or the newest value from `POST /expiry`) with
  no request from the dashboard.

## Dashboard side (`auto-czoc0`)

- `tools/dashboard/browser_routes.py` — `POST /api/browser/leases`,
  `GET|DELETE /api/browser/leases/{lease}`; session token plus the
  workspace's `browser` capability. Admission: fewer than `max_leases` active
  and `min_free_gib` free, else 503 with the reason; a busy persistent profile
  is 409.
- `tools/dashboard/browser_containers.py` — `docker create/start` with the
  caps (memory = memory-swap, CPUs, pids, 1 GiB shm), `--rm`, restart `no`,
  `no-new-privileges`, the seccomp profile, no published ports, on the
  `autonomy-browser` network (only the dashboard and leases join it).
- `tools/dashboard/browser_reconciler.py` — started at worker activation:
  takes a new epoch (fencing the previous worker's writes), adopts running
  leases, then every 5 s health-checks and releases on the ending events.
- `tools/dashboard/dao/browser_leases.py` — the `browser_leases` table in
  `dashboard.db`; per-lease secret and VNC password AES-GCM-sealed under a key
  derived from `dashboard-session.secret`.
- Limits: the machine Setting `autonomy.browser.defaults`
  (`tools/graph/schemas/browser_defaults.py`).

### Invariants and residual risk (review of auto-czoc0)

- **Leases cannot open connections to the dashboard.** The dashboard joins
  `autonomy-browser` only to dial leases. At activation, a short-lived helper
  sharing the dashboard's network namespace installs
  `INPUT -s <lease subnet> -m conntrack --ctstate NEW -j DROP` there
  (`browser_containers.isolate_dashboard`); leases are refused (503
  `isolation`) until that rule is verified. Defence in depth: every dashboard
  listener is HTTPS with a certificate no lease hostname matches, and a lease
  never ignores certificate errors (pinned in `tests/test_lease_agent.py`).
  Removing the rule, adding a plain-HTTP listener, or ignoring certificate
  errors would expose the dashboard — and on a node whose human gate is not
  yet enforced, operator authority — to web content.
- **Leases reach only the public internet** (`auto-i5okc`). Host rules drop
  every new connection from the lease bridge to 10/8, 172.16/12, 192.168/16,
  100.64/10, 169.254/16 and 127/8 (Docker's `DOCKER-USER` chain), and to the
  node itself (`INPUT`: its LAN address, gateway and published ports).
  Traffic within the bridge (dashboard <-> lease) is untouched, and the
  dashboard's own address on the bridge is exempt: Docker older than 28
  ignores `--gw-priority`, so its default route, and its LAN/NAS traffic, runs
  through this bridge. The rules live in our own chains,
  `AUTONOMY-BROWSER-FWD` and `AUTONOMY-BROWSER-IN`, each reached by exactly
  one `-i <lease bridge>` jump from `DOCKER-USER` and `INPUT`; those shared
  chains (Docker's, tailscaled's) are never rewritten or deleted from by
  position. Each activation rebuilds our chains and verifies them
  (`browser_containers.restrict_egress`), and leases are refused until they
  hold. Why host rules rather than rules in each lease's namespace: they are
  in place before any lease starts, so no page (e.g. a restored tab) can load
  ahead of them. Needs Docker's iptables firewall backend; with nftables,
  leases are refused.

## Lease image

`image/build.sh` builds `autonomy-browser:local` (about 1.3 GB) from a flat
context: branded Google Chrome through patchright, Xvfb, x11vnc on 5900
(password `BROWSER_VNC_PASSWORD`; VNC authentication uses its first 8
characters), tini as PID 1. Launch environment: `BROWSER_LEASE_SECRET`,
`BROWSER_VNC_PASSWORD`, `BROWSER_LEASE_EXPIRES_AT` (Unix seconds), `TZ`,
optionally `BROWSER_SCREEN` (default `1920x1080x24`). A persistent profile is
mounted at `/profile`, owned by uid 1000.

Chrome runs with its sandbox on, which needs
`--security-opt seccomp=tools/browser_broker/image/seccomp-chrome.json`.
That profile is moby's default (`moby/profiles` `seccomp/default.json`,
fetched 2026-09-28, sha256 `785b2429264afba4d594320337cb17f144f3c7d51585f9805eef72e28f4f9334`) plus one final rule allowing
`clone`, `setns` and `unshare` so Chrome can create its user, PID and network
namespaces without `CAP_SYS_ADMIN`. Under Docker's default profile Chrome's
zygote fails ("Failed to move to new namespace").

## Storage

`browser_profiles` (`BROWSER_PROFILES_DIR`, default `data/browser-profiles/`)
is declared in `tools/data_paths.py::STORE_MANIFEST`. Its backup action is
`exclude`: profiles hold site sign-in cookies, and a copy of a running
profile would be torn. A lost profile costs one fresh sign-in.
