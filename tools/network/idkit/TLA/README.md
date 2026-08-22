# TLA+ model — personal-identity factor / auth state machine

Formal model of how the personal-root master KEK is armored (password,
passkey, combined MFA), the transitions the factor-management UI and the
re-arm backend expose, and the safety properties every reachable
configuration must keep. `MODEL.md` is the abstraction ledger — read it
before trusting any green run.

The factor model encoded here is the operator-confirmed one recorded in
`/workspace/output/factor-ui/UNDERSTANDING.md`; the **UI is the plan of
record**, this model makes it machine-checked.

## Run

```bash
# toolchain: a JRE/JDK plus tla2tools.jar
#   default locations: ~/tools/jdk-*/bin/java and ~/tools/tla2tools.jar
#   overrides: TLA_JAVA, TLA_TOOLS_JAR
python3 tools/network/idkit/TLA/run_tlc.py              # full suite (~10 s)
python3 tools/network/idkit/TLA/run_tlc.py GreenCore    # one config
```

The suite passes only when the green configuration checks clean AND every
calibration/probe fails with a real violation. The runner greps for explicit
violation markers, so a crashed or unparseable spec reads as BROKEN — never as
"failed as required".

## Layout

- `FactorAuth.tla` — the model: all state, transitions, design switches,
  invariants, and reachability probes.
- `GreenCore.cfg` — the correct design; must be clean.
- `calibration/Cal*.cfg` — one restored broken design each; must fail on the
  named invariant.
- `calibration/Probe*.cfg` — reachability probes; must fail (a "never reach
  it" invariant that is violated proves the target state is reachable).
- `run_tlc.py` — runner with the must-pass / must-fail gate.

## What is checked

Green `Inv` (all hold across the whole reachable state space):

- `RootReachable` — once founded, there is always a way to open the root.
- `MFAExclusive` — the combined MFA factor never coexists with a standalone
  individual opener (enabling MFA clears the individuals).
- `SeedNeverPersisted` (I1) — the plaintext root secret is never in a
  forbidden location.
- `ProvenanceOK` — every statement check used the ledger root public key,
  never the payload's own signer (the fatal one-liner).
- `PublishedBeforeAuthority` — a passkey holds root authority only after its
  provisioning public key was published to the ledger (key-exchange order).
- `TypeOK`.

Calibrations prove each guard is load-bearing (removing it breaks exactly the
named invariant). Probes prove `password-only`, `passkey-only`, and the
`nothing -> combined` **direct** founding path are all reachable and land in
states where every green invariant still holds.

## The honesty rule (the change rule)

Any change to the factor/armor state machine — `tools/network/idkit/armor.py`
(factor add/remove/open, master-KEK wrapping), `tools/dashboard/identity_routes.py`
(the re-arm route, `_root_reachable`, `DELETE passkey`), or
`tools/dashboard/static/js/ceremony/primitives.js` (`signArmorUpdate`,
`addPasskeyFactor`, the combined-MFA construction) that adds/reorders a
transition, changes a guard, or changes where key material lives **must change
`FactorAuth.tla` in the same commit**, and `run_tlc.py` must pass. When you fix
a bug in this machinery, add a calibration switch that restores the broken
behavior and verify TLC rediscovers the failure on its own.

## What the model ASSUMES (cannot check)

- Symbolic crypto: a factor opens the KEK iff its holder supplies the factor's
  material; a statement verifies iff checked against the ledger key. The model
  proves the protocol/state machine, not the primitives.
- Ceremony atomicity: each armor-touching action is one step; the intra-ceremony
  browser window where the plaintext secret is briefly live is not modeled as a
  separate state (assumed by construction — the seed is zeroed before the action
  completes). I1 asserts no persistence *after* any action.
- Code conformance: that `armor.py` / `identity_routes.py` / `primitives.js`
  implement exactly these transitions and guards is a separate obligation the
  honesty rule names; the model does not read the code.
