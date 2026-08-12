----------------------- MODULE RelayTunnelOwnership -----------------------
(***************************************************************************)
(* Relay tunnel ownership for tools/network/registry/relay.py and          *)
(* tools/network/relaykit/connector.py.                                     *)
(*                                                                         *)
(* The critical deployment-fidelity decision is in Init: CONNECTORS may    *)
(* contain two or more processes mapped by ConnectorOrg to the SAME org.   *)
(* The intended deployment has one connector per org, but the production   *)
(* algorithm does not enforce that premise; excluding it would make the    *)
(* observed replacement livelock unreachable by construction.             *)
(*                                                                         *)
(* Registration and eviction are deliberately separate actions.           *)
(* TunnelHub.register installs the new tunnel synchronously, then the       *)
(* endpoint awaits close(4409) on the displaced WebSocket before sending   *)
(* the successful hello. The model preserves that interval and the         *)
(* identity-guarded unregister rule: closing an old tunnel never clears a   *)
(* newer holder.                                                            *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets, TLC

CONSTANTS
    ORGS,
    CONNECTORS,
    ConnectorOrg,       \* [CONNECTORS -> ORGS]
    Version,            \* [CONNECTORS -> Nat], observation only
    MinBackoff,
    MaxRetryBackoff,
    HardBackoff,
    Jitter,
    MaxDelay,
    EvictionPolicy,     \* "redial" | "standDown" | "hardBackoff"
    RestartBudget

NoConnector == "none"

ClientStates == {
    "dialing", "handshaking", "connected", "closed",
    "sleeping", "stoodDown"
}
ServerStates == {"idle", "evict", "ack", "serve"}
CloseReasons == {"none", "replaced"}

ASSUME ORGS # {}
ASSUME CONNECTORS # {}
ASSUME ConnectorOrg \in [CONNECTORS -> ORGS]
ASSUME Version \in [CONNECTORS -> Nat]
ASSUME \A o \in ORGS : \E c \in CONNECTORS : ConnectorOrg[c] = o
ASSUME MinBackoff \in Nat \ {0}
ASSUME MaxRetryBackoff \in Nat
ASSUME MinBackoff <= MaxRetryBackoff
ASSUME HardBackoff \in Nat
ASSUME MaxRetryBackoff <= HardBackoff
ASSUME Jitter \in Nat
ASSUME MaxDelay \in Nat
ASSUME HardBackoff + Jitter <= MaxDelay
ASSUME EvictionPolicy \in {"redial", "standDown", "hardBackoff"}
ASSUME RestartBudget \in Nat

VARIABLES
    holder,             \* [ORGS -> CONNECTORS \cup {NoConnector}]
    client,             \* connector-loop state
    server,             \* endpoint state for the connector's current WS
    victim,             \* tunnel object displaced by this registration
    socketOpen,
    closeReason,
    backoff,
    wait,
    restartsLeft,
    everHeld,           \* ghost: org has had a holder at least once
    herdSeen            \* ghost: >=2 same-org retries became ready together

vars == << holder, client, server, victim, socketOpen, closeReason,
           backoff, wait, restartsLeft, everHeld, herdSeen >>

(***************************************************************************)
(* Helpers                                                                 *)
(***************************************************************************)

Min(a, b) == IF a <= b THEN a ELSE b

NextBackoff(b) == Min(2 * b, MaxRetryBackoff)

RetryBase(c) ==
    IF closeReason[c] = "replaced" /\ EvictionPolicy = "hardBackoff"
    THEN HardBackoff
    ELSE backoff[c]

DelayRange(base) == base..Min(base + Jitter, MaxDelay)

OrgConnectors(o) == {c \in CONNECTORS : ConnectorOrg[c] = o}

ActiveSockets == {c \in CONNECTORS : socketOpen[c]}

Collision(connectors, delays) ==
    \E o \in ORGS, d \in 0..MaxDelay :
        Cardinality({c \in connectors :
                       ConnectorOrg[c] = o /\ delays[c] = d}) > 1

(***************************************************************************)
(* Init                                                                    *)
(***************************************************************************)

Init ==
    /\ holder = [o \in ORGS |-> NoConnector]
    \* Every configured connector starts independently. In the incident
    \* configuration both "old" and "new" map to "org1"; neither the
    \* model nor the production hello gate assumes uniqueness.
    /\ client = [c \in CONNECTORS |-> "dialing"]
    /\ server = [c \in CONNECTORS |-> "idle"]
    /\ victim = [c \in CONNECTORS |-> NoConnector]
    /\ socketOpen = [c \in CONNECTORS |-> FALSE]
    /\ closeReason = [c \in CONNECTORS |-> "none"]
    /\ backoff = [c \in CONNECTORS |-> MinBackoff]
    /\ wait = [c \in CONNECTORS |-> 0]
    /\ restartsLeft = RestartBudget
    /\ everHeld = [o \in ORGS |-> FALSE]
    /\ herdSeen = FALSE

(***************************************************************************)
(* Relay endpoint and connector loop                                       *)
(***************************************************************************)

\* A valid hello has reached TunnelHub.register. This is the synchronous
\* dict assignment: the new tunnel becomes visible before its predecessor
\* is closed and before the new connector receives {ok:true}.
Register(c) ==
    /\ client[c] = "dialing"
    /\ server[c] = "idle"
    /\ wait[c] = 0
    /\ LET o == ConnectorOrg[c]
           old == holder[o]
       IN /\ holder' = [holder EXCEPT ![o] = c]
          /\ client' = [client EXCEPT ![c] = "handshaking"]
          /\ server' = [server EXCEPT ![c] =
                           IF old = NoConnector THEN "ack" ELSE "evict"]
          /\ victim' = [victim EXCEPT ![c] = old]
          /\ socketOpen' = [socketOpen EXCEPT ![c] = TRUE]
          /\ closeReason' = [closeReason EXCEPT ![c] = "none"]
          /\ everHeld' = [everHeld EXCEPT ![o] = TRUE]
    /\ UNCHANGED << backoff, wait, restartsLeft, herdSeen >>

\* The awaited close(4409). The victim may already have been closed by a
\* later registration; close_quietly makes that a no-op. Crucially this
\* action never clears holder: unregister(old_tunnel) is identity guarded.
EvictPrevious(c) ==
    /\ server[c] = "evict"
    /\ victim[c] \in CONNECTORS
    /\ LET old == victim[c] IN
       /\ IF socketOpen[old]
          THEN /\ socketOpen' = [socketOpen EXCEPT ![old] = FALSE]
               /\ client' = [client EXCEPT ![old] = "closed"]
               /\ closeReason' = [closeReason EXCEPT ![old] = "replaced"]
          ELSE /\ UNCHANGED socketOpen
               /\ UNCHANGED client
               /\ UNCHANGED closeReason
       /\ server' = [server EXCEPT ![c] = "ack"]
       /\ victim' = [victim EXCEPT ![c] = NoConnector]
    /\ UNCHANGED << holder, backoff, wait, restartsLeft,
                    everHeld, herdSeen >>

\* The connector sees a successful hello only here. This is the production
\* backoff reset that turns the two-connector replacement cycle into a
\* tight livelock rather than an exponentially slowing failure loop.
HelloOK(c) ==
    /\ server[c] = "ack"
    /\ socketOpen[c]
    /\ client[c] = "handshaking"
    /\ server' = [server EXCEPT ![c] = "serve"]
    /\ client' = [client EXCEPT ![c] = "connected"]
    /\ backoff' = [backoff EXCEPT ![c] = MinBackoff]
    /\ victim' = [victim EXCEPT ![c] = NoConnector]
    /\ UNCHANGED << holder, socketOpen, closeReason, wait,
                    restartsLeft, everHeld, herdSeen >>

\* A displaced endpoint reaches finally. If a newer tunnel is now in the
\* hub, production unregister(old_tunnel) does nothing; that identity test
\* is abstracted by leaving holder unchanged here.
CleanupClosed(c) ==
    /\ ~socketOpen[c]
    /\ server[c] \in {"ack", "serve"}
    /\ client[c] = "closed"
    /\ server' = [server EXCEPT ![c] = "idle"]
    /\ victim' = [victim EXCEPT ![c] = NoConnector]
    /\ UNCHANGED << holder, client, socketOpen, closeReason,
                    backoff, wait, restartsLeft, everHeld, herdSeen >>

\* Connector.run handles the close. Production uses the first branch for
\* every close, including 4409. The two candidate variants are explicit:
\* standDown makes 4409 terminal; hardBackoff still retries after a finite
\* delay. Relay-restart closes are scheduled directly by RelayRestart and
\* never take the 4409-only stand-down branch.
HandleClose(c) ==
    /\ client[c] = "closed"
    /\ server[c] = "idle"
    /\ IF closeReason[c] = "replaced" /\ EvictionPolicy = "standDown"
       THEN /\ client' = [client EXCEPT ![c] = "stoodDown"]
            /\ wait' = [wait EXCEPT ![c] = 0]
            /\ closeReason' = [closeReason EXCEPT ![c] = "none"]
            /\ UNCHANGED backoff
       ELSE \E d \in DelayRange(RetryBase(c)) :
            /\ client' = [client EXCEPT ![c] = "sleeping"]
            /\ wait' = [wait EXCEPT ![c] = d]
            /\ backoff' = [backoff EXCEPT ![c] =
                              IF closeReason[c] = "replaced"
                                 /\ EvictionPolicy = "hardBackoff"
                              THEN MaxRetryBackoff
                              ELSE NextBackoff(@)]
            /\ closeReason' = [closeReason EXCEPT ![c] = "none"]
    /\ UNCHANGED << holder, server, victim, socketOpen,
                    restartsLeft, everHeld, herdSeen >>

\* One clock step for all connector retry timers. Jitter is represented by
\* nondeterministic delay selection, not probability; equal random draws
\* remain a permitted behavior and therefore cannot prove herd avoidance.
Tick ==
    /\ \E c \in CONNECTORS : client[c] = "sleeping" /\ wait[c] > 0
    /\ LET readyTogether == {c \in CONNECTORS :
                               client[c] = "sleeping" /\ wait[c] = 1}
       IN /\ wait' = [c \in CONNECTORS |->
                        IF client[c] = "sleeping" /\ wait[c] > 0
                        THEN wait[c] - 1 ELSE wait[c]]
          /\ herdSeen' = (herdSeen \/
               (\E o \in ORGS :
                   Cardinality(readyTogether \cap OrgConnectors(o)) > 1))
    /\ UNCHANGED << holder, client, server, victim, socketOpen,
                    closeReason, backoff, restartsLeft, everHeld >>

Wake(c) ==
    /\ client[c] = "sleeping"
    /\ wait[c] = 0
    /\ client' = [client EXCEPT ![c] = "dialing"]
    /\ UNCHANGED << holder, server, victim, socketOpen, closeReason,
                    backoff, wait, restartsLeft, everHeld, herdSeen >>

(***************************************************************************)
(* Relay restart                                                          *)
(***************************************************************************)

\* An atomic stop/start abstraction: in-memory hub ownership and endpoint
\* tasks disappear; connector retry timers survive. Every socket that was
\* live at the boundary schedules an ordinary transient retry. This is
\* sufficient for ownership and retry-alignment questions; outage duration
\* and failed TCP attempts while the relay is down are deliberately absent.
RelayRestart ==
    /\ restartsLeft > 0
    /\ LET active == ActiveSockets IN
       \E delays \in [CONNECTORS -> 0..MaxDelay] :
         /\ \A c \in CONNECTORS :
              IF c \in active
              THEN delays[c] \in DelayRange(backoff[c])
              ELSE delays[c] = 0
         /\ holder' = [o \in ORGS |-> NoConnector]
         /\ client' = [c \in CONNECTORS |->
                         IF c \in active THEN "sleeping" ELSE client[c]]
         /\ server' = [c \in CONNECTORS |-> "idle"]
         /\ victim' = [c \in CONNECTORS |-> NoConnector]
         /\ socketOpen' = [c \in CONNECTORS |-> FALSE]
         /\ closeReason' = [c \in CONNECTORS |-> "none"]
         /\ wait' = [c \in CONNECTORS |->
                       IF c \in active THEN delays[c] ELSE wait[c]]
         /\ backoff' = [c \in CONNECTORS |->
                          IF c \in active THEN NextBackoff(backoff[c])
                          ELSE backoff[c]]
         /\ herdSeen' = (herdSeen \/ Collision(active, delays))
    /\ restartsLeft' = restartsLeft - 1
    /\ UNCHANGED everHeld

(***************************************************************************)
(* Next / fairness                                                        *)
(***************************************************************************)

ConnectorActs ==
    \E c \in CONNECTORS :
        Register(c) \/ EvictPrevious(c) \/ HelloOK(c)
        \/ CleanupClosed(c) \/ HandleClose(c) \/ Wake(c)

Next == ConnectorActs \/ Tick \/ RelayRestart

Spec == Init /\ [][Next]_vars

\* Connector loops, endpoint continuations, and timer progress are weakly
\* fair. RelayRestart is an unfair, bounded environment action.
FairSpec ==
    /\ Spec
    /\ \A c \in CONNECTORS :
         /\ WF_vars(Register(c))
         /\ WF_vars(EvictPrevious(c))
         /\ WF_vars(HelloOK(c))
         /\ WF_vars(CleanupClosed(c))
         /\ WF_vars(HandleClose(c))
         /\ WF_vars(Wake(c))
    /\ WF_vars(Tick)

(***************************************************************************)
(* Safety                                                                 *)
(***************************************************************************)

TypeOK ==
    /\ holder \in [ORGS -> CONNECTORS \cup {NoConnector}]
    /\ client \in [CONNECTORS -> ClientStates]
    /\ server \in [CONNECTORS -> ServerStates]
    /\ victim \in [CONNECTORS -> CONNECTORS \cup {NoConnector}]
    /\ socketOpen \in [CONNECTORS -> BOOLEAN]
    /\ closeReason \in [CONNECTORS -> CloseReasons]
    /\ backoff \in [CONNECTORS -> MinBackoff..MaxRetryBackoff]
    /\ wait \in [CONNECTORS -> 0..MaxDelay]
    /\ restartsLeft \in 0..RestartBudget
    /\ everHeld \in [ORGS -> BOOLEAN]
    /\ herdSeen \in BOOLEAN

\* Dict ownership always names the newest registered tunnel's open socket.
HolderHasOpenSocket ==
    \A o \in ORGS :
        holder[o] # NoConnector => socketOpen[holder[o]]

\* Before the first modeled relay restart, register-then-evict can never
\* create a no-holder gap after an org has acquired a holder. This is the
\* identity-guarded unregister result the non-atomic split is meant to test.
NoUncausedVacuum ==
    \A o \in ORGS :
        (everHeld[o] /\ restartsLeft = RestartBudget)
            => holder[o] # NoConnector

\* Even with 4409 stand-down, a no-holder state always retains at least one
\* connector that can recover without restarting a stood-down process.
NoTerminalVacuum ==
    \A o \in ORGS :
        holder[o] = NoConnector =>
            \E c \in OrgConnectors(o) : client[c] # "stoodDown"

NoHerd == ~herdSeen

(***************************************************************************)
(* Liveness / selection                                                    *)
(***************************************************************************)

\* The ownership value eventually becomes constant. The current production
\* policy violates this with two same-org connectors: TLC finds a lasso in
\* which each successful hello resets its backoff before the peer evicts it.
EventuallyStable ==
    \A o \in ORGS :
        \E h \in CONNECTORS \cup {NoConnector} : <>[](holder[o] = h)

EventuallyAvailable ==
    <>[](\A o \in ORGS : holder[o] # NoConnector)

IsNewest(c) ==
    \A other \in OrgConnectors(ConnectorOrg[c]) :
        Version[c] >= Version[other]

\* Last-writer-wins has no version order. Even the stabilizing stand-down
\* variant permits the older process to register last and hold forever.
EventuallyNewest ==
    <>[](\A o \in ORGS :
          holder[o] # NoConnector /\ IsNewest(holder[o]))

===========================================================================
