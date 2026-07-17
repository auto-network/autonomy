# ledger — auto.network org authority ledger core (F1)

Pure Python library (no storage, no network, no UI) implementing the org
authority ledger: a **hash-linked DAG of signed authority events** plus the
**deterministic fold** that turns the DAG into authority state. Built on
idkit (`tools/network/idkit/` — the only dependency besides `cryptography`).

Spec: graph note `eb245082-b76` §2–3, §11 · Bead: `auto-16cjl` (F1).
Downstream: F2 storage/projections, F3 sync, F5 invites, F7 recovery.

## Data model

Every event: `{v, author_key, parents[], hlc, payload, sig}` — canonical
JSON, domain-separated Ed25519 signature, event id = SHA-256 of the wire
bytes (id commits to the signature, git-style). `parents` are the author's
seen-heads; causality is carried entirely by hashes (HLC is an ordering
hint, forced strictly monotone along causal edges at admission).

Event vocabulary (closed — **L8**: anything else, e.g. content views, is
schema-rejected and cannot exist in the DAG):

| type | payload | effect |
|---|---|---|
| `genesis` | `org, root_pub` | anchors the DAG; self-signed by root |
| `delegate` | `child_pub, scope[], can_redelegate, ttl?` | standing authority grant |
| `revoke` | `target_event \| target_key, reason?` | kills grants/invites/roles/claims |
| `role.define` | `name, scope_set[], claim_requires, version` | LWW by (version, hash) |
| `role.grant` / `role.revoke` | `persona, role` | role membership facts |
| `invite` | `invite_pub \| token_hash, granted_role, expiry, sponsor` | scoped join offer |
| `member.claim` | `invite_ref, persona_pub, profile, approvals[], token?` | membership fact |
| `member.rekey` | `persona, old_pub, new_pub, approvals[]` | persona key rotation |
| `key.rotate` | `old_pub, new_pub, continuity` | org root rotation (lineage) |
| `checkpoint` | `state_hash, signers[]` | fast-cold-join stub (no fold effect) |

Scopes are opaque strings with two pattern forms: `*` (everything — held
implicitly by the current root) and `prefix:*`. Fold-internal scopes:
`role:define`, `role:grant:<role>`, `invite:<role>`, `checkpoint`.

## The fold — semantics in one page

**Issuance validity (L1, L2).** An event is valid iff its author held the
required authority *in the state derived from the event's own causal
ancestry*. Same DAG ⇒ same judgement on every replica; replay/insertion
order can never matter (determinism is by construction and property-tested
across shuffled replays). Attenuation-only at every hop: delegations must
be covered by the author's *delegable* set, invites by `invite:<role>`,
role definitions by the definer's delegable coverage of the `scope_set`
(role scopes flow to holders, so defining is a delegation hop too).
Invalid events stay in the DAG (replicas must exchange them to converge)
but are permanently inert.

**Revocation (L3, safety-biased).** A revocation kills targets that are
causally *ancestral or concurrent* to it — a revoke racing a grant wins in
every replay order — while a grant issued causally *after* the revoke
survives (re-grants are explicit). Additionally, a grant/claim whose
author's supporting authority was broken by a *concurrent* revocation is
**permanently void**: it does not revive if its author is later re-granted
(a compromised key's race-window grants stay dead). Equal-rank conflicts
(two concurrent claims of one invite, same-version role.defines,
concurrent rotations/rekeys) break deterministically by event hash.

**Standing authority vs facts (L4).** Delegations and unclaimed invites
are *standing*: live only while a chain of live grants connects them to
the root lineage — revoking a delegation silently de-authorizes every
descendant lacking another live path (the cascade), and an explicit
upstream re-grant restores the surviving subtree. Memberships and role
grants are *facts*: once established they persist — they die only by
explicit revocation (`revoke {target_event: claim}`, `role.revoke`) or by
losing a concurrency race, never because their sponsor was later demoted.
Every claim permanently records its sponsor (accountability trail).

**Invites.** `invite` + `member.claim` need zero extra machinery: the fold
rejects overreaching invites (L2), unclaimed invites die with their
inviter's demotion, claim-vs-revoke races resolve revoke-wins, expiry is
judged against the claim's HLC. `claim_requires` (from the role def):
`self` / `sponsor` (countersignature by the sponsor) / `admin-ack`
(countersignature by root or a `role:grant:<role>` holder). Token invites
carry `token_hash`; the claim reveals the token.

**Revocation authority is provenance.** Root revokes anything; otherwise
you may revoke what you created, what descends from you (via delegation /
invite / sponsorship edges), or yourself (renounce). Provenance survives
your own demotion — a removed admin can still clean up their subtree
(attenuation-safe: revoking only removes authority).

**Root rotation** (`key.rotate`): continuity proof signed by the *new*
key; grants made while root stay anchored (reputation survives rotation);
the rotated-out key loses root authority for all causally-later events —
and root-anchored effects minted *concurrently* with a rotation away from
their author are permanently void (a stolen root key cannot outrun its
own rotation).

## Usage

```python
from tools.network.idkit import KeyPair
from tools.network.ledger import HLC, Ledger, fold, make_event

root = KeyPair.generate()
ledger = Ledger()
ledger.add(make_event(root, {"type": "genesis", "org": org_uuid,
                             "root_pub": root.public_hex}, [], HLC(now_ms)))

alice = KeyPair.generate()
ledger.add(make_event(root, {
    "type": "delegate", "child_pub": alice.public_hex,
    "scope": ["invite:member", "link:publish"], "can_redelegate": True,
}, ledger.heads(), HLC(now_ms + 1)))

state = fold(ledger)                      # or fold(ledger, heads=..., now=...)
state.holds(alice.public_hex, "link:publish")   # True
state.authority(alice.public_hex)               # frozenset of scope patterns
state.members / state.roles(pid) / state.invites / state.role_defs
state.valid / state.reasons                     # per-event fold judgement
state.fingerprint()                             # canonical state hash (L1)

# Sync (git-fetch shaped): ship wire bytes, ingest in any order.
replica = Ledger()
replica.ingest([Event.from_json(b) for b in bundle])   # buffers orphans
assert fold(replica).fingerprint() == state.fingerprint()
```

The fold never reads the wall clock: pass `now` (unix ms) to evaluate
invite expiry / delegation TTLs, omit it for a time-independent state.

## Structural admission (Ledger.add)

Signature verify → parents exist → HLC strictly exceeds every parent →
genesis rules (exactly one, self-signed). `add` is idempotent;
`ingest` accepts unordered batches (sync bundles) and resolves internally.
Anti-malleable parsing as in idkit: exactly one accepted byte form per
event, so an event has exactly one id.

## Tests

```bash
pytest tools/network/ledger/tests/
```

Property tests are hypothesis-style on plain pytest (no extra deps):
seeded random DAGs — concurrent branches, unauthorized events, revocation
races, invite claims — replayed into fresh replicas in shuffled orders
must produce bit-identical fingerprints, validity maps, and reasons (L1).
L2 (attenuation incl. invites/roles), L3 (revoke-beats-grant, permanence,
hash tiebreaks), L4 (cascade, explicit re-grant, multi-path survival),
invite lifecycle (demotion, races, claim_requires), rotation/rekey, and
adversarial suites (escalation laundering, revoke abuse, replica
divergence) are all pinned.

## v1 boundaries (deliberate)

- Role scopes are **non-delegable** (use `delegate` for onward grants).
- `member.rekey` policy is self-or-root; M-of-N trustee policies land in
  F7 (`approvals[]` is already carried and signature-checked).
- `checkpoint` is schema + authority only; state-hash verification and
  cold-join land with storage (F2).
- HLC-based expiry (invite `expiry`, delegate `ttl`) is advisory ordering
  metadata; revocation is the authoritative kill switch.
