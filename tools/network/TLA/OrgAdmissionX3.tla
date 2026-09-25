---- MODULE OrgAdmissionX3 ----
(***************************************************************************)
(* Three joiners and two consecutive removal re-keys (bead auto-qrmlg.11). *)
(* Bound: the founder's events follow one fixed order — admit j1, j2, j3, *)
(* remove j2, remove j3 — so j1 can lag across both re-keys; every member  *)
(* action (install, adopt, pull, Δ apply, catch-up) interleaves freely.    *)
(***************************************************************************)
EXTENDS OrgAdmission

MCScript == << [kind |-> "claim", who |-> "j1"], [kind |-> "claim", who |-> "j2"],
               [kind |-> "claim", who |-> "j3"], [kind |-> "remove", who |-> "j2"],
               [kind |-> "remove", who |-> "j3"] >>
MCJoiners == {"j1", "j2", "j3"}
====
