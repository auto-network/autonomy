# Multi-node production-path harness

This is the headless acceptance ladder for the portable node image:

```bash
python3 -m deploy.harness
```

It builds the image from this checkout by default, starts a real registry and
relay plus isolated node containers, and runs six labelled phases:

1. start the isolated topology;
2. found a throwaway organization on A and open its serving connector;
3. join B through the production `ViewerChannel`;
4. merge two independently signed approvals and let B finalize on restart;
5. quiesce A, snapshot its volume, and restore into fresh C;
6. prove C has A's cryptographic identity and serving delegate, then fetch
   A's note from C through the real relay while A remains stopped.

Each counted node owns a named volume and a unique loopback dashboard port.
The restored C is an additional service: after A stops, B + C + peers 3..N
still leaves `N` dashboards running side by side for a visible demo. No host
data directory or Docker socket is mounted. Every persistent store is
explicitly rooted below `/app/data` and the real-data fallback guard is on.

The generated Compose file contains only `${AUTONOMY_HARNESS_INVITE_B:-}`;
the invitation value is supplied to the single `up node-b` subprocess.

**TEST AUTOMATION ONLY:** the harness's synthetic personal password crosses
stdin into a mode-0600 file in B's isolated named volume. Its input is named
`AUTONOMY_TEST_PERSONAL_PASSWORD_FILE` and is accepted only while both
`AUTONOMY_TEST_AUTOMATION=1` and
`AUTONOMY_REFUSE_REAL_DATA_FALLBACK=1` are set. Production installation and
browser invitation flows do not support a mounted personal-passphrase file.
Neither test credential is written to the Compose file or preserved logs.

## CI requirement

The real ladder requires a Linux Docker host with the Compose plugin. Unit
tests validate topology isolation, command sequencing, secret handling,
teardown scoping, the fixture's real ledger/identity records, and the explicit
sync extension-point refusals without pretending that those are the Docker
acceptance. The release CI job must run the command above; a machine without
Docker fails immediately and clearly.

Useful options:

```bash
python3 -m deploy.harness --nodes 5
python3 -m deploy.harness --no-build --image autonomy-dashboard@sha256:...
python3 -m deploy.harness --project autonomy-harness-demo
```

`--no-build --image ...` supports the verified-published B2 path. Building
from source remains the default and requires no image registry.

The `Harness` object exposes the six phase methods individually. A later
demo-day presentation layer pauses between them and opens the loopback
dashboard ports without replacing any acceptance logic.

## Visible demo-day presentation

The presentation command accepts only the verified-published path:

```bash
python3 -m deploy.demo \
  --image registry.example/autonomy/autonomy-node@sha256:<digest> \
  --cosign-public-key deploy/cosign.pub \
  --recording-path harness-artifacts/demo-day.mp4
```

It runs `deploy/verify-image.sh` before starting Docker, enables the existing
node TLS initializer, pins each host-side HTTPS probe to that node's captured
self-signed certificate, and drives these same phase methods with manual
narration pauses. The local certificate is real self-signed HTTPS, not public
CA endorsement; accept its browser warning deliberately. Node graph and
Design Studio tabs demonstrate tools inside the sovereign node. The separate
note link is the fixture's real relay-published content; the demo does not
claim that Design Studio itself is relay-published.

The command opens browser tabs by default. Host-specific launchers are
parameterized (`--browser-command 'open {url}'` on macOS or
`--browser-command 'xdg-open {url}'` on Linux), as are the Docker and Compose
commands and loopback ports. `--pace auto` and `--pace none` support rehearsals.

Screen capture remains an operator-side act on a real Docker host. The command
never fabricates a recording; it writes `demo-transcript.md` and
`demo-urls.json` beneath the run artifact directory and records the requested
recording destination in that manifest.

## Deliberate fixture boundary

Until the registry-side D21 contract lands, fixture setup inserts the
throwaway org UUID binding and two registry grant rows directly. It also
establishes the corresponding local grant cache and throwaway roots. This is
an explicit extension point, not a simulated registration claim. The actual
join, countersign/finalize, tunnel reconnect, and restored content fetch all
run through production code and real services.

`partition()`, `heal()`, and `assert_converged()` deliberately raise
`NotImplementedError`. The sync sprint replaces those refusals with real
network controls and convergence assertions; this harness never reports a
fake sync success.

## Production TURN acceptance

The TURN acceptance is a separate command because it writes short-lived test
organizations to the selected Registry, while the admission/portability
ladder above owns an entirely local Registry:

```bash
python3 -m deploy.harness.production_turn \
  --registry-url https://registry.auto.network \
  --relay-url wss://relay.auto.network
```

Both endpoints are required; the runner has no implicit production host. It
creates two data-root-isolated serving nodes, registers and publishes through
the normal signed Registry API, and marks only the nodes' local Mission grants
`relay_only`. Each serving tunnel uses the WSS form of the Registry URL; guest
link channels use the separately supplied Relay URL. In both directions it
requires a selected relay/relay candidate
pair, fetches a Mission document larger than one record, receives a sealed
Mission feed event, and makes another request afterward. It then opens a fresh
TURN-backed channel before closing the first and proves the replacement still
serves after the old channel closes.

Link tokens, org roots, serving keys, TURN coupons, and feed keys remain in
temporary mode-0600 state. By default the runner revokes both links and deletes
that state; two successful revocations, stopped connector processes, and
deleted secret roots are required before the run records a pass.
`--keep-state` is only for bounded diagnosis. The durable
`evidence.json` contains candidate types, artifact sizes and hashes, feed and
renewal results, and no bearer material. The local SSE source is the sole
stand-in: it replaces the Dashboard's `/api/events` emitter, while Registry,
relay, TURN, connector, encryption, Mission rendering, and feed delivery all
use production implementations.
