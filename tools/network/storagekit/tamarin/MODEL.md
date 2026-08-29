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
| `VaultConcurrentRekey.spthy` | model 5 green: §1e concurrent re-key convergence (bead auto-veal7) | all lemmas verified |
| `VaultConcurrentRekeyNoConverge.spthy` | model 5 calibration: **winner→loser seal deleted** | `loser_converges` falsified, rest verified |
| `VaultRecoverySuccession.spthy` | model 6 green: recovery-code succession witness window (bead auto-loov7) | all lemmas verified |
| `VaultRecoverySuccessionNoCancel.spthy` | model 6 calibration: **no-cancellation precondition deleted** | `cancellation_blocks_completion` falsified, rest verified |
| `VaultFactorPolicy.spthy` | model 7 green: root-factor-policy AND/OR share algebra (bead auto-5wpjm) | all lemmas verified |
| `VaultFactorPolicyNoSplit.spthy` | model 7 calibration: **AND node's fresh blind deleted** | `password_alone_no_root` falsified, rest verified |
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

Verified 2026-08-29 with tamarin-prover 1.12.0 + Maude 3.5.1: all seven
models (15 theories, 81 lemma expectations) green — each green theory
fully verifies and each calibration falsifies exactly its headline
lemma. Whole suite runs in a few seconds. NOTE: the toolchain needs a
UTF-8 locale (`LC_ALL=C.UTF-8`); `run_tamarin.py` sets it.

INDEPENDENT RE-AUDIT (2026-08-28, session auto-0828-112630): models 3-6
were re-audited from a fresh seat for lemma non-vacuity and abstraction
fidelity against the crib + `idkit/root_factor_policy.py` +
`ledger/fold.py` (models 1-2 already had same-seat review). Verdict:
sound; the abstraction registers below were sharpened, three vacuity /
statement gaps were fixed in place (model 5 `b_can_win`, model 6
`veto_possible`, model 3 MAIN C quantifier), and one framing point now
stated explicitly: in models 4 and 6 the ordering/precedence lemmas are
CONTRACT-CONSISTENCY checks — the fold winner-rule and the completion
guards are encoded as restrictions, so those lemmas certify that the
stated contract entails the claim (and the calibrations certify which
element is load-bearing), not that the property emerges from a modeled
mechanism. The genuinely derived (Dolev-Yao) results are the secrecy
lemmas: `root_secret_open_store`, `no_key_to_forged_pk`,
`session_only_for_enrolled_factor` (model 3), `recovery_key_secret`
(model 4), `concurrent_grant_secret` (model 5). Mechanism-level
derivation of the fold's causal-merge behaviour is the TLA+ side-track
(auto-xtt5v).

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
  injected rows ever leaks the root seed. Holds in all three theories
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
  root share). AUDIT NOTE: in this bounded model the full factor has no
  reveal rule, so this is a labeled corollary of `root_secret_open_store`
  — it would only ever fail together with it. Its independent content
  (root survives unlock-reveal even when the full factor is separately
  compromised) needs a full-factor-compromise variant, where root
  secrecy itself intentionally falls. Kept as the named design claim.
  RESOLVED BY MODEL 7: `unlock_only_no_root` in VaultFactorPolicy.spthy
  is exactly that variant — full-factor reveal rules exist there and
  root release is reachable, so the claim now has independent force.
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

FOLD ABSTRACTION (honesty, sharpened by the 2026-08-28 re-audit): the
fold's partial-order resolution is abstracted to
`self_rekey_loses_to_revoke` — a self-rekey off k cannot occur in any
trace where k is recovery-revoked. That one restriction bundles THREE
code paths verified against `fold.py`: an ancestral revoke refuses the
rekey at issuance (`R_REKEY_REVOKED_KEY`), a causally CONCURRENT revoke
race-kills it (`_rekey_alive`), and the revoke-strictly-after case
cannot arise because by then old_pub is no longer current
(`R_REKEY_WRONG_KEY` refuses the recovery rekey citing it) — so the
order-independent restriction matches the code's net observable
behaviour, covering the concurrent case the crib specifically claims.
CONSEQUENCE: `recovery_beats_thief` is a one-step corollary of this
restriction plus the revoke marking on the recovery rule — a contract-
consistency check whose calibration shows the implicit revoke is the
load-bearing trigger; the independently DERIVED result in this model is
`recovery_key_secret`. Mechanism-level fold fidelity is the TLA+
side-track (auto-xtt5v). Door 2 (root-authorized rekey) is not modeled
— the race under test is door 1 vs door 3. recovery_pub SUCCESSION
(swapping the enrolled recovery key) is deliberately deferred to model
6, whose witness-chain window is its native home.

## Model 5 — §1e concurrent re-key convergence (bead auto-veal7)

Completes the re-key trilogy (model 1 incumbent exclusion, model 2 fleet
distribution, model 5 concurrent convergence). Two machines re-key from
the SAME authority view with distinct counters → divergent keypairs at
concurrent (incomparable) frontiers; the selector's `max(kem_key_id)`
tie-break IS reached (unlike model 1, where the marker makes the
successor descend). Modeled as a NONDETERMINISTIC pick between the two
honest credentials, so safety is proved for every possible winner.

Proved (green, 5/5): executability of the concurrent re-key + grant,
`concurrent_grant_secret` (whichever honest credential wins, the new
generation is secret from the network adversary — SAFETY for any pick),
`loser_converges` (the non-winning machine reads the new generation via
the winner's kem_priv sealed to its machine key), and the two winner
witnesses `either_can_win` + `b_can_win` (one grant per trace, so each
winner needs its own exists-trace; added by the 2026-08-28 re-audit —
the original single witness only showed A could win).

Calibration (`VaultConcurrentRekeyNoConverge.spthy`): delete the
winner→loser convergence seal → `loser_converges` falsified (the loser
is stranded, forced re-login) while safety AND executability still hold.
This is the crisp availability/confidentiality split the design makes:
FleetMachineCredential purpose (2) (D-013) provides convergence; without
it the loser loses availability, never confidentiality.

## Model 6 — recovery-code succession witness window (bead auto-loov7)

The fully-unbuilt (🪦) regenerate-with-lost-code flow (crib §9 FINAL
STATE): a root-signed declaration of a NEW recovery_pub enters the
witnessed head-set at t_D; completion requires a witness attestation at
t ≥ t_D + W over a chain containing the declaration and no cancellation;
a cancellation by the old code or a root veto before completion kills
it. Design verification before code exists.

MONOTONIC CLOCK: Tamarin's trace order is the witness chain's never-
decreasing entry time; a `Tick` action is one chain position and the
window W is "at least two ticks strictly between declaration and
completion" (qualitative, not the literal 7 days).

Proved (green, 6/6): executability, `veto_possible` (a vacuity witness
that the root veto can fire — added by the 2026-08-28 re-audit; the
cancel's fireability was already witnessed by the calibration's attack
trace, the veto's by nothing), `window_cannot_be_fast_forwarded`
(every completion is preceded by its declaration + a full window of
ticks), `cancellation_blocks_completion` (an old-code cancel before
completion always stops it), `veto_blocks_completion`, and
`completion_implies_witnessed_declaration` (the announcement cannot be
hidden).

CONTRACT-CONSISTENCY framing (re-audit): the window / cancel / veto
guards are encoded as restrictions on the completion rule, so the three
corresponding lemmas certify that the specified completion contract
entails the safety claims — the right shape for a fully-unbuilt flow,
where the model IS the spec. The non-trivial content is: `executable`
(the guarded contract is satisfiable — over-restriction would dead-end
the flow), `completion_implies_witnessed_declaration` (derived from
fact flow, not a restriction), and the calibration (the no-cancel guard
alone carries MAIN B). Authorization is by possession of the secret
facts, not signature terms — equivalent at this level of abstraction.

Calibration (`VaultRecoverySuccessionNoCancel.spthy`): delete the
no-cancellation precondition → `cancellation_blocks_completion` falsified
(6-step trace: completion proceeds despite the true owner's cancel)
while window and veto lemmas still hold.

ACCOUNTABILITY SCOPE (honesty): this proves the WINDOW SAFETY. The
split-view-as-self-contained-fraud-proof property (a dishonest witness
showing two conflicting chains is caught) is a genuine accountability
property (Künnemann et al.) modeled here only as an honest monotonic
log; the adversarial-witness increment is the next step. Model 6 also
absorbs the recovery_pub-succession swap-resistance deferred from model
4: a completed succession IS a recovery-key swap, and here it requires
the window + no cancellation, so a thief cannot silently swap it.

## Model 7 — root-factor-policy share algebra (bead auto-5wpjm)

The AND/OR algebra of the root factor policy itself (crib §2, §18;
`root_factor_policy.py`), which model 3 deliberately did not test: one
concrete policy with both operators — password AND (passkey1 OR
passkey2) — compiled the way `_compile_node` does. AND XOR-splits its
node secret (here the root seed): the OR branch's share is a fresh
blind, the password's share is the seed XORed with that blind; OR gives
both passkey leaves the same share; each leaf seals its share to the
factor's seed-derived recipient (`derive_encapsulation_keypair` under
`autonomy/root-factor-recipient/v1`). A fourth, unlock-only factor
derives only the dashboard access signer (`factor_access_keypair`) and
holds no share.

The adversary holds the full public armor and per-factor reveal rules;
each secrecy lemma pins one NAMED non-satisfying subset, so a failure
names the broken policy branch. There is deliberately NO blanket
root-secrecy lemma: releasing the root to a satisfying set is the
design, and the two release witnesses prove it happens.

Proved (green, 1.9 s, 8/8):
- `executable` — both satisfying opens (password+passkey1,
  password+passkey2) reconstruct the true root seed via `_open_node`'s
  XOR, and the unlock-only access path is live.
- `password_and_passkey1_release_root` /
  `password_and_passkey2_release_root` (exists-trace) — a satisfying
  seed set yields the root seed to its holder, each OR branch alone
  completing the AND. These are the vacuity guards for every secrecy
  lemma below.
- `password_alone_no_root`, `passkey1_alone_no_root`,
  `passkey2_alone_no_root`, `passkeys_both_no_root` — every named
  non-satisfying subset leaves the root seed secret. All four are
  genuine Dolev-Yao derivations (the adversary has the ciphertexts,
  the XOR theory, and the revealed seeds; nothing is
  restriction-encoded).
- `unlock_only_no_root` — the unlock-only factor never yields the
  root, in a model where full-factor reveals exist and root release is
  reachable. This supplies the independent force model 3's
  `unlock_only_yields_no_root` lacked (see the RESOLVED note there).

Calibration (`VaultFactorPolicyNoSplit.spthy`): the AND node's fresh
blind is deleted (`zero` replaces it), so the password's sealed share
normalizes to the node secret → `password_alone_no_root` falsified in
5 steps (the password leaf alone opens the root; trace via
`tamarin-prover --prove VaultFactorPolicyNoSplit.spthy`), all seven
other lemmas unchanged. ENCODING NOTE: the naive "hand both children
the node secret" variant would falsify five lemmas at once (either
passkey leaf would open straight to the root, and the honest opener's
XOR of two identical shares collapses to zero, killing `executable`) —
a calibration that breaks everything demonstrates nothing about which
element is load-bearing. Deleting only the blind's freshness
(`os.urandom(32)` degrading to a zero buffer in `_compile_node`) is
the minimal single-element deletion under which the AND stops
splitting knowledge, and it isolates the failure to exactly the
password branch.

Abstraction register:

1. **HPKE seal → `aenc`/`adec`** (builtin), as in every model; the
   computational gap is covered by the RFC 9180 CryptoVerif proof
   (crib §5's shared primitive).
2. **XOR is the real operator, not an abstraction.** `builtins: xor`
   gives the adversary the full equational theory (AC + cancellation +
   unit), so share-recombination attacks are in scope — this is the
   point of the model.
3. **Envelope signature verification out of scope.** Model 3 owns
   verify-at-use of the policy row; here every row is genuine, so no
   signing is modeled. The two models compose: model 3 shows only a
   root-signed envelope is consumed, model 7 shows what a genuine
   envelope's share tree releases.
4. **One recipient per factor.** A synced passkey's per-device PRF
   slots (crib §2) are one recipient here: slots of one factor hold
   identical shares, so collapsing them loses no adversary knowledge.
5. **Factor seeds are atoms.** The password's PBKDF2 wrapping of its
   seed is below this model (password guessing is computational);
   revealing a factor means revealing its 32-byte seed, exactly what
   `open_password_factor` / a PRF evaluation returns.
6. **Wrap-purpose path labels omitted.** `_wrap_purpose` binds digest
   and tree path to each seal against cross-generation share mixing;
   with one bounded generation (`OnceEnroll`) there is nothing to mix.
7. **The access signer is a free function.** `accesskey/1` and
   `recipientkey/1` model `factor_access_keypair`'s HKDF independence
   from the recipient derivation; holding the seed yields both, as in
   code.
8. **Bounded scenario:** one enrollment, three full factors in one
   fixed two-operator policy, one unlock-only factor. Deeper
   expressions repeat the same two compiled node shapes; the
   independence rule (every factor id occurs once,
   root_factor_policy.py:25) keeps the elementary XOR construction
   sound at any depth.

## Roadmap (tracker note graph://8277c76c-ad1; beads filed)

All seven Tamarin models DONE (auto-loxsf, -djh2m, -cpbkf, -veal7,
-loov7, -5wpjm, plus the pilot). Remaining:

1. **TLA+ side-track (auto-xtt5v)**: c6z70 settings-resolution
   discriminator + fleet-roster OR-set convergence — attacker-free
   convergence, belongs in the existing TLA+ practice, parallelizable.
2. **Model 6 increment**: adversarial-witness split-view accountability
   (Künnemann et al.) over the honest-log base here.
3. **Transitive frontier descent** w/ induction (relaxes the one-edge
   abstraction shared by models 1-2).
4. **D-006 halt/continue window** (unattended disenrollment-fact window).
5. **SAPIC+ port** for equivalence properties (unlinkability, §23/§24
   deniability) on ProVerif/DeepSec backends.
