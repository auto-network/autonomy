---- MODULE OrgAdmissionLeaves ----
(***************************************************************************)
(* E-any acceptance against rekeyed and re-admitted personas (bead         *)
(* auto-qrmlg.11).  Safety only.                                            *)
(*                                                                         *)
(* A checkpoint commits to LEAVES, each member's CURRENT persona key        *)
(* (membership_commitment.member_pubs: "member.rekey swaps exactly one     *)
(* leaf", membership_commitment.py:185-190).  A leaf is <<persona, v>>,    *)
(* v the number of rekeys of that persona so far.  A persona key is        *)
(* derived from the personal root and the genesis, so a re-admitted        *)
(* persona that did not rekey returns with the SAME leaf.                  *)
(*                                                                         *)
(* The verifier is the founder: it holds the full ledger and checkpoints   *)
(* at every event (P2).  Every proof anyone could offer is on offer: any   *)
(* leaf under any authentic record that commits to it (an old key may be  *)
(* stolen after a rekey; a removed persona keeps its key and its old       *)
(* records).  The acceptance rule decides.                                 *)
(*                                                                         *)
(* Rule:                                                                   *)
(*  "newest"          production: only the newest record                   *)
(*  "predecessor"     C unbounded: newest or the one before                *)
(*  "eany"            E-any: any older authentic record, the leaf in the   *)
(*                    verifier's NEWEST leaf set                           *)
(*  "eany_admission"  E-any, and the record is at or after the record that *)
(*                    appended the persona's current admission             *)
(***************************************************************************)
EXTENDS Naturals, Sequences, FiniteSets

CONSTANTS Personas, F, Rule, MaxEvents

VARIABLES
    fLedger,   \* Seq of [kind: {"claim","remove","rekey"}, who: Personas]
    admitted   \* sticky flags over every admitted proof: [oldKey, preRemoval, stale]

vars == <<fLedger, admitted>>

Pos(k) == 1..k
Count(p, kind, k) == Cardinality({i \in Pos(k) : fLedger[i].kind = kind /\ fLedger[i].who = p})
IsMember(p, k) == Count(p, "claim", k) > Count(p, "remove", k)
Leaf(p, k) == <<p, Count(p, "rekey", k)>>
LeavesAt(k) == {<<F, 0>>} \cup {Leaf(p, k) : p \in {q \in Personas : IsMember(q, k)}}

\* Record s commits the ledger prefix of length s - 1 (record 1 is the seed).
N == Len(fLedger) + 1
RecLeaves(s) == LeavesAt(s - 1)

\* The record created by the persona's latest claim (its current admission).
AdmitRec(p) ==
    LET cl == {i \in Pos(Len(fLedger)) : fLedger[i].kind = "claim" /\ fLedger[i].who = p}
    IN IF cl = {} THEN 0 ELSE (CHOOSE i \in cl : \A j \in cl : j <= i) + 1

Accepts(leaf, s) ==
    /\ leaf \in RecLeaves(s)
    /\ CASE Rule = "newest"         -> s = N
         [] Rule = "predecessor"    -> s \in {N, N - 1}
         [] Rule = "eany"           -> s = N \/ leaf \in RecLeaves(N)
         [] Rule = "eany_admission" -> s = N \/ (leaf \in RecLeaves(N) /\ s >= AdmitRec(leaf[1]))
         [] OTHER -> FALSE

Init == fLedger = << >> /\ admitted = [oldKey |-> FALSE, preRemoval |-> FALSE, stale |-> FALSE]

\* A removal of persona p appended after record s's prefix ended.
RemovedSince(p, s) ==
    \E i \in Pos(Len(fLedger)) : i >= s /\ fLedger[i].kind = "remove" /\ fLedger[i].who = p

Event(kind, p) ==
    /\ Len(fLedger) < MaxEvents
    /\ CASE kind = "claim"  -> ~IsMember(p, Len(fLedger))
         [] kind = "remove" -> IsMember(p, Len(fLedger))
         [] kind = "rekey"  -> IsMember(p, Len(fLedger))
         [] OTHER -> FALSE
    /\ fLedger' = Append(fLedger, [kind |-> kind, who |-> p])
    /\ UNCHANGED admitted

\* Any leaf ever committed, offered under any record committing it.
Prove(leaf, s) ==
    /\ s \in 1..N
    /\ Accepts(leaf, s)
    /\ admitted' = [oldKey     |-> admitted.oldKey \/ leaf \notin RecLeaves(N),
                    preRemoval |-> admitted.preRemoval \/ (leaf[1] # F /\ RemovedSince(leaf[1], s)),
                    stale      |-> admitted.stale \/ s # N]
    /\ UNCHANGED fLedger

AllLeaves == UNION {RecLeaves(s) : s \in 1..N}

Next ==
    \/ \E k \in {"claim", "remove", "rekey"}, p \in Personas : Event(k, p)
    \/ \E s \in 1..N : \E leaf \in AllLeaves : Prove(leaf, s)

Spec == Init /\ [][Next]_vars

\* After member.rekey, a proof under the member's OLD leaf via an older
\* authentic record is refused: every admitted leaf is in the verifier's
\* newest leaf set at the time of admission.
RekeyedOldKeyExcluded == ~admitted.oldKey

\* A removed then re-admitted persona proves only under a record at or
\* after its re-admission: no admitted proof is under a record from before
\* an intervening removal of that persona.
ReAdmitAfterRemoval == ~admitted.preRemoval

\* Non-vacuity: some proof under an OLDER record is admitted.
NoStaleAdmission == ~admitted.stale

====
