# Factor / auth state machine — abstraction ledger

TLA+ model of the personal-identity factor state machine: the configurations
the personal-root master KEK can be armored in, the transitions the
factor-management UI + re-arm backend expose, and the safety invariants each
reachable configuration must keep. Checked by TLC via `run_tlc.py`.

Source of truth for the factor MODEL: `/workspace/output/factor-ui/UNDERSTANDING.md`
(operator-confirmed) and the designs it points to (two-gate, all-sequences,
passkey-management). Source of truth for the CODE the model abstracts:
`tools/network/idkit/armor.py`, `tools/dashboard/identity_routes.py`,
`tools/dashboard/static/js/ceremony/primitives.js`. When the shape of the
factor transitions/guards in that code changes, this model changes in the same
commit (README, honesty rule). Method follows the sibling rollout-ingestion
model (`tools/dashboard/TLA/`): model the property class that matters, write
down every abstraction, keep it honest with calibration switches.

## Property class

This machine's center of gravity is **safety**, not liveness: the failures that
matter are locking yourself out (no way to open the root), a pointless-and-
dangerous MFA state (a standalone opener left beside the pair), the plaintext
root secret leaking to disk, and a statement verified against the wrong key.
All invariants are safety invariants; no fairness is needed, so the spec is
`Init /\ [][Next]_vars` and the runner needs no `-lncheck`. Terminal factor
configurations are legitimate (you may stop reconfiguring), so the runner does
not pass `-deadlock`.

## State

- `identity` (BOOLEAN) — has an identity been founded.
- `hasPassword` (BOOLEAN) — a STANDALONE password factor wraps the KEK.
- `role[pk]` ∈ {unenrolled, access, root} — per passkey device:
  - `unenrolled`: device not registered.
  - `access`: enrolled; derives a dashboard session key only; does NOT open the
    root. (This is what the operator's iPhone enrollment produced.)
  - `root`: enrolled AND a standalone root factor (opens the KEK alone).
- `combined` ∈ {none} ∪ PASSKEYS — the passkey bound into the combined MFA
  factor, or none. `combined # none` is the MFA state.
- `pubPublished[pk]` (BOOLEAN) — the passkey's provisioning public key is on the
  ledger (published at enrollment; a prerequisite for holding root authority).
- `plainSecretLoc` ∈ {none, browser, disk, python, relay} — location of the
  plaintext root secret (seed / master KEK). `none` between ceremonies; a broken
  design leaks it (I1).
- `provOK` (BOOLEAN) — every statement verified so far used the ledger root key.

The factor model (from UNDERSTANDING.md): individual factors (password,
passkey) each open the root ALONE — password-only, passkey-only, both-individual
(OR) are all valid. MFA is ONE combined password+PRF factor whose enablement
CLEARS the individuals. `require_pair` in the code is only the UI guard
(rootReachable), not the MFA mechanism — so it is NOT a state variable here; the
mechanism is the `combined` factor.

## Transitions (operation → real action)

| Action | Real-world operation |
|---|---|
| `FoundFreshPassword` | onboard a new identity with a password factor |
| `FoundFreshPasskey` | onboard with a passkey as a root factor |
| `FoundFreshCombined` | onboard STRAIGHT to password+PRF (nothing → combined) |
| `EnrollPasskeyAccess` | register a device (session key); publish its pubkey; verify the root-signed enrollment statement. Does not touch the root |
| `PromotePasskeyToRoot` | open armor with an existing opener, re-wrap the KEK to a passkey → standalone root factor |
| `AddPassword` | add a standalone password factor |
| `RemovePassword` | drop the standalone password factor (rootReachable-guarded) |
| `DemotePasskey` | root passkey → access-only (rootReachable-guarded) |
| `RemovePasskeyDevice` | remove a device (refused while root — demote first; refused for the combined member) |
| `EnableMFA` | combine an existing password + passkey → the both-required factor, clearing individuals |

Not modeled yet (open, pending design confirmation): `DisableMFA` (split the
combined factor back to individuals). The designs do not clearly expose it and
inventing it could assert a transition the product does not support; it is left
out rather than guessed. Add it (and a probe) once the design confirms it.

## Design switches (green value = correct)

- `ClearOnCombine` (TRUE) — EnableMFA clears the individual factors.
- `GuardLastOpener` (TRUE) — removals enforce rootReachable.
- `TrustLedgerPub` (TRUE) — statements checked against the ledger root key.
- `ZeroSeedAfterUse` (TRUE) — plaintext secret zeroed at ceremony end.
- `AllowUpgradeMFA` (TRUE) — the upgrade path to MFA is available (turned off in
  the probe that isolates the fresh-found-to-MFA path).

## Invariants

- `RootReachable == identity => OpenerExists` — never lock yourself out.
- `MFAExclusive` — `combined # none => (~hasPassword ∧ no root passkey)`.
- `SeedNeverPersisted` (I1) — `plainSecretLoc ∈ {none, browser}`.
- `ProvenanceOK == provOK` — the fatal-one-liner rule, machine-checked.
- `PublishedBeforeAuthority` — root authority (standalone or combined) only for
  a passkey whose pubkey is on the ledger.
- `TypeOK`.

## Calibrations (must fail) and probes (must fail = reachable)

| Config | Flips | Rediscovered violation / proof |
|---|---|---|
| `CalKeepIndividuals` | `ClearOnCombine=FALSE` | `MFAExclusive` — clearing is load-bearing |
| `CalNoRootGuard` | `GuardLastOpener=FALSE` | `RootReachable` — the guard is load-bearing |
| `CalTrustPayload` | `TrustLedgerPub=FALSE` | `ProvenanceOK` — the fatal one-liner is real |
| `CalSeedPersisted` | `ZeroSeedAfterUse=FALSE` | `SeedNeverPersisted` — I1 is real |
| `ProbePasskeyOnly` | — | `NoPasskeyOnly` violated → passkey-only reachable |
| `ProbePasswordOnly` | — | `NoPasswordOnly` violated → password-only reachable |
| `ProbeFoundMFADirect` | `AllowUpgradeMFA=FALSE` | `NoMFA` violated with the upgrade path OFF → nothing→combined **direct** is reachable and safe |

Because the green run over the whole reachable space is clean, each probe also
shows the target state satisfies every green invariant — i.e. passkey-only,
password-only, and fresh-to-MFA are not just reachable but SAFE.

## Boundary (what a green run does NOT prove)

- The crypto primitives (sealing, PRF, signatures) — abstracted symbolically.
- That the code implements exactly these transitions/guards — the honesty rule
  names the obligation; the model does not read the code.
- Intra-ceremony key handling — actions are atomic; the brief browser-memory
  window for the plaintext secret is assumed correct, not modeled.
- Anything about timing/liveness — this is a pure safety model.
