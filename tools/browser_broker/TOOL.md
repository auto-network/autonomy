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
