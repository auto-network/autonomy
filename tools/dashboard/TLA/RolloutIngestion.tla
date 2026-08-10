--------------------------- MODULE RolloutIngestion ---------------------------
(***************************************************************************)
(* Model of the dashboard rollout-file ingestion state machine specified   *)
(* by bead auto-suvcp, together with switch-restorable broken designs      *)
(* (production incidents and adversarial-review findings).  See MODEL.md   *)
(* for the abstraction rationale, the operation->source map, and the       *)
(* entry-context table.  Checked by TLC via run_tlc.py.                    *)
(*                                                                         *)
(* Green configurations (all switches at the fixed design) must check      *)
(* clean.  Each calibration configuration flips switches back to a design  *)
(* that failed in production or review, and TLC must find the              *)
(* corresponding violation on its own.                                     *)
(*                                                                         *)
(* Channel semantics (the load-bearing modeling decision): the inotify     *)
(* event channel is EDGE-TRIGGERED and lossy-before-arming — a write while *)
(* no track is armed changes state but queues no signal.  IN_CREATE is a   *)
(* FIFO queue (kernel order); IN_MODIFY is a coalescing per-file flag.     *)
(* The reconciliation tick is LEVEL-TRIGGERED and reliable (weak fairness) *)
(* — liveness must hold through it even if every edge event is lost.       *)
(***************************************************************************)
EXTENDS Naturals, Sequences, FiniteSets, TLC

CONSTANTS
    SESSIONS,           \* dashboard sessions, e.g. {"s1"}
    FILES,              \* rollout files, e.g. {"m1","c1"}
    FKind,              \* [FILES -> {"main","sub"}] ground-truth identity
    FSession,           \* [FILES -> SESSIONS] whose harness writes the file
    FSucc,              \* [FILES -> FILES \cup {"none"}] rollover successor
    FFirst,             \* [SESSIONS -> FILES] first main file of a session
    MaxLines,           \* per-file complete-line budget (line units)
    \* ---- design switches: fixed value = the auto-suvcp design ----------
    AttachCatchUp,      \* TRUE  | FALSE: 80126ec1 attach without catch-up
    RetainUnknown,      \* TRUE  | FALSE: watch_scan defer without an owner
    TriStateClassifier, \* TRUE  | FALSE: pre-80126ec1 boolean fails open
    ContinuityProvenance, \* TRUE | FALSE: T102 first-observed trusted
    DrainGate,          \* TRUE  | FALSE: T84 no single-owner drain
    DirtyTransfer,      \* TRUE  | FALSE: T87 contended signal skipped
    TimeDrivenDeadline, \* TRUE  | FALSE: T110 deadline only on file events
    SuppressPreDrainBroadcast, \* TRUE | FALSE: link-time linked+zero broadcast
    PersistOrder,       \* "idempotent" | "processFirst" | "persistFirst" (A4)
    SyncSchedulesDrain, \* TRUE  | FALSE: B2 sync claim, schedule() no-ops
    ReobserveMode,      \* "progress" | "never" (B3) | "always" (A6)
    CASRetry,           \* TRUE  | FALSE: B4 rollover CAS loser terminal
    PerSessionGate,     \* TRUE  | FALSE: A1 per-(s,f) gates, no gen CAS
    EpochBarrier,       \* TRUE  | FALSE: A7 no registration-epoch snapshot
    MergeHarnessAtWrite,\* TRUE  | FALSE: B7 harness_state RMW clobber
    GateReleaseOnCancel,\* "guarded" | "finally" (A5) | "none" (B5)
    OrderedHandover,    \* TRUE  | FALSE: rollover cedes the old file's tail
    WORKERS,            \* drain worker slots per session, e.g. {"wA","wB"}
    \* ---- environment budgets -------------------------------------------
    RestartBudget, CrashBudget, CancelBudget, PollerBudget,
    LateFlushBudget     \* writes landing on a SUPERSEDED main (late flush)

NoFile    == "none"
MaxPasses == 2                  \* bounded consecutive dirty passes

ASSUME PersistOrder \in {"idempotent", "processFirst", "persistFirst"}
ASSUME ReobserveMode \in {"progress", "never", "always"}
ASSUME GateReleaseOnCancel \in {"guarded", "finally", "none"}

VARIABLES
    \* -- filesystem + writer (shared observable state) --
    fExists, fLines, wActive,
    \* -- watches / event channel --
    registered,     \* sessions with an armed directory watch
    epochSnap,      \* [SESSIONS -> SUBSET FILES] existing at registration
    createSeen,     \* [SESSIONS -> BOOLEAN] a CREATE was already delivered
    evCreate,       \* FIFO queue (Seq) of files with undelivered IN_CREATE
    evMod,          \* SUBSET FILES: undelivered coalesced IN_MODIFY
    \* -- per-file ingestion tracks + drain gates (in-memory) --
    track,          \* [SESSIONS \X FILES -> [st, prov, expPrev, dl, closeL]]
    busyS, dirtyS,  \* per-session gates      (PerSessionGate = TRUE)
    busyF, dirtyF,  \* per-(session,file) gates (PerSessionGate = FALSE)
    drainReq,       \* [SESSIONS \X FILES -> BOOLEAN] pending drain request
    pubAfter,       \* [SESSIONS -> BOOLEAN] publish registry after catch-up
    cancelRel,      \* [SESSIONS -> BOOLEAN] guarded post-cancel release due
    \* -- drain workers (own processes; the awaiter is separate) --
    wk,             \* [SESSIONS \X WORKERS -> [pc, file, snap, upTo, hs,
                    \*                          passes, cancelled]]
    \* -- ordered handover (in-memory): a verified successor whose
    \*    predecessors still have unpublished lines waits here --
    pendingLink,    \* [SESSIONS -> FILES \cup {NoFile}]
    pendingExp,     \* [SESSIONS -> FILES \cup {NoFile}] CAS expectation
    \* -- persisted DB row (tmux_sessions) --
    rowPath, rowOffset, rowComposer, composerEverSet,
    \* -- operator-visible outcomes --
    consumed,       \* [FILES -> Nat] line-processings delivered downstream
    regLinked, regCount,
    \* -- ghost history (specification observers, survive restart) --
    sealedSet,      \* files whose pre-advance final check has happened
    sealedAt,       \* [FILES -> 0..MaxLines] lines present at that check
    \* -- environment budgets --
    restartsLeft, crashesLeft, cancelsLeft, pollsLeft, lateFlushLeft

vars == << fExists, fLines, wActive, registered, epochSnap, createSeen,
           evCreate, evMod, track, busyS, dirtyS, busyF, dirtyF, drainReq,
           pubAfter, cancelRel, pendingLink, pendingExp, wk,
           rowPath, rowOffset, rowComposer,
           composerEverSet, consumed, regLinked, regCount,
           sealedSet, sealedAt,
           restartsLeft, crashesLeft, cancelsLeft, pollsLeft,
           lateFlushLeft >>

envVars  == << restartsLeft, crashesLeft, cancelsLeft, pollsLeft,
               lateFlushLeft >>
fsVars   == << fExists, fLines, wActive >>
chanVars == << registered, epochSnap, createSeen, evCreate, evMod >>
rowVars  == << rowPath, rowOffset, rowComposer, composerEverSet >>
obsVars  == << consumed, regLinked, regCount >>

TrackInit == [st |-> "none", prov |-> "na", expPrev |-> NoFile,
              dl |-> FALSE, closeL |-> 0]
WkInit    == [pc |-> "idle", file |-> NoFile, snap |-> 0, upTo |-> 0,
              hs |-> FALSE, passes |-> 0, cancelled |-> FALSE]

(***************************************************************************)
(* Helpers                                                                 *)
(***************************************************************************)

\* Classification over the available prefix: the session_meta header is the
\* first complete line, so zero complete lines is unclassifiable.
Classify(f) == IF fLines[f] = 0 THEN "unknown" ELSE FKind[f]

\* What an observer acts on: the tri-state classifier never fails open; the
\* historical boolean classifier treated not-provably-sub as main.
EffClassify(f) ==
    IF TriStateClassifier THEN Classify(f)
    ELSE IF Classify(f) = "sub" THEN "sub" ELSE "main"

GBusy(s, f)  == IF PerSessionGate THEN busyS[s] ELSE busyF[<<s, f>>]
GDirty(s, f) == IF PerSessionGate THEN dirtyS[s] ELSE dirtyF[<<s, f>>]

SetBusyS(s, v)   == IF PerSessionGate THEN [busyS EXCEPT ![s] = v] ELSE busyS
SetBusyF(s,f,v)  == IF PerSessionGate THEN busyF ELSE [busyF EXCEPT ![<<s,f>>] = v]
SetDirtyS(s, v)  == IF PerSessionGate THEN [dirtyS EXCEPT ![s] = v] ELSE dirtyS
SetDirtyF(s,f,v) == IF PerSessionGate THEN dirtyF ELSE [dirtyF EXCEPT ![<<s,f>>] = v]

TrackArmed(s, f) == track[<<s, f>>].st \in {"char", "stream"}

\* N4 corollary: every rescheduled continuation (bounded-pass exhaustion,
\* crash, cancelled-worker stop) targets the row's CURRENT path under the
\* per-session gate — the claimed file may have been superseded meanwhile.
ContinueTarget(s, f0) ==
    IF PerSessionGate /\ rowPath[s] # NoFile THEN rowPath[s] ELSE f0

\* Chain order: g strictly precedes f in a session's rollover succession
\* (bounded to three-file chains, matching the scenarios).
ChainBefore(g, f) ==
    /\ g # f
    /\ \/ FSucc[g] = f
       \/ FSucc[g] # NoFile /\ FSucc[FSucc[g]] = f

\* Predecessors of f that exist on disk with unpublished lines.
HasIncompletePred(f) ==
    \E g \in FILES :
        g \in fExists /\ ChainBefore(g, f) /\ consumed[g] < fLines[g]

\* A predecessor is READY once it is published to its current end-of-file
\* AND its final check has been recorded (sealed).  The handover walk
\* makes predecessors ready oldest-first, so no later file publishes
\* before every earlier file's check.
PredReady(g) == consumed[g] = fLines[g] /\ g \in sealedSet

HasUnreadyPred(f) ==
    \E g \in FILES :
        g \in fExists /\ ChainBefore(g, f) /\ ~PredReady(g)

OldestUnreadyPred(f) ==
    CHOOSE g \in FILES :
        /\ g \in fExists /\ ChainBefore(g, f) /\ ~PredReady(g)
        /\ \A q \in FILES :
             (q \in fExists /\ ChainBefore(q, g)) => PredReady(q)
AnyArmed(f)      == \E s \in SESSIONS : TrackArmed(s, f)
Max(a, b)        == IF a >= b THEN a ELSE b
FreeSlot(s)      == \E w \in WORKERS : wk[<<s, w>>].pc = "idle"
PickSlot(s)      == CHOOSE w \in WORKERS : wk[<<s, w>>].pc = "idle"

\* Someone watches this file's directory (per-session container dirs:
\* exactly the owning session).
DirWatched(f) == FSession[f] \in registered

\* Finding N2 (model-discovered): a late-provenance main may promote only
\* if no VIABLE newer rollout exists — otherwise late multi-main
\* resolution can settle the row on a superseded file and strand the
\* newest one.  Refinement (second counterexample): "newer" must mean a
\* chain-later file WITH CONTENT — an empty successor is not yet viable
\* (it may be quarantined and never linkable), and treating it as a
\* superseder strands the contentful file's lines forever.
NoNewerExists(f) ==
    ~\E g \in FILES :
        ChainBefore(f, g) /\ g \in fExists /\ fLines[g] > 0

\* Can (s, f) be (re-)observed?  Terminal tracks re-open only per
\* ReobserveMode; live tracks are idempotently skipped by the callers.
Observable(s, f) ==
    LET tr == track[<<s, f>>] IN
    \/ tr.st = "none"
    \/ /\ tr.st \in {"ign", "closed"}
       /\ \/ ReobserveMode = "always"
          \/ /\ ReobserveMode = "progress"
             /\ fLines[f] > tr.closeL

(***************************************************************************)
(* Init                                                                    *)
(***************************************************************************)

Init ==
    /\ fExists = {}
    /\ fLines = [f \in FILES |-> 0]
    /\ wActive = [s \in SESSIONS |-> NoFile]
    /\ registered = {}
    /\ epochSnap = [s \in SESSIONS |-> {}]
    /\ createSeen = [s \in SESSIONS |-> FALSE]
    /\ evCreate = << >>
    /\ evMod = {}
    /\ track = [p \in SESSIONS \X FILES |-> TrackInit]
    /\ busyS = [s \in SESSIONS |-> FALSE]
    /\ dirtyS = [s \in SESSIONS |-> FALSE]
    /\ busyF = [p \in SESSIONS \X FILES |-> FALSE]
    /\ dirtyF = [p \in SESSIONS \X FILES |-> FALSE]
    /\ drainReq = [p \in SESSIONS \X FILES |-> FALSE]
    /\ pubAfter = [s \in SESSIONS |-> FALSE]
    /\ cancelRel = [s \in SESSIONS |-> FALSE]
    /\ pendingLink = [s \in SESSIONS |-> NoFile]
    /\ pendingExp = [s \in SESSIONS |-> NoFile]
    /\ wk = [p \in SESSIONS \X WORKERS |-> WkInit]
    /\ rowPath = [s \in SESSIONS |-> NoFile]
    /\ rowOffset = [s \in SESSIONS |-> 0]
    /\ rowComposer = [s \in SESSIONS |-> FALSE]
    /\ composerEverSet = [s \in SESSIONS |-> FALSE]
    /\ consumed = [f \in FILES |-> 0]
    /\ regLinked = [s \in SESSIONS |-> FALSE]
    /\ regCount = [s \in SESSIONS |-> 0]
    /\ sealedSet = {}
    /\ sealedAt = [f \in FILES |-> 0]
    /\ restartsLeft = RestartBudget
    /\ crashesLeft = CrashBudget
    /\ cancelsLeft = CancelBudget
    /\ pollsLeft = PollerBudget
    /\ lateFlushLeft = LateFlushBudget

(***************************************************************************)
(* Core: promotion (serialized finalization) and observation.              *)
(* These operators assign exactly the eleven "monitor core" variables:     *)
(* track, rowPath, rowOffset, regLinked, regCount, pubAfter,               *)
(* busyS, busyF, dirtyS, dirtyF, drainReq.                                 *)
(***************************************************************************)

CoreUnchanged ==
    UNCHANGED << track, rowPath, rowOffset, regLinked, regCount, pubAfter,
                 busyS, busyF, dirtyS, dirtyF, drainReq,
                 pendingLink, pendingExp, sealedSet, sealedAt >>

\* Rollover / first-resolution compare-and-set:
\*   persisted provenance re-attaches the already-linked file;
\*   otherwise the row must still equal the expected previous path.
CASOk(s, f, prv, exp) ==
    IF prv = "persisted" THEN rowPath[s] = f
    ELSE rowPath[s] = exp /\ rowPath[s] # f

\* Promote (s, f): CAS, link, close the superseded file's track, defer or
\* emit the registry broadcast, and request the common drain.  `sync`
\* models entry points with no running event loop (_init_inotify, startup
\* recovery): under the broken design the busy claim happens but the drain
\* task is never scheduled (B2).
LinkEffect(s, f, prv, sync, exp) ==
    LET old == rowPath[s] IN
         /\ rowPath' = [rowPath EXCEPT ![s] = f]
         /\ rowOffset' = IF prv = "persisted" THEN rowOffset
                         ELSE [rowOffset EXCEPT ![s] = 0]
         /\ track' = LET t1 == [track EXCEPT ![<<s, f>>] =
                                 [st |-> "stream", prov |-> prv,
                                  expPrev |-> exp, dl |-> FALSE, closeL |-> 0]]
                     IN IF old \in FILES /\ old # f
                        THEN [t1 EXCEPT ![<<s, old>>] =
                                [st |-> "closed", prov |-> t1[<<s, old>>].prov,
                                 expPrev |-> t1[<<s, old>>].expPrev,
                                 dl |-> FALSE, closeL |-> fLines[old]]]
                        ELSE t1
         /\ IF SuppressPreDrainBroadcast
            THEN /\ regLinked' = regLinked
                 /\ regCount' = regCount
            ELSE \* the existing link-time broadcast: linked, zero entries
                 /\ regLinked' = [regLinked EXCEPT ![s] = TRUE]
                 /\ regCount' = [regCount EXCEPT ![s] = 0]
         /\ pubAfter' = [pubAfter EXCEPT ![s] = TRUE]
         /\ IF AttachCatchUp
            THEN IF sync /\ ~SyncSchedulesDrain
                 THEN \* B2: busy claimed synchronously, schedule() no-ops
                      /\ busyS' = SetBusyS(s, TRUE)
                      /\ busyF' = SetBusyF(s, f, TRUE)
                      /\ drainReq' = drainReq
                 ELSE /\ busyS' = busyS
                      /\ busyF' = busyF
                      /\ drainReq' = [drainReq EXCEPT ![<<s, f>>] = TRUE]
            ELSE \* 80126ec1: arm the watch, read nothing already present
                 /\ busyS' = busyS
                 /\ busyF' = busyF
                 /\ drainReq' = drainReq
         /\ UNCHANGED << dirtyS, dirtyF >>
         \* Ghost: advancing past the predecessors IS their pre-advance
         \* final check — seal each not-yet-sealed one at the line count
         \* present right now.  (In the green design the handover/commit
         \* guard guarantees consumed = fLines here; in the cede design
         \* this seals unpublished lines, which the OrderedDelivery
         \* invariant then exposes.)
         /\ LET newPreds == {g \in FILES :
                               g \in fExists /\ ChainBefore(g, f)
                               /\ g \notin sealedSet}
            IN /\ sealedSet' = sealedSet \cup newPreds
               /\ sealedAt' = [g \in FILES |->
                                 IF g \in newPreds THEN fLines[g]
                                 ELSE sealedAt[g]]

\* Promote (s, f): CAS, then either link directly, or — when predecessors
\* still hold unpublished lines (operator requirement: no content loss,
\* in order) — defer the link into the ordered handover: predecessors are
\* published oldest-first (HandoverDrain) and the link commits afterwards
\* (CommitLink).
Promote(s, f, prv, exp, sync) ==
    IF prv # "persisted" /\ rowPath[s] = f
    THEN \* Promoting the file the row ALREADY links is a re-attach
         \* (persisted-identity semantics), never a first-resolution or
         \* rollover CAS — that CAS rightly refuses rowPath = f, and a
         \* track left in CHARACTERIZING for its own linked file would
         \* churn the CAS forever (TLC counterexample: a deferred
         \* handover racing a direct promotion of the same file).
         /\ LinkEffect(s, f, "persisted", sync, f)
         /\ UNCHANGED << pendingLink, pendingExp >>
    ELSE
    IF CASOk(s, f, prv, exp)
    THEN IF OrderedHandover /\ prv # "persisted" /\ HasIncompletePred(f)
         THEN \* defer: no link, no broadcast, no offset reset yet
              /\ pendingLink' = [pendingLink EXCEPT ![s] = f]
              /\ pendingExp' = [pendingExp EXCEPT ![s] = exp]
              /\ track' = [track EXCEPT ![<<s, f>>] =
                             [st |-> "char", prov |-> prv, expPrev |-> exp,
                              dl |-> FALSE, closeL |-> 0]]
              /\ UNCHANGED << rowPath, rowOffset, regLinked, regCount,
                              pubAfter, busyS, busyF, dirtyS, dirtyF,
                              drainReq, sealedSet, sealedAt >>
         ELSE /\ LinkEffect(s, f, prv, sync, exp)
              /\ UNCHANGED << pendingLink, pendingExp >>
    ELSE \* CAS lost: never overwrite the newer association
         /\ track' = [track EXCEPT ![<<s, f>>] =
                IF CASRetry
                THEN [@ EXCEPT !.st = "char", !.expPrev = rowPath[s]]
                ELSE [@ EXCEPT !.st = "closed", !.closeL = fLines[f]]]
         /\ UNCHANGED << rowPath, rowOffset, regLinked, regCount, pubAfter,
                         busyS, busyF, dirtyS, dirtyF, drainReq,
                         pendingLink, pendingExp, sealedSet, sealedAt >>

\* Observe (s, f) with provenance p.  Trusted provenance (birth continuity
\* or persisted identity) promotes WITHOUT content classification — that is
\* the definition of trust, and exactly what the T102 broken design granted
\* to merely-first-observed files.  Ambiguous provenance classifies:
\* sub -> IGNORED, unknown -> CHARACTERIZING (or dropped, per
\* RetainUnknown), main -> CHARACTERIZING (verified next step).
ObserveOutcome(s, f, p, sync) ==
    IF p \in {"birth", "persisted"}
    THEN \* Birth trust is FIRST-RESOLUTION-ONLY: the trusted promote
         \* compares against NULL (the bead's "first_resolution and
         \* row.jsonl_path is not NULL -> close_as_superseded"), so a
         \* trusted CREATE delivered after the row acquired a link by any
         \* other path loses the CAS instead of rolling the row backwards
         \* (TLC found the backward link when this was mis-encoded as a
         \* compare against the current row — a tautology).
         Promote(s, f, p, IF p = "persisted" THEN f ELSE NoFile, sync)
    ELSE LET cls == EffClassify(f)
             chr == [st |-> "char", prov |-> p, expPrev |-> rowPath[s],
                     dl |-> FALSE, closeL |-> 0]
         IN
         IF cls = "sub"
         THEN /\ track' = [track EXCEPT ![<<s, f>>] =
                             [chr EXCEPT !.st = "ign", !.closeL = fLines[f]]]
              /\ UNCHANGED << rowPath, rowOffset, regLinked, regCount,
                              pubAfter, busyS, busyF, dirtyS, dirtyF,
                              drainReq,
                              pendingLink, pendingExp, sealedSet, sealedAt >>
         ELSE IF cls = "main"
         THEN \* ambiguous provenance: characterize; verification promotes
              \* on the next recheck/event step (never skips CHARACTERIZING)
              /\ track' = [track EXCEPT ![<<s, f>>] = chr]
              /\ UNCHANGED << rowPath, rowOffset, regLinked, regCount,
                              pubAfter, busyS, busyF, dirtyS, dirtyF,
                              drainReq,
                              pendingLink, pendingExp, sealedSet, sealedAt >>
         ELSE \* unknown
              IF RetainUnknown
              THEN /\ track' = [track EXCEPT ![<<s, f>>] = chr]
                   /\ UNCHANGED << rowPath, rowOffset, regLinked, regCount,
                                   pubAfter, busyS, busyF, dirtyS, dirtyF,
                                   drainReq,
                                   pendingLink, pendingExp, sealedSet, sealedAt >>
              ELSE \* defer without an owner (watch_scan path): no track,
                   \* no retry, responsibility dropped
                   CoreUnchanged

(***************************************************************************)
(* Writer environment (the Codex process and the pane poller).  No         *)
(* fairness: the writer may go quiet at any time — quiescence after a      *)
(* burst is the incident shape.                                            *)
(***************************************************************************)

WCreateMain(s) ==
    /\ wActive[s] = NoFile
    /\ FFirst[s] \notin fExists
    /\ fExists' = fExists \cup {FFirst[s]}
    /\ wActive' = [wActive EXCEPT ![s] = FFirst[s]]
    /\ evCreate' = IF DirWatched(FFirst[s]) THEN Append(evCreate, FFirst[s])
                   ELSE evCreate
    /\ UNCHANGED << fLines, registered, epochSnap, createSeen, evMod >>
    /\ CoreUnchanged
    /\ UNCHANGED << cancelRel, wk, rowComposer, composerEverSet >>
    /\ UNCHANGED obsVars /\ UNCHANGED envVars

WCreateSub(f) ==
    /\ FKind[f] = "sub"
    /\ f \notin fExists
    /\ wActive[FSession[f]] # NoFile
    /\ fExists' = fExists \cup {f}
    /\ evCreate' = IF DirWatched(f) THEN Append(evCreate, f) ELSE evCreate
    /\ UNCHANGED << fLines, wActive, registered, epochSnap, createSeen, evMod >>
    /\ CoreUnchanged
    /\ UNCHANGED << cancelRel, wk, rowComposer, composerEverSet >>
    /\ UNCHANGED obsVars /\ UNCHANGED envVars

\* Append one complete line.  Edge-triggered: a MODIFY is queued only if
\* some track is armed at write time.  A SUPERSEDED main may still receive
\* a bounded number of late-flushed lines (the operator's point: nothing
\* proves the old file goes quiet when the successor opens).
WWrite(f) ==
    LET late == FKind[f] = "main" /\ wActive[FSession[f]] # f IN
    /\ f \in fExists
    /\ fLines[f] < MaxLines
    /\ (FKind[f] = "sub" \/ wActive[FSession[f]] = f
        \/ (late /\ lateFlushLeft > 0))
    /\ fLines' = [fLines EXCEPT ![f] = @ + 1]
    /\ evMod' = IF AnyArmed(f) THEN evMod \cup {f} ELSE evMod
    /\ lateFlushLeft' = IF late THEN lateFlushLeft - 1 ELSE lateFlushLeft
    /\ UNCHANGED << fExists, wActive, registered, epochSnap, createSeen,
                    evCreate >>
    /\ CoreUnchanged
    /\ UNCHANGED << cancelRel, wk, rowComposer, composerEverSet >>
    /\ UNCHANGED obsVars
    /\ UNCHANGED << restartsLeft, crashesLeft, cancelsLeft, pollsLeft >>

WRollover(s) ==
    /\ wActive[s] # NoFile
    /\ FSucc[wActive[s]] # NoFile
    /\ FSucc[wActive[s]] \notin fExists
    /\ fExists' = fExists \cup {FSucc[wActive[s]]}
    /\ wActive' = [wActive EXCEPT ![s] = FSucc[wActive[s]]]
    /\ evCreate' = IF DirWatched(FSucc[wActive[s]])
                   THEN Append(evCreate, FSucc[wActive[s]]) ELSE evCreate
    /\ UNCHANGED << fLines, registered, epochSnap, createSeen, evMod >>
    /\ CoreUnchanged
    /\ UNCHANGED << cancelRel, wk, rowComposer, composerEverSet >>
    /\ UNCHANGED obsVars /\ UNCHANGED envVars

\* The pane poller sets composer_ready on the persisted row — the
\* independently-enabled row-update edge for the B7 lost update.
PollerSet(s) ==
    /\ pollsLeft > 0
    /\ ~rowComposer[s]
    /\ rowComposer' = [rowComposer EXCEPT ![s] = TRUE]
    /\ composerEverSet' = [composerEverSet EXCEPT ![s] = TRUE]
    /\ pollsLeft' = pollsLeft - 1
    /\ UNCHANGED fsVars /\ UNCHANGED chanVars /\ CoreUnchanged
    /\ UNCHANGED << cancelRel, wk, rowPath, rowOffset >>
    /\ UNCHANGED obsVars
    /\ UNCHANGED << restartsLeft, crashesLeft, cancelsLeft, lateFlushLeft >>

(***************************************************************************)
(* Event delivery and registration (the asyncio inotify loop).             *)
(***************************************************************************)

Register(s) ==
    /\ s \notin registered
    /\ registered' = registered \cup {s}
    /\ epochSnap' = [epochSnap EXCEPT ![s] = fExists]
    /\ UNCHANGED << fExists, fLines, wActive, createSeen, evCreate, evMod >>
    /\ CoreUnchanged
    /\ UNCHANGED << cancelRel, wk, rowComposer, composerEverSet >>
    /\ UNCHANGED obsVars /\ UNCHANGED envVars

\* Deliver the head IN_CREATE (kernel FIFO) to the watching session.
\* Birth trust = the watch was armed before the file existed (epoch
\* barrier) AND this is the first CREATE observed on this watch.
DeliverCreate ==
    /\ evCreate # << >>
    /\ LET f == Head(evCreate)
           s == FSession[f]
       IN
       /\ s \in registered
       /\ evCreate' = Tail(evCreate)
       /\ createSeen' = [createSeen EXCEPT ![s] = TRUE]
       /\ IF Observable(s, f)
          THEN LET trusted ==
                     IF ContinuityProvenance
                     THEN \* Finding N1 (model-discovered): trust requires
                          \* the watch to have been armed over an EMPTY
                          \* directory — "armed before this file existed"
                          \* plus first-create is NOT enough, because a
                          \* subagent forked after a late arming is the
                          \* first create on a watch armed before it
                          \* existed.  The bead's rule as written admits
                          \* the child; the empty-epoch strengthening
                          \* closes it.
                          (IF EpochBarrier THEN epochSnap[s] = {} ELSE TRUE)
                          /\ ~createSeen[s]
                     ELSE \* T102: any first-observed file for an unresolved
                          \* row is trusted, categorization skipped
                          rowPath[s] = NoFile
                   p == IF trusted THEN "birth" ELSE "late"
               IN ObserveOutcome(s, f, p, FALSE)
          ELSE CoreUnchanged
    /\ UNCHANGED << fExists, fLines, wActive, registered, epochSnap, evMod >>
    /\ UNCHANGED << cancelRel, wk, rowComposer, composerEverSet >>
    /\ UNCHANGED << consumed >>
    /\ UNCHANGED envVars

\* Deliver a coalesced IN_MODIFY to an armed track for the file.
DeliverModify(f) ==
    /\ f \in evMod
    /\ evMod' = evMod \ {f}
    /\ \E s \in SESSIONS :
        /\ TrackArmed(s, f)
        /\ LET tr == track[<<s, f>>] IN
           IF tr.st = "char"
           THEN IF EffClassify(f) = "sub"
                THEN /\ track' = [track EXCEPT ![<<s, f>>] =
                                    [@ EXCEPT !.st = "ign",
                                              !.closeL = fLines[f]]]
                     /\ UNCHANGED << rowPath, rowOffset, regLinked, regCount,
                                     pubAfter, busyS, busyF, dirtyS, dirtyF,
                                     drainReq,
                                     pendingLink, pendingExp, sealedSet, sealedAt >>
                ELSE IF EffClassify(f) = "main" /\ NoNewerExists(f)
                THEN Promote(s, f, tr.prov, tr.expPrev, FALSE)
                ELSE IF EffClassify(f) = "main"
                THEN \* N2: superseded before verification — close
                     /\ track' = [track EXCEPT ![<<s, f>>] =
                                    [@ EXCEPT !.st = "closed",
                                              !.closeL = fLines[f]]]
                     /\ UNCHANGED << rowPath, rowOffset, regLinked, regCount,
                                     pubAfter, busyS, busyF, dirtyS, dirtyF,
                                     drainReq,
                                     pendingLink, pendingExp, sealedSet, sealedAt >>
                ELSE \* still unknown: the event-driven deadline check —
                     \* under the broken design this is the ONLY place the
                     \* deadline is ever evaluated (T110)
                     IF ~TimeDrivenDeadline /\ tr.dl
                     THEN /\ track' = [track EXCEPT ![<<s, f>>] =
                                         [@ EXCEPT !.st = "closed",
                                                   !.closeL = fLines[f]]]
                          /\ UNCHANGED << rowPath, rowOffset, regLinked,
                                          regCount, pubAfter, busyS, busyF,
                                          dirtyS, dirtyF, drainReq,
                                          pendingLink, pendingExp, sealedSet, sealedAt >>
                     ELSE CoreUnchanged
           ELSE \* streaming: request the common drain
                /\ drainReq' = [drainReq EXCEPT ![<<s, f>>] = TRUE]
                /\ UNCHANGED << track, rowPath, rowOffset, regLinked,
                                regCount, pubAfter, busyS, busyF,
                                dirtyS, dirtyF, pendingLink, pendingExp,
                                sealedSet, sealedAt >>
    /\ UNCHANGED << fExists, fLines, wActive, registered, epochSnap,
                    createSeen, evCreate >>
    /\ UNCHANGED << cancelRel, wk, rowComposer, composerEverSet >>
    /\ UNCHANGED << consumed >>
    /\ UNCHANGED envVars

(***************************************************************************)
(* Drain: single-owner catch-up/ingest.  ClaimDrain is the synchronous     *)
(* check-and-claim (no await between them); the worker is its own process  *)
(* (asyncio.to_thread), so cancelling the awaiter does not stop it.        *)
(***************************************************************************)

ClaimDrain(s, f) ==
    /\ drainReq[<<s, f>>]
    /\ track[<<s, f>>].st = "stream"
    /\ FreeSlot(s)
    /\ IF DrainGate
       THEN /\ ~GBusy(s, f)
            /\ busyS' = SetBusyS(s, TRUE)
            /\ busyF' = SetBusyF(s, f, TRUE)
       ELSE /\ busyS' = busyS
            /\ busyF' = busyF
    /\ drainReq' = [drainReq EXCEPT ![<<s, f>>] = FALSE]
    /\ wk' = [wk EXCEPT ![<<s, PickSlot(s)>>] =
                [WkInit EXCEPT !.pc = "readRow", !.file = f]]
    /\ UNCHANGED fsVars /\ UNCHANGED chanVars
    /\ UNCHANGED << track, dirtyS, dirtyF, pubAfter, cancelRel,
                    pendingLink, pendingExp, sealedSet, sealedAt >>
    /\ UNCHANGED rowVars /\ UNCHANGED obsVars /\ UNCHANGED envVars

\* A request meeting a held gate: transfer through dirty (fixed) or drop
\* the signal (skip-on-contention, T87).
ClaimContended(s, f) ==
    /\ drainReq[<<s, f>>]
    /\ DrainGate
    /\ GBusy(s, f)
    /\ drainReq' = [drainReq EXCEPT ![<<s, f>>] = FALSE]
    /\ IF DirtyTransfer
       THEN /\ dirtyS' = SetDirtyS(s, TRUE)
            /\ dirtyF' = SetDirtyF(s, f, TRUE)
       ELSE /\ dirtyS' = dirtyS
            /\ dirtyF' = dirtyF
    /\ UNCHANGED fsVars /\ UNCHANGED chanVars
    /\ UNCHANGED << track, busyS, busyF, pubAfter, cancelRel, wk,
                    pendingLink, pendingExp, sealedSet, sealedAt >>
    /\ UNCHANGED rowVars /\ UNCHANGED obsVars /\ UNCHANGED envVars

\* A cancelled worker finishes its current step and then stops; under the
\* guarded release the gate is freed (with responsibility retained) only
\* once the worker has actually stopped.
WkStopIfCancelled(s, w, nextPc) ==
    IF wk[<<s, w>>].cancelled THEN "stopped" ELSE nextPc

WkReadRow(s, w) ==
    LET rec == wk[<<s, w>>] IN
    /\ rec.pc = "readRow"
    /\ wk' = [wk EXCEPT ![<<s, w>>] =
                [@ EXCEPT !.pc = WkStopIfCancelled(s, w, "readFile"),
                          !.snap = rowOffset[s],
                          !.hs = rowComposer[s],
                          \* Finding N4 (model-discovered): under the
                          \* per-session gate the owner drains the row's
                          \* CURRENT path, re-resolved each pass — a
                          \* file-bound owner consumes a dirty signal
                          \* raised for the successor file and strands
                          \* it (rollover-boundary stall).
                          !.file = IF PerSessionGate /\ rowPath[s] # NoFile
                                   THEN rowPath[s] ELSE @]]
    /\ UNCHANGED fsVars /\ UNCHANGED chanVars /\ CoreUnchanged
    /\ UNCHANGED << cancelRel, rowComposer, composerEverSet >>
    /\ UNCHANGED obsVars /\ UNCHANGED envVars

WkReadFile(s, w) ==
    LET rec == wk[<<s, w>>] IN
    /\ rec.pc = "readFile"
    /\ wk' = [wk EXCEPT ![<<s, w>>] =
                [@ EXCEPT !.pc = WkStopIfCancelled(s, w,
                             IF PersistOrder = "persistFirst"
                             THEN "persist" ELSE "process"),
                          !.upTo = fLines[rec.file]]]
    /\ UNCHANGED fsVars /\ UNCHANGED chanVars /\ CoreUnchanged
    /\ UNCHANGED << cancelRel, rowComposer, composerEverSet >>
    /\ UNCHANGED obsVars /\ UNCHANGED envVars

\* Generation guard: persistence lands only if the row still points at the
\* file this drain was claimed for (part of the A1 fix bundle).
GenOK(s, w) == rowPath[s] = wk[<<s, w>>].file

WkPersist(s, w) ==
    LET rec == wk[<<s, w>>] IN
    /\ rec.pc = "persist"
    /\ IF PerSessionGate /\ ~GenOK(s, w)
       THEN /\ rowOffset' = rowOffset          \* stale generation loses safely
            /\ rowComposer' = rowComposer
       ELSE /\ rowOffset' = [rowOffset EXCEPT ![s] = Max(@, rec.upTo)]
            /\ rowComposer' = IF MergeHarnessAtWrite
                              THEN rowComposer  \* merged in the UPDATE
                              ELSE [rowComposer EXCEPT ![s] = rec.hs]  \* B7
    /\ wk' = [wk EXCEPT ![<<s, w>>] =
                [@ EXCEPT !.pc = WkStopIfCancelled(s, w,
                             IF PersistOrder = "persistFirst"
                             THEN "process" ELSE "final")]]
    /\ UNCHANGED fsVars /\ UNCHANGED chanVars
    /\ UNCHANGED << track, rowPath, regLinked, regCount, pubAfter,
                    busyS, busyF, dirtyS, dirtyF, drainReq,
                    pendingLink, pendingExp, sealedSet, sealedAt >>
    /\ UNCHANGED << cancelRel, composerEverSet >>
    /\ UNCHANGED << consumed >>
    /\ UNCHANGED envVars

\* Process + publish the read range downstream.  Under "idempotent" the
\* downstream dedups by line index (re-delivery cannot inflate); otherwise
\* every delivery counts and duplicates are visible.
WkProcess(s, w) ==
    LET rec == wk[<<s, w>>] IN
    /\ rec.pc = "process"
    /\ consumed' =
         IF PersistOrder = "idempotent"
         THEN [consumed EXCEPT ![rec.file] = Max(@, rec.upTo)]
         ELSE [consumed EXCEPT ![rec.file] =
                 @ + (IF rec.upTo > rec.snap THEN rec.upTo - rec.snap ELSE 0)]
    /\ wk' = [wk EXCEPT ![<<s, w>>] =
                [@ EXCEPT !.pc = WkStopIfCancelled(s, w,
                             IF PersistOrder = "persistFirst"
                             THEN "final" ELSE "persist")]]
    /\ UNCHANGED fsVars /\ UNCHANGED chanVars /\ CoreUnchanged
    /\ UNCHANGED << cancelRel, rowComposer, composerEverSet >>
    /\ UNCHANGED << regLinked, regCount >>
    /\ UNCHANGED envVars

\* The await-free final sequence: dirty check, bounded continuation,
\* release, post-drain registry publish.
WkFinal(s, w) ==
    LET rec == wk[<<s, w>>]
        f   == rec.file
    IN
    /\ rec.pc = "final"
    /\ IF GDirty(s, f) /\ rec.passes < MaxPasses
       THEN /\ dirtyS' = SetDirtyS(s, FALSE)
            /\ dirtyF' = SetDirtyF(s, f, FALSE)
            /\ wk' = [wk EXCEPT ![<<s, w>>] =
                        [@ EXCEPT !.pc = "readRow", !.passes = @ + 1]]
            /\ UNCHANGED << busyS, busyF, drainReq, pubAfter,
                            regLinked, regCount >>
       ELSE /\ busyS' = SetBusyS(s, FALSE)
            /\ busyF' = SetBusyF(s, f, FALSE)
            /\ wk' = [wk EXCEPT ![<<s, w>>] = WkInit]
            /\ IF GDirty(s, f)
               THEN \* bounded passes exhausted: schedule a continuation
                    \* for the row's current path (N4 corollary)
                    /\ dirtyS' = SetDirtyS(s, FALSE)
                    /\ dirtyF' = SetDirtyF(s, f, FALSE)
                    /\ drainReq' = [drainReq EXCEPT
                                      ![<<s, ContinueTarget(s, f)>>] = TRUE]
                    /\ UNCHANGED << pubAfter, regLinked, regCount >>
               ELSE /\ dirtyS' = dirtyS
                    /\ dirtyF' = dirtyF
                    /\ drainReq' = drainReq
                    /\ pubAfter' = [pubAfter EXCEPT ![s] = FALSE]
                    /\ regLinked' = [regLinked EXCEPT ![s] =
                                       rowPath[s] # NoFile]
                    /\ regCount' = [regCount EXCEPT ![s] =
                                      IF rowPath[s] # NoFile
                                      THEN consumed[rowPath[s]] ELSE 0]
    /\ UNCHANGED fsVars /\ UNCHANGED chanVars
    /\ UNCHANGED << track, cancelRel, pendingLink, pendingExp,
                    sealedSet, sealedAt >>
    /\ UNCHANGED rowVars
    /\ UNCHANGED << consumed >>
    /\ UNCHANGED envVars

\* A cancelled worker that ran out of steps stops; guarded release then
\* frees the gate with responsibility retained.
WkStopped(s, w) ==
    LET rec == wk[<<s, w>>] IN
    /\ rec.pc = "stopped"
    /\ wk' = [wk EXCEPT ![<<s, w>>] = WkInit]
    /\ IF GateReleaseOnCancel = "guarded"
       THEN LET tgt == ContinueTarget(s, rec.file) IN
            /\ busyS' = SetBusyS(s, FALSE)
            /\ busyF' = SetBusyF(s, rec.file, FALSE)
            /\ drainReq' = [drainReq EXCEPT ![<<s, tgt>>] =
                              track[<<s, tgt>>].st = "stream"]
       ELSE \* finally: gate already freed at cancel; none: stays pinned
            /\ busyS' = busyS
            /\ busyF' = busyF
            /\ drainReq' = drainReq
    /\ UNCHANGED fsVars /\ UNCHANGED chanVars
    /\ UNCHANGED << track, dirtyS, dirtyF, pubAfter, cancelRel,
                    pendingLink, pendingExp, sealedSet, sealedAt >>
    /\ UNCHANGED rowVars /\ UNCHANGED obsVars /\ UNCHANGED envVars

\* Drain failure: the worker dies mid-flight.  The fixed design retains
\* responsibility: release the gate and reschedule.
WkCrash(s, w) ==
    LET rec == wk[<<s, w>>] IN
    /\ crashesLeft > 0
    /\ rec.pc \in {"readRow", "readFile", "process", "persist"}
    /\ crashesLeft' = crashesLeft - 1
    /\ wk' = [wk EXCEPT ![<<s, w>>] = WkInit]
    /\ busyS' = SetBusyS(s, FALSE)
    /\ busyF' = SetBusyF(s, rec.file, FALSE)
    /\ drainReq' = LET tgt == ContinueTarget(s, rec.file) IN
                   [drainReq EXCEPT ![<<s, tgt>>] =
                      track[<<s, tgt>>].st = "stream"]
    /\ UNCHANGED fsVars /\ UNCHANGED chanVars
    /\ UNCHANGED << track, dirtyS, dirtyF, pubAfter, cancelRel,
                    pendingLink, pendingExp, sealedSet, sealedAt >>
    /\ UNCHANGED rowVars /\ UNCHANGED obsVars
    /\ UNCHANGED << restartsLeft, cancelsLeft, pollsLeft, lateFlushLeft >>

\* Cancellation of the drain AWAITER.  The to_thread worker keeps running:
\* it completes its current step and then stops (pc = "stopped").
\*   guarded: gate freed only when the worker stops (WkStopped).
\*   finally: gate freed immediately while the worker still runs (A5).
\*   none:    gate never freed (B5).
WkCancel(s, w) ==
    LET rec == wk[<<s, w>>] IN
    /\ cancelsLeft > 0
    /\ rec.pc \in {"readRow", "readFile", "process", "persist"}
    /\ ~rec.cancelled
    /\ cancelsLeft' = cancelsLeft - 1
    /\ wk' = [wk EXCEPT ![<<s, w>>] = [@ EXCEPT !.cancelled = TRUE]]
    /\ IF GateReleaseOnCancel = "finally"
       THEN /\ busyS' = SetBusyS(s, FALSE)
            /\ busyF' = SetBusyF(s, rec.file, FALSE)
       ELSE /\ busyS' = busyS
            /\ busyF' = busyF
    /\ UNCHANGED fsVars /\ UNCHANGED chanVars
    /\ UNCHANGED << track, dirtyS, dirtyF, drainReq, pubAfter, cancelRel,
                    pendingLink, pendingExp, sealedSet, sealedAt >>
    /\ UNCHANGED rowVars /\ UNCHANGED obsVars
    /\ UNCHANGED << restartsLeft, crashesLeft, pollsLeft, lateFlushLeft >>

(***************************************************************************)
(* Reconciliation (level-triggered, reliable; carries the fairness that    *)
(* makes the lossy edge channel tolerable).                                *)
(***************************************************************************)

\* Reconciliation scan: observe any existing file without a live track.
\* Finding N3 (model-discovered): restricting this scan to unresolved rows
\* (the current code's get_sessions_needing_resolution) strands a rollover
\* successor created just before a dashboard restart — the row is linked
\* to the old file, the IN_CREATE was lost with the kernel queue, and no
\* mechanism ever observes the new file.  The bead's reconciliation
\* pseudocode scans for "newly observed paths" unrestricted; this models
\* that reading, which closes the gap.
ReconLateObserve(s, f) ==
    /\ s \in registered
    /\ f \in fExists
    /\ FSession[f] = s
    /\ Observable(s, f)
    /\ LET p == IF f = rowPath[s] THEN "persisted"
                \* N3 refinement (model-discovered): the row's own linked
                \* path re-observes as persisted-identity re-attach — as an
                \* ambiguous observation its promotion CAS can never
                \* succeed (rowPath = f) and the file strands in
                \* CHARACTERIZING.
                ELSE IF ~ContinuityProvenance THEN "birth" ELSE "late"
       IN ObserveOutcome(s, f, p, FALSE)
    /\ UNCHANGED fsVars /\ UNCHANGED chanVars
    /\ UNCHANGED << cancelRel, wk, rowComposer, composerEverSet >>
    /\ UNCHANGED << consumed >>
    /\ UNCHANGED envVars

\* A characterizing track whose classification became decidable is
\* resolved by the tick as well (the pending scan re-runs classification;
\* this also covers bytes written before the track armed, which produced
\* no event).
ReconRecheck(s, f) ==
    /\ track[<<s, f>>].st = "char"
    /\ EffClassify(f) # "unknown"
    /\ IF EffClassify(f) = "sub"
       THEN /\ track' = [track EXCEPT ![<<s, f>>] =
                           [@ EXCEPT !.st = "ign", !.closeL = fLines[f]]]
            /\ UNCHANGED << rowPath, rowOffset, regLinked, regCount,
                            pubAfter, busyS, busyF, dirtyS, dirtyF,
                            drainReq,
                            pendingLink, pendingExp, sealedSet, sealedAt >>
       ELSE IF NoNewerExists(f)
       THEN Promote(s, f, track[<<s, f>>].prov, track[<<s, f>>].expPrev,
                    FALSE)
       ELSE \* N2: a successor exists — this main is superseded; close it
            \* rather than let it win (or spin in) the promotion CAS.
            /\ track' = [track EXCEPT ![<<s, f>>] =
                           [@ EXCEPT !.st = "closed", !.closeL = fLines[f]]]
            /\ UNCHANGED << rowPath, rowOffset, regLinked, regCount,
                            pubAfter, busyS, busyF, dirtyS, dirtyF,
                            drainReq,
                            pendingLink, pendingExp, sealedSet, sealedAt >>
    /\ UNCHANGED fsVars /\ UNCHANGED chanVars
    /\ UNCHANGED << cancelRel, wk, rowComposer, composerEverSet >>
    /\ UNCHANGED << consumed >>
    /\ UNCHANGED envVars

\* Characterization-deadline expiry, driven by time (the tick), never by a
\* file event.  Under the broken design this action does not exist.
ReconExpire(s, f) ==
    /\ TimeDrivenDeadline
    /\ track[<<s, f>>].st = "char"
    /\ track[<<s, f>>].dl
    /\ EffClassify(f) = "unknown"
    /\ track' = [track EXCEPT ![<<s, f>>] =
                    [@ EXCEPT !.st = "closed", !.closeL = fLines[f]]]
    /\ UNCHANGED fsVars /\ UNCHANGED chanVars
    /\ UNCHANGED << busyS, dirtyS, busyF, dirtyF, drainReq, pubAfter,
                    cancelRel, wk, pendingLink, pendingExp,
                    sealedSet, sealedAt >>
    /\ UNCHANGED rowVars /\ UNCHANGED obsVars /\ UNCHANGED envVars

\* Guarded post-cancel release is folded into WkStopped.  The environment:
\* a characterization deadline elapses (time as nondeterministic
\* enablement).
DeadlinePass(s, f) ==
    /\ track[<<s, f>>].st = "char"
    /\ ~track[<<s, f>>].dl
    /\ track' = [track EXCEPT ![<<s, f>>] = [@ EXCEPT !.dl = TRUE]]
    /\ UNCHANGED fsVars /\ UNCHANGED chanVars
    /\ UNCHANGED << busyS, dirtyS, busyF, dirtyF, drainReq, pubAfter,
                    cancelRel, wk, pendingLink, pendingExp,
                    sealedSet, sealedAt >>
    /\ UNCHANGED rowVars /\ UNCHANGED obsVars /\ UNCHANGED envVars

(***************************************************************************)
(* Ordered handover: a verified successor waits while predecessors hold    *)
(* unpublished lines; predecessors publish oldest-first; the link commits  *)
(* only through a guard that re-reads the filesystem — the pre-advance     *)
(* final check the operator required.  Bytes arriving after that check    *)
(* are the documented residual (nothing can prove a file won't be written  *)
(* after its successor opens).                                             *)
(***************************************************************************)

\* Make the oldest unready predecessor ready: publish it to EOF as
\* currently visible AND record its final check (seal), in one walk step.
\* Sealing oldest-first is what guarantees chain order: no later file is
\* touched before every earlier file's check.  Publication is idempotent-
\* by-line-index (the A4 requirement), so re-publication after a crash or
\* restart cannot inflate counts.
HandoverDrain(s) ==
    /\ pendingLink[s] # NoFile
    /\ HasUnreadyPred(pendingLink[s])
    /\ LET p == OldestUnreadyPred(pendingLink[s]) IN
       /\ consumed' = [consumed EXCEPT ![p] = Max(@, fLines[p])]
       /\ sealedSet' = sealedSet \cup {p}
       /\ sealedAt' = [sealedAt EXCEPT ![p] = Max(@, fLines[p])]
    /\ UNCHANGED fsVars /\ UNCHANGED chanVars
    /\ UNCHANGED << track, rowPath, rowOffset, regLinked, regCount,
                    pubAfter, busyS, busyF, dirtyS, dirtyF, drainReq,
                    pendingLink, pendingExp >>
    /\ UNCHANGED << cancelRel, wk, rowComposer, composerEverSet >>
    /\ UNCHANGED envVars

\* The final check and the advance, in one guarded step: the link commits
\* only when NO predecessor has unpublished lines at this instant (the
\* guard re-reads fLines, so a late flush landing before the check blocks
\* the advance and gets drained first).  The CAS expectation captured at
\* defer time is re-verified; a stale commit re-arms the candidate.
CommitLink(s) ==
    /\ pendingLink[s] # NoFile
    /\ ~HasUnreadyPred(pendingLink[s])
    /\ IF rowPath[s] = pendingExp[s]
       THEN /\ LinkEffect(s, pendingLink[s],
                          track[<<s, pendingLink[s]>>].prov, FALSE,
                          pendingExp[s])
            /\ pendingLink' = [pendingLink EXCEPT ![s] = NoFile]
            /\ pendingExp' = [pendingExp EXCEPT ![s] = NoFile]
       ELSE IF rowPath[s] = pendingLink[s]
       THEN \* the pending file got linked by another path meanwhile:
            \* re-attach (persisted semantics), never demote to char
            /\ LinkEffect(s, pendingLink[s], "persisted", FALSE,
                          pendingLink[s])
            /\ pendingLink' = [pendingLink EXCEPT ![s] = NoFile]
            /\ pendingExp' = [pendingExp EXCEPT ![s] = NoFile]
       ELSE /\ track' = [track EXCEPT ![<<s, pendingLink[s]>>] =
                           [@ EXCEPT !.st = "char", !.expPrev = rowPath[s]]]
            /\ pendingLink' = [pendingLink EXCEPT ![s] = NoFile]
            /\ pendingExp' = [pendingExp EXCEPT ![s] = NoFile]
            /\ UNCHANGED << rowPath, rowOffset, regLinked, regCount,
                            pubAfter, busyS, busyF, dirtyS, dirtyF,
                            drainReq, sealedSet, sealedAt >>
    /\ UNCHANGED fsVars /\ UNCHANGED chanVars
    /\ UNCHANGED << cancelRel, wk, rowComposer, composerEverSet >>
    /\ UNCHANGED << consumed >>
    /\ UNCHANGED envVars

(***************************************************************************)
(* Dashboard restart: in-memory state is lost; the DB row and the files    *)
(* survive.  Recovery re-arms watches; linked rows re-attach with          *)
(* persisted provenance IN SYNC CONTEXT (B2's entry); unlinked rows are    *)
(* left to the reconciliation scan (late observation).                     *)
(***************************************************************************)

Restart ==
    /\ restartsLeft > 0
    /\ restartsLeft' = restartsLeft - 1
    /\ track' = [p \in SESSIONS \X FILES |-> TrackInit]
    /\ busyS' = [s \in SESSIONS |-> FALSE]
    /\ dirtyS' = [s \in SESSIONS |-> FALSE]
    /\ busyF' = [p \in SESSIONS \X FILES |-> FALSE]
    /\ dirtyF' = [p \in SESSIONS \X FILES |-> FALSE]
    /\ drainReq' = [p \in SESSIONS \X FILES |-> FALSE]
    /\ pubAfter' = [s \in SESSIONS |-> FALSE]
    /\ cancelRel' = [s \in SESSIONS |-> FALSE]
    /\ pendingLink' = [s \in SESSIONS |-> NoFile]
    /\ pendingExp' = [s \in SESSIONS |-> NoFile]
    /\ wk' = [p \in SESSIONS \X WORKERS |-> WkInit]
    /\ evCreate' = << >>
    /\ evMod' = {}
    /\ registered' = SESSIONS               \* _init_inotify re-arms all
    /\ epochSnap' = [s \in SESSIONS |-> fExists]  \* everything pre-existing
    /\ createSeen' = [s \in SESSIONS |-> FALSE]   \* fresh watch epoch
    /\ UNCHANGED fsVars /\ UNCHANGED rowVars /\ UNCHANGED obsVars
    /\ UNCHANGED << sealedSet, sealedAt >>
    /\ UNCHANGED << crashesLeft, cancelsLeft, pollsLeft, lateFlushLeft >>

\* Startup recovery of a linked row (sync context): persisted-identity
\* re-attach, which under the fixed design promotes and drains.
RecoverLinked(s) ==
    /\ rowPath[s] # NoFile
    /\ s \in registered
    /\ track[<<s, rowPath[s]>>].st = "none"
    /\ ObserveOutcome(s, rowPath[s], "persisted", TRUE)
    /\ UNCHANGED fsVars /\ UNCHANGED chanVars
    /\ UNCHANGED << cancelRel, wk, rowComposer, composerEverSet >>
    /\ UNCHANGED << consumed >>
    /\ UNCHANGED envVars

(***************************************************************************)
(* Next / Spec                                                             *)
(***************************************************************************)

WriterActs ==
    \/ \E s \in SESSIONS : WCreateMain(s) \/ WRollover(s) \/ PollerSet(s)
    \/ \E f \in FILES : WCreateSub(f) \/ WWrite(f)

MonitorActs ==
    \/ \E s \in SESSIONS : Register(s) \/ RecoverLinked(s)
                           \/ HandoverDrain(s) \/ CommitLink(s)
    \/ DeliverCreate
    \/ \E f \in FILES : DeliverModify(f)
    \/ \E s \in SESSIONS, f \in FILES :
         ClaimDrain(s, f) \/ ClaimContended(s, f)
         \/ ReconLateObserve(s, f) \/ ReconRecheck(s, f)
         \/ ReconExpire(s, f) \/ DeadlinePass(s, f)
    \/ \E s \in SESSIONS, w \in WORKERS :
         WkReadRow(s, w) \/ WkReadFile(s, w) \/ WkPersist(s, w)
         \/ WkProcess(s, w) \/ WkFinal(s, w) \/ WkStopped(s, w)
         \/ WkCrash(s, w) \/ WkCancel(s, w)
    \/ Restart

Next == WriterActs \/ MonitorActs

Spec == Init /\ [][Next]_vars

\* Liveness: weak fairness on the monitor's reliable machinery — the
\* level-triggered reconciliation actions, event delivery, drain progress,
\* recovery, and the passage of time.  The writer and the failure
\* environment (crash/cancel/restart) remain unfair.
FairSpec ==
    /\ Spec
    /\ \A s \in SESSIONS :
         /\ WF_vars(Register(s))
         /\ WF_vars(RecoverLinked(s))
         /\ WF_vars(HandoverDrain(s))
         /\ WF_vars(CommitLink(s))
    /\ WF_vars(DeliverCreate)
    /\ \A f \in FILES : WF_vars(DeliverModify(f))
    /\ \A s \in SESSIONS, f \in FILES :
         /\ WF_vars(ClaimDrain(s, f))
         /\ WF_vars(ClaimContended(s, f))
         /\ WF_vars(ReconLateObserve(s, f))
         /\ WF_vars(ReconRecheck(s, f))
         /\ WF_vars(ReconExpire(s, f))
         /\ WF_vars(DeadlinePass(s, f))
    /\ \A s \in SESSIONS, w \in WORKERS :
         /\ WF_vars(WkReadRow(s, w)) /\ WF_vars(WkReadFile(s, w))
         /\ WF_vars(WkPersist(s, w)) /\ WF_vars(WkProcess(s, w))
         /\ WF_vars(WkFinal(s, w))   /\ WF_vars(WkStopped(s, w))

(***************************************************************************)
(* Invariants (safety)                                                     *)
(***************************************************************************)

TypeOK ==
    /\ fExists \subseteq FILES
    /\ fLines \in [FILES -> 0..MaxLines]
    /\ rowPath \in [SESSIONS -> FILES \cup {NoFile}]
    /\ rowOffset \in [SESSIONS -> 0..MaxLines]
    /\ consumed \in [FILES -> Nat]
    /\ evCreate \in Seq(FILES)

\* The row never points at a subagent rollout or another session's file.
NoChildAdoption ==
    \A s \in SESSIONS :
        rowPath[s] # NoFile =>
            /\ FKind[rowPath[s]] = "main"
            /\ FSession[rowPath[s]] = s

\* Every line is delivered downstream at most once.
NoDuplicates ==
    \A f \in FILES : consumed[f] <= fLines[f]

\* The acked offset never exceeds the linked file's real content.
OffsetCoherent ==
    \A s \in SESSIONS :
        rowPath[s] # NoFile => rowOffset[s] <= fLines[rowPath[s]]

\* composer_ready is sticky once the poller sets it.
ComposerSticky ==
    \A s \in SESSIONS : composerEverSet[s] => rowComposer[s]

\* At most one drain worker per session is active (single owner).
SingleOwner ==
    \A s \in SESSIONS :
        Cardinality({w \in WORKERS : wk[<<s, w>>].pc # "idle"}) <= 1

Inv == TypeOK /\ NoChildAdoption /\ NoDuplicates
       /\ OffsetCoherent /\ ComposerSticky

\* Guard-branch reachability probe (must be VIOLATED in the green design):
\* the birth-trust CAS *failure* branch — a trusted CREATE delivered after
\* the row acquired a link, downgraded to characterization — must be
\* reachable, or the first-resolution guard is vacuous (the V1 class).
\* A char track with prov = "na" arises uniquely from the CAS-fail
\* downgrade of a fresh trusted promote (every other char entry stamps a
\* real provenance via CharEntry).
ProbeBirthCASFailUnreached ==
    \A p \in SESSIONS \X FILES :
        ~(track[p].st = "char" /\ track[p].prov = "na")

(***************************************************************************)
(* Temporal properties (liveness)                                          *)
(***************************************************************************)

\* Anti-stall headline: every byte of a linked main rollout eventually
\* becomes operator-visible (auto-0807-225218 violates this).
EventuallyDrained ==
    <>[] (\A s \in SESSIONS :
            rowPath[s] # NoFile => consumed[rowPath[s]] = fLines[rowPath[s]])

\* Every session whose writer produced content eventually links the file
\* the writer actually wrote.
EventuallyLinked ==
    <>[] (\A s \in SESSIONS :
            (wActive[s] # NoFile /\ fLines[wActive[s]] > 0)
                => rowPath[s] = wActive[s])

\* No track sits in CHARACTERIZING forever (bounded responsibility).
CharacterizingResolves ==
    <>[] (\A s \in SESSIONS, f \in FILES : track[<<s, f>>].st # "char")

\* The registry is eventually truthful — no durable "Starting..." card
\* over a rollout that has content.
NoDurableStartingCard ==
    <>[] (\A s \in SESSIONS :
            ~(rowPath[s] # NoFile /\ regLinked[s] /\ regCount[s] = 0
              /\ fLines[rowPath[s]] > 0))

\* Operator requirement (safety half): nothing publishes from a successor
\* file until every existing predecessor has been through its pre-advance
\* final check and everything that check saw is published.
OrderedDelivery ==
    \A f \in FILES :
        consumed[f] > 0 =>
            \A g \in FILES :
                (g \in fExists /\ ChainBefore(g, f)) =>
                    \/ consumed[g] = fLines[g]
                    \/ (g \in sealedSet /\ consumed[g] >= sealedAt[g])

\* Operator requirement (liveness half): for a resolved session every
\* existing main file eventually publishes up to its seal — in full for
\* the linked file; up to the final pre-advance check for superseded
\* files (bytes landing after that check are the documented residual).
PublishedUpToSeal ==
    <>[] (\A s \in SESSIONS :
            rowPath[s] # NoFile =>
                \A f \in FILES :
                    (FSession[f] = s /\ FKind[f] = "main" /\ f \in fExists)
                        => consumed[f] >= (IF f \in sealedSet
                                           THEN sealedAt[f] ELSE fLines[f]))

================================================================================
