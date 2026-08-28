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
| `VaultRekeyMarker.spthy` | green theory: re-key **with** the frontier marker | all lemmas verified |
| `VaultRekeyF001.spthy` | calibration: identical, marker **deleted** | `exclusion_forward` falsified (the F-001 attack), rest verified |
| `run_tamarin.py` | harness enforcing both expectations | exit 0 iff both hold |

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

## Roadmap (next models, in value order)

1. **§1e concurrent re-key** — two honest re-keys from the same view;
   prove the winner/loser convergence claim and that the deterministic
   tie-break is safe *because both are honest* (the marker supplies
   recency only in the incumbent case).
2. **Snapshot lemmas per §9 descriptor** — one adversary-knowledge lemma
   per key class (per-machine key snapshot ZERO vs persona KEM snapshot
   TOTAL), mechanizing the classification rubric that has already been
   wrong twice by analogy.
3. **Transitive frontier descent** — replace the one-edge abstraction
   with chain ancestry + induction lemmas.
4. **D-006 window** — between accepting a disenrollment fact unattended
   and the next root-present login, prove HALT preserves confidentiality
   and CONTINUE leaks exactly the new content, no more.
5. **SAPIC+ port** for the equivalence-shaped properties (unlinkability,
   §23/§24 deniability) where ProVerif/DeepSec are the stronger backends.
