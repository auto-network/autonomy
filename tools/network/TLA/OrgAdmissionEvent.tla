---- MODULE OrgAdmissionEvent ----
(***************************************************************************)
(* The admission event (bead auto-qrmlg.12; operator ruling 2026-09-25):   *)
(* an approver-authored ledger event carries the invitee's ORIGINAL signed *)
(* member.claim, unchanged, plus the approvals; the fold admits on it.     *)
(* This realizes P3 (admit on approval) without re-signing the claim: a    *)
(* claim's author signature covers its whole payload including approvals  *)
(* and parents (events.py:618-619), so the approver cannot add approvals  *)
(* to it, and an approval covers only (kind, invite_ref, persona)          *)
(* (events.py:737-744), so approvals are reusable across claims of one    *)
(* persona for one invite.                                                 *)
(*                                                                         *)
(* Actors: the founder F (honest approver, the only checkpointer); A, a    *)
(* legitimate member approver whose key the adversary holds (it authors   *)
(* admission events at will: forged claims under its own key, its own     *)
(* approval, approvals copied from the ledger, replays of existing         *)
(* events); honest invitees, who sign only their own claims.               *)
(*                                                                         *)
(* The fold's checks on an admission event, each deletable:               *)
(*  CheckSig        the carried claim verifies under the invitee's key and *)
(*                  was submitted by the invitee at its staged position    *)
(*  CheckThreshold  at least Threshold distinct authorized approvals       *)
(*  CheckRedeemed   the claim's invite is not already redeemed             *)
(*  CheckFloor      the claim's position is after the persona's latest     *)
(*                  removal (the admission floor of E-any-adm)             *)
(* A persona already a member is never re-admitted.                        *)
(*                                                                         *)
(* P2: the founder's approval appends the admission event AND publishes    *)
(* the checkpoint reflecting it in the same step.                          *)
(***************************************************************************)
EXTENDS Naturals, Sequences, FiniteSets

CONSTANTS Personas, F, A, Threshold, MaxEvents, MaxInvites,
          CheckSig, CheckThreshold, CheckRedeemed, CheckFloor

Approvers == {F, A}

VARIABLES
    fLedger,    \* Seq of [kind: "admit", claim, approvals] | [kind: "remove", who]
    submitted,  \* claims honest invitees signed and submitted (staged)
    nextInvite, \* invites issued so far (an invite is a number)
    approved,   \* claims the founder approved
    cps,        \* Seq of member sets: checkpoints (the last is current)
    p2ok        \* every founder approval published a checkpoint including it

vars == <<fLedger, submitted, nextInvite, approved, cps, p2ok>>

\* A claim: [who, pos (ledger length it is parented on), invite, signer].
Claim(p, pos, inv, signer) == [who |-> p, pos |-> pos, invite |-> inv, signer |-> signer]

(* ── The fold ─────────────────────────────────────────────────────────── *)

Empty == [members |-> {F}, redeemed |-> {}, lastRemove |-> [p \in Personas |-> 0],
          accepted |-> {}]

Admits(st, ev, i) ==
    LET c == ev.claim IN
    /\ c.who \notin st.members
    /\ CheckSig       => (c.signer = c.who /\ c \in submitted)
    /\ CheckThreshold => Cardinality(ev.approvals \cap Approvers) >= Threshold
    /\ CheckRedeemed  => c.invite \notin st.redeemed
    /\ CheckFloor     => c.pos >= st.lastRemove[c.who]

Step(st, ev, i) ==
    IF ev.kind = "admit"
      THEN IF Admits(st, ev, i)
             THEN [st EXCEPT !.members = @ \cup {ev.claim.who},
                             !.redeemed = @ \cup {ev.claim.invite},
                             !.accepted = @ \cup {[i |-> i, claim |-> ev.claim,
                                                   approvals |-> ev.approvals,
                                                   floor |-> st.lastRemove[ev.claim.who]]}]
             ELSE st
      ELSE IF ev.who \in st.members
             THEN [st EXCEPT !.members = @ \ {ev.who}, !.lastRemove[ev.who] = i]
             ELSE st

RECURSIVE FoldTo(_)
FoldTo(k) == IF k = 0 THEN Empty ELSE Step(FoldTo(k - 1), fLedger[k], k)

Fold == FoldTo(Len(fLedger))
Members == Fold.members

(* ── Actions ──────────────────────────────────────────────────────────── *)

Init ==
    /\ fLedger = << >>
    /\ submitted = {}
    /\ nextInvite = 0
    /\ approved = {}
    /\ cps = << {F} >>
    /\ p2ok = TRUE

Room == Len(fLedger) < MaxEvents

\* An honest invitee accepts a fresh invite and submits its claim (staged).
Submit(p) ==
    /\ p \notin Members
    /\ nextInvite < MaxInvites
    /\ nextInvite' = nextInvite + 1
    /\ submitted' = submitted \cup {Claim(p, Len(fLedger), nextInvite + 1, p)}
    /\ UNCHANGED <<fLedger, approved, cps, p2ok>>

\* F3 under the ruling: the founder approves a staged claim; its window
\* appends the admission event with its approval (plus A's, when the
\* threshold needs two) and publishes the checkpoint (P2).
Approve(c) ==
    /\ Room
    /\ c \in submitted \ approved
    /\ LET ev == [kind |-> "admit", claim |-> c,
                  approvals |-> IF Threshold >= 2 THEN {F, A} ELSE {F}]
           next == Append(fLedger, ev)
           st == FoldTo(Len(fLedger))
       IN /\ Admits(st, ev, Len(fLedger) + 1)      \* the founder's own fold check
          /\ fLedger' = next
          /\ approved' = approved \cup {c}
          /\ cps' = Append(cps, st.members \cup {c.who})
          /\ p2ok' = (p2ok /\ c.who \in st.members \cup {c.who})
    /\ UNCHANGED <<submitted, nextInvite>>

Remove(p) ==
    /\ Room
    /\ p \in Members
    /\ fLedger' = Append(fLedger, [kind |-> "remove", who |-> p])
    /\ cps' = Append(cps, Members \ {p})
    /\ UNCHANGED <<submitted, nextInvite, approved, p2ok>>

\* The adversary's approver A appends any admission event it can build.
AnyClaims ==
    \* forged claims are bounded to the current position (state-space bound)
    submitted \cup {Claim(p, Len(fLedger), inv, A) : p \in Personas, inv \in 1..MaxInvites}
SeenApprovals(p) ==
    UNION {fLedger[i].approvals : i \in {i \in 1..Len(fLedger) :
                                     fLedger[i].kind = "admit" /\ fLedger[i].claim.who = p}}
AdvAdmit(c, S) ==
    /\ Room
    /\ c \in AnyClaims
    /\ S \subseteq {A} \cup SeenApprovals(c.who)
    /\ fLedger' = Append(fLedger, [kind |-> "admit", claim |-> c, approvals |-> S])
    /\ UNCHANGED <<submitted, nextInvite, approved, cps, p2ok>>

\* Replay of an existing event.
Replay(i) ==
    /\ Room
    /\ i \in 1..Len(fLedger) /\ fLedger[i].kind = "admit"
    /\ fLedger' = Append(fLedger, fLedger[i])
    /\ UNCHANGED <<submitted, nextInvite, approved, cps, p2ok>>

Next ==
    \/ \E p \in Personas : Submit(p) \/ Remove(p)
    \/ \E c \in submitted : Approve(c)
    \/ \E c \in AnyClaims : \E S \in SUBSET ({A} \cup SeenApprovals(c.who)) : AdvAdmit(c, S)
    \/ \E i \in 1..Len(fLedger) : Replay(i)

Spec == Init /\ [][Next]_vars

(* ── Lemmas ───────────────────────────────────────────────────────────── *)

\* An admission whose carried claim is not the invitee's own signed claim at
\* its staged position is refused.
ApproverCannotForgeClaim ==
    \A a \in Fold.accepted : a.claim.signer = a.claim.who /\ a.claim \in submitted

\* A replayed admission event admits nothing new: no invite is redeemed twice.
NoDoubleAdmission ==
    \A a, b \in Fold.accepted : a.i # b.i => a.claim.invite # b.claim.invite

\* An admission (replayed or not) never re-admits a persona on a claim from
\* before its latest removal.
AdmissionRespectsRemoval ==
    \A a \in Fold.accepted : a.claim.pos >= a.floor

\* Every accepted admission carries the threshold of authorized approvals.
AdmissionHasThreshold ==
    \A a \in Fold.accepted : Cardinality(a.approvals \cap Approvers) >= Threshold

\* P2: the founder's approval published a checkpoint including the admitted.
ApprovalPublishesCheckpoint == p2ok

\* A claim the founder approved is admitted in that step (P3 realized by the
\* admission event, no further ceremony by anyone): its persona is a member
\* unless removed since.  System-level liveness (ApprovedIsAdmitted and
\* EveryAdmittedMemberPulls under the final rules) is checked in
\* OrgAdmission.tla (OrgAdmissionFinalApproval.cfg).
ApprovedAdmitted ==
    \A c \in approved :
        \/ c.who \in Members
        \/ \E i \in 1..Len(fLedger) : fLedger[i].kind = "remove" /\ fLedger[i].who = c.who
                                     /\ i > c.pos

\* Executability, negated: some approval admits someone.
NoApprovalAdmits == \A c \in approved : c.who \notin Members

====
