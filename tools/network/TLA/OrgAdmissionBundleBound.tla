---- MODULE OrgAdmissionBundleBound ----
(***************************************************************************)
(* bundle_adopt with a registry bound, against an adversarial sponsor     *)
(* (auto-qrmlg.3 review of 7aee75ad; coordinator decision 2026-09-26).     *)
(*                                                                         *)
(* One joiner J (the victim), the founder F (honest checkpointer, P2: a    *)
(* checkpoint at every ledger event), and the registry (honest; serves its *)
(* current tuple only).  The founder's ledger follows a fixed script:      *)
(* event 1 admits J, event 2 admits K, event 3 removes K.  Record seq s    *)
(* commits the ledger prefix of length s (seq 0 = the seed at genesis), so *)
(* the genuine root at seq s is Root(s) and its head is s.                 *)
(*                                                                         *)
(* The joiner's install is two steps, as in code: the sponsor serves the   *)
(* bundle (snapshot prefix k and a record b = [seq, head]; the sponsor     *)
(* reads b before the events), then the install reads the registry's       *)
(* current tuple reg ONCE.  Founder events may land in between.            *)
(*                                                                         *)
(* Install rule (the decision): b is adopted iff                           *)
(*   b.seq <= reg.seq, and b.seq = reg.seq => b.head = reg.head (same root);*)
(*   the fold check: b.head <= k (head held) — the root adopted is then     *)
(*   Root(b.head), recomputed;                                             *)
(*   seq-monotone retention.                                               *)
(* reg itself is retained by fold when reg.head <= k.  The page's one      *)
(* recovery call then adopts the registry's current tuple by fold.         *)
(*                                                                         *)
(* A retained record is [seq, root] where root is what the joiner's fold   *)
(* computed; it is GENUINE iff root = Root(seq).  The adversarial sponsor  *)
(* may serve any seq (the install's bound is what refuses a high one) with *)
(* any authentic pair (root of the fold at head h, head h <= k), and may   *)
(* withhold events (k below its ledger length) when Withhold is TRUE.      *)
(*                                                                         *)
(* Pulls follow E-any-adm + prover-downgrade: the joiner proves under its  *)
(* newest retained record that includes it; the founder proves back under *)
(* the same seq.  A proof verifies only when both sides hold the SAME root *)
(* at that seq (the path is computed against the prover's root and checked *)
(* against the verifier's).                                                *)
(***************************************************************************)
EXTENDS Integers, FiniteSets

CONSTANTS AdversarialSponsor, Withhold, AutoAdopt, Bound,
          AdoptableOnly, \* the adversary serves only records the install check would accept at serve time
          Exact,         \* candidate: adopt b only if it EQUALS the registry tuple; else re-request the bundle
          Rebundle       \* candidate: keep the bound; re-request the bundle while the newest record cannot complete a hello

MaxEv == 3
Members(k) == {"F"} \cup (IF k >= 1 THEN {"J"} ELSE {})
                    \cup (IF k >= 2 /\ k < 3 THEN {"K"} ELSE {})
Root(k) == Members(k)            \* a root is identified with its member set

VARIABLES
    fLen,      \* founder ledger length = registry current seq (P2)
    bundle,    \* [k, seq, head] served by the sponsor, or [k |-> -1] before
    jLen,      \* prefix length the joiner holds; -1 before install
    ret,       \* set of [seq, root]: records the joiner retains
    recovered, \* the page's one recovery call was made
    synced,    \* pulled from and by the founder under the founder's newest
    tried      \* the install has processed the current bundle

vars == <<fLen, bundle, jLen, ret, recovered, synced, tried>>

None == [k |-> -1, seq |-> -1, head |-> -1]

NewestSeq == IF ret = {} THEN -1 ELSE CHOOSE s \in {r.seq : r \in ret} :
                 \A t \in {r.seq : r \in ret} : t <= s
Newest == IF ret = {} THEN [seq |-> -1, root |-> {}] ELSE CHOOSE r \in ret : r.seq = NewestSeq

Init ==
    /\ fLen = 0 /\ bundle = None /\ jLen = -1 /\ ret = {}
    /\ recovered = FALSE /\ synced = FALSE /\ tried = FALSE

\* The founder's next scripted event (admit J, admit K, remove K), with P2.
FounderEvent ==
    /\ fLen < MaxEv
    /\ fLen' = fLen + 1
    /\ synced' = FALSE
    /\ UNCHANGED <<bundle, jLen, ret, recovered, tried>>

\* The sponsor serves the bundle once J is admitted.
Serve(k, s, h) ==
    /\ bundle = None /\ fLen >= 1
    /\ k \in (IF Withhold THEN 1..fLen ELSE {fLen})
    /\ IF AdversarialSponsor
         THEN /\ s \in 0..MaxEv /\ h \in 0..k  \* any seq, any authentic (root, head)
              /\ AdoptableOnly => (s <= fLen /\ (s = fLen => h = fLen) /\ s >= 1)
         ELSE s = k /\ h = k                   \* the honest sponsor's newest record
    /\ bundle' = [k |-> k, seq |-> s, head |-> h]
    /\ UNCHANGED <<fLen, jLen, ret, recovered, synced, tried>>

Keep(r) == r.seq > NewestSeq             \* seq-monotone retention

\* The install: reads the registry once (reg = fLen now), adopts b under the
\* bound and the fold check, retains reg by fold when its head is held.
Install ==
    /\ bundle # None /\ ~tried /\ (jLen = -1 \/ (Exact /\ ret = {}) \/ Rebundle)
    /\ tried' = TRUE
    /\ LET k == bundle.k
           b == [seq |-> bundle.seq, root |-> Root(bundle.head)]
           bOK == /\ bundle.head <= k
                  /\ (Bound => (bundle.seq <= fLen /\ (bundle.seq = fLen => bundle.head = fLen)))
                  /\ (Exact => (bundle.seq = fLen /\ bundle.head = fLen))
           r == [seq |-> fLen, root |-> Root(fLen)]
           rOK == fLen <= k
       IN /\ jLen' = k
          /\ ret' = ret \cup (IF bOK /\ Keep(b) THEN {b} ELSE {}) \cup
                    (IF rOK /\ Keep(r) THEN {r} ELSE {})
    /\ UNCHANGED <<fLen, bundle, recovered, synced>>

\* A registry adoption by fold: the page's one recovery call, or (AutoAdopt)
\* a machine step whenever the head is held.
RegAdopt ==
    /\ jLen >= 0
    /\ fLen <= jLen
    /\ Keep([seq |-> fLen, root |-> Root(fLen)])
    /\ ret' = ret \cup {[seq |-> fLen, root |-> Root(fLen)]}
    /\ UNCHANGED <<fLen, bundle, jLen, recovered, synced, tried>>

Recover ==
    /\ jLen >= 0 /\ ~recovered
    /\ recovered' = TRUE
    /\ IF fLen <= jLen /\ Keep([seq |-> fLen, root |-> Root(fLen)])
         THEN ret' = ret \cup {[seq |-> fLen, root |-> Root(fLen)]}
         ELSE UNCHANGED ret
    /\ UNCHANGED <<fLen, bundle, jLen, synced, tried>>

\* The prover's record: the newest retained record that includes J.
ProverRec ==
    LET inc == {r \in ret : "J" \in r.root}
    IN IF inc = {} THEN [seq |-> -1, root |-> {}]
       ELSE CHOOSE r \in inc : \A q \in inc : q.seq <= r.seq

\* The mutual hello verifies iff J's root at that seq is the founder's
\* (genuine) root there, J is in the founder's newest set, and the record is
\* at or after J's admission (seq >= 1).
HelloOK ==
    LET r == ProverRec IN
    /\ r.seq >= 1
    /\ r.root = Root(r.seq)
    /\ "J" \in Members(fLen)

Pull ==
    /\ jLen >= 0 /\ HelloOK
    /\ (jLen < fLen \/ ~synced)
    /\ jLen' = fLen
    /\ synced' = TRUE
    /\ UNCHANGED <<fLen, bundle, ret, recovered, tried>>

\* "Exact": the install adopted nothing, so the page re-requests the bundle
\* over the still-open join channel (a fresh snapshot and record).
Retry ==
    /\ jLen >= 0 /\ bundle # None /\ tried
    /\ \/ Exact /\ ret = {}
       \/ Rebundle /\ ~HelloOK
    /\ bundle' = None /\ tried' = FALSE
    /\ UNCHANGED <<fLen, jLen, ret, recovered, synced>>

Next == FounderEvent \/ (\E k, s, h \in 0..MaxEv : Serve(k, s, h)) \/ Install \/ Retry
        \/ Recover \/ Pull \/ (AutoAdopt /\ RegAdopt)

Spec == Init /\ [][Next]_vars
        /\ WF_vars(Install) /\ WF_vars(Recover) /\ WF_vars(Pull) /\ WF_vars(Retry)
        /\ (AutoAdopt => WF_vars(RegAdopt))
        /\ WF_vars(\E k, s, h \in 0..MaxEv : Serve(k, s, h))

(* Liveness: eventually, for good, J holds the founder's newest record     *)
(* (genuine), all its events, and has pulled under it.                     *)
Healed == /\ Newest.seq = fLen /\ Newest.root = Root(fLen)
          /\ jLen = fLen /\ synced
EventuallyHealed == <>[](fLen >= 1 => Healed)

(* Safety: retention never regresses; the joiner's newest is never above  *)
(* the registry's current seq.                                             *)
NoRegression == [][NewestSeq' >= NewestSeq]_vars
NotAboveRegistry == NewestSeq <= fLen

====
