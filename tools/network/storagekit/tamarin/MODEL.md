# Tamarin models — storagekit re-key (pilot)

Machine-checked symbolic verification (Dolev-Yao) of the §1d
disenrollment re-key from the vault + storage design of record
(crib sheet `graph://1e005d5c-c11`). First model of the formal-methods
track chosen 2026-08-28 (session auto-0827-232652): Tamarin was picked
over ProVerif/Verifpal because the design is *stateful* (KeyControlStore,
frontier markers, an append-only store) — multiset rewriting models that
natively — and because F-001 gives a known, hand-found bug to calibrate
against.

## Files

| file | role | expectation |
|---|---|---|
| `VaultRekeyMarker.spthy` | model 1 green: re-key **with** the frontier marker | all lemmas verified |
| `VaultRekeyF001.spthy` | model 1 calibration: identical, marker **deleted** | `exclusion_forward` falsified (the F-001 attack), rest verified |
| `VaultFleetDist.spthy` | model 2 green: fleet KEM distribution + §9 snapshot lemmas (bead auto-loxsf) | all lemmas verified |
| `VaultFleetDistNoGuard.spthy` | model 2 calibration: **kick guard deleted** from distribution | `kicked_machine_excluded` falsified, rest verified |
| `VaultOpenStore.spthy` | model 3 green: open-write-store adversary vs armor + Tier-2 (bead auto-djh2m) | all lemmas verified |
| `VaultOpenStoreNoArmorVerify.spthy` | model 3 calibration A: **envelope check deleted** | `session_only_for_enrolled_factor` falsified, rest verified |
| `VaultOpenStoreNoTierVerify.spthy` | model 3 calibration B: **enrollment-statement check deleted** | `no_key_to_forged_pk` falsified, rest verified |
| `VaultRecoveryRace.spthy` | model 4 green: member.rekey recovery race (bead auto-cpbkf) | all lemmas verified |
| `VaultRecoveryRaceNoRevoke.spthy` | model 4 calibration: **implicit revoke deleted** from the recovery rekey | `recovery_beats_thief` falsified, rest verified |
| `run_tamarin.py` | harness enforcing every expectation above | exit 0 iff all hold |

The pairing is the point: a green proof is only trusted because the
calibration variant demonstrably fails. If the calibration ever starts
verifying, the model has lost the behaviour the marker exists to kill —
treat it as a broken harness, never as good news.

## What is proved

- **`exclusion_forward`** (main): after a marker-writing re-key, a
  generation secret minted and granted afterwards is unreachable to the
  removed machine, even though that machine keeps `kem_priv(C_old)`,
  `G_old`, and a full copy of the replicated store. This is the §9
  "snapshot" discipline mechanized: the adversary literally receives the
  removed machine's snapshot (`RevealRemovedMachine`) plus every public
  record, and secrecy of the post-re-key generation is proved against
  that knowledge.
- **`executable_end_to_end`**: kick → re-key → advance/grant → an honest
  remaining machine opens the grant and walks the ParentBridge back to
  `G_old`. Guards the security lemmas against vacuous truth.
- **`root_secret`**: the protocol itself never exposes the root.
- **`old_generation_stays_readable`** (exists-trace, deliberately):
  the §3 renounced non-claim — the removed machine keeps everything it
  already had. Stated as a theorem so it stays renounced (§22: "any
  claim that ANY key rotation reclaims a shared secret" is prohibited).

## Abstraction register

Honesty ledger — every simplification, and why it does not weaken the
result:

1. **HPKE → `aenc`/`adec`** (builtin). Symbolic perfect encryption; the
   computational gap is covered by consuming the RFC 9180 CryptoVerif
   proof, not by this model (crib §5's shared primitive).
2. **Frontier ancestry → one marker edge.** `strictly_descends`
   (credentials.py:302-311) is modeled as: a grant may not target a
   credential whose cited frontier some marker has advanced past
   (`selector_drops_descended`). The pilot scenario needs exactly one
   descent step; no transitive closure is modeled. Extending to chains
   is future work (see below).
3. **`max(kem_key_id)` tie-break → adversarial choice.** The restriction
   models only the *drop* step. Any candidate surviving the drop may be
   chosen by the adversary — a strict superset of every fixed hash
   order, so proofs are conservative and the calibration attack does not
   depend on which credential a real hash order would pick.
4. **Byte-identical multi-machine derivation (§14)** is why
   `RevealRemovedMachine` yields the *persona's* `sk1`: every machine
   holds the same KEM secret, which is exactly the D-006 weakness the
   re-key exists to heal.
5. **Bounded scenario**: one persona, one removed machine, one re-key,
   one new generation (`Once*` restrictions). Sufficient to exhibit
   F-001 and its fix; concurrency (§1e) is a separate planned model.
6. **HLC, receipts, fold verification, TTLs**: out of scope here; the
   fold's signing-key checks are assumed (caller pre-filters retired
   signing keys — credentials.py:294).

## Running

```bash
# toolchain (one-time; ~30 MB total)
curl -sL -o /tmp/tamarin.tar.gz https://github.com/tamarin-prover/tamarin-prover/releases/download/1.12.0/tamarin-prover-1.12.0-linux64-ubuntu.tar.gz
mkdir -p /tmp/tamarin && tar xzf /tmp/tamarin.tar.gz -C /tmp/tamarin && chmod +x /tmp/tamarin/tamarin-prover
curl -sL -o /tmp/maude.zip https://github.com/maude-lang/Maude/releases/download/Maude3.5.1/Maude-3.5.1-linux-x86_64.zip
unzip -q /tmp/maude.zip -d /tmp/maude-dist && chmod +x /tmp/maude-dist/maude
export PATH=/tmp/maude-dist:/tmp/tamarin:$PATH

python3 tools/network/storagekit/tamarin/run_tamarin.py
```

Verified 2026-08-28 with tamarin-prover 1.12.0 + Maude 3.5.1: green
theory 4/4 verified (0.7 s), calibration falsifies `exclusion_forward`
with a 9-step attack trace (0.6 s).

## Model 2 — fleet distribution + §9 snapshot lemmas

Extends model 1 with the fleet layer BEFORE the KEM-distribution record
(auto-pw9bs.6) is built: machine keypairs `machkey(root, machine_id)`
(§10: derived, never stored; machine_id public), an honest roster
(enroll/kick), and the §1d distribution rule
`seal(kem_priv(C_new), m.machine_public_key)` for `m ∈ fleet∖removed` —
the guard *is* the design element under test. Distribution ciphertexts
are globally visible (conservative; the real records ride fleet_sync).

Proved (green, 1.2 s, 6/6):
- **`machine_key_yields_nothing_undistributed`** — §9 snapshot ZERO
  mechanized: a stolen machine key + every visible record yields a
  generation secret ONLY via a distribution addressed to that machine.
- **`kicked_machine_excluded`** — the kicked machine's complete
  snapshot (machine key + old persona KEM secret + G_old + all public
  records + distributions addressed to others) cannot reach any
  post-re-key generation.
- **`machine_key_reach_via_distribution`** (exists-trace) — the §9
  "reaches" line is real: key theft + an addressed distribution DOES
  open the new generation. The accepted DISK-class damage, bounded by
  fleet membership; proves the ZERO lemma's conditional isn't vacuous.
- **`persona_kem_snapshot_total`** (exists-trace) — the §9 contrast
  class: reveal the current persona KEM secret and everything opens.
- Executability (kick → re-key → distribute → survivor machine reads
  G_new) and root secrecy.

Calibration (`VaultFleetDistNoGuard.spthy`): delete only the kick
guard → `kicked_machine_excluded` falsified (11-step trace: the kicked
machine receives `aenc(kem_priv(C_new), machine_pub)` like any fleet
member and opens every new grant); every other lemma unchanged.

Design-feedback note for auto-pw9bs.6: the whole exclusion property of
the fleet layer rests on the distribution writer consulting the roster
at seal time. The record format matters less than that check — and the
check must read the roster's post-kick state, not a cached fleet list.

## Model 3 — B1 open-write-store adversary (bead auto-djh2m)

A DIFFERENT trust model from models 1-2: the adversary can WRITE
arbitrary rows into the store, modeled by having every honest consumer
read its policy / factor / enrollment inputs from `In()`. Verify-at-use
is modeled structurally — an honest rule pattern-matches
`sign(row, ~rs)` against the true root seed, so only a genuinely
root-signed row is accepted; each calibration relaxes exactly one such
pattern to "accept the row by presence."

Proved (green, 0.7 s, 5/5):
- **`root_secret_open_store`** — the B1 thesis itself: no sequence of
  injected rows ever leaks the root seed. Holds in ALL FOUR theories
  (both calibrations included) — the store being open is never a threat
  to the root, only to decisions that skip their verify.
- **`session_only_for_enrolled_factor`** — a dashboard session is
  granted only for an access key carried in a genuinely root-signed
  envelope (the `root_factor_policy.py:14` "signature verified before
  any factor material is used" property).
- **`no_key_to_forged_pk`** — Tier-2: a fresh session key is sealed only
  to a root-enrolled wrapping pk; an injected pk row never receives a
  seal ("AN UNVERIFIED pk ROW MUST NEVER RECEIVE A SEAL").
- **`unlock_only_yields_no_root`** — revealing the unlock-only factor in
  full never reconstructs the root (it derives a session signer, not a
  root share).
- Executability of all three honest ceremonies.

Two calibrations, each a single deleted verify:
- `VaultOpenStoreNoArmorVerify.spthy` (envelope check dropped) →
  `session_only_for_enrolled_factor` falsified in 4 steps: an injected
  factor row with the attacker's access key authorizes a session as the
  user. Every other lemma — including root secrecy — unchanged.
- `VaultOpenStoreNoTierVerify.spthy` (enrollment check dropped) →
  `no_key_to_forged_pk` falsified in 7 steps: renewal seals a fresh
  signing key to the attacker's injected wrapping pk.

The clean separation (each calibration breaks exactly one lemma, root
secrecy survives both) is the evidence the two verify points are
independent and each load-bearing. Related live defects in this exact
class: jh59f (forged `excludes`/base row wins selection) and c6z70
(crib §17) — the settings resolver branching on unsigned fields is the
same "trust presence" mistake this model isolates.

## Model 4 — member.rekey recovery race (bead auto-cpbkf)

The crib §9 claim "the recovery-authorized path additionally REVOKES the
key it moves away from, so a stolen key cannot outrun its own recovery"
as a race. Grounded in `ledger/fold.py:_h_member_rekey` (three doors;
Item 4's implicit revoke; `_rekey_alive`). Two parts:
- crypto authorization — the enrolled recovery key never leaks, so the
  thief (holding the stolen current signing key + all public material)
  cannot forge a recovery-authorized rekey;
- the causal winner-rule — once a recovery rekey revokes old_pub, no
  self-authorized rekey off old_pub is ever the effective authority.

Signature checks use the Eq idiom (`Equal(verify(sig,m,pk), true)` + a
generic `Equal(x,y) ⇒ x=y` restriction), because the fold verifies
against a public key whose secret it does not hold — unlike models 1-3.

Proved (green, 4/4): executability of a recovery rekey, the thief-can-
self-rekey sanity trace (guards vacuity), `recovery_key_secret`, and
**`recovery_beats_thief`** — no trace has both a recovery rekey revoking
k and an effective self-rekey off k.

Calibration (`VaultRecoveryRaceNoRevoke.spthy`): delete the implicit
revoke → `recovery_beats_thief` falsified (11-step trace: the thief's
self-rekey survives concurrently with recovery — Item 4's exact
failure). The thief-sanity and executability traces still verify, so the
falsification is the revoke's absence, not a broken model.

FOLD ABSTRACTION (honesty): the fold's partial-order resolution is
abstracted to `self_rekey_loses_to_revoke` — a self-rekey off k cannot
occur in any trace where k is recovery-revoked. Order-independent, so it
covers the concurrent case the crib specifically claims. Full causal-
merge fidelity is the TLA+ side-track (auto-xtt5v). recovery_pub
SUCCESSION (swapping the enrolled recovery key) is deliberately deferred
to model 6, whose witness-chain window is its native home.

## Roadmap (tracker note graph://8277c76c-ad1; beads filed)

1. ~~Model 2: snapshot lemmas + F4 distribution~~ — DONE, auto-loxsf.
2. ~~Model 3: B1 open-write-store adversary~~ — DONE, auto-djh2m.
3. ~~Model 4: member.rekey recovery race~~ — DONE (above), auto-cpbkf.
4. **Model 5 (auto-veal7): §1e concurrent re-key** — honest-concurrency
   convergence; requires transitive/branching frontier descent.
5. **Model 6 (auto-loov7): recovery-code succession witness window** —
   accountability formulation, monotonic time; ALSO absorbs the
   recovery_pub-succession swap resistance deferred from model 4.
6. **TLA+ side-track (auto-xtt5v)**: c6z70 settings-resolution
   discriminator + fleet-roster OR-set convergence.
7. Later: transitive frontier descent w/ induction; D-006 halt/continue
   window; SAPIC+ port for equivalence properties (unlinkability,
   §23/§24 deniability) on ProVerif/DeepSec backends.
