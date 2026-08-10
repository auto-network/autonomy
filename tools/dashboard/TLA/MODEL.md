# Rollout-ingestion model — design notes

TLA+ model of the dashboard's rollout-file ingestion state machine as
specified by bead `auto-suvcp` (the FileTrack FSM: DISCOVERED /
CHARACTERIZING / STREAMING / IGNORED / CLOSED, with busy/dirty drain
ownership), checked by TLC via `run_tlc.py`.  The method follows the
Anchore job-framework model (`enterprise_ng` branch `jspilman/TLA`,
graph notes `c9258052-67e` / `523f9406-6ba`): model the one property
class that matters, write down every abstraction, and keep the model
honest with calibration switches that must rediscover every historical
failure.

The source of truth is `tools/dashboard/session_monitor.py`,
`tools/dashboard/session_harness.py`, and
`tools/dashboard/dao/dashboard_db.py`, plus the bead's pseudocode and
invariants.  When the observation/dispatch/drain/persistence order in
that code changes shape, this model must change in the same commit
(see README.md).

## Property classes

This machine's center of gravity is **liveness and message ordering**,
not lock cycles: the production failures are stalls (a session never
shows its transcript), lost signals, and identity races.  Checked:

- Safety: `NoChildAdoption` (identity), `BoundedDuplicates` (duplicates only after a failure event —
  crash, cancellation, restart — at most one in-flight read window each;
  exactly-once when failure-free; strict `NoDuplicates` remains the
  invariant for the RACE calibrations, where duplication without any
  failure is still forbidden), `OffsetCoherent` (acked offset vs. linked content),
  `ComposerSticky` (no lost update on `harness_state`), `SingleOwner`
  (drain gate), `TypeOK`.
- Liveness (weak fairness on the reliable machinery only):
  `EventuallyDrained` (anti-stall headline — `auto-0807-225218`
  violates it), `EventuallyLinked` (the row converges to the file the
  writer actually writes), `CharacterizingResolves` (bounded
  responsibility), `NoDurableStartingCard` (the registry is eventually
  truthful — no durable "Starting..." card over a full transcript).

## Two-layer state

- **Shared observable state** — the filesystem (`fExists`, `fLines`
  as complete-line counts, the writer's active file), the persisted
  `tmux_sessions` row (`rowPath`, `rowOffset`, `rowComposer`), the
  downstream deliveries (`consumed`), and the last registry broadcast
  (`regLinked`, `regCount`).
- **Per-actor state** — ingestion tracks per `(session, file)`, drain
  gates (`busy`/`dirty`/`drainReq`/`pubAfter`), and drain workers as
  their own processes with a program counter and locals
  (`snap`/`upTo`/`hs`) — the awaiter and the `to_thread` worker are
  separate, so cancellation semantics (A5) fall out structurally.

## Granularity

One model step per potentially-blocking boundary: every `await`, every
`to_thread` hop, every event delivery, every DB read/write.  Pure
computation between boundaries is fused.  The bead's "no await between
check and claim" and "the final dirty check/release/reschedule contains
no await" clauses are modeled as single atomic actions — they are
**assumptions the implementation must honor**, listed below.

## Channel semantics (the load-bearing decision)

- The inotify event channel is **edge-triggered and lossy before
  arming**: a write while no track is armed changes the file but queues
  nothing.  `IN_CREATE` is a FIFO queue (kernel order); `IN_MODIFY` is
  a coalescing per-file flag.  Once armed, delivery is reliable
  (kernel-queue overflow is abstracted; see below).
- The **reconciliation tick is level-triggered and reliable** and is
  the only fair recovery path: liveness must hold through it even if
  every edge event is lost.  Weak fairness on the tick actions models
  the always-scheduled 300 s loop; weak fairness is NOT given to the
  writer, crashes, cancellations, or restarts.
- Time is **nondeterministic enablement**: `DeadlinePass` may fire any
  time after a track starts characterizing; there is no clock.
  Consequently the model proves "eventually expires", not "expires
  within deadline + one tick" — interval sizing stays an engineering
  judgment (host review B3: size against container-under-load
  startup).

## Operation -> source map

| Model action | Source |
|---|---|
| `Register` / `epochSnap` | `_add_dir_watch` + `_scan_dir_for_existing_jsonls` (session_monitor.py:1809/1847) |
| `DeliverCreate` | `_handle_in_create` -> `_handle_container_create` (2714/2851) |
| `DeliverModify` | `_inotify_tailer_loop` MODIFY coalescing (2668-2706) |
| `ObserveOutcome` | `observe_rollout` (bead design; today `_handle_jsonl_appeared`, 1875) |
| `Promote` (CAS, close-superseded, broadcast suppression) | bead `promote_to_streaming`; today `resolve_session` + `_link_session_file` + broadcast at 1966-1971 |
| `ClaimDrain` / `ClaimContended` / dirty | bead `request_drain` (design; no equivalent today — the absence is CalNoDrainGate) |
| `WkReadRow`..`WkFinal` | bead `drain_as_owner`; today `_tail_one` (3449) + `_process_tail_entries` (2282) |
| `WkPersist` order switch | `update_tail_state` call inside `_tail_one` at 3545/3566 — **persist-before-process is the shipped order** (A4) |
| `ReconLateObserve` / `ReconRecheck` / `ReconExpire` | `reconciliation_tick` (3796); the unrestricted scan is finding N3's fix — today's code gates on `jsonl_path IS NULL` |
| `Restart` / `RecoverLinked` | `_on_startup` -> `_init_inotify` (1749, sync `def`) / `_recover_unresolved_sessions` (1662, `async def`) |
| `PollerSet` | `_screen_poll_loop` writing `harness_state.composer_ready` |

## Entry-context table (sync vs async — required to reach B2)

| Entry point | Context in code | In the model |
|---|---|---|
| `_init_inotify` + startup recovery of linked rows | sync `def`, no running loop (comment at 1966-1968 says callers broadcast after return; note `_recover_unresolved_sessions` itself is `async def` — the bead once quoted a stale comment claiming otherwise) | `RecoverLinked` passes `sync=TRUE`; under `SyncSchedulesDrain=FALSE` the busy claim happens and the drain task is never scheduled (B2) |
| watch_scan (`_scan_dir_for_existing_jsonls`) | sync `def` called from both sync and async callers | folded into `ReconLateObserve`/`DeliverCreate` observation (async), plus the `RetainUnknown=FALSE` drop switch for its no-retry defer |
| IN_CREATE / IN_MODIFY / reconciliation | async (event loop) | `sync=FALSE` |

## Deliberate abstractions

Recorded so nobody mistakes model silence for a checked guarantee:

- **Lines, not bytes.** `fLines` counts complete JSONL lines; the
  partial-trailing-line rule ("advance only to the last newline") is
  the definition of a line unit, so partial-line handling is by
  construction, not checked.
- **Kernel-queue overflow is not modeled.**  Once armed, MODIFY
  delivery is reliable-with-coalescing.  Overflow (IN_Q_OVERFLOW) is
  a real, rarer loss mode; the level-triggered reconciliation is the
  designed backstop, and the fixed design's liveness through recon
  (checked) is exactly the property that would absorb it.  IN_IGNORED
  / wd-reuse dispatch (host B6) is likewise not modeled — wd identity
  is abstracted to the (session, file) track key; the (wd, epoch)
  joint-identity rule from the review stands as an implementation
  requirement, not a checked property.
- **One directory per session** (container layout).  The shared-dir
  host layout is out of scope; A7 is modeled through the registration
  epoch (`createSeen`/`epochSnap`), not through multi-session dirs.
- **Graph append is folded into `WkProcess`** (one "downstream
  delivery" step).  The graph/viewer split (separate appender, eager
  sources) shares the offset regime and adds no distinct interleaving
  at this granularity.
- **The atomic claim and the atomic final sequence** (`ClaimDrain`,
  `WkFinal`) model the bead's await-free critical sections.  The
  implementation must actually be await-free there — including the
  CPython try-claim fast path the review flagged; the model assumes
  it and cannot check it.
- **`update_tail_state` cannot write lifecycle columns** (verified in
  dao code: no `state`/`startup_state` parameters), so lifecycle
  non-interference is by construction and not modeled at all.
  `STATE_AUTHORITY` does not appear in this model.
- **Late flush.**  A superseded main may receive `LateFlushBudget`
  further writes (nothing proves the old file goes quiet when the
  successor opens).  `sealedSet`/`sealedAt` are ghost observer
  variables — specification history, so they survive restart.
- **Budgets bound the environment**: at most one restart, one crash,
  one cancellation, one poller write per behavior (per config).  Every
  found violation needs at most one of each; raising budgets grows the
  space without new phenomena at these bounds.
- **Bounds argument**: one session (the machine is per-session;
  cross-session interaction exists only through the shared event loop,
  whose head-of-line concern T87 is modeled by the dirty-transfer
  switch), two files for identity/drain phenomena (main + sibling), a
  three-file chain only for rollover races (a CAS race needs
  loser + winner + a superseded row).  `MaxLines = 2` (1 for rollover
  configs): duplicate/lost-byte phenomena need one boundary between
  "some" and "more"; symmetry beyond that adds nothing.  Config split:
  the rollover configs run with `CancelBudget = 0` — cancellation
  mechanics are chain-agnostic and fully exercised in the two-file
  configs, and their one chain interaction (continuation retargeting)
  is also exercised by the crash budget; the cross-product would
  multiply the seal/late-flush state space for no new phenomena.

## Model-discovered findings (fed back into the design)

- **N1 — first-create trust is unsound over a non-empty directory.**
  The bead's observational-continuity rule ("the watch was armed
  before the file existed and this is its first observed CREATE")
  admits a subagent forked after a late arming: the fork's rollout IS
  the first create on a watch armed before it existed.  TLC violated
  `NoChildAdoption` in the green design at depth 9.  Fix modeled (and
  required of the implementation): birth trust additionally requires
  the registration snapshot to be **empty** — the watch must have been
  armed over an empty directory.  A7's pre-existing-paths barrier is
  not sufficient.
- **N2 — late multi-main resolution needs an ordering rule.**  With
  two mains of one chain observed late (restart / pre-registration
  rollover), promotion order is arbitrary; CAS-retry lets the OLDER
  file win last and the newest file close as superseded — a permanent
  wrong-file link with no recovery signal (`EventuallyLinked`
  violation).  Fix modeled: a late/ambiguous main promotes only if no
  VIABLE newer file exists (`NoNewerExists`); a characterized main
  with a viable newer file closes as superseded.  In production the
  succession order is available from rollout filename timestamps.
  **Refinement (second counterexample, found under the no-loss
  properties): "newer" must mean a chain-later file WITH CONTENT.**
  An empty successor may be quarantined and never linkable; treating
  its mere existence as supersession closes the contentful file and
  strands its lines forever (`PublishedUpToSeal` violation).
- **N3 — the reconciliation pending-scan must not be gated on
  unresolved rows.**  A restart between a rollover's CREATE and its
  link leaves a linked row, a lost kernel queue, and a successor file
  no mechanism ever observes (today's `get_sessions_needing_resolution`
  gates on `jsonl_path IS NULL`; the inode safety net does not fire —
  the old file is intact).  `EventuallyLinked` violation.  Fix
  modeled: reconciliation observes ANY existing file without a live
  track, matching the bead pseudocode's unrestricted
  `reconciliation_scan()`.
  **N3 refinement (also model-discovered):** in that unrestricted scan,
  the row's own linked path must re-observe with *persisted* provenance
  (re-attach), never as an ambiguous late observation — an ambiguous
  track for the linked file can never pass the promotion CAS
  (`rowPath = f` fails the rollover arm) and strands the file in
  CHARACTERIZING after a restart (green-liveness counterexample found
  by TLC at depth 17).
- **N4 — the drain owner must drain the row's current path, re-resolved
  each pass, not the file it was claimed for.**  With the A1 resolution
  (per-session gate) and a rollover mid-drain, the successor's drain
  request transfers into the dirty flag while the old file's worker owns
  the gate; the worker's continuation re-drains its own claimed file,
  consumes the signal, and releases — the successor's bytes are
  stranded with no owner, no request, and no event
  (green-liveness counterexample, 28 states).  This breaks the bead as
  reconciled: `drain_as_owner(track)` is track-bound while the gate
  became session-scoped.  Fix modeled: under the per-session gate the
  worker re-resolves `rowPath` at every read-row step ("drain the
  session, not the track"); the generation CAS at persistence is
  unchanged.  **Corollary (second counterexample, 35 states):** every
  rescheduled continuation — bounded-pass exhaustion, crash recovery,
  cancelled-worker stop — must likewise target the row's *current*
  path; a continuation scheduled for the worker's claimed-but-
  superseded file is a dead request against a CLOSED track.
- **V1 — the bead's first-resolution CAS clause is load-bearing
  (validation, not a gap).**  While iterating, the model briefly
  encoded the birth-trusted promote's CAS as a compare against the
  *current* row (a tautology) instead of against NULL.  TLC immediately
  produced a backward link: a queued CREATE delivered late — after
  reconciliation had already linked a successor — "rolled the row over"
  to the original file.  The bead's `if first_resolution and
  row.jsonl_path is not NULL: close_as_superseded` guard is exactly
  what prevents this; implementers should treat that clause as
  essential, not defensive boilerplate.  (Also a demonstration that the
  green suite catches model-fidelity errors, not just design errors.)
- **N5 — promoting the already-linked path is ALWAYS a re-attach.**
  The first-resolution/rollover CAS rightly refuses `rowPath = f`, so
  any path that leaves the linked file's own track in CHARACTERIZING
  (a deferred handover racing a direct promotion of the same file; a
  reconciliation re-observation) would churn that CAS forever — the
  linked file ends up permanently unpromotable while linked
  (green-liveness counterexample, 31 states).  Rule fed back into the
  design: every promotion of the file the row already links takes
  persisted-identity re-attach semantics (offset preserved, catch-up
  drain requested), in `observe_rollout`, in the handover commit's
  stale branch, and anywhere else a verified-main track can meet its
  own row.
- **A4 adjudication, superseded by operator decision (2026-08-10).**
  `CalOffsetAckFirst` (persist-then-process, the shipped order) loses
  the startup burst under one crash — still forbidden.  The operator
  accepted the other horn: rare duplicate lines in a crash-retry
  window.  Green configs therefore run `PersistOrder = "processFirst"`
  (publish-then-persist, no dedup layer) and prove `BoundedDuplicates`;
  `CalProcessFirstCrash` is repurposed as the documented pin of the
  accepted window.  Two FAILURE-FREE duplicate routes surfaced by the
  relaxation are forbidden by proven rules: the handover advances the
  persisted offset when it publishes the currently-linked file, and
  linking never rewinds publication (offset initializes at the
  already-published level).


## Ordered handover (operator decision, 2026-08-09)

Operator ruling: losing a superseded rollout's unread tail is NOT
acceptable (today's code and the bead as written both cede it), and the
guarantee must be proven.  The model now includes the required design:
a verified successor defers its link (`pendingLink`); predecessors are
made READY oldest-first — published to current end-of-file AND
final-checked ("sealed") — and the link commits only through a guard
that re-reads the filesystem, so the pre-advance final check is
structural.  Checked properties: `OrderedDelivery` (safety: nothing
publishes from a successor while any predecessor is neither fully
published nor sealed) and `PublishedUpToSeal` (liveness: every existing
main publishes up to its seal; the linked file in full).  `CalCedeTail`
restores the link-immediately/cede-the-tail behavior and must fail.

**Impossibility result (model-proven — the precondition IS the
theorem):** UNDER late flushes (a superseded file receiving a further
write, which this channel permits and nothing can fence), strict
no-loss AND strict global order are jointly unsatisfiable
(`LateFlushBudget` models the precondition): TLC
produced the trace — a late flush landing on an earlier file after a
later file already published.  Nothing can prove a file won't be
written after its successor opens.  What IS proven: every byte present
at a file's final pre-advance check is published; checks happen in
chain order before anything later publishes; bytes arriving after a
file's check are the explicit residual (`consumed >= sealedAt`, not
`= fLines`).  The operator's tick-reselect backstop suggestion was
separately rejected as over-design; kernel-queue overflow remains a
documented abstraction.

Implementation consequences for the bead: promotion of a rollover
successor performs (inside the serialized finalization, before the
link) a final catch-up drain of every not-yet-checked predecessor in
chain order, then advances; the drain is idempotent-by-line-index so
crash/restart re-walks are safe.

## Calibration switches

Every entry restores a design that failed in production or in
adversarial review, and must FAIL its config (see `calibration/`):

| Config | Restores | Expected finding |
|---|---|---|
| CalStartupStall | 80126ec1: defer w/o owner + attach w/o catch-up + link-time broadcast | temporal: durable "Starting..." card (the auto-0807-225218 incident) |
| CalFailOpen | pre-80126ec1 boolean classifier | NoChildAdoption (2026-05/2026-08 incidents) |
| CalOrdinalProvenance | T102 "first file needs no categorization" | NoChildAdoption after late scan |
| CalNoDrainGate | T84 no drain gate, snapshot offsets | NoDuplicates |
| CalSkipOnContention | T87 skip contended signal | EventuallyDrained (stranding) |
| CalEventDrivenDeadline | T110 deadline only in on_file_event | CharacterizingResolves |
| CalOffsetAckFirst | A4 shipped persist-before-process + crash | EventuallyDrained (lost burst) |
| CalProcessFirstCrash | the accepted crash-window (documented residual pin) | NoDuplicates (strict form — shows where the acceptance boundary is) |
| CalSyncEntryNoop | B2 sync claim, schedule() no-op | EventuallyDrained (busy pinned) |
| CalTerminalQuarantine | B3 terminal quarantine | EventuallyLinked (slow start unadoptable) |
| CalNaiveReobserve | A6 unconditional re-observation | CharacterizingResolves (livelock) |
| CalCancelPinned | B5 cancel skips release | EventuallyDrained |
| CalFinallyRelease | A5 finally-release + to_thread continues | NoDuplicates (double-tail) |
| CalRMWHarness | B7 harness_state RMW across await | ComposerSticky |
| CalNoEpochBarrier | A7 watch-level continuity | NoChildAdoption |
| CalPerPathGates | A1 per-(s,f) gates, no gen CAS | OffsetCoherent |
| CalRolloverCAS | B4 CAS loser terminal | EventuallyLinked (newest closed) |
| CalCedeTail | today's rollover: link immediately, cede the old tail | OrderedDelivery |

Green configurations: `GreenCore` / `GreenLive` (main + sibling, all
budgets on), `GreenRollover` / `GreenRolloverLive` (three-file chain,
restart enabled — the N2/N3 regression pin).

A note on applying the impossibility result: it is conditional.  On a
channel where late flushes are actually fenced (e.g. the writer
provably closes the old file before creating the successor), both
properties are simultaneously achievable and this theorem does not
apply.  Codex rollouts give no such fence, so seal-ordered delivery is
the strongest guarantee available here — do not spend a revision trying
to recover both.
