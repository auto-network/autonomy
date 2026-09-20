---- MODULE FleetSyncWriteFloors ----
(***************************************************************************)
(* Fleet sync write floors, as the record specifies them (bead auto-mmwgu, *)
(* Propagation; decision note graph://d9153c5a-76e O-K), checked against   *)
(* the constitution's principle 1 (graph://6ad52a52-f75): a watermark is a *)
(* contiguous cursor, a number a machine can only claim by holding the     *)
(* data.                                                                   *)
(*                                                                         *)
(* Every machine is the origin of its own rows.  A row is its timestamp.   *)
(* A machine's write floor is the value it seals at the end of a round:    *)
(* max(last write, now), so every row it has written is at or below it and *)
(* every row it will write is above it.                                    *)
(*                                                                         *)
(* A pull is two steps, because the server builds it in two steps          *)
(* (fleet_sync_scheduler.py: the transaction pages are streamed first; the *)
(* write floor frames are read on a fresh connection after the last page). *)
(* BeginPull fixes the rows the server will send; FinishPull delivers them *)
(* and the floors the server holds at THAT moment.                         *)
(*                                                                         *)
(* The record's rules, verbatim in effect:                                 *)
(*   R1 the puller's watermark for an origin is the greater of its cursor  *)
(*      and the newest verified floor it holds (catalog.origin_watermarks) *)
(*   R2 the server sends every floor it holds above the puller's watermark *)
(*   R3 the puller stores a received floor and moves its cursor for that   *)
(*      origin to it ("by the promise there is nothing to apply between")  *)
(*   R4 floors are relayed unchanged by any holder                         *)
(*                                                                         *)
(* With Corrected = TRUE the rules are the operator's correction:          *)
(*   C1 the server's reply is one snapshot, and everything it sends about *)
(*      a machine is bounded by its own cursor for that machine: rows at   *)
(*      or below the cursor, and the floor only when the cursor reaches it *)
(*   C2 the puller moves its cursor to a received floor only once every    *)
(*      row of that machine in the same reply resolved without quarantine  *)
(*      (zero rows resolves); the floor is stored either way, R4 stands    *)
(*   C3 the watermark is the cursor alone                                  *)
(*                                                                         *)
(* Quarantine: a delivered row may be held unresolved; the cursor waits    *)
(* below it until it drains.  A server serves only resolved rows.          *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets

CONSTANTS Machines,   \* every machine is the origin of its own rows
          Writers,    \* the machines that write and seal in this model; the rest relay
          MaxRows,    \* rows each origin writes, at most
          MaxClock,   \* bound on each origin's clock
          Corrected   \* FALSE: the record as written; TRUE: the correction

None == [kind |-> "none"]

VARIABLES
    clock,   \* [Machines -> Nat]: each origin's clock (last timestamp used)
    rows,    \* [Machines -> SUBSET Nat]: timestamps each origin has written
    floor,   \* [Machines -> Nat]: each origin's own sealed write floor, 0 = none
    held,    \* [Machines -> [Machines -> SUBSET Nat]]: held[m][o] rows of o that m holds
    cursor,  \* [Machines -> [Machines -> Nat]]: cursor[m][o] m's cursor for origin o
    known,   \* [Machines -> [Machines -> Nat]]: known[m][o] newest floor of o that m holds
    quar,    \* [Machines -> [Machines -> SUBSET Nat]]: held rows not yet resolved
    reply    \* the reply in flight, or None

vars == <<clock, rows, floor, held, cursor, known, quar, reply>>

Max(a, b) == IF a >= b THEN a ELSE b
SetMax(S) == IF S = {} THEN 0 ELSE CHOOSE x \in S : \A y \in S : y <= x

Resolved(m, o) == held[m][o] \ quar[m][o]

\* The position through which m has resolved every row o wrote: what m may claim.
HeldThrough(m, o) ==
    SetMax({t \in 0..clock[o] : \A r \in rows[o] : r <= t => r \in Resolved(m, o)})

\* The cursor a store may carry from its own resolved rows: the newest one
\* below every unresolved row it holds.
OwnCursor(m, o) ==
    SetMax({r \in Resolved(m, o) : \A q \in quar[m][o] : r < q})

\* R1, or C3
Watermark(p, o) == IF Corrected THEN cursor[p][o] ELSE Max(cursor[p][o], known[p][o])

Init ==
    /\ clock = [o \in Machines |-> 0]
    /\ rows = [o \in Machines |-> {}]
    /\ floor = [o \in Machines |-> 0]
    /\ held = [m \in Machines |-> [o \in Machines |-> {}]]
    /\ cursor = [m \in Machines |-> [o \in Machines |-> 0]]
    /\ known = [m \in Machines |-> [o \in Machines |-> 0]]
    /\ quar = [m \in Machines |-> [o \in Machines |-> {}]]
    /\ reply = None

\* An origin writes one row, above its floor (the gate).
Write(o) ==
    /\ clock[o] < MaxClock
    /\ Cardinality(rows[o]) < MaxRows
    /\ LET ts == clock[o] + 1 IN
        /\ ts > floor[o]
        /\ clock' = [clock EXCEPT ![o] = ts]
        /\ rows' = [rows EXCEPT ![o] = @ \cup {ts}]
        /\ held' = [held EXCEPT ![o][o] = @ \cup {ts}]
        /\ cursor' = [cursor EXCEPT ![o][o] = ts]
    /\ UNCHANGED <<floor, known, quar, reply>>

\* An origin seals its write floor at max(last write, now), with now past
\* the last write; it holds all its own rows, so its own cursor is the floor.
Seal(o) ==
    /\ clock[o] < MaxClock
    /\ LET f == clock[o] + 1 IN
        /\ clock' = [clock EXCEPT ![o] = f]
        /\ floor' = [floor EXCEPT ![o] = f]
        /\ known' = [known EXCEPT ![o][o] = f]
        /\ cursor' = [cursor EXCEPT ![o][o] = f]
    /\ UNCHANGED <<rows, held, quar, reply>>

\* The server fixes what it sends in one snapshot. Under the record: every
\* resolved row above the puller's watermark (R2). Under C1: everything it
\* sends about a machine is bounded by its own cursor for that machine, the
\* rows at or below the cursor and the floor only when the cursor reaches it.
BeginPull(p, s) ==
    /\ p # s
    /\ reply = None
    /\ LET W == [o \in Machines |-> Watermark(p, o)]
           Bound(o) == IF Corrected THEN cursor[s][o] ELSE MaxClock
       IN reply' = [kind |-> "reply", puller |-> p, server |-> s, wm |-> W,
                    rows |-> [o \in Machines |->
                        {r \in Resolved(s, o) : r > W[o] /\ r <= Bound(o)}],
                    floors |-> [o \in Machines |->
                        IF known[s][o] > W[o] /\ known[s][o] <= Bound(o)
                        THEN known[s][o] ELSE 0]]
    /\ UNCHANGED <<clock, rows, floor, held, cursor, known, quar>>

\* The rows arrive; any of them may land in quarantine. Then the floors:
\* under the record, those the server holds NOW above the watermark (R2,
\* read after the pages), claimed unconditionally (R3); under the
\* correction, those fixed at BeginPull (C1), claimed only when every row of
\* that machine in this reply resolved (C2). Stored either way (R4).
FinishPull ==
    /\ reply # None
    /\ \E Q \in [Machines -> SUBSET (1..MaxClock)] :
        \* only a row not already resolved here can land in quarantine: a
        \* transaction the catalog has applied is a no-op when delivered again
        /\ \A o \in Machines : Q[o] \subseteq reply.rows[o] \ Resolved(reply.puller, o)
        /\ LET p == reply.puller
               s == reply.server
               served == [o \in Machines |->
                            IF Corrected THEN reply.floors[o]
                            ELSE IF known[s][o] > reply.wm[o] THEN known[s][o] ELSE 0]
               \* a row already in quarantine stays there when delivered again
               unresolved == [o \in Machines |-> quar[p][o] \cup Q[o]]
               \* the cursor advances through the delivered rows that resolved,
               \* below the first unresolved one, as _advance_cursor does
               base == [o \in Machines |->
                          Max(cursor[p][o],
                              SetMax({r \in reply.rows[o] \ unresolved[o] :
                                        \A q \in unresolved[o] : r < q}))]
               claim == [o \in Machines |->
                          IF Corrected /\ reply.rows[o] \cap unresolved[o] # {}
                          THEN base[o] ELSE Max(base[o], served[o])]
           IN
            /\ held' = [held EXCEPT ![p] = [o \in Machines |-> @[o] \cup reply.rows[o]]]
            /\ quar' = [quar EXCEPT ![p] = unresolved]
            /\ cursor' = [cursor EXCEPT ![p] = claim]
            /\ known' = [known EXCEPT ![p] = [o \in Machines |-> Max(@[o], served[o])]]
    /\ reply' = None
    /\ UNCHANGED <<clock, rows, floor>>

\* A quarantined row resolves; the cursor advances through it.
Drain(m, o, r) ==
    /\ r \in quar[m][o]
    /\ quar' = [quar EXCEPT ![m][o] = @ \ {r}]
    /\ cursor' = [cursor EXCEPT ![m][o] =
                    Max(@, SetMax({x \in Resolved(m, o) \cup {r} :
                                     \A q \in quar[m][o] \ {r} : x < q}))]
    /\ UNCHANGED <<clock, rows, floor, held, known, reply>>

Next ==
    \/ \E o \in Writers : Write(o) \/ Seal(o)
    \/ \E p, s \in Machines : BeginPull(p, s)
    \/ FinishPull
    \/ \E m, o \in Machines, r \in 1..MaxClock : Drain(m, o, r)

\* One reply is in flight at a time in this model, so a pull between one
\* pair is enabled only between other pairs' pulls: strong fairness says
\* every pair keeps pulling, which is what the scheduler's rounds do.
Fairness ==
    /\ WF_vars(FinishPull)
    /\ \A p, s \in Machines : SF_vars(BeginPull(p, s))
    /\ \A m, o \in Machines, r \in 1..MaxClock : WF_vars(Drain(m, o, r))

Spec == Init /\ [][Next]_vars /\ Fairness

\* Principle 1: a cursor never claims a position the machine does not hold.
CursorHoldsData ==
    \A m, o \in Machines : cursor[m][o] <= HeldThrough(m, o)

\* Every row eventually reaches and resolves on every machine.
Converges == <>[](\A m, o \in Machines : Resolved(m, o) = rows[o])

====
