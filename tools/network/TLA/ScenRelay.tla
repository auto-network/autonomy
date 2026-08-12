----------------------------- MODULE ScenRelay -----------------------------
(***************************************************************************)
(* One-org scenarios. Every configured tunnel and viewer maps to org1.     *)
(* Tunnel capacities are equal so least-active-channel admission is the    *)
(* natural load-shedding rule.                                             *)
(***************************************************************************)
EXTENDS RelayTunnelOwnership

ScenTunnelOrg == [t \in TUNNELS |-> "org1"]

\* Multiple outbound tunnels can terminate on different servers behind the
\* same public anycast name. The logical org pool spans those relay nodes.
ScenTunnelRelay ==
    [t \in TUNNELS |-> IF t \in {"t2", "new"} THEN "r2" ELSE "r1"]

ScenViewerOrg == [v \in VIEWERS |-> "org1"]

ScenViewerIngress ==
    [v \in VIEWERS |-> IF v \in {"v2", "v4"} THEN "r2" ELSE "r1"]

ScenCapacity == [t \in TUNNELS |-> 3]

===========================================================================
