----------------------- MODULE RelayTunnelOwnership -----------------------
(***************************************************************************)
(* Relay tunnel POOL semantics. Multiple authenticated connectors for the  *)
(* same org are cooperating capacity, not rival owners.                    *)
(*                                                                         *)
(* The green design stores a set of live tunnels per org. Register adds;   *)
(* disconnect removes exactly that tunnel; a viewer is admitted to a       *)
(* least-loaded tunnel with capacity and remains pinned there.             *)
(*                                                                         *)
(* PoolRegistration = FALSE restores the shipped singular last-writer      *)
(* replacement algorithm solely as a calibration. TLC must rediscover its  *)
(* replacement lasso. LeastLoadedAdmission = FALSE similarly restores      *)
(* arbitrary admission and must expose avoidable skew.                     *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets, TLC

CONSTANTS
    ORGS,
    RELAYS,
    TUNNELS,
    VIEWERS,
    TunnelOrg,              \* [TUNNELS -> ORGS]
    TunnelRelay,            \* [TUNNELS -> RELAYS], outbound termination
    ViewerOrg,              \* [VIEWERS -> ORGS]
    ViewerIngress,          \* [VIEWERS -> RELAYS], e.g. anycast landing
    Capacity,               \* [TUNNELS -> Nat \ {0}]
    MinBackoff,
    MaxBackoff,
    Jitter,
    MaxDelay,
    PoolRegistration,       \* TRUE = intended pool; FALSE = old replacement
    LeastLoadedAdmission,   \* TRUE = shed new viewers to least-loaded member
    RestartBudget,
    DisconnectBudget,
    RefuseBudget,           \* bounded pre-serving refusals (auto-d8if0)
    FailoverMaxCandidates   \* tunnels one dial may try (relay FAILOVER_MAX_CANDIDATES)

NoTunnel == "none"

ConnectorStates == {"dialing", "connected", "sleeping"}
ViewerStates == {"new", "open"}

ASSUME ORGS # {}
ASSUME RELAYS # {}
ASSUME TUNNELS # {}
ASSUME TunnelOrg \in [TUNNELS -> ORGS]
ASSUME TunnelRelay \in [TUNNELS -> RELAYS]
ASSUME ViewerOrg \in [VIEWERS -> ORGS]
ASSUME ViewerIngress \in [VIEWERS -> RELAYS]
ASSUME Capacity \in [TUNNELS -> Nat \ {0}]
ASSUME \A o \in ORGS : \E t \in TUNNELS : TunnelOrg[t] = o
ASSUME MinBackoff \in Nat \ {0}
ASSUME MaxBackoff \in Nat
ASSUME MinBackoff <= MaxBackoff
ASSUME Jitter \in Nat
ASSUME MaxBackoff + Jitter <= MaxDelay
ASSUME PoolRegistration \in BOOLEAN
ASSUME LeastLoadedAdmission \in BOOLEAN
ASSUME RestartBudget \in Nat
ASSUME DisconnectBudget \in Nat
ASSUME RefuseBudget \in Nat
ASSUME FailoverMaxCandidates \in Nat \ {0}

VARIABLES
    pool,                   \* [ORGS -> SUBSET TUNNELS]
    connector,              \* connector process state
    backoff,
    wait,
    stable,                 \* served for one MaxBackoff interval
    viewer,
    assignment,             \* [VIEWERS -> TUNNELS \cup {NoTunnel}]
    restartsLeft,
    disconnectsLeft,
    refusalsLeft,
    refused,                \* [VIEWERS -> SUBSET TUNNELS]: tunnels that refused this dial
    healthyEvictionSeen,    \* ghost: register removed another live tunnel
    badAdmissionSeen        \* ghost: viewer bypassed a less-loaded tunnel

vars == << pool, connector, backoff, wait, stable, viewer, assignment,
           restartsLeft, disconnectsLeft, refusalsLeft, refused,
           healthyEvictionSeen, badAdmissionSeen >>

(***************************************************************************)
(* Helpers                                                                 *)
(***************************************************************************)

Min(a, b) == IF a <= b THEN a ELSE b

NextBackoff(b) == Min(2 * b, MaxBackoff)

DelayRange(base) == base..Min(base + Jitter, MaxDelay)

OrgTunnels(o) == {t \in TUNNELS : TunnelOrg[t] = o}

Load(t) == Cardinality({v \in VIEWERS : assignment[v] = t})

Available(o) ==
    {t \in pool[o] : connector[t] = "connected" /\ Load(t) < Capacity[t]}

LeastLoaded(t, o) ==
    t \in Available(o)
    /\ \A other \in Available(o) : Load(t) <= Load(other)

\* A tunnel that may still be dialed for viewer v: live, with capacity, and
\* not one that already refused this dial (auto-s81lo: a refusing member is
\* skipped, never re-tried for the same dial).
CanServe(t, v) ==
    t \in Available(ViewerOrg[v]) /\ t \notin refused[v]

Candidates(v) == {t \in TUNNELS : CanServe(t, v)}

\* Least-loaded among the candidates still open to this dial.
LeastLoadedCandidate(t, v) ==
    t \in Candidates(v)
    /\ \A other \in Candidates(v) : Load(t) <= Load(other)

CrossRelay(v) ==
    assignment[v] # NoTunnel
    /\ ViewerIngress[v] # TunnelRelay[assignment[v]]

\* The model deliberately does not require ingress and tunnel termination to
\* be the same relay. Anycast chooses an ingress edge; a directory/internal
\* handoff makes every logical pool member selectable from that edge.

(***************************************************************************)
(* Init: every configured tunnel is an independent connector for its org.  *)
(* Multiple same-org connectors are required, not an exceptional state.    *)
(***************************************************************************)

Init ==
    /\ pool = [o \in ORGS |-> {}]
    /\ connector = [t \in TUNNELS |-> "dialing"]
    /\ backoff = [t \in TUNNELS |-> MinBackoff]
    /\ wait = [t \in TUNNELS |-> 0]
    /\ stable = [t \in TUNNELS |-> FALSE]
    /\ viewer = [v \in VIEWERS |-> "new"]
    /\ assignment = [v \in VIEWERS |-> NoTunnel]
    /\ restartsLeft = RestartBudget
    /\ disconnectsLeft = DisconnectBudget
    /\ refusalsLeft = RefuseBudget
    /\ refused = [v \in VIEWERS |-> {}]
    /\ healthyEvictionSeen = FALSE
    /\ badAdmissionSeen = FALSE

(***************************************************************************)
(* Tunnel membership                                                       *)
(***************************************************************************)

\* A successful authenticated hello. Green behavior is a set insertion.
\* Hello proves identity, not useful service, and therefore does NOT reset
\* retry backoff. MarkStable records the separate health threshold.
\* The calibration branch is the current dict assignment plus 4409 close:
\* all previous members are removed and their connector loops sleep/retry.
Register(t) ==
    /\ connector[t] = "dialing"
    /\ wait[t] = 0
    /\ LET o == TunnelOrg[t]
           displaced == pool[o] \ {t}
       IN IF PoolRegistration
          THEN /\ pool' = [pool EXCEPT ![o] = @ \cup {t}]
               /\ connector' = [connector EXCEPT ![t] = "connected"]
               /\ backoff' = backoff
               /\ wait' = [wait EXCEPT ![t] = 0]
               /\ stable' = [stable EXCEPT ![t] = FALSE]
               /\ UNCHANGED << viewer, assignment,
                               healthyEvictionSeen >>
          ELSE /\ pool' = [pool EXCEPT ![o] = {t}]
               /\ connector' = [q \in TUNNELS |->
                    IF q = t THEN "connected"
                    ELSE IF q \in displaced THEN "sleeping"
                    ELSE connector[q]]
               /\ backoff' = [q \in TUNNELS |->
                    IF q \in displaced
                    THEN NextBackoff(
                         IF stable[q] THEN MinBackoff ELSE backoff[q])
                    ELSE backoff[q]]
               /\ wait' = [q \in TUNNELS |->
                    IF q = t THEN 0
                    ELSE IF q \in displaced
                    THEN IF stable[q] THEN MinBackoff ELSE backoff[q]
                    ELSE wait[q]]
               /\ stable' = [q \in TUNNELS |->
                    IF q = t \/ q \in displaced THEN FALSE ELSE stable[q]]
               /\ viewer' = [v \in VIEWERS |->
                    IF assignment[v] \in displaced THEN "new" ELSE viewer[v]]
               /\ assignment' = [v \in VIEWERS |->
                    IF assignment[v] \in displaced
                    THEN NoTunnel ELSE assignment[v]]
               /\ healthyEvictionSeen' =
                    (healthyEvictionSeen \/ displaced # {})
    /\ UNCHANGED << restartsLeft, disconnectsLeft, refusalsLeft, refused,
                    badAdmissionSeen >>

\* Abstracts one authenticated MaxBackoff service interval. It is separate
\* from Register so an immediate post-hello flap cannot masquerade as health.
MarkStable(t) ==
    /\ connector[t] = "connected"
    /\ ~stable[t]
    /\ stable' = [stable EXCEPT ![t] = TRUE]
    /\ UNCHANGED << pool, connector, backoff, wait, viewer, assignment,
                    restartsLeft, disconnectsLeft, refusalsLeft, refused,
                    healthyEvictionSeen, badAdmissionSeen >>

\* Exact-instance unregister plus ordinary connector retry. Only viewers
\* pinned to this tunnel are returned to admission; every other assignment
\* is untouched.
Disconnect(t) ==
    /\ disconnectsLeft > 0
    /\ connector[t] = "connected"
    /\ t \in pool[TunnelOrg[t]]
    /\ LET base == IF stable[t] THEN MinBackoff ELSE backoff[t] IN
       \E d \in DelayRange(base) :
         /\ pool' = [pool EXCEPT ![TunnelOrg[t]] = @ \ {t}]
         /\ connector' = [connector EXCEPT ![t] = "sleeping"]
         /\ wait' = [wait EXCEPT ![t] = d]
         /\ backoff' = [backoff EXCEPT ![t] = NextBackoff(base)]
         /\ stable' = [stable EXCEPT ![t] = FALSE]
         /\ viewer' = [v \in VIEWERS |->
              IF assignment[v] = t THEN "new" ELSE viewer[v]]
         /\ assignment' = [v \in VIEWERS |->
              IF assignment[v] = t THEN NoTunnel ELSE assignment[v]]
         /\ refused' = [v \in VIEWERS |->
              IF assignment[v] = t THEN {} ELSE refused[v]]
    /\ disconnectsLeft' = disconnectsLeft - 1
    /\ UNCHANGED << restartsLeft, refusalsLeft,
                    healthyEvictionSeen, badAdmissionSeen >>

\* auto-s81lo / auto-d8if0: a connected member ends the viewer's channel
\* BEFORE serving a byte (no grant, unarmed, key resolution refused). The
\* tunnel stays in the pool: a refusal is about this link, not the member.
\* The viewer returns to admission for the same dial and may not be
\* re-assigned to this tunnel; whether the dial may try another tunnel is
\* OpenViewer's bound, not the refuser's.
Refuse(t, v) ==
    /\ refusalsLeft > 0
    /\ viewer[v] = "open"
    /\ assignment[v] = t
    /\ connector[t] = "connected"
    /\ viewer' = [viewer EXCEPT ![v] = "new"]
    /\ assignment' = [assignment EXCEPT ![v] = NoTunnel]
    /\ refused' = [refused EXCEPT ![v] = @ \cup {t}]
    /\ refusalsLeft' = refusalsLeft - 1
    /\ UNCHANGED << pool, connector, backoff, wait, stable,
                    restartsLeft, disconnectsLeft,
                    healthyEvictionSeen, badAdmissionSeen >>

Tick ==
    /\ \E t \in TUNNELS : connector[t] = "sleeping" /\ wait[t] > 0
    /\ wait' = [t \in TUNNELS |->
         IF connector[t] = "sleeping" /\ wait[t] > 0
         THEN wait[t] - 1 ELSE wait[t]]
    /\ UNCHANGED << pool, connector, backoff, stable, viewer, assignment,
                    restartsLeft, disconnectsLeft, refusalsLeft, refused,
                    healthyEvictionSeen, badAdmissionSeen >>

Wake(t) ==
    /\ connector[t] = "sleeping"
    /\ wait[t] = 0
    /\ connector' = [connector EXCEPT ![t] = "dialing"]
    /\ UNCHANGED << pool, backoff, wait, stable, viewer, assignment,
                    restartsLeft, disconnectsLeft, refusalsLeft, refused,
                    healthyEvictionSeen, badAdmissionSeen >>

\* Atomic relay stop/start abstraction. The in-memory pool disappears;
\* live connector sockets retry. Existing sleeping timers remain intact.
RelayRestart ==
    /\ restartsLeft > 0
    /\ LET live == {t \in TUNNELS : connector[t] = "connected"} IN
       \E delays \in [TUNNELS -> 0..MaxDelay] :
         /\ \A t \in TUNNELS :
              IF t \in live
              THEN delays[t] \in DelayRange(
                   IF stable[t] THEN MinBackoff ELSE backoff[t])
              ELSE delays[t] = 0
         /\ pool' = [o \in ORGS |-> {}]
         /\ connector' = [t \in TUNNELS |->
              IF t \in live THEN "sleeping" ELSE connector[t]]
         /\ wait' = [t \in TUNNELS |->
              IF t \in live THEN delays[t] ELSE wait[t]]
         /\ backoff' = [t \in TUNNELS |->
              IF t \in live
              THEN NextBackoff(IF stable[t] THEN MinBackoff ELSE backoff[t])
              ELSE backoff[t]]
         /\ stable' = [t \in TUNNELS |->
              IF t \in live THEN FALSE ELSE stable[t]]
         /\ viewer' = [v \in VIEWERS |-> "new"]
         /\ assignment' = [v \in VIEWERS |-> NoTunnel]
         /\ refused' = [v \in VIEWERS |-> {}]
    /\ restartsLeft' = restartsLeft - 1
    /\ UNCHANGED << disconnectsLeft, refusalsLeft,
                    healthyEvictionSeen, badAdmissionSeen >>

(***************************************************************************)
(* Viewer admission                                                        *)
(***************************************************************************)

\* Selection happens once, at channel open. The assignment is never moved
\* merely because another tunnel later joins or becomes less loaded. A dial
\* tries at most FailoverMaxCandidates tunnels: once that many have refused
\* it, the viewer is not admitted again for this dial (the relay closes it
\* with the honest code), whatever candidates remain.
OpenViewer(v) ==
    /\ viewer[v] = "new"
    /\ Cardinality(refused[v]) < FailoverMaxCandidates
    /\ Candidates(v) # {}
    /\ \E t \in Candidates(v) :
         /\ (LeastLoadedAdmission => LeastLoadedCandidate(t, v))
         /\ viewer' = [viewer EXCEPT ![v] = "open"]
         /\ assignment' = [assignment EXCEPT ![v] = t]
         /\ badAdmissionSeen' =
              (badAdmissionSeen \/ ~LeastLoadedCandidate(t, v))
    /\ UNCHANGED << pool, connector, backoff, wait, stable,
                    restartsLeft, disconnectsLeft, refusalsLeft, refused,
                    healthyEvictionSeen >>

(***************************************************************************)
(* Next / fairness                                                         *)
(***************************************************************************)

Next ==
    \/ \E t \in TUNNELS : Register(t) \/ MarkStable(t) \/ Disconnect(t) \/ Wake(t)
    \/ \E v \in VIEWERS : OpenViewer(v)
    \/ \E t \in TUNNELS, v \in VIEWERS : Refuse(t, v)
    \/ Tick
    \/ RelayRestart

Spec == Init /\ [][Next]_vars

\* Connector registration/retry, timers, and viewer admission are reliable.
\* Tunnel disconnect and relay restart are bounded, unfair environment acts.
FairSpec ==
    /\ Spec
    /\ \A t \in TUNNELS :
         /\ WF_vars(Register(t))
         /\ WF_vars(MarkStable(t))
         /\ WF_vars(Wake(t))
    /\ \A v \in VIEWERS : WF_vars(OpenViewer(v))
    /\ WF_vars(Tick)

(***************************************************************************)
(* Safety                                                                  *)
(***************************************************************************)

TypeOK ==
    /\ pool \in [ORGS -> SUBSET TUNNELS]
    /\ connector \in [TUNNELS -> ConnectorStates]
    /\ backoff \in [TUNNELS -> MinBackoff..MaxBackoff]
    /\ wait \in [TUNNELS -> 0..MaxDelay]
    /\ stable \in [TUNNELS -> BOOLEAN]
    /\ viewer \in [VIEWERS -> ViewerStates]
    /\ assignment \in [VIEWERS -> TUNNELS \cup {NoTunnel}]
    /\ restartsLeft \in 0..RestartBudget
    /\ disconnectsLeft \in 0..DisconnectBudget
    /\ refusalsLeft \in 0..RefuseBudget
    /\ refused \in [VIEWERS -> SUBSET TUNNELS]
    /\ healthyEvictionSeen \in BOOLEAN
    /\ badAdmissionSeen \in BOOLEAN

PoolCoherent ==
    \A o \in ORGS :
        pool[o] = {t \in OrgTunnels(o) : connector[t] = "connected"}

AssignmentsUseLiveTunnel ==
    \A v \in VIEWERS :
        viewer[v] = "open" =>
            /\ assignment[v] \in pool[ViewerOrg[v]]
            /\ TunnelOrg[assignment[v]] = ViewerOrg[v]

CapacityRespected ==
    \A t \in TUNNELS : Load(t) <= Capacity[t]

NoHealthyEviction == ~healthyEvictionSeen

\* A refusal is never assigned twice within one dial, and a dial never
\* exceeds the relay's candidate bound.
RefusalsRespected ==
    \A v \in VIEWERS :
        /\ (viewer[v] = "open" => assignment[v] \notin refused[v])
        /\ Cardinality(refused[v]) <= FailoverMaxCandidates

AdmissionUsesLeastLoad == ~badAdmissionSeen

(***************************************************************************)
(* Liveness                                                                *)
(***************************************************************************)

\* After bounded failures/restart, every healthy connector is a pool member.
\* The old replacement algorithm violates this with the known retry lasso.
EventuallyAllTunnelsRegistered ==
    <>[](\A t \in TUNNELS :
          connector[t] = "connected" /\ t \in pool[TunnelOrg[t]])

\* Green configs provision enough aggregate capacity for their viewers.
EventuallyEveryViewerAssigned ==
    <>[](\A v \in VIEWERS : viewer[v] = "open")

\* With refusals bounded, a viewer that still has a serving candidate is
\* eventually seated on one; a viewer every candidate refused is the
\* honest 4502/4503/4505 verdict the relay returns, not a stuck dial.
EventuallyEveryServableViewerAssigned ==
    <>[](\A v \in VIEWERS : viewer[v] = "open" \/ Candidates(v) = {})

===========================================================================
