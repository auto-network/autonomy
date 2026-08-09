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

- Safety: `NoChildAdoption` (identity), `NoDuplicates` (exactly-once
  delivery), `OffsetCoherent` (acked offset vs. linked content),
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
  "some" and "more"; symmetry beyond that adds nothing.

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
  successor exists (`NoNewerExists`); a characterized main with an
  existing successor closes as superseded.  In production the
  succession order is available from rollout filename timestamps.
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
- **A4 adjudication (design question left open by the review).**
  `CalOffsetAckFirst` (persist-then-process, the shipped order)
  violates `EventuallyDrained` under one crash: the offset is acked
  past bytes that were never delivered.  `CalProcessFirstCrash`
  (process-then-persist) violates `NoDuplicates` under one crash:
  the retry re-delivers.  Only idempotent downstream publication
  (dedup by line index/offset before persist) satisfies both — the
  green configs run `PersistOrder = "idempotent"`, which is therefore
  a REQUIREMENT on the implementation, not a free choice.

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
| CalProcessFirstCrash | A4 naive alternative + crash | NoDuplicates (re-delivery) |
| CalSyncEntryNoop | B2 sync claim, schedule() no-op | EventuallyDrained (busy pinned) |
| CalTerminalQuarantine | B3 terminal quarantine | EventuallyLinked (slow start unadoptable) |
| CalNaiveReobserve | A6 unconditional re-observation | CharacterizingResolves (livelock) |
| CalCancelPinned | B5 cancel skips release | EventuallyDrained |
| CalFinallyRelease | A5 finally-release + to_thread continues | NoDuplicates (double-tail) |
| CalRMWHarness | B7 harness_state RMW across await | ComposerSticky |
| CalNoEpochBarrier | A7 watch-level continuity | NoChildAdoption |
| CalPerPathGates | A1 per-(s,f) gates, no gen CAS | OffsetCoherent |
| CalRolloverCAS | B4 CAS loser terminal | EventuallyLinked (newest closed) |

Green configurations: `GreenCore` / `GreenLive` (main + sibling, all
budgets on), `GreenRollover` / `GreenRolloverLive` (three-file chain,
restart enabled — the N2/N3 regression pin).
