# TLA+ model — relay tunnel ownership

Formal model of the one-live-tunnel-per-org algorithm implemented by
`registry/relay.py::TunnelHub.register` and `relaykit/connector.py::run`.
The model deliberately permits multiple connector processes for one org.
That is the deployed state which produced the incident; assuming connector
uniqueness would make the model green and useless.

Read `MODEL.md` before interpreting a green result.

## Run

Requires a JRE/JDK and `tla2tools.jar`:

```bash
python3 tools/network/TLA/run_tlc.py
```

The runner requires the stand-down candidate to pass and every negative
configuration to fail with a real TLC violation.

To print the full known-livelock counterexample, run TLC directly:

```bash
java -cp ~/tools/tla2tools.jar tlc2.TLC \
  -deadlock -noGenerateSpecTE -lncheck final \
  -config tools/network/TLA/CurrentLivelock.cfg \
  tools/network/TLA/ScenRelay.tla
```

## Configurations

- `CurrentLivelock.cfg` — shipped behavior; `EventuallyStable` must fail.
- `StandDownStable.cfg` — 4409 is terminal; stability and post-restart
  availability must pass.
- `calibration/HardBackoffLivelock.cfg` — a finite hard delay still cycles.
- `calibration/StandDownNewest.cfg` — stand-down can stabilize on stale code.
- `calibration/RestartHerd.cfg` — random jitter cannot guarantee separation.

The model is evidence for choosing a production change; it does not implement
one.
