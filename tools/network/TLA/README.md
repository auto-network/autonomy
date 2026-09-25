# TLA+ model — relay tunnel pools

Formal model of the intended relay algorithm: every authenticated outbound
tunnel for an org joins one cooperative pool. New viewers are assigned to a
least-loaded member with capacity and remain pinned to it. A tunnel disconnect
removes that exact member and reopens only its viewers.

The connector retry model separates authenticated registration from useful
service. A hello never resets backoff; one stable service interval does.

The pool is logical, not process-local. `AnycastPoolGreen.cfg` places the
viewer and the only usable tunnel on different relay nodes. This captures the
required outcome behind `relay.auto.network`; it intentionally abstracts the
directory or internal handoff needed to implement that outcome.

Read `MODEL.md` before interpreting a green result. `PERFORMANCE.md` measures
the current Python relay, and `TRANSPORT_DIRECTION.md` records the boundaries
for anycast, RaptorQ, and ICE/STUN/TURN.

## Run

Requires a JRE/JDK and `tla2tools.jar`:

```bash
python3 tools/network/TLA/run_tlc.py
```

The runner requires both intended configurations to pass and every negative
configuration to fail its exact named property.

To print the full known replacement-livelock counterexample, run TLC directly:

```bash
java -cp ~/tools/tla2tools.jar tlc2.TLC \
  -deadlock -noGenerateSpecTE -lncheck final \
  -config tools/network/TLA/CurrentLivelock.cfg \
  tools/network/TLA/ScenRelay.tla
```

## Configurations

- `PoolGreen.cfg` — three same-org tunnels coexist; four viewers shed across
  them; one tunnel disconnect and one relay restart recover.
- `AnycastPoolGreen.cfg` — a viewer entering relay `r2` reaches the org's only
  tunnel on relay `r1` through the logical pool.
- `CurrentLivelock.cfg` — shipped singular last-writer replacement; must fail
  `EventuallyAllTunnelsRegistered` with the known retry lasso.
- `calibration/RandomAdmission.cfg` — pool membership without least-loaded
  admission; must fail `AdmissionUsesLeastLoad` when it creates avoidable skew.

The model records the intended algorithm and calibrates it against the current
defect. It does not implement the production change or choose a distributed
directory.

## Org admission (`OrgAdmission.tla`, bead auto-qrmlg.5)

Admission -> checkpoint -> adoption -> org sync pull, with two joiners, the
founder as the only checkpointer, and an untrusted registry. See MODEL.md.

- `OrgAdmissionCurrent.cfg` — the built rules; must fail
  `EveryAdmittedMemberPulls` with the adoption deadlock.
- `OrgAdmissionProposed.cfg`, `OrgAdmissionProposedApproval.cfg` —
  adopt-by-verification + checkpoint-at-admission (+ admit-on-approval);
  clean, with no later founder or joiner ceremony assumed.
- `OrgAdmissionProposedForge.cfg`, `OrgAdmissionCurrentForge.cfg` — a
  forging registry; safety holds under both rule sets.
- `OrgAdmissionAdoptOnly.cfg` — adopt-by-verification alone, founder signs
  on again; clean.
- `calibration/CheckpointAtAdmissionOnly.cfg`, `AdoptOnlyNoSignOn.cfg`,
  `ApprovalWithoutAdmitOnApproval.cfg`, `NoSignerCheck.cfg` — each deletes
  one element and must fail its named property;
  `ProposedApprovalReachesSync.cfg` must reach full sync (executability).
