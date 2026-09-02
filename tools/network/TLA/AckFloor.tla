------------------------------ MODULE AckFloor ------------------------------
(***************************************************************************)
(* Served-acknowledgement journal retirement, as shipped in catalog.py     *)
(* (record_served_ack / acknowledged_journal_floor / prune_acknowledged)   *)
(* and the continuity decision in fleet_relay_sync.                        *)
(*                                                                         *)
(* One serving machine's journal is modeled from its own perspective.      *)
(* `consumed` is ground truth: the prefix (by local transaction ref) each  *)
(* peer has verifiably applied. `acked` is what the server recorded from   *)
(* presented resume trails. The safety story is a chain:                   *)
(*   trail resolution  =>  acked[p] <= consumed[p]        (AckSoundness)   *)
(*   prune <= min acked over the FULL roster              (PruneFloor)     *)
(*   =>  no frame in any peer's unconsumed suffix is ever retired, or      *)
(*       its absence is visible as a journal gap and answered with a       *)
(*       checkpoint                                        (Recoverable)   *)
(*                                                                         *)
(* Three constants weaken one shipped mechanism each, so calibration       *)
(* configurations can prove that mechanism is load-bearing:                *)
(*   PruneByTimestamp: retire by authored timestamp instead of ref —       *)
(*     remote imports interleave timestamps behind serving order, so a     *)
(*     timestamp floor can retire an unconsumed frame.                     *)
(*   RequireFullRosterAck = FALSE: prune without every active peer's       *)
(*     acknowledgement — a concurrently enrolling peer loses its replay.   *)
(*   AckResetOnInstall = FALSE: keep recorded acks across a checkpoint     *)
(*     install — the install renumbers refs, so stale acks authorize       *)
(*     retiring brand-new frames with no gap left behind: silent loss.     *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets

CONSTANTS
    Peers,               \* initially active peers
    Enrollee,            \* one peer that may enroll mid-run
    MaxRef,              \* authored transaction bound
    PruneByTimestamp,    \* calibration: timestamp floor instead of ref floor
    RequireFullRosterAck,\* shipped: TRUE
    AckResetOnInstall,   \* shipped: TRUE
    GapRescue            \* relay path after continuity decision: TRUE

AllPeers == Peers \cup {Enrollee}

VARIABLES
    nextRef,     \* refs authored so far (rows are 1..nextRef)
    ts,          \* [1..nextRef -> timestamp]: authored stamp, may interleave
    journal,     \* refs whose frames are retained
    rows,        \* refs whose transaction rows exist (journal \subseteq rows)
    pinned,      \* refs pinned by a current winner
    roster,      \* active peers
    consumed,    \* [AllPeers -> ref]: ground-truth applied prefix
    acked        \* [AllPeers -> ref or timestamp]: server-recorded acks

vars == <<nextRef, ts, journal, rows, pinned, roster, consumed, acked>>

Init ==
    /\ nextRef = 0
    /\ ts = <<>>
    /\ journal = {}
    /\ rows = {}
    /\ pinned = {}
    /\ roster = Peers
    /\ consumed = [p \in AllPeers |-> 0]
    /\ acked = [p \in AllPeers |-> 0]

(* Author one transaction. Its timestamp is any unused stamp, which is     *)
(* exactly how a remote import lands: a fresh (high) ref carrying an old   *)
(* (low) timestamp. Authoring may supersede any older winner, unpinning it.*)
Author ==
    /\ nextRef < MaxRef
    /\ \E stamp \in (1..MaxRef) \ {ts[r] : r \in 1..nextRef} :
        ts' = [r \in 1..(nextRef + 1) |->
                  IF r <= nextRef THEN ts[r] ELSE stamp]
    /\ nextRef' = nextRef + 1
    /\ journal' = journal \cup {nextRef + 1}
    /\ rows' = rows \cup {nextRef + 1}
    /\ \E drop \in SUBSET pinned :
        pinned' = (pinned \ drop) \cup {nextRef + 1}
    /\ UNCHANGED <<roster, consumed, acked>>

(* A delta pull: the peer advances through retained, contiguous frames.    *)
(* It can only consume ref r when every ref in its gap up to r is retained.*)
Serve(p) ==
    /\ p \in roster
    /\ \E target \in (consumed[p] + 1)..nextRef :
        /\ \A r \in (consumed[p] + 1)..target : r \in journal
        /\ consumed' = [consumed EXCEPT ![p] = target]
    /\ UNCHANGED <<nextRef, ts, journal, rows, pinned, roster, acked>>

(* The peer's presented trail names its consumed position; it resolves     *)
(* only while that transaction row still exists, and the recorded value    *)
(* is the ref (shipped) or its timestamp (calibration).                    *)
RecordAck(p) ==
    /\ p \in roster
    /\ consumed[p] \in rows
    /\ acked' = [acked EXCEPT ![p] =
                    IF PruneByTimestamp THEN ts[consumed[p]] ELSE consumed[p]]
    /\ UNCHANGED <<nextRef, ts, journal, rows, pinned, roster, consumed>>

FloorOver(active) ==
    IF active = {} THEN 0
    ELSE CHOOSE f \in {acked[p] : p \in active} :
            \A p \in active : acked[p] >= f

(* Retire acknowledged frames. Shipped semantics: every active peer must   *)
(* have a nonzero acknowledgement, the floor is their minimum, journal     *)
(* frames at or below it retire, and unpinned rows strictly below it       *)
(* retire once frameless.                                                  *)
Prune ==
    /\ roster /= {}
    /\ RequireFullRosterAck => \A p \in roster : acked[p] > 0
    /\ LET floor == FloorOver({p \in roster : acked[p] > 0})
       IN /\ floor > 0
          /\ journal' =
                IF PruneByTimestamp
                THEN {r \in journal : ts[r] > floor}
                ELSE {r \in journal : r > floor}
          /\ rows' = {r \in rows :
                \/ r \in journal'
                \/ r \in pinned
                \/ (IF PruneByTimestamp THEN ts[r] > floor ELSE r >= floor)}
    /\ UNCHANGED <<nextRef, ts, pinned, roster, consumed, acked>>

(* A machine joins the active roster with nothing consumed. Under shipped  *)
(* semantics its zero acknowledgement blocks all pruning until it syncs.   *)
Enroll ==
    /\ Enrollee \notin roster
    /\ roster' = roster \cup {Enrollee}
    /\ UNCHANGED <<nextRef, ts, journal, rows, pinned, consumed, acked>>

(* The peer falls back to a checkpoint: full current state, any trail.     *)
Checkpoint(p) ==
    /\ p \in roster
    /\ consumed' = [consumed EXCEPT ![p] = nextRef]
    /\ UNCHANGED <<nextRef, ts, journal, rows, pinned, roster, acked>>

(* The SERVING machine installs a checkpoint from a peer: the staging     *)
(* database renumbers every transaction ref, so the id space restarts and *)
(* previously recorded acknowledgements point at ids that fresh authorship *)
(* will reuse. Shipped code nulls the copied acks (_copy_peer_state);      *)
(* peers' old trails no longer resolve, so their ground-truth consumed     *)
(* position against the new journal is zero.                               *)
ServerInstall ==
    /\ nextRef' = 0
    /\ ts' = <<>>
    /\ journal' = {}
    /\ rows' = {}
    /\ pinned' = {}
    /\ consumed' = [p \in AllPeers |-> 0]
    /\ acked' = IF AckResetOnInstall
                THEN [p \in AllPeers |-> 0]
                ELSE acked
    /\ UNCHANGED roster

Next ==
    \/ Author
    \/ Enroll
    \/ ServerInstall
    \/ \E p \in AllPeers : Serve(p) \/ RecordAck(p) \/ Checkpoint(p)
    \/ Prune

Spec ==
    /\ Init
    /\ [][Next]_vars
    /\ WF_vars(Prune)
    /\ \A p \in AllPeers : WF_vars(Serve(p)) /\ WF_vars(RecordAck(p))

----------------------------------------------------------------------------

(* What the server recorded never exceeds what the peer truly consumed —   *)
(* meaningful only under ref-order acknowledgements.                       *)
AckSoundness ==
    PruneByTimestamp \/ \A p \in roster : acked[p] <= consumed[p]

(* Every active peer's unconsumed suffix is servable by retained deltas.   *)
DeltaServable(p) ==
    \A r \in (consumed[p] + 1)..nextRef : r \in journal

DeltaAlwaysServable == \A p \in roster : DeltaServable(p)

(* A retired frame is visible: some transaction row lacks its frame.       *)
JournalGap == \E r \in rows : r \notin journal

(* Either deltas fully serve the peer, or the gap signal stands and the    *)
(* continuity decision answers the unresolvable trail with a checkpoint.   *)
Recoverable ==
    \A p \in roster : DeltaServable(p) \/ (GapRescue /\ JournalGap)

(* Under fairness, an authored frame does not stay retained forever once   *)
(* the whole roster keeps pulling: the journal is eventually bounded.      *)
EventuallyRetired == (nextRef > 0) ~> (1 \notin journal)

=============================================================================
