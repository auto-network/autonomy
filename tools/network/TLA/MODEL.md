# Relay tunnel pools — abstraction ledger and findings

This model covers the guaranteed auto.network fallback path. The system's
preferred data path is direct viewer-to-dashboard connectivity after the tiny
bootloader/rendezvous phase. A successful direct session never enters this
relay-pool state machine; a failed or lost direct session does.

## Intended machine

There is no singular owner of an org. Every successfully authenticated tunnel
is a member of that org's pool:

- registration adds the exact tunnel;
- disconnect removes the exact tunnel;
- new viewer admission chooses a least-active-channel member with remaining
  capacity;
- an admitted viewer stays pinned, so changing load does not cause migration
  or tunnel competition;
- loss of one member reopens only the viewers pinned to it;
- after a relay restart, every connector retries and rejoins the pool.

Registration proves authentication, not retry health. `Register` preserves the
connector's accumulated backoff. `MarkStable` is the separate abstraction of
one authenticated `MaxBackoff` service interval; only a later disconnect of a
stable tunnel uses `MinBackoff` for its next retry. A connector that repeatedly
registers and disconnects before `MarkStable` therefore reaches and stays at
`MaxBackoff` instead of resetting to the minimum on every hello.

The current `TunnelHub` dict assignment and 4409 replacement behavior remains
in the model only behind `PoolRegistration = FALSE`. That negative
configuration is a calibration: TLC must rediscover the production lasso or
the model/runner is not trustworthy.

## Load-bearing initial state

`Init` starts every configured tunnel as an independent connector. Multiple
connectors for one org are normal capacity, not an exceptional deployment
state. Assuming connector uniqueness would erase both the incident and the
desired pool behavior.

`Capacity[t]` is concurrent viewer capacity on a tunnel. `OpenViewer` chooses
a member with minimum `Load(t)` among those below capacity. Ties are
nondeterministic; no tie-breaking protocol is required for correct shedding.

## Anycast and multiple relay servers

`TunnelRelay[t]` records the relay node on which an outbound connector
terminated. `ViewerIngress[v]` records the node selected for a viewer, such as
by an anycast route. Admission deliberately ranges over the complete org pool,
not only tunnels terminating at the ingress node.

`AnycastPoolGreen.cfg` makes that assumption observable: the viewer enters
`r2`, the only tunnel terminates on `r1`, and eventual viewer admission still
has to pass. Therefore an implementation needs one of:

1. a shared/replicated directory from org to live tunnel endpoints, followed by
   an internal relay-to-relay hop; or
2. equivalent routing that brings viewer and selected tunnel to the same
   forwarding process.

Anycast alone supplies neither. The model treats lookup/handoff as atomic and
reliable; it does not claim a particular directory design, consensus system,
or cross-relay transport is already implemented.

Multiple connector sockets to the same anycast name may terminate on different
relay nodes, but ordinary ECMP does not guarantee diversity. Diversity and
node steering are deployment questions outside this state machine.

## Fairness

Connector registration/retry, timer progress, and viewer admission are weakly
fair. Tunnel disconnect and relay restart are bounded, unfair environment
actions. Without fair connector actions, an infinite stutter could hide a
broken retry path; without bounded failures, eventual stable service would be
unprovable for any algorithm.

## Checked results

1. **Healthy same-org tunnels coexist.** Registration is set insertion and
   never evicts a healthy peer (`NoHealthyEviction`).
2. **Admission naturally sheds load.** A viewer cannot be assigned to a tunnel
   while another available member is less loaded (`AdmissionUsesLeastLoad`).
   Ties remain free and viewers are not rebalanced after admission.
3. **Capacity is additive.** Every tunnel retains its own cap and the pool can
   admit up to their sum (`CapacityRespected`).
4. **Failure is local.** Disconnect removes exactly one tunnel and only its
   assigned viewers return to admission. Other assignments remain unchanged.
5. **Restart recovers.** After the bounded restart/disconnect budget is spent,
   all connectors eventually rejoin and all provisioned viewers reopen.
6. **Cross-relay admission is permitted.** An anycast ingress can use a tunnel
   terminating elsewhere; the necessary directory/handoff is an explicit
   implementation obligation.
7. **Current last-writer replacement still livelocks.** TLC's calibration
   trace alternates the two connectors forever: each retry displaces the other.
   Stability-gated backoff bounds the attempt rate but cannot repair singular
   ownership; cooperative pool membership removes the cycle.
8. **Arbitrary admission permits avoidable skew.** TLC finds a trace where a
   new viewer chooses a loaded tunnel while an idle member exists.
9. **Hello alone never resets retry health.** The transition that authenticates
   a tunnel leaves `backoff` unchanged. Only a service interval represented by
   `MarkStable` earns a minimum-delay reconnect after later loss. This preserves
   prompt ordinary recovery without letting a post-hello flap hammer the relay.

## Deliberate abstractions

- Tunnel authentication is represented by eligibility to take `Register`; key
  verification and certificate details do not affect pool membership after a
  hello succeeds. Useful lifetime is reduced to the separate `MarkStable`
  transition; the model does not count wall-clock seconds. TLC therefore
  validates the qualitative separation and its pool consequences, while the
  runtime's exact `served_for >= max_backoff` threshold is established by the
  deterministic connector tests.
- Viewer payloads, encryption, stream retention, and byte backpressure are
  omitted. `PERFORMANCE.md` treats those implementation costs separately.
- A viewer is assigned once per socket. Transparent live migration is not
  modeled and is not required by the algorithm.
- Selection observes active viewer count atomically. Distributed implementations
  can use slightly stale load without compromising membership safety, but the
  model does not quantify skew from stale observations.
- The cross-relay directory/handoff, partitions between relay nodes, and stale
  membership leases are not modeled. They require a later distributed-system
  model before an anycast deployment is claimed safe.
- Jitter is nondeterminism, not probability. It can spread reconnect load but
  is not an ownership or admission correctness mechanism.

## Served-ack journal retirement (`AckFloor.tla`)

Models the fleet-sync journal floor shipped in `catalog.py`
(`record_served_ack`, `acknowledged_journal_floor`, `prune_acknowledged`)
together with the continuity decision in `fleet_relay_sync`. The safety
chain: a resume trail resolves only while its transaction row exists, so a
recorded acknowledgement never exceeds the peer's truly consumed prefix
(`AckSoundness`); the prune floor is the minimum acknowledgement over the
FULL active roster; therefore every peer's unconsumed suffix stays servable
by retained deltas, or its absence is visible as a journal gap and the
unresolvable trail is answered with a checkpoint (`Recoverable`). Fairness
gives the liveness half: an authored frame is eventually retired
(`EventuallyRetired`) — the journal is bounded when the roster keeps
pulling.

Three calibrations prove each mechanism is load-bearing, not incidental:

- `calibration/TimestampPrune.cfg` — acknowledgements and the floor keyed
  by authored timestamp instead of transaction ref. Remote imports
  interleave low stamps behind high refs, so a timestamp floor retires an
  unconsumed frame; on the direct path (no checkpoint fallback) the peer's
  suffix becomes unservable. This is why `local_watermark` stores a ref.
- `calibration/SoloPrune.cfg` — pruning without every active peer's
  acknowledgement. A concurrently enrolled machine with nothing consumed
  loses the replay it was owed. This is why an empty or partially
  acknowledged roster yields no floor.
- `calibration/NoAckResetOnInstall.cfg` — recorded acknowledgements kept
  across a checkpoint install. The staging database renumbers the id
  space, stale acks collide with fresh refs, and pruning retires new
  frames and their rows together — no gap signal survives, so not even
  the checkpoint rescue can see the loss. This is why `_copy_peer_state`
  nulls `local_watermark`, and it is the one variant that fails silently.

Deliberate abstractions: one serving machine's perspective (each machine
runs this protocol symmetrically); checkpoint content is atomic and always
available; the install's id renumbering is modeled as a clean restart of
the ref space; restore-from-backup is subsumed by the install action (the
same rewind shape with the same reset). Peer-side receipt bookkeeping and
the transports are not modeled.

## Pre-serving refusal and failover (auto-s81lo, auto-d8if0)

A connected member may end a viewer's channel before serving a byte: it
lacks the grant, it is unarmed, or key resolution refused. The relay then
tries the next candidate in the same dial, never the refusing member again,
and gives up after `FAILOVER_MAX_CANDIDATES` with the most specific refusal
code. `Refuse(t, v)` models exactly that: the tunnel stays in the pool (a
refusal is about the link, not the member, so `PoolCoherent` and
`NoHealthyEviction` are untouched), the viewer returns to admission with
`t` added to `refused[v]`, and admission ranges over `Candidates(v)`, the
live members with capacity that have not refused this dial. `RefuseBudget`
bounds refusals as an unfair environment act, like disconnects.

Two consequences shape the properties. `EventuallyEveryViewerAssigned` is no
longer the claim: a viewer every candidate refused is the honest verdict
the relay returns, not a stuck dial. The claim is
`EventuallyEveryServableViewerAssigned`: a viewer that still has a serving
candidate is eventually seated on one. `RefusalsRespected` is the safety
half: an open viewer is never on a tunnel that refused its dial, and a dial
never exceeds the candidate bound.

The candidate bound is a bound on the DIAL: `OpenViewer` admits a viewer
only while fewer than `FailoverMaxCandidates` tunnels have refused it, so
an exhausted dial ends with the honest close code whatever candidates
remain. `calibration/FailoverCap1.cfg` is PoolGreen with the bound at one:
one refusal ends the dial while other members could serve, and TLC must
find `EventuallyEveryServableViewerAssigned` violated there. Until 2026-09-20
the bound sat on `Refuse` instead, which let a refused viewer re-enter
admission and made the calibration pass vacuously.

Not modeled, deliberately: the hello replay to the next candidate and the
open-budget timer. Both are transport mechanics below the ownership
question this model answers.

## Fleet sync write floors (`FleetSyncWriteFloors.tla`)

The record's rules for write floors (bead auto-mmwgu, Propagation; decision
note graph://d9153c5a-76e O-K), checked against the constitution's principle 1
(graph://6ad52a52-f75): a watermark is a contiguous cursor, a number a machine
can only claim by holding the data.

Model: every machine is the origin of its own rows; a row is its timestamp; a
write floor is sealed at max(last write, now). A pull is two steps, because
the server builds it in two steps (`fleet_sync_scheduler.py`: transaction
pages are streamed first; the write floor frames are read on a fresh
connection after the last page). The record's rules are taken verbatim: the
watermark is the greater of cursor and held floor (R1); the server sends every
floor it holds above the puller's watermark (R2); the puller stores a received
floor and moves its cursor to it (R3); floors are relayed unchanged (R4).

Result (`FleetSyncWriteFloorsRecord.cfg`, three machines, one row each):
`CursorHoldsData` is violated in five states. Puller b begins a pull from
origin a while a has written nothing. Then a writes row 1 and seals floor 2.
b's reply finishes: zero rows, then a's floor 2, read after the pages. b's
cursor for a becomes 2 while b holds no row of a. b's watermark for a is now
2, so a never serves row 1 again: `Converges` fails too.

The record does not say that the floors in a reply are fixed at the same
instant as its rows, and the code reads them later. Under quarantine the same
claim is made without a two-step reply: R1 claims a held floor as the
watermark while the cursor waits below a quarantined row. Both are the same
defect: a floor claimed without the rows below it.

The correction (`Corrected = TRUE`, `FleetSyncWriteFloorsCorrected.cfg`),
three rules that replace the record's Propagation paragraph:

- C1 A server's reply is one snapshot, and everything it sends about a
  machine is bounded by its own cursor for that machine: the rows at or below
  its cursor, and the machine's floor only when its cursor reaches it. What a
  server cannot claim itself, it does not pass on.
- C2 A puller moves its cursor for a machine to a received floor only once
  every row of that machine in the same reply has resolved without
  quarantine. Zero rows resolves, so an idle machine's floor moves the
  cursor. The floor is stored either way and relays as before.
- C3 The pull request's watermark is the cursor alone.

Result: `CursorHoldsData` and `Converges` both hold, at one row per writer
(3,431 distinct states) and at two rows with two seals
(`FleetSyncWriteFloorsWide.cfg`, 37,721 distinct states). Quarantine is in
the model: a delivered row may be held unresolved; the cursor waits below it;
a server serves only resolved rows; a row already resolved is a no-op when
delivered again.

The wider bound found a second hole before C1 took its final form. With
floors handled but rows unbounded, a relay holding row 1 in quarantine and
row 2 applied served row 2 to a puller whose watermark was 0; the puller's
cursor advanced to 2 across a gap it could not see. The record's O-G rule
lets a server serve every applied row above the puller's watermark, past its
own cursor. Bounding rows by the server's cursor closes it; the floor rule is
the same bound applied to floors.

Deliberate abstractions: rows apply in one step; one reply in flight at a
time, so pulls take strong fairness (each pair keeps pulling, as the
scheduler's rounds do); one origin writes at most one row and seals twice,
which reaches every ordering that matters.

## Org admission and checkpoint adoption (`OrgAdmission.tla`)

Bead auto-qrmlg.5; ceremony record graph://cde6c8c6-041. The founder's
ledger is linear (a claim on stale heads is refused, claim_service.py:
170-171), so an event is named by the joiner it admits and members at a
head are a prefix. Each current rule C1-C8 in the module header cites the
code it abstracts.

Checked results:

1. **The built rules deadlock.** TLC's trace (`OrgAdmissionCurrent.cfg`):
   j1 admitted, j1 bootstraps holding {g, j1}; j2 admitted; the founder
   checkpoints at head j2; j1 cannot fold at j2 so adopts nothing, and no
   pull is admitted without an adopted checkpoint including it. Nothing
   later delivers j2 to j1: a pull needs the adoption first. This holds
   even when the founder signs on again infinitely often (fair checkpoint).
2. **Adopt-by-verification and checkpoint-at-admission restore liveness**
   without assuming any later human ceremony (`OrgAdmissionProposed.cfg`),
   and with admit-on-approval also under an approval role
   (`OrgAdmissionProposedApproval.cfg`, the planner's founder 2 / joiner 2
   schedule). Executability is checked: full sync is reachable.
3. **Safety** (`NoPullWithoutInclusion`, `OutsiderNeverPulls`,
   `AdoptedIsAuthentic`, `AdmittedWereApproved`) holds under both rule sets
   against a registry that serves one forged roster naming an outsider.
4. **Each proposed element is load-bearing.** Checkpoint-at-admission
   without adopt-by-verification strands an earlier member at the older
   seq (the verifier holds one adopted record, org_sync_channels.py:189-193);
   adopt-by-verification without checkpoint-at-admission depends on a later
   founder sign-on; without admit-on-approval admission waits on the
   joiner's finalize ceremony; adopt-by-verification without the signer
   check admits the outsider's pull.

Deliberate abstractions: signatures are the `auth` bit (the signer-in-
previous-checkpointers-root check and the prev chain are one predicate);
the prover's inclusion path under adopt-by-verification is granted to any
member (today it is folded locally at the head, org_sync_channels.py:
173-179, 197-205, so the implementation must deliver it with the
checkpoint: unproven which carrier); the reprove window is omitted (it
only extends acceptance of a seq the verifier has adopted); org channel
certificates, addresses and relay slots are assumed present (the keyreg
planner covers them); the joiner's own appends are omitted (adoption does
not require the head to be a current head, only present).

## Existing members across a checkpoint advance (bead auto-qrmlg.11)

`OrgAdmission.tla` adds, behind constants that leave every auto-qrmlg.5
configuration's verdict unchanged (`PathRule = "granted"`):
- **Path construction (C9).** The prover folds its local ledger at the
  record's head (org_sync_channels.py:173-179, 197-205), so it needs every
  event up to that head.
- **Delivery (K3).** Events and generation grants reach a joiner only by an
  admitted pull, its own append, or the join install.
- **Production acceptance.** The verifier accepts its single newest adopted
  seq only (fleet_org_channel.py:286-292, org_sync_channels.py:189-193).
  The reprove window has no production caller.
- **Removals and time.** Each removal re-keys the storage generation. Time
  is modeled for the bounded window.
- **Event order.** An optional fixed order of the founder's events bounds
  the three-joiner runs (`OrgAdmissionX3.tla`: admit j1, j2, j3, then
  remove j2, then j3). Member actions stay free, so j1 can lag across both
  removals.

Pull history is kept as per-joiner sync flags, reset whenever the founder
adopts, plus sticky violation flags. `OrgAdmissionLeaves.tla` checks the
acceptance rule alone (safety) against rekeyed leaves and re-admitted
personas. Leaves are current persona keys (membership_commitment.py:185-190).
A persona key derives from root + genesis, so a re-admitted persona that did
not rekey returns with the same leaf.

Results, P1 + P2 (run_tlc.py). Liveness is `EveryAdmittedMemberPulls`; "x3"
means three joiners and two consecutive removal re-keys.

| Rule | Liveness | Safety |
|---|---|---|
| production (fold, newest only) | violated | — |
| A path in record / B registry path (reference only: violate K1) | clean | clean |
| C bounded predecessor window | violated (window expires) | — |
| C unbounded predecessor | clean | `RemovedExcluded` violated; `RekeyedOldKeyExcluded` violated |
| D1 / D2 as specified | violated (no grant before the first pull) | — |
| D1 / D2 + grant in join bundle, Δ under previous generation | clean with two joiners; **violated x3** | clean against wrong Δ and replayed records |
| D1 / D2 + bundle, Δ under the post-rekey generation | violated | — |
| E, one predecessor | violated (member two records behind) | — |
| E-any, any older authentic record, prover in verifier's newest set | clean, clean x3 | `RekeyedOldKeyExcluded` holds; `ReAdmitAfterRemoval` **violated** |
| **E-any-adm**: E-any, and the record is at or after the prover's current admission | clean, clean x3 | `RekeyedOldKeyExcluded` and `ReAdmitAfterRemoval` hold; non-vacuous (an older-record proof is admitted) |
| E-any-adm with today's fold-only adoption (no P1) | violated | — |

D1 without the members_root recomputation violates
`ReconstructedIsCommitted`. D1 without monotone adoption violates
`NoRegression`. Executability holds for D1, D2, E-any and E-any-adm.

Liveness assumption of E-any / E-any-adm (from the fairness in `Spec`): a
lagging member is eventually dialed by, or dials, a node that holds every
event up to the newest head (the founder in these runs). Its pulls from
that node are weakly fair. It has adopted, by verification (P1), a record
under which it can verify that node's proof.

Traces:
- **production.** j1 is admitted and installs holding {1}. j2 is admitted,
  and the P2 checkpoint is at head 2. j1 adopts by verification without
  event 2, cannot fold its rider, and the founder accepts only its newest
  record.
- **C bounded.** The founder pulls from j1 under s2 inside the window. Two
  ticks expire the window before j1 pulls from the founder.
- **C unbounded.** j1 is admitted, installed and removed. The founder still
  accepts j1's proof under the pre-removal record.
- **D1 as specified.** j1 installs holding events 1-2 and j2 is removed (s4).
  j1 holds no generation grant, so it cannot open Δ4.
- **D1/D2 + bundle, x3.** j1 rebuilds through record 5 (j2 removed) with
  generation 0. Δ6 (j3 removed) is under generation 1. That generation's
  grant reaches j1 only by a pull from a node on record 6, which j1 cannot
  make: the generation-grant custody chain.
- **post-rekey key.** j1 holds generation 0; Δ4 is under generation 1.
- **E.** j2 knows only s2. j1 is admitted (s3) and removed (s4). The
  founder accepts s4 or s3 only.
- **E-any, re-admission.** p1 is admitted (record 2), removed (record 3) and
  re-admitted (record 4). Its unchanged leaf proves under the pre-removal
  record 2, and it is admitted.
- **E-any-adm, fold-only adoption.** j1 installs holding {1}. j2 is admitted
  and removed before j1 adopts. The registry's current record is beyond
  j1's ledger, so j1 adopts nothing and cannot verify the founder in the
  mutual hello.
