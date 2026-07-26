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
the invitation value is supplied to the single `up node-b` subprocess. The
personal password crosses stdin into a mode-0600 file in B's named volume.
Neither credential is written to the Compose file or preserved logs.

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
demo-day presentation layer can pause between them and open the loopback
dashboard ports without replacing any acceptance logic.

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
