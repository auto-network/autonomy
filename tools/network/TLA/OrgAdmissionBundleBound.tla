---- MODULE OrgAdmissionBundleBound ----
(***************************************************************************)
(* bundle_adopt with a registry bound, against an adversarial sponsor     *)
(* (auto-qrmlg.3 review; coordinator decisions 2026-09-26).                *)
(*                                                                         *)
(* One joiner J (the victim), the founder F (honest checkpointer), and an  *)
(* honest registry that serves its current tuple only.  The founder's      *)
(* ledger follows a fixed script:                                          *)
(*   e1 admits J, e2 admits K, e3 rekeys K (a leaf swap), e4 removes K.    *)
(* A root is the set of current LEAVES (persona, key version)               *)
(* (membership_commitment.member_pubs, py:185-190), so a rekey changes the *)
(* root.  P2 checkpoints every admission and removal; RekeyCheckpointed    *)
(* says whether a rekey is checkpointed too.  Registry seq s names the     *)
(* s-th checkpointed head (seq 0 = the seed at genesis).                   *)
(*                                                                         *)
(* The install is two steps, as in code: the sponsor serves the bundle     *)
(* (snapshot prefix k and a record [seq, head], the record read before the *)
(* events), then the install reads the registry's current tuple ONCE.      *)
(* Founder events may land in between.                                     *)
(*                                                                         *)
(* Install rule (the decision): b is adopted iff                           *)
(*   Bound: b.seq <= reg.seq, and at equal seq b is the registry's record; *)
(*   the fold check: b.head <= k; the root adopted is Root(b.head);         *)
(*   seq-monotone retention.                                               *)
(* The registry's record is retained by fold when its head is held; the    *)
(* page makes one recovery call; AutoAdopt (C3 item (c)) fold-adopts the   *)
(* registry tuple whenever its head is held.                               *)
(*                                                                         *)
(* A retained record is [seq, root].  The adversarial sponsor may serve    *)
(* any seq with the root of the fold at any head h <= k (an authentic      *)
(* root, possibly never checkpointed or checkpointed at another seq), and  *)
(* may withhold events.                                                    *)
(*                                                                         *)
(* Hello (E-any-adm + prover-downgrade), two keyings:                      *)
(*  RootMatch = FALSE  the verifier looks up its record BY SEQ: the proof  *)
(*                     verifies iff J's root at that seq is the verifier's *)
(*  RootMatch = TRUE   the verifier recomputes the root from the proof and *)
(*                     accepts iff SOME record it retains has that root, at *)
(*                     or after J's admission; the server proves back       *)
(*                     under that root with the client's label             *)
(* In both, J must be in the verifier's newest set.  Candidate Rebundle    *)
(* re-requests the bundle while the newest record cannot complete a hello; *)
(* candidate Exact adopts b only if it equals the registry's record.       *)
(* ProveOwnFold (with RootMatch): the prover need not hold a record: it    *)
(* proves under the root of its own fold at any head it holds that         *)
(* includes it; the verifier accepts iff that root is one of its retained  *)
(* records at or after the prover's admission.                              *)
(***************************************************************************)
EXTENDS Integers, FiniteSets, Sequences

CONSTANTS AdversarialSponsor, Withhold, AutoAdopt, Bound, AdoptableOnly,
          Exact, Rebundle, RootMatch, RekeyCheckpointed,
          ProveOwnFold   \* with RootMatch: J proves under the root of its own fold at any head it holds

MaxEv == 4
\* Leaves after the first k events.
Leaves(k) ==
    {<<"F", 0>>}
      \cup (IF k >= 1 THEN {<<"J", 0>>} ELSE {})
      \cup (IF k >= 2 /\ k < 4 THEN {<<"K", IF k >= 3 THEN 1 ELSE 0>>} ELSE {})
Root(k) == Leaves(k)
HasJ(root) == <<"J", 0>> \in root

\* Heads (ledger lengths) the founder checkpoints: every event under P2,
\* except a rekey when RekeyCheckpointed is FALSE.
Checkpointed(h) == h # 3 \/ RekeyCheckpointed
CkHeads(n) == {h \in 0..n : Checkpointed(h)}
RegSeq(n) == Cardinality(CkHeads(n)) - 1
HeadOf(s) == CHOOSE h \in 0..MaxEv : Checkpointed(h) /\ Cardinality(CkHeads(h)) - 1 = s
MaxSeq == RegSeq(MaxEv)
AdmitSeq == 1                        \* J's admission record (head 1)

VARIABLES fLen, bundle, jLen, ret, recovered, synced, tried

vars == <<fLen, bundle, jLen, ret, recovered, synced, tried>>

None == [k |-> -1, seq |-> -1, head |-> -1]
RS == RegSeq(fLen)
RH == HeadOf(RS)

NewestSeq == IF ret = {} THEN -1 ELSE CHOOSE s \in {r.seq : r \in ret} :
                 \A t \in {r.seq : r \in ret} : t <= s
Newest == IF ret = {} THEN [seq |-> -1, root |-> {}] ELSE CHOOSE r \in ret : r.seq = NewestSeq
Keep(r) == r.seq > NewestSeq

Init ==
    /\ fLen = 0 /\ bundle = None /\ jLen = -1 /\ ret = {}
    /\ recovered = FALSE /\ synced = FALSE /\ tried = FALSE

FounderEvent ==
    /\ fLen < MaxEv
    /\ fLen' = fLen + 1
    /\ synced' = FALSE
    /\ UNCHANGED <<bundle, jLen, ret, recovered, tried>>

Serve(k, s, h) ==
    /\ bundle = None /\ fLen >= 1
    /\ k \in (IF Withhold THEN 1..fLen ELSE {fLen})
    /\ IF AdversarialSponsor
         THEN /\ s \in 0..MaxSeq /\ h \in 0..k
              /\ AdoptableOnly => (s <= RS /\ (s = RS => h = RH) /\ s >= 1)
         ELSE /\ s = RegSeq(k) /\ h = HeadOf(RegSeq(k))   \* its newest record, genuine
    /\ bundle' = [k |-> k, seq |-> s, head |-> h]
    /\ UNCHANGED <<fLen, jLen, ret, recovered, synced, tried>>

Install ==
    /\ bundle # None /\ ~tried /\ (jLen = -1 \/ (Exact /\ ret = {}) \/ Rebundle)
    /\ tried' = TRUE
    /\ LET k == bundle.k
           b == [seq |-> bundle.seq, root |-> Root(bundle.head)]
           bOK == /\ bundle.head <= k
                  /\ (Bound => (bundle.seq <= RS /\ (bundle.seq = RS => bundle.head = RH)))
                  /\ (Exact => (bundle.seq = RS /\ bundle.head = RH))
           r == [seq |-> RS, root |-> Root(RH)]
           rOK == RH <= k
       IN /\ jLen' = IF k > jLen THEN k ELSE jLen
          /\ ret' = ret \cup (IF bOK /\ Keep(b) THEN {b} ELSE {}) \cup
                    (IF rOK /\ Keep(r) THEN {r} ELSE {})
    /\ UNCHANGED <<fLen, bundle, recovered, synced>>

RegAdopt ==
    /\ jLen >= 0 /\ RH <= jLen
    /\ Keep([seq |-> RS, root |-> Root(RH)])
    /\ ret' = ret \cup {[seq |-> RS, root |-> Root(RH)]}
    /\ UNCHANGED <<fLen, bundle, jLen, recovered, synced, tried>>

Recover ==
    /\ jLen >= 0 /\ ~recovered
    /\ recovered' = TRUE
    /\ IF RH <= jLen /\ Keep([seq |-> RS, root |-> Root(RH)])
         THEN ret' = ret \cup {[seq |-> RS, root |-> Root(RH)]}
         ELSE UNCHANGED ret
    /\ UNCHANGED <<fLen, bundle, jLen, synced, tried>>

\* The prover's record: the newest retained record that includes J.
ProverRec ==
    LET inc == {r \in ret : HasJ(r.root)}
    IN IF inc = {} THEN [seq |-> -1, root |-> {}]
       ELSE CHOOSE r \in inc : \A q \in inc : q.seq <= r.seq

\* The founder retains every genuine record up to the registry's seq.
OwnFoldOK ==
    \E h \in 1..jLen : HasJ(Root(h)) /\ \E s \in AdmitSeq..RS : Root(HeadOf(s)) = Root(h)

HelloOK ==
    LET r == ProverRec IN
    /\ HasJ(Leaves(fLen))                           \* J in the verifier's newest set
    /\ IF RootMatch /\ ProveOwnFold THEN OwnFoldOK ELSE r.seq >= 0 /\ HasJ(r.root)
    /\ IF RootMatch /\ ProveOwnFold
         THEN TRUE
         ELSE IF RootMatch
         THEN \E s \in AdmitSeq..RS : Root(HeadOf(s)) = r.root
         ELSE /\ r.seq >= AdmitSeq /\ r.seq <= RS
              /\ r.root = Root(HeadOf(r.seq))

Pull ==
    /\ jLen >= 0 /\ HelloOK
    /\ (jLen < fLen \/ ~synced)
    /\ jLen' = fLen
    /\ synced' = TRUE
    /\ UNCHANGED <<fLen, bundle, ret, recovered, tried>>

Retry ==
    /\ jLen >= 0 /\ bundle # None /\ tried
    /\ \/ Exact /\ ret = {}
       \/ Rebundle /\ ~HelloOK
    /\ bundle' = None /\ tried' = FALSE
    /\ UNCHANGED <<fLen, jLen, ret, recovered, synced>>

Next == FounderEvent \/ (\E k, h \in 0..MaxEv, s \in 0..MaxSeq : Serve(k, s, h))
        \/ Install \/ Retry \/ Recover \/ Pull \/ (AutoAdopt /\ RegAdopt)

Spec == Init /\ [][Next]_vars
        /\ WF_vars(Install) /\ WF_vars(Recover) /\ WF_vars(Pull) /\ WF_vars(Retry)
        /\ (AutoAdopt => WF_vars(RegAdopt))
        /\ WF_vars(\E k, h \in 0..MaxEv, s \in 0..MaxSeq : Serve(k, s, h))

(* Liveness: eventually, for good, J holds the registry's newest record    *)
(* (genuine), every event, and has pulled under it.                        *)
Healed == /\ Newest.seq = RS /\ Newest.root = Root(RH)
          /\ jLen = fLen /\ synced
EventuallyHealed == <>[](fLen >= 1 => Healed)

(* Safety.                                                                  *)
NoRegression == [][NewestSeq' >= NewestSeq]_vars
NotAboveRegistry == NewestSeq <= RS

====
