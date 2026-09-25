---- MODULE OrgAdmission ----
(***************************************************************************)
(* Org admission -> checkpoint -> adoption -> org sync pull (bead           *)
(* auto-qrmlg.5; ceremony record graph://cde6c8c6-041).                     *)
(*                                                                         *)
(* Actors: the founder F (the only checkpointer), joiners, the registry,   *)
(* and an outsider X that is not a member.  The founder's ledger is linear:*)
(* every admission appends one claim event parented on the current head   *)
(* (claim_service.py:170-175 refuses a claim on stale heads).  An event is *)
(* named by the joiner it admits; "g" is genesis.                          *)
(*                                                                         *)
(* The CURRENT rules, each from code at master effd6489:                   *)
(*  C1 a joiner's ledger rows come only from its own appends or rows a pull*)
(*     delivers (ledger/settings_bridge.py:11-18)                          *)
(*  C2 a pull is admitted only when the verifier has adopted the seq the   *)
(*     peer proves under and the peer is included under its members_root  *)
(*     (fleet_org_channel.py:286-309); a node holds exactly one adopted    *)
(*     record, so both sides must hold the same seq                        *)
(*     (org_sync_channels.py:189-193); both hellos are verified            *)
(*  C3 at join the joiner adopts the registry's CURRENT checkpoint          *)
(*     (network_routes.py:943-945): Adopt may run as soon as Bootstrap has *)
(*  C4 bootstrap carries the founder's events as of that moment            *)
(*     (claim_service.py:233-235)                                          *)
(*  C5 every admission appends to the founder's ledger                     *)
(*     (claim_service.py:172-175)                                          *)
(*  C6 a checkpoint's ledger_head is the founder's first head at assembly  *)
(*     (membership_checkpoint.py:103-106); with a linear ledger, its head  *)
(*  C7 adoption folds the LOCAL ledger at ledger_head and requires         *)
(*     members_root equality; an absent head raises and nothing is adopted *)
(*     (network_routes.py:759-768, fold.py:499-500, ledger.py:36-42,66-77) *)
(*  C8 the checkpoint is published only in a founder sign-on               *)
(*     (signon_preparation.py:125-133, network-signon.mjs:1560-1577)       *)
(*                                                                         *)
(* The PROPOSED rules:                                                     *)
(*  P1 AdoptByVerification: a node adopts a registry checkpoint whose      *)
(*     signer is in the previous checkpointers root and whose prev chains  *)
(*     to the adopted record; no local head is needed.  The prover's own  *)
(*     inclusion path must then come with the checkpoint (today it is      *)
(*     folded locally at the head, org_sync_channels.py:173-179, 197-205): *)
(*     the model grants a member its path.                                 *)
(*  P2 CheckpointAtAdmission: the admission and the checkpoint that first  *)
(*     includes the admitted persona are one step (a hot checkpoint signer *)
(*     or the approver's window).                                          *)
(*                                                                         *)
(*  P3 AdmitOnApproval: under a role that requires approval, the founder's *)
(*     countersign appends the staged claim (today it only stores the      *)
(*     approval, and the joiner's second ceremony re-mints and appends:    *)
(*     claim_service.py:189-193, accept-controller.js:270-319).            *)
(*                                                                         *)
(* Approval: the invite's role requires the founder's approval.  Claim,    *)
(* Approve and Finalize are human ceremonies and are never assumed fair.   *)
(*                                                                         *)
(* FounderSignsOn: the stand-alone checkpoint (C8) is weakly fair, i.e.    *)
(* the founder eventually signs on again.  A human ceremony is not         *)
(* assumed fair unless the configuration says so.                          *)
(*                                                                         *)
(* RegistryForges: the registry (untrusted for rosters) may serve one      *)
(* forged record naming the outsider, not signed by a checkpointer.       *)
(* VerifySigner: P1 checks the signer; FALSE deletes that check (a         *)
(* calibration).                                                           *)
(***************************************************************************)
EXTENDS Naturals, Sequences, FiniteSets

CONSTANTS Joiners,              \* model values
          F, X,                 \* the founder; the outsider
          AdoptByVerification,  \* P1
          CheckpointAtAdmission,\* P2
          AdmitOnApproval,      \* P3
          Approval,             \* the role requires the founder's approval
          FounderSignsOn,       \* weak fairness of the stand-alone checkpoint
          RegistryForges,       \* the registry may serve one forged record
          VerifySigner          \* P1's signer check (TRUE except in a calibration)

Nodes  == {F} \cup Joiners
Actors == Nodes \cup {X}

VARIABLES
    fLedger,   \* Seq(Joiners): the founder's claim events in append order
    jHas,      \* [Joiners -> SUBSET ({"g"} \cup Joiners)]: {} = not installed
    cps,       \* Seq of [head, members, auth]: registry records; the last is current
    adopted,   \* [Actors -> Nat]: index into cps of the adopted record; 0 = none
    pulls,     \* set of [a, b, s]: a pulled from b under record s
    forged,    \* the registry has served its forged record
    claimed,   \* joiners that submitted a claim (J3)
    approved   \* joiners whose claim the founder countersigned (F3)

vars == <<fLedger, jHas, cps, adopted, pulls, forged, claimed, approved>>

Admitted == {fLedger[i] : i \in 1..Len(fLedger)}
FHas     == {"g"} \cup Admitted
FHead    == IF fLedger = << >> THEN "g" ELSE fLedger[Len(fLedger)]

\* Members of the founder's ledger folded at head h (linear ledger).
MembersAt(h) ==
    IF h = "g" THEN {F}
    ELSE {F} \cup {fLedger[i] : i \in 1..(CHOOSE k \in 1..Len(fLedger) : fLedger[k] = h)}

Has(n) == IF n = F THEN FHas ELSE jHas[n]
Installed(n) == IF n = F THEN TRUE ELSE jHas[n] # {}

NewCheckpoint == [head |-> FHead, members |-> {F} \cup Admitted, auth |-> TRUE]

Init ==
    /\ fLedger = << >>
    /\ jHas = [j \in Joiners |-> {}]
    \* F0: the seed, root-signed at genesis, adopted by the founder.
    /\ cps = << [head |-> "g", members |-> {F}, auth |-> TRUE] >>
    /\ adopted = [n \in Actors |-> IF n = F THEN 1 ELSE 0]
    /\ pulls = {}
    /\ forged = FALSE
    /\ claimed = {}
    /\ approved = {}

\* C5 (+P2): the founder's connector appends the claim; under P2 the
\* checkpoint that first includes it is the same step.
AdmitStep(j) ==
    /\ fLedger' = Append(fLedger, j)
    /\ IF CheckpointAtAdmission
         THEN LET rec == [head |-> j, members |-> {F} \cup Admitted \cup {j}, auth |-> TRUE]
              IN /\ cps' = Append(cps, rec)
                 /\ adopted' = [adopted EXCEPT ![F] = Len(cps) + 1]
         ELSE UNCHANGED <<cps, adopted>>

\* J3: the joiner's claim; a self-admitting role is appended at submit.
Claim(j) ==
    /\ j \notin claimed
    /\ claimed' = claimed \cup {j}
    /\ IF Approval THEN UNCHANGED <<fLedger, cps, adopted>> ELSE AdmitStep(j)
    /\ UNCHANGED <<jHas, pulls, forged, approved>>

\* F3: the founder countersigns; under P3 that appends the claim.
Approve(j) ==
    /\ Approval
    /\ j \in claimed \ approved
    /\ approved' = approved \cup {j}
    /\ IF AdmitOnApproval THEN AdmitStep(j) ELSE UNCHANGED <<fLedger, cps, adopted>>
    /\ UNCHANGED <<jHas, pulls, forged, claimed>>

\* J4: the joiner re-mints the claim with the approvals; it is appended.
Finalize(j) ==
    /\ Approval /\ ~AdmitOnApproval
    /\ j \in approved \ Admitted
    /\ AdmitStep(j)
    /\ UNCHANGED <<jHas, pulls, forged, claimed, approved>>

\* C4: install from the founder's ledger as of now.
Bootstrap(j) ==
    /\ j \in Admitted
    /\ jHas[j] = {}
    /\ jHas' = [jHas EXCEPT ![j] = FHas]
    /\ UNCHANGED <<fLedger, cps, adopted, pulls, forged, claimed, approved>>

\* C6/C8: the founder's sign-on publishes when the member set changed.
Checkpoint ==
    /\ cps[adopted[F]].members # {F} \cup Admitted
    /\ cps' = Append(cps, NewCheckpoint)
    /\ adopted' = [adopted EXCEPT ![F] = Len(cps) + 1]
    /\ UNCHANGED <<fLedger, jHas, pulls, forged, claimed, approved>>

\* The untrusted registry serves a record naming the outsider.
Forge ==
    /\ RegistryForges /\ ~forged
    /\ cps' = Append(cps, [head |-> FHead,
                           members |-> cps[Len(cps)].members \cup {X},
                           auth |-> FALSE])
    /\ forged' = TRUE
    /\ UNCHANGED <<fLedger, jHas, adopted, pulls, claimed, approved>>

\* C7 or P1: a joiner adopts the registry's current record.
Acceptable(j, rec) ==
    IF AdoptByVerification
      THEN ~VerifySigner \/ rec.auth
      ELSE /\ rec.head \in jHas[j]
           /\ rec.members = MembersAt(rec.head)

Adopt(j) ==
    LET c == Len(cps) IN
    /\ Installed(j)
    /\ c > adopted[j]
    /\ Acceptable(j, cps[c])
    /\ adopted' = [adopted EXCEPT ![j] = c]
    /\ UNCHANGED <<fLedger, jHas, cps, pulls, forged, claimed, approved>>

\* The outsider adopts whatever it likes; it proves nothing honestly.
OutsiderAdopt(s) ==
    /\ s \in 1..Len(cps)
    /\ adopted' = [adopted EXCEPT ![X] = s]
    /\ UNCHANGED <<fLedger, jHas, cps, pulls, forged, claimed, approved>>

\* C2: an honest party checks the other's inclusion under the record it has
\* adopted and must itself be included to prove (its own rider).
HonestSide(h, other, s) ==
    /\ Installed(h)
    /\ adopted[h] = s
    /\ h \in cps[s].members
    /\ other \in cps[s].members

Pull(a, b) ==
    \E s \in 1..Len(cps) :
      /\ a # b
      /\ a \in Nodes \/ b \in Nodes
      /\ \A h \in {a, b} \cap Nodes : HonestSide(h, IF h = a THEN b ELSE a, s)
      /\ [a |-> a, b |-> b, s |-> s] \notin pulls
         \/ (a \in Joiners /\ ~(Has(b) \subseteq jHas[a]))
      /\ pulls' = pulls \cup {[a |-> a, b |-> b, s |-> s]}
      /\ jHas' = IF a \in Joiners /\ b \in Nodes
                   THEN [jHas EXCEPT ![a] = @ \cup Has(b)] ELSE jHas
      /\ UNCHANGED <<fLedger, cps, adopted, forged, claimed, approved>>

Next ==
    \/ \E j \in Joiners : Claim(j) \/ Approve(j) \/ Finalize(j) \/ Bootstrap(j) \/ Adopt(j)
    \/ Checkpoint
    \/ Forge
    \/ \E s \in 1..Len(cps) : OutsiderAdopt(s)
    \/ \E a, b \in Actors : Pull(a, b)

Fairness ==
    /\ \A j \in Joiners : WF_vars(Bootstrap(j)) /\ WF_vars(Adopt(j))
    /\ \A a, b \in Nodes : WF_vars(Pull(a, b))
    /\ FounderSignsOn => WF_vars(Checkpoint)

Spec == Init /\ [][Next]_vars /\ Fairness

---------------------------------------------------------------------------
(* Safety: no pull without inclusion under an adopted, authentic checkpoint *)
(* that the founder's ledger actually produces.                            *)
NoPullWithoutInclusion ==
    \A p \in pulls :
        /\ cps[p.s].auth
        /\ p.a \in cps[p.s].members /\ p.b \in cps[p.s].members
        /\ cps[p.s].members \subseteq {F} \cup Admitted

OutsiderNeverPulls == \A p \in pulls : p.a # X /\ p.b # X

\* P3 admits nothing the founder did not approve.
AdmittedWereApproved == Approval => Admitted \subseteq approved

AdoptedIsAuthentic == \A n \in Nodes : adopted[n] > 0 => cps[adopted[n]].auth

TypeOK ==
    /\ \A n \in Actors : adopted[n] \in 0..Len(cps)
    /\ \A j \in Joiners : jHas[j] \subseteq {"g"} \cup Joiners

(* Liveness: eventually, for good, every admitted member has adopted the   *)
(* founder's current checkpoint and has pulled from the founder and been  *)
(* pulled by it under that checkpoint.                                     *)
InSync(j) ==
    LET s == adopted[F] IN
    /\ adopted[j] = s
    /\ [a |-> j, b |-> F, s |-> s] \in pulls
    /\ [a |-> F, b |-> j, s |-> s] \in pulls

EveryAdmittedMemberPulls == <>[](\A j \in Admitted : InSync(j))

(* Admission does not wait on a second joiner ceremony. *)
ApprovedIsAdmitted == \A j \in Joiners : j \in approved ~> j \in Admitted

(* Executability: negated, so TLC must find a state where every joiner is  *)
(* admitted and in sync; a green result that never gets there is vacuous.  *)
NotAllJoinedInSync == ~(Admitted = Joiners /\ \A j \in Joiners : InSync(j))

====
