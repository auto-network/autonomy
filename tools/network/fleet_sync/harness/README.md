# Fleet sync scenario harness

Builds a local fleet of N real scheduler processes (independent databases,
fixture roster, real WebSocket transport) with every inter-machine dial
routed through a per-direction userspace TCP proxy whose faults a scenario
scripts at runtime. Runs fully locally, unprivileged, under agent-test.

## Shape

```python
from tools.network.fleet_sync.harness import HarnessFleet, Step

fleet = HarnessFleet(tmp_path / "fleet", size=3).build()
fleet.start_all()
try:
    fleet.run_timeline([
        Step(0.0, lambda f: f.write(0, "n1", "from zero"), "write"),
        Step(0.2, lambda f: f.partition(0, 2), "partition 0|2"),
        Step(1.0, lambda f: f.heal(0, 2), "heal"),
    ])
    fleet.wait_converged(timeout=20.0)
    fleet.write_evidence(tmp_path / "evidence.json")
finally:
    fleet.shutdown()
```

A `Step` is `(at_seconds_from_timeline_start, action(fleet), label)`. Actions
are ordinary methods: `write`, `partition`/`heal`, `set_link_faults(dialer,
target, ...)`, `set_pair_faults`, `restart(i, kill=True)`, `stop`/`start`.
Assertions poll through `wait`/`wait_converged` (byte-identical `sources`
digest across all machines) and everything lands in one evidence JSON.

## Fault vocabulary (`LinkFaults`)

`latency_s` + `jitter_s` per chunk; `bandwidth_bytes_per_s` cap;
`stall_rate`/`stall_s` — probabilistic retransmit-like delay;
`reset_rate` — probabilistic mid-stream connection kill;
`partitioned` — refuse new dials and abort live connections.

Honesty note on loss: a userspace TCP proxy cannot un-ACK bytes, so packet
loss is not emulated by discarding stream bytes (that would corrupt the
WebSocket framing, which real loss never does). What applications observe
under loss — retransmission delay, then dead connections — is injected
directly via `stall_rate` and `reset_rate`. Machine-level flap is
`restart(i, kill=True)` in a timeline loop.

## Boundaries

The worker process is the two-process probe's `--worker` mode
(`process_dashboard_sync.py`) — direct-path sync only, delta-only
bootstrap (all machines enroll before first writes). Checkpoint-over-direct
arrives with bead auto-bo6qr. Seed scenarios live in
`tests/test_harness_scenarios.py`; the adversarial matrix is bead
auto-doio1.
