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
| `VaultPolicyClass.spthy` | model 8 green: policy-class generation lifecycle (bead auto-ncokx) | all lemmas verified |
| `VaultPolicyClassNoReseal.spthy` | model 8 calibration A: **revocation reuses the old class_key** | `revoked_factor_excluded_forward` falsified, rest verified |
| `VaultPolicyClassNoAnchor.spthy` | model 8 calibration B: **anchor wrap deleted from the new generation** | `root_reaches_every_generation` falsified, rest verified |
| `VaultRootRotation.spthy` | model 9 green: personal-root rotation dual authority (bead auto-lythl) | all lemmas verified |
| `VaultRootRotationNoCosign.spthy` | model 9 calibration: **recovery co-signature check deleted** | `thief_cannot_rotate` falsified, rest verified |
| `VaultDelegateChain.spthy` | model 10 green: delegate-chain resolution at use (bead auto-9ldpx) | all lemmas verified |
| `VaultDelegateChainNoResolve.spthy` | model 10 calibration: **roster resolution deleted from acceptance** | `write_resolves_to_current_member` falsified, rest verified |
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

Verified 2026-08-29 with tamarin-prover 1.12.0 + Maude 3.5.1: all ten
models (22 theories, 115 lemma expectations) green — each green theory
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

## Model 8 — policy-class generation lifecycle (bead auto-ncokx)

The vault policy-class key-generation discipline (crib §3, §18;
`tools/vault/policy_class.py`): one password-policy class with two
member factors and the mandatory personal-root anchor. Each Generation
is a fresh class_key sealed to the members admitted at mint time plus
the anchor (`_mint_generation`); the generation publishes the sealing
public key derived from the class_key (`CLASS_SEAL_KEY_PURPOSE` →
`sealkey/1`), so `seal_cek` writes with no factor present and only
reads derive the private half. `revoke_factor` APPENDS the next
generation sealed to the survivor plus the anchor; generation 1 stays
byte-identical. Scenario: enroll → write cek1 → revoke f2 → write
cek2; every wrap, sealed CEK, and public key is Out at mint time, so a
revealed seed models a holder with a full replicated store copy (the
§9 snapshot discipline).

Proved (green, 1.0 s, 4/4):
- **`revoked_factor_excluded_forward`** (MAIN, derived Dolev-Yao) —
  the revoked factor's seed plus the full public store never opens a
  CEK sealed under the post-revocation generation, unless a surviving
  credential also leaked. "Applies at the next write" mechanized.
- **`revoked_factor_keeps_old`** (exists-trace, deliberately) — the
  §3 renounced non-claim as a theorem, the model-1
  `old_generation_stays_readable` move: the revoked seed alone opens
  pre-revocation content. Doubles as the MAIN premise's vacuity guard.
- **`root_reaches_every_generation`** (exists-trace) — the anchor seed
  alone, no member factor revealed, reaches CEKs under BOTH
  generations: crib §18 widen-only, the wrap `put_class` refuses to
  drop.
- Executability: enroll → write → revoke → write, survivor reads both
  CEKs (`open_cek`'s survivor-still-reads-old behaviour included).
  Post-revocation ordering is structural: `Write_Gen2` needs
  `!Gen2Key`, which only the revocation mints.

Two calibrations, each deleting one element of the revocation mint:
- `VaultPolicyClassNoReseal.spthy` (old class_key reused) →
  `revoked_factor_excluded_forward` falsified in 7 steps: the revoked
  factor opens its generation-1 wrap, recovers the still-current key,
  derives the sealing private key, reads every new write. Everything
  else holds.
- `VaultPolicyClassNoAnchor.spthy` (anchor wrap deleted from the new
  generation) → `root_reaches_every_generation` falsified: the root
  opens generation 1 but has no route into generation 2 — exactly the
  enclave record `put_class` refuses to persist. Forward exclusion and
  old-readability are anchor-independent and hold.

The split is the evidence the two mint-time elements are independent
and each load-bearing: fresh key ⇒ forward exclusion; anchor wrap ⇒
root reach.

Abstraction register:

1. **HPKE seal → `aenc`** (builtin), both for factor wraps
   (`idkit.sealing.seal`) and the class→CEK public seal — the suite's
   standard §5 abstraction.
2. **Wrap purposes omitted.** `_wrap_purpose` /
   `_cek_public_seal_purpose` bind class, generation, policy, role,
   and setting into every HPKE info string against cross-context
   replay; with one class, distinct per-generation keys, and no role
   split there is nothing to confuse. Cross-class/cross-role confusion
   is a purpose-label property consumed from the sealing layer, not
   re-derived here.
3. **Password policy only (single wrap).** The `both` policy's XOR
   split is exactly model 7's AND-node algebra — proved there, not
   repeated. One member survives, one is revoked; more members add
   symmetric copies of the same wraps.
4. **The derived sealing keypair is `pk(sealkey(k))`.** Faithful to
   `derive_encapsulation_keypair(class_key, ...)`: knowing the
   class_key yields the private half (the adversary can apply
   `sealkey`), publishing the public half lets anyone write.
5. **Revocation is ceremony-free** as in code: the mint consumes only
   public keys; the model's premise on seed facts is scope-binding
   only, nothing secret flows into the new generation's outputs.
6. **`created_at`, governance records, store refusal logic**: out of
   scope. `put_class`'s refusal is mechanized by its consequence — the
   NoAnchor calibration shows what the refused record would cost — not
   by modeling the store.
7. **Bounded scenario:** one class, two generations, one revocation,
   one write per generation (`Once*` on enroll/revoke; writes may
   repeat). Longer generation chains repeat the same mint shape.

## Model 9 — personal-root rotation dual authority (bead auto-lythl)

`idkit/root_rotation.py`'s own security argument, machine-checked: a
personal-root succession record carries three signatures over one
binding — the OLD root (`rotation_input`: the author opens the armor),
the enrolled RECOVERY key (`rotation_recovery_input`: the author holds
the printed code), and the NEW root (`rotation_continuity_input`: the
successor is controlled). Neither the thief (armor + password, i.e.
the old root seed) nor a code finder (recovery key alone) can rotate;
the owner, holding both, rotates away from a stolen root. Signature
checks use the model-4 Eq idiom (the verifier holds no secrets); the
three distinct signing domains are structural tags.

CRIB CORRECTION recorded here (bead auto-lythl): crib §9 still marks
this flow 🪦 unbuilt, but the armor-layer code (`make_rotation`,
`verify_rotation`, `resolve_current_root`) EXISTS. The unbuilt piece
is the registry that DECLARES the recovery public key —
`verify_rotation` trusts whatever `recovery_pub` its caller supplies.
The model states that boundary honestly: the verifier reads the
recovery pk from a setup-minted honest binding (`!DeclaredRecoveryPk`),
never from `In()`, and `recovery_pk_is_declared` restates that
assumption as a lemma. A caller that instead resolved the pk from
attacker-writable rows would be behaviourally the calibration.

Proved (green, 0.6 s, 5/5):
- **`thief_cannot_rotate`** (MAIN, derived Dolev-Yao) — while the
  recovery key is unrevealed, every accepted rotation is
  owner-authored, even with the old root fully public: recovery
  co-signature unforgeability over the exact `<old_pub, new_pub>`
  binding (an owner co-signature for one successor cannot be replayed
  for another).
- **`code_finder_cannot_rotate`** (MAIN, derived) — the symmetric
  direction: while the old root is unrevealed, the printed code alone
  produces no accepted rotation the owner did not author.
- **`owner_rotates_away_from_stolen_root`** (exists-trace; the model's
  executability lemma) — with the old root already revealed and the
  code safe, the owner completes an accepted rotation: the elegant
  wipe runs under exactly the compromise it exists for.
- **`both_secrets_suffice`** (exists-trace) — with BOTH secrets
  revealed the adversary forges an accepted rotation the owner never
  made. Rotation authority is exactly the pair; guards both MAIN
  premises against vacuity.
- **`recovery_pk_is_declared`** (contract-consistency, restriction-
  level) — restates the honest declared-recovery binding; carries no
  derived force and stands in for the unbuilt registry.

Derived vs encoded, explicitly: the two `*_cannot_rotate` lemmas and
both witnesses are derived from the modeled signature mechanism;
`recovery_pk_is_declared` restates the honest-binding assumption.

Calibration (`VaultRootRotationNoCosign.spthy`): the recovery
co-signature check is deleted from `Accept_Rotation` →
`thief_cannot_rotate` falsified in 8 steps (the adversary mints its
own successor, signs 'rot' with the stolen old root and 'cont' with
its key, and is accepted); the owner reachability, the code-finder
direction (old_sig still checked), and the binding lemma all hold —
the co-signature is load-bearing only against the thief.

Abstraction register:

1. **Ed25519 → `signing` builtin**; the three domain strings →
   structural tags `'rot'`/`'cont'`/`'rec'` in the signed message,
   preserving the no-cross-substitution property the domains exist
   for.
2. **Single lineage, single step.** `origin_pub` and `seq` bind a
   co-signature to one step of one person's lineage;
   `resolve_current_root`'s walk (order, no gaps, chain-start) is
   collapsed to the one-step chain-start check `old_pub = origin`.
   Multi-step succession and cross-lineage replay need the chain walk
   modeled with induction — future work, and the reason `seq` exists
   in the record.
3. **The armor is the reveal.** "Thief holds armor + password" is
   modeled as revealing the old root SEED directly — strictly
   conservative (the armor's factor policy is model 7's subject).
4. **`make_rotation`'s `new_pub != old_pub` refusal** is not modeled;
   no lemma depends on it (a self-rotation would still need both
   signatures).
5. **`rotated_at`, record-shape validation, version checks**: out of
   scope — parser hygiene, not authority.

## Model 10 — delegate-chain resolution at use (bead auto-9ldpx)

The PIN 6b acceptance rule (crib §7, §11; `storagekit/acceptance.py`,
`ledger/fold.py:_h_delegate`/`_h_revoke`): a delegated key authorizes
by resolving its chain to a member persona and checking that persona's
CURRENT membership; a chain terminating outside the roster is void;
generic scope-holding is never consulted for `storage:state:advance` /
`storage:capability:grant`. Personas enroll and are removed; personas
mint delegate certificates for arbitrary public scopes; delegates
(agents, possibly coopted) sign write requests for arbitrary public
scopes; a delegate key can be stolen outright (DISK-class, §9);
revocation folding and TTL expiry end liveness.

DERIVED vs ENCODED, explicitly: the acceptor's two STATE READS —
roster currency and delegate liveness — are restrictions on the
acceptance action (`roster_read_current`, `liveness_read`), the
model-4/6 contract-consistency convention: they are reads of folded
state, not cryptographic steps. The DERIVED Dolev-Yao content is the
chain itself — the `Delegated` conjunct of the MAIN lemma (persona-
signature unforgeability: an accepted write's certificate was
genuinely minted by the enrolled persona it names, so a fabricated
chain resolves onto no member and is void) — plus scope confinement,
which is structural (acceptance rules exist for exactly the two
storage scopes; the model-3 verify-at-use idiom). A first encoding
carried the roster as consume-and-restore linear facts to make the
membership conjunct derived too; the Accept→Accept token regression
diverged for every search heuristic, and the restriction encoding is
the suite's documented convention for folded-state reads.

Proved (green, 1.0 s, 6/6):
- **`write_resolves_to_current_member`** (MAIN) — every accepted
  write's chain ends at the persona that minted the delegate
  (derived), and that persona is enrolled and not removed at
  acceptance (contract). Covers the bead's scenario: a delegate minted
  during membership yields nothing after its persona's removal.
- **`delegate_scopes_only`** — accepted writes carry exactly the two
  storage execution scopes, in a model where delegates for intent
  scopes ARE minted and writes under them ARE signed (mint and sign
  take an arbitrary `$scope`).
- **`write_requires_live_delegate`** — no accepted write after the
  delegate's revocation folds or its TTL lapses (contract over the
  liveness read). With the MAIN lemma this is the stolen-delegate
  bound: theft yields writes only inside the persona's membership and
  only until revocation — after it, none.
- **`stolen_delegate_write_witness`** (exists-trace) — a revealed
  delegate key yields an attacker-authored accepted write (payload is
  the adversary's public constant; honest payloads are fresh). The
  bead-required premise witness: theft is live, revocation ends it.
- **`revocation_and_expiry_possible`** (exists-trace) — both
  liveness-ending events fire; vacuity guard for the liveness lemma.
- Executability: one persona's delegates advance a generation and
  issue a capability grant, both accepted.

Calibration (`VaultDelegateChainNoResolve.spthy`): the RosterRead
action — and with it the roster contract — is deleted from both
acceptance rules; the liveness read stays →
`write_resolves_to_current_member` falsified in 8 steps (a chain
terminating outside the roster is accepted; the removed-persona
scenario follows identically, since nothing re-reads the roster at
use). All five other lemmas hold.

Abstraction register:

1. **Ed25519 → `signing` builtin; Eq idiom** for both certificate and
   write verification (the acceptor holds no secrets).
2. **Chain depth 1.** The fold terminates storage chains at a persona
   one hop up (the bounded self-delegation persona condition,
   fold.py:_h_delegate — "a delegate is not a persona"). Multi-hop
   `_upstream` resolution is not modeled.
3. **One scope per certificate.** The real agent delegate carries the
   two-scope set; splitting it into two single-scope certs changes
   neither confinement nor resolution.
4. **TTL is nondeterministic expiry.** `Expired` may fire at any time
   after mint — every accepted write must therefore tolerate the
   strictest timing; the literal clock is out of scope (model 6's
   monotonic-clock note applies).
5. **Delegate proof-of-possession, grant nonces, attenuation lattice**
   (`_h_delegate`'s consent proof, `R_DELEGATE_NONCE_REUSED`,
   `attenuates`): out of scope — they govern which delegate EVENTS
   fold, not how a folded delegate authorizes a storage write. The
   fold's admission is collapsed into the honest `Mint_Delegate` rule.
6. **Roster and liveness as trace conditions**, not fact state — see
   the encoding note above; the calibration certifies the roster read
   is load-bearing.

## Roadmap (tracker note graph://8277c76c-ad1; beads filed)

All ten Tamarin models DONE (auto-loxsf, -djh2m, -cpbkf, -veal7,
-loov7, -5wpjm, -ncokx, -lythl, -9ldpx, plus the pilot). Remaining:

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
