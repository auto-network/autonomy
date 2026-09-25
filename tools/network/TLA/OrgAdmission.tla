---- MODULE OrgAdmission ----
(***************************************************************************)
(* Org admission -> checkpoint -> adoption -> org sync pull (beads          *)
(* auto-qrmlg.5, auto-qrmlg.11; ceremony record graph://cde6c8c6-041;      *)
(* closing note graph://25d07566-32c).                                     *)
(*                                                                         *)
(* Actors: the founder F (the only checkpointer), joiners, the registry,   *)
(* and an outsider X that is not a member.  The founder's ledger is linear:*)
(* every event is parented on the current head (claim_service.py:170-171  *)
(* refuses a claim on stale heads).  Events are indexed by position; a     *)
(* checkpoint's head is a ledger length (0 = genesis).  Events are claims  *)
(* and removals.                                                           *)
(*                                                                         *)
(* The CURRENT rules, each from code at master 2c2621c8:                   *)
(*  C1 a joiner's ledger rows come only from its own appends, the join     *)
(*     install, or rows a pull delivers (ledger/settings_bridge.py:11-18,  *)
(*     store.py:239-250, fleet_sync_scheduler.py:4424-4430)                *)
(*  C2 each side of a pull verifies the other's proof: the proof's seq     *)
(*     must be one the verifier has adopted, and the verifier holds ONE    *)
(*     adopted record (fleet_org_channel.py:286-292,                       *)
(*     org_sync_channels.py:189-193); the reprove window is armed only by  *)
(*     note_adoption, which no production path calls                       *)
(*     (fleet_org_channel.py:472-475; sole caller the acceptance probe     *)
(*     fleet_sync/process_dashboard_sync.py:134)                           *)
(*  C3 at join the joiner adopts the registry's CURRENT checkpoint          *)
(*     (network_routes.py:943-945)                                         *)
(*  C4 bootstrap carries the founder's events as of that moment            *)
(*     (claim_service.py:233-235)                                          *)
(*  C5 every admission appends to the founder's ledger                     *)
(*     (claim_service.py:172-175)                                          *)
(*  C6 a checkpoint's ledger_head is the founder's head at assembly         *)
(*     (membership_checkpoint.py:103-106)                                  *)
(*  C7 adoption folds the LOCAL ledger at ledger_head and requires         *)
(*     members_root equality; an absent head refuses                        *)
(*     (network_routes.py:759-768)                                         *)
(*  C8 the checkpoint is published only in a founder sign-on               *)
(*     (signon_preparation.py:125-133, network-signon.mjs:1560-1577)       *)
(*  C9 a prover's own rider is built by folding its local ledger at its    *)
(*     newest adopted record's ledger_head (org_sync_channels.py:173-179,  *)
(*     197-205): it needs every event up to that head                      *)
(*                                                                         *)
(* The PROPOSED rules:                                                     *)
(*  P1 AdoptByVerification: adopt a registry record whose signer is in the *)
(*     previous checkpointers root and whose prev chains; no local head.   *)
(*  P2 CheckpointAtAdmission: the admission (and a removal) and the        *)
(*     checkpoint reflecting it are one step.                              *)
(*  P3 AdmitOnApproval: the founder's countersign appends the staged claim.*)
(*                                                                         *)
(* PathRule — how a prover obtains its inclusion path, and which seqs a    *)
(* verifier accepts (auto-qrmlg.11):                                       *)
(*  "granted"     any member of its newest adopted record has its path;    *)
(*                verifier accepts only its newest (the auto-qrmlg.5       *)
(*                abstraction)                                             *)
(*  "fold"        C9: path only when the prover holds every event up to    *)
(*                the head; verifier accepts only its newest (production)  *)
(*  "record"      A: the path travels with the checkpoint record           *)
(*  "registry"    B: the registry serves the path for its CURRENT record   *)
(*  "window"      C: the prover proves under the newest authentic record   *)
(*                its fold reaches, adopted or not; a verifier             *)
(*                accepts its previous record for WindowTicks ticks after  *)
(*                adopting the newest (modeled time)                       *)
(*  "predecessor" C unbounded: as "window" with no time bound              *)
(*  "delta_registry" D1: C_{k} travels with Enc(Δ_k), the membership delta*)
(*                relative to C_{k-1}, served by the registry as a journal *)
(*                (ciphertext only); a member holding M_{k-1} and the key  *)
(*                reconstructs M_k, recomputes members_root, refuses on    *)
(*                mismatch, computes its own path                          *)
(*  "delta_hello" D2: the same Δ chain, carried in the org hello by a peer *)
(*                already on the newer record, from the stale side's newest*)
(*                known seq; the stale side applies it and re-dials        *)
(*  DeltaKey: "prev" Enc(Δ_k) under the generation current at C_{k-1};    *)
(*            "next" under the generation after C_k (a removal re-keys);   *)
(*            a member receives a generation's grant only by a pull (K3)   *)
(*  RootCheck: the members_root recomputation (FALSE: a calibration)      *)
(*  MonotoneAdopt: a record at or below the adopted seq is refused         *)
(*  GrantInBundle: the join bundle carries the new member's grant for the  *)
(*            generation current at install (K3 allows the join bundle)    *)
(*  "verifier_any_adm" E-any-adm: as "verifier_any", and the record is at  *)
(*                or after the prover's current admission (see             *)
(*                OrgAdmissionLeaves.tla: re-admission)                    *)
(*  "verifier_any" E-any (model-suggested): as "verifier", accepting a     *)
(*                proof under ANY authentic record at or below the         *)
(*                verifier's newest, not only its predecessor              *)
(*  "verifier"    E (model-suggested): the prover proves as under "window";*)
(*                a verifier that holds every event up to its own newest   *)
(*                head accepts a proof under its previous record when the  *)
(*                prover is in its NEWEST member set, which it folds       *)
(*                itself; no time bound                                    *)
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
          VerifySigner,         \* P1's signer check (TRUE except in a calibration)
          PathRule,             \* see the header
          AllowRemoval,         \* the founder may remove a joiner (once each)
          WindowTicks,          \* "window": ticks the previous record stays acceptable
          MaxTime,              \* bound on modeled time (0: no clock)
          DeltaKey,             \* "prev" or "next" (D1/D2)
          RootCheck,            \* D1/D2 members_root recomputation
          MonotoneAdopt,        \* adoption refuses a record at or below the adopted seq
          Adversarial,          \* the registry/peer may serve a wrong Δ or an old record
          GrantInBundle         \* the join bundle carries the grant for the current generation

Nodes  == {F} \cup Joiners
Actors == Nodes \cup {X}

VARIABLES
    fLedger,    \* Seq of [kind: {"claim","remove"}, who: Joiners]
    jHas,       \* [Joiners -> SUBSET Nat]: ledger positions the joiner holds
    installed,  \* joiners that ran the join install
    cps,        \* Seq of [head: Nat, members, auth]: registry records; last is current
    adopted,    \* [Actors -> Nat]: index into cps of the newest adopted record; 0 = none
    prevAd,     \* [Actors -> Nat]: the record adopted before it; 0 = none
    adoptedAt,  \* [Actors -> Nat]: modeled time of the newest adoption
    now,        \* modeled time
    pulls,      \* [from, by: SUBSET Joiners — pulled from / by the founder since its
                \*  current adoption; inclBad, freshBad, outsider: sticky violation flags]
    forged,     \* the registry has served its forged record
    claimed,    \* joiners that submitted a claim
    approved,   \* joiners whose claim the founder countersigned
    recon,      \* [Joiners -> SUBSET Nat]: records whose member set was rebuilt from Δ
    badRecon,   \* [Joiners -> SUBSET Nat]: records rebuilt from a WRONG Δ
    gens        \* [Joiners -> SUBSET Nat]: generations whose grant the joiner holds

vars == <<fLedger, jHas, installed, cps, adopted, prevAd, adoptedAt, now,
          pulls, forged, claimed, approved, recon, badRecon, gens>>

\* Variables the delta mechanism adds; unchanged by the auto-qrmlg.5 actions.
dvars == <<recon, badRecon, gens>>

Pos(k) == 1..k
MembersAt(k) ==
    ({F} \cup {fLedger[i].who : i \in {i \in Pos(k) : fLedger[i].kind = "claim"}})
      \ {fLedger[i].who : i \in {i \in Pos(k) : fLedger[i].kind = "remove"}}
Admitted   == {fLedger[i].who : i \in {i \in Pos(Len(fLedger)) : fLedger[i].kind = "claim"}}
Current    == MembersAt(Len(fLedger))
CurJoiners == Current \ {F}

Has(n) == IF n = F THEN Pos(Len(fLedger)) ELSE jHas[n]

\* Ledger position of the joiner's latest claim (its current admission).
AdmitPos(j) ==
    LET cl == {i \in Pos(Len(fLedger)) : fLedger[i].kind = "claim" /\ fLedger[i].who = j}
    IN IF cl = {} THEN 0 ELSE CHOOSE i \in cl : \A x \in cl : x <= i

\* An optional fixed order of the founder's events (claims and removals),
\* overridden by a model module to bound the state space; << >> = free.
Script == << >>
Scripted(ev) ==
    \/ Script = << >>
    \/ Len(fLedger) < Len(Script) /\ Script[Len(fLedger) + 1] = ev

\* C9: the prover can fold at record s's head.
Foldable(n, s) == s > 0 /\ Pos(cps[s].head) \subseteq Has(n)

DeltaModes == {"delta_registry", "delta_hello"}
DeltaMode  == PathRule \in DeltaModes

\* The storage generation current at record k: one re-key per removal.
Gen(k) == Cardinality({i \in Pos(cps[k].head) : fLedger[i].kind = "remove"})
KeyGen(k) == IF DeltaKey = "prev" THEN Gen(k - 1) ELSE Gen(k)
HoldsGen(n, g) == IF n = F THEN TRUE ELSE g \in gens[n]
\* A generation's grant is addressed only to members of a record in it.
Entitled(n, g) == \E k \in 1..Len(cps) : cps[k].auth /\ Gen(k) = g /\ n \in cps[k].members
GensOf(n) == IF n = F THEN 0..Len(fLedger) ELSE gens[n]

\* n holds the member set of record k: it can fold at its head, or rebuilt
\* it from the Δ chain.
Knows(n, k) == k > 0 /\ (IF n = F THEN TRUE ELSE Foldable(n, k) \/ k \in recon[n])
KnownSeqs(n) == {k \in 1..Len(cps) : cps[k].auth /\ Knows(n, k)}
MaxKnown(n) == IF KnownSeqs(n) = {} THEN 0
               ELSE CHOOSE k \in KnownSeqs(n) : \A l \in KnownSeqs(n) : l <= k

Init ==
    /\ fLedger = << >>
    /\ jHas = [j \in Joiners |-> {}]
    /\ installed = {}
    \* F0: the seed, root-signed at genesis, adopted by the founder.
    /\ cps = << [head |-> 0, members |-> {F}, auth |-> TRUE] >>
    /\ adopted = [n \in Actors |-> IF n = F THEN 1 ELSE 0]
    /\ prevAd = [n \in Actors |-> 0]
    /\ adoptedAt = [n \in Actors |-> 0]
    /\ now = 0
    /\ pulls = [from |-> {}, by |-> {}, inclBad |-> FALSE, freshBad |-> FALSE, outsider |-> FALSE]
    /\ forged = FALSE
    /\ claimed = {}
    /\ approved = {}
    /\ recon = [j \in Joiners |-> {}]
    /\ badRecon = [j \in Joiners |-> {}]
    /\ gens = [j \in Joiners |-> {}]

\* The founder appends one event; under P2 the checkpoint reflecting it is
\* the same step and the founder adopts it.
ResetSync == pulls' = [pulls EXCEPT !.from = {}, !.by = {}]

AppendEvent(ev) ==
    /\ fLedger' = Append(fLedger, ev)
    /\ IF CheckpointAtAdmission
         THEN LET k == Len(fLedger) + 1
                  rec == [head |-> k,
                          members |-> IF ev.kind = "claim" THEN Current \cup {ev.who}
                                      ELSE Current \ {ev.who},
                          auth |-> TRUE]
              IN /\ cps' = Append(cps, rec)
                 /\ adopted' = [adopted EXCEPT ![F] = Len(cps) + 1]
                 /\ prevAd' = [prevAd EXCEPT ![F] = adopted[F]]
                 /\ adoptedAt' = [adoptedAt EXCEPT ![F] = now]
                 /\ ResetSync
         ELSE UNCHANGED <<cps, adopted, prevAd, adoptedAt, pulls>>

AdmitStep(j) == AppendEvent([kind |-> "claim", who |-> j])

\* J3: the joiner's claim; a self-admitting role is appended at submit.
Claim(j) ==
    /\ j \notin claimed
    /\ Approval \/ Scripted([kind |-> "claim", who |-> j])
    /\ claimed' = claimed \cup {j}
    /\ IF Approval THEN UNCHANGED <<fLedger, cps, adopted, prevAd, adoptedAt, pulls>>
                   ELSE AdmitStep(j)
    /\ UNCHANGED <<jHas, installed, now, forged, approved, recon, badRecon, gens>>

\* F3: the founder countersigns; under P3 that appends the claim.
Approve(j) ==
    /\ Approval
    /\ j \in claimed \ approved
    /\ approved' = approved \cup {j}
    /\ IF AdmitOnApproval THEN AdmitStep(j)
                          ELSE UNCHANGED <<fLedger, cps, adopted, prevAd, adoptedAt, pulls>>
    /\ UNCHANGED <<jHas, installed, now, forged, claimed, recon, badRecon, gens>>

\* J4: the joiner re-mints the claim with the approvals; it is appended.
Finalize(j) ==
    /\ Approval /\ ~AdmitOnApproval
    /\ j \in approved \ Admitted
    /\ AdmitStep(j)
    /\ UNCHANGED <<jHas, installed, now, forged, claimed, approved, recon, badRecon, gens>>

\* The founder removes a current member (at most once each).
Remove(j) ==
    /\ AllowRemoval
    /\ j \in CurJoiners
    /\ Scripted([kind |-> "remove", who |-> j])
    /\ AppendEvent([kind |-> "remove", who |-> j])
    /\ UNCHANGED <<jHas, installed, now, forged, claimed, approved, recon, badRecon, gens>>

\* C4: install from the founder's ledger as of now.
Bootstrap(j) ==
    /\ j \in CurJoiners
    /\ j \notin installed
    /\ installed' = installed \cup {j}
    /\ jHas' = [jHas EXCEPT ![j] = Pos(Len(fLedger))]
    /\ gens' = IF DeltaMode /\ GrantInBundle
                THEN [gens EXCEPT ![j] = @ \cup
                        {Cardinality({i \in Pos(Len(fLedger)) : fLedger[i].kind = "remove"})}]
                ELSE gens
    /\ UNCHANGED <<fLedger, cps, adopted, prevAd, adoptedAt, now, pulls, forged,
                   claimed, approved, recon, badRecon>>

\* C6/C8: the founder's sign-on publishes when the member set changed.
Checkpoint ==
    /\ cps[adopted[F]].members # Current
    /\ cps' = Append(cps, [head |-> Len(fLedger), members |-> Current, auth |-> TRUE])
    /\ adopted' = [adopted EXCEPT ![F] = Len(cps) + 1]
    /\ prevAd' = [prevAd EXCEPT ![F] = adopted[F]]
    /\ adoptedAt' = [adoptedAt EXCEPT ![F] = now]
    /\ ResetSync
    /\ UNCHANGED <<fLedger, jHas, installed, now, forged, claimed, approved, recon, badRecon, gens>>

\* The untrusted registry serves a record naming the outsider.
Forge ==
    /\ RegistryForges /\ ~forged
    /\ cps' = Append(cps, [head |-> Len(fLedger),
                           members |-> cps[Len(cps)].members \cup {X},
                           auth |-> FALSE])
    /\ forged' = TRUE
    /\ UNCHANGED <<fLedger, jHas, installed, adopted, prevAd, adoptedAt, now, pulls,
                   claimed, approved, recon, badRecon, gens>>

\* C7 or P1: a joiner adopts the registry's current record.
Acceptable(j, rec) ==
    IF AdoptByVerification
      THEN ~VerifySigner \/ rec.auth
      ELSE /\ Pos(rec.head) \subseteq jHas[j]
           /\ rec.members = MembersAt(rec.head)

Adopt(j) ==
    LET c == Len(cps) IN
    /\ j \in installed
    /\ c > adopted[j]
    /\ Acceptable(j, cps[c])
    /\ adopted' = [adopted EXCEPT ![j] = c]
    /\ prevAd' = [prevAd EXCEPT ![j] = adopted[j]]
    /\ adoptedAt' = [adoptedAt EXCEPT ![j] = now]
    /\ UNCHANGED <<fLedger, jHas, installed, cps, now, pulls, forged, claimed, approved, recon, badRecon, gens>>

\* The outsider adopts whatever it likes; it proves nothing honestly.
OutsiderAdopt(s) ==
    /\ RegistryForges        \* without a forged record it proves nothing anyway
    /\ s \in 1..Len(cps)
    /\ adopted' = [adopted EXCEPT ![X] = s]
    /\ UNCHANGED <<fLedger, jHas, installed, cps, prevAd, adoptedAt, now, pulls,
                   forged, claimed, approved, recon, badRecon, gens>>

Tick ==
    /\ now < MaxTime
    /\ now' = now + 1
    /\ UNCHANGED <<fLedger, jHas, installed, cps, adopted, prevAd, adoptedAt, pulls,
                   forged, claimed, approved, recon, badRecon, gens>>

Ready(n) == n = F \/ n \in installed

\* The seqs honest prover h can prove under (its own rider).
ProverSeqs(h) ==
    LET newest == adopted[h]
        mine   == {s \in {newest} \ {0} : h \in cps[s].members}
    IN CASE PathRule = "granted"  -> mine
         [] PathRule = "record"   -> mine
         [] PathRule = "registry" -> {s \in mine : s = Len(cps)}
         [] PathRule = "fold"     -> {s \in mine : Foldable(h, s)}
         [] DeltaMode             -> {s \in mine : Knows(h, s) /\ (IF h = F THEN TRUE ELSE s \notin badRecon[h])}
         [] OTHER ->
              \* "window", "predecessor", "verifier": the newest authentic
              \* record its fold reaches ("until the fold reaches H_{n+1}"),
              \* adopted or not.
              {s \in {MaxKnown(h)} \ {0} : h \in cps[s].members}

InWindow(v) == now <= adoptedAt[v] + WindowTicks

\* Honest verifier v accepts other's proof under seq s.
Accepts(v, other, s) ==
    /\ other \in cps[s].members
    /\ \/ s = adopted[v]
       \/ /\ s = prevAd[v] /\ s > 0
          /\ CASE PathRule = "window"      -> InWindow(v)
               [] PathRule = "predecessor" -> TRUE
               [] PathRule = "verifier"    -> /\ Foldable(v, adopted[v])
                                              /\ other \in MembersAt(cps[adopted[v]].head)
               [] OTHER -> FALSE
       \/ /\ PathRule \in {"verifier_any", "verifier_any_adm"}
          /\ s > 0 /\ s < adopted[v] /\ cps[s].auth
          /\ Foldable(v, adopted[v])
          /\ other \in MembersAt(cps[adopted[v]].head)
          \* "verifier_any_adm": not before the prover's current admission
          /\ (PathRule = "verifier_any_adm" /\ other \in Joiners) =>
                cps[s].head >= AdmitPos(other)

\* The prover side, or anything at all for the outsider.
CanProve(n, s) == IF n = X THEN s \in 1..Len(cps) ELSE s \in ProverSeqs(n)

Pull(a, b) ==
    \E sa, sb \in 1..Len(cps) :
      /\ a # b
      /\ a \in Nodes \/ b \in Nodes
      /\ \A n \in {a, b} \cap Nodes : Ready(n)
      /\ CanProve(a, sa) /\ CanProve(b, sb)
      /\ b \in Nodes => Accepts(b, a, sa)
      /\ a \in Nodes => Accepts(a, b, sb)
      /\ LET fresh == /\ (b \in Nodes => a \in cps[adopted[b]].members)
                      /\ (a \in Nodes => b \in cps[adopted[a]].members)
             inwin == (b \in Nodes /\ sa # adopted[b]) \/ (a \in Nodes /\ sb # adopted[a])
             incl  == /\ cps[sa].auth /\ a \in cps[sa].members
                      /\ cps[sb].auth /\ b \in cps[sb].members
             newGens == IF DeltaMode /\ a \in Joiners /\ b \in Nodes
                          THEN {g \in GensOf(b) : Entitled(a, g)} \ gens[a] ELSE {}
             np == [from |-> IF a \in Joiners /\ b = F THEN pulls.from \cup {a} ELSE pulls.from,
                    by   |-> IF a = F /\ b \in Joiners THEN pulls.by \cup {b} ELSE pulls.by,
                    inclBad |-> pulls.inclBad \/ ~incl,
                    freshBad |-> pulls.freshBad \/ ~(fresh \/ (PathRule = "window" /\ inwin)),
                    outsider |-> pulls.outsider \/ a = X \/ b = X]
         IN /\ \/ np # pulls
               \/ (a \in Joiners /\ b \in Nodes /\ ~(Has(b) \subseteq jHas[a]))
               \/ newGens # {}
            /\ pulls' = np
            \* K3: grants, like events, cross only on an admitted pull.
            /\ gens' = IF newGens = {} THEN gens ELSE [gens EXCEPT ![a] = @ \cup newGens]
      \* C1: events cross only on an admitted pull.
      /\ jHas' = IF a \in Joiners /\ b \in Nodes
                   THEN [jHas EXCEPT ![a] = @ \cup Has(b)] ELSE jHas
      /\ UNCHANGED <<fLedger, installed, cps, adopted, prevAd, adoptedAt, now, forged,
                     claimed, approved, recon, badRecon>>

\* D1: the registry's journal serves record k with Enc(Δ_k); a member that
\* holds M_{k-1} and the key rebuilds M_k (the members_root check passes
\* for the true Δ).
ApplyDelta(n, k) ==
    /\ PathRule = "delta_registry"
    /\ n \in installed
    /\ k \in 2..Len(cps) /\ cps[k].auth
    /\ Knows(n, k - 1) /\ ~Knows(n, k)
    /\ HoldsGen(n, KeyGen(k))
    /\ recon' = [recon EXCEPT ![n] = @ \cup {k}]
    /\ UNCHANGED <<fLedger, jHas, installed, cps, adopted, prevAd, adoptedAt, now, pulls,
                   forged, claimed, approved, badRecon, gens>>

\* D2: stale m hellos peer p with a proof under its newest known record;
\* p, on a newer record, answers with the record chain and Enc(Δ) for
\* every step; m applies the steps it can open, in order, and adopts the
\* last (it re-dials afterwards: an ordinary Pull).
CatchUp(m, p) ==
    LET sm == MaxKnown(m)
        tp == adopted[p]
        ok(l) == \A k \in (sm + 1)..l : HoldsGen(m, KeyGen(k)) /\ Knows(p, k) /\ Knows(p, k - 1)
        reach == {l \in (sm + 1)..tp : ok(l)}
        top == IF reach = {} THEN sm ELSE CHOOSE l \in reach : \A x \in reach : x <= l
    IN
    /\ PathRule = "delta_hello"
    /\ m \in installed /\ p \in Nodes /\ p # m /\ Ready(p)
    /\ sm > 0 /\ sm < tp
    /\ m \in cps[sm].members          \* p verifies m under sm
    /\ top > sm
    /\ recon' = [recon EXCEPT ![m] = @ \cup ((sm + 1)..top)]
    /\ IF top > adopted[m]
         THEN /\ adopted' = [adopted EXCEPT ![m] = top]
              /\ prevAd' = [prevAd EXCEPT ![m] = adopted[m]]
              /\ adoptedAt' = [adoptedAt EXCEPT ![m] = now]
         ELSE UNCHANGED <<adopted, prevAd, adoptedAt>>
    /\ UNCHANGED <<fLedger, jHas, installed, cps, now, pulls, forged, claimed, approved,
                   badRecon, gens>>

\* A wrong Δ (from the registry or a peer): refused by the members_root
\* recomputation; accepted only when RootCheck is deleted.
WrongDelta(n, k) ==
    /\ DeltaMode /\ Adversarial /\ ~RootCheck
    /\ n \in installed
    /\ k \in 2..Len(cps) /\ cps[k].auth
    /\ Knows(n, k - 1) /\ ~Knows(n, k)
    /\ recon' = [recon EXCEPT ![n] = @ \cup {k}]
    /\ badRecon' = [badRecon EXCEPT ![n] = @ \cup {k}]
    /\ UNCHANGED <<fLedger, jHas, installed, cps, adopted, prevAd, adoptedAt, now, pulls,
                   forged, claimed, approved, gens>>

\* A replayed older authentic record: refused when MonotoneAdopt holds.
ReplayOld(n, k) ==
    /\ Adversarial /\ ~MonotoneAdopt
    /\ n \in installed
    /\ k \in 1..Len(cps) /\ cps[k].auth /\ k < adopted[n]
    /\ adopted' = [adopted EXCEPT ![n] = k]
    /\ prevAd' = [prevAd EXCEPT ![n] = adopted[n]]
    /\ adoptedAt' = [adoptedAt EXCEPT ![n] = now]
    /\ UNCHANGED <<fLedger, jHas, installed, cps, now, pulls, forged, claimed, approved,
                   recon, badRecon, gens>>

Next ==
    \/ \E j \in Joiners, k \in 1..Len(cps) : ApplyDelta(j, k) \/ WrongDelta(j, k) \/ ReplayOld(j, k)
    \/ \E m \in Joiners, p \in Nodes : CatchUp(m, p)
    \/ \E j \in Joiners : Claim(j) \/ Approve(j) \/ Finalize(j) \/ Remove(j)
                          \/ Bootstrap(j) \/ Adopt(j)
    \/ Checkpoint
    \/ Forge
    \/ Tick
    \/ \E s \in 1..Len(cps) : OutsiderAdopt(s)
    \/ \E a, b \in Actors : Pull(a, b)

Fairness ==
    /\ \A j \in Joiners : WF_vars(Bootstrap(j)) /\ WF_vars(Adopt(j))
    /\ \A a, b \in Nodes : WF_vars(Pull(a, b))
    /\ \A j \in Joiners : \A k \in 1..8 : WF_vars(ApplyDelta(j, k))
    /\ \A m \in Joiners, p \in Nodes : WF_vars(CatchUp(m, p))
    /\ FounderSignsOn => WF_vars(Checkpoint)

Spec == Init /\ [][Next]_vars /\ Fairness

---------------------------------------------------------------------------
(* Safety.                                                                  *)
\* Every pull was admitted under authentic records that include both
\* provers.
NoPullWithoutInclusion == ~pulls.inclBad

\* No verifier admitted a peer outside its own newest member set, except
\* under "window" inside the bounded window.

OutsiderNeverPulls == ~pulls.outsider

\* No verifier admitted a peer outside its own newest member set, except
\* under "window" inside the bounded window.
RemovedExcluded == ~pulls.freshBad

\* D1/D2: every member set a member rebuilt equals the committed one.
ReconstructedIsCommitted == \A j \in Joiners : badRecon[j] = {}

\* A replayed record cannot regress a member's adopted seq.
NoRegression == [][\A j \in Joiners : adopted'[j] >= adopted[j]]_vars

AdoptedIsAuthentic == \A n \in Nodes : adopted[n] > 0 => cps[adopted[n]].auth

AdmittedWereApproved == Approval => Admitted \subseteq approved

TypeOK ==
    /\ \A n \in Actors : adopted[n] \in 0..Len(cps) /\ prevAd[n] \in 0..Len(cps)
    /\ \A j \in Joiners : jHas[j] \subseteq Pos(Len(fLedger))
    /\ \A j \in Joiners : badRecon[j] \subseteq recon[j]

(* Liveness: eventually, for good, every current member has adopted the    *)
(* founder's newest checkpoint, holds every event up to its head, and has  *)
(* pulled from the founder and been pulled by it since the founder adopted *)
(* it.  This covers an existing member across a checkpoint advance.        *)
InSync(j) ==
    LET s == adopted[F] IN
    /\ adopted[j] = s
    /\ Foldable(j, s)
    /\ j \in pulls.from
    /\ j \in pulls.by

EveryAdmittedMemberPulls == <>[](\A j \in CurJoiners : InSync(j))

(* Admission does not wait on a second joiner ceremony. *)
ApprovedIsAdmitted == \A j \in Joiners : j \in approved ~> j \in Admitted

(* Executability, negated: TLC must find a state where every joiner is     *)
(* admitted and in sync.                                                   *)
NotAllJoinedInSync == ~(Admitted = Joiners /\ \A j \in Joiners : InSync(j))

====
