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
(***************************************************************************)
EXTENDS Naturals, FiniteSets

CONSTANTS Machines,   \* every machine is the origin of its own rows
          MaxRows,    \* rows each origin writes, at most
          MaxClock    \* bound on each origin's clock

None == [kind |-> "none"]

VARIABLES
    clock,   \* [Machines -> Nat]: each origin's clock (last timestamp used)
    rows,    \* [Machines -> SUBSET Nat]: timestamps each origin has written
    floor,   \* [Machines -> Nat]: each origin's own sealed write floor, 0 = none
    held,    \* [Machines -> [Machines -> SUBSET Nat]]: held[m][o] rows of o that m holds
    cursor,  \* [Machines -> [Machines -> Nat]]: cursor[m][o] m's cursor for origin o
    known,   \* [Machines -> [Machines -> Nat]]: known[m][o] newest floor of o that m holds
    reply    \* the reply in flight, or None

vars == <<clock, rows, floor, held, cursor, known, reply>>

Max(a, b) == IF a >= b THEN a ELSE b
SetMax(S) == IF S = {} THEN 0 ELSE CHOOSE x \in S : \A y \in S : y <= x

\* The position through which m holds every row o wrote: what m may claim.
HeldThrough(m, o) ==
    SetMax({t \in 0..clock[o] : \A r \in rows[o] : r <= t => r \in held[m][o]})

\* R1
Watermark(p, o) == Max(cursor[p][o], known[p][o])

Init ==
    /\ clock = [o \in Machines |-> 0]
    /\ rows = [o \in Machines |-> {}]
    /\ floor = [o \in Machines |-> 0]
    /\ held = [m \in Machines |-> [o \in Machines |-> {}]]
    /\ cursor = [m \in Machines |-> [o \in Machines |-> 0]]
    /\ known = [m \in Machines |-> [o \in Machines |-> 0]]
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
    /\ UNCHANGED <<floor, known, reply>>

\* An origin seals its write floor at max(last write, now), with now past
\* the last write; it holds all its own rows, so its own cursor is the floor.
Seal(o) ==
    /\ clock[o] < MaxClock
    /\ LET f == clock[o] + 1 IN
        /\ clock' = [clock EXCEPT ![o] = f]
        /\ floor' = [floor EXCEPT ![o] = f]
        /\ known' = [known EXCEPT ![o][o] = f]
        /\ cursor' = [cursor EXCEPT ![o][o] = f]
    /\ UNCHANGED <<rows, held, reply>>

\* The server fixes the rows above the puller's watermark (R1, R2).
BeginPull(p, s) ==
    /\ p # s
    /\ reply = None
    /\ LET W == [o \in Machines |-> Watermark(p, o)]
       IN reply' = [kind |-> "reply", puller |-> p, server |-> s, wm |-> W,
                    rows |-> [o \in Machines |-> {r \in held[s][o] : r > W[o]}]]
    /\ UNCHANGED <<clock, rows, floor, held, cursor, known>>

\* The rows arrive and apply; then the floors the server holds NOW above the
\* puller's watermark (R2, read after the pages), stored and claimed (R3, R4).
FinishPull ==
    /\ reply # None
    /\ LET p == reply.puller
           s == reply.server
           served == [o \in Machines |->
                        IF known[s][o] > reply.wm[o] THEN known[s][o] ELSE 0]
       IN
        /\ held' = [held EXCEPT ![p] = [o \in Machines |-> @[o] \cup reply.rows[o]]]
        /\ cursor' = [cursor EXCEPT ![p] = [o \in Machines |->
                        Max(Max(@[o], SetMax(reply.rows[o])), served[o])]]
        /\ known' = [known EXCEPT ![p] = [o \in Machines |-> Max(@[o], served[o])]]
    /\ reply' = None
    /\ UNCHANGED <<clock, rows, floor>>

Next ==
    \/ \E o \in Machines : Write(o) \/ Seal(o)
    \/ \E p, s \in Machines : BeginPull(p, s)
    \/ FinishPull

Fairness ==
    /\ WF_vars(FinishPull)
    /\ \A p, s \in Machines : WF_vars(BeginPull(p, s))

Spec == Init /\ [][Next]_vars /\ Fairness

\* Principle 1: a cursor never claims a position the machine does not hold.
CursorHoldsData ==
    \A m, o \in Machines : cursor[m][o] <= HeldThrough(m, o)

\* Every row eventually reaches every machine.
Converges == <>[](\A m, o \in Machines : held[m][o] = rows[o])

====
