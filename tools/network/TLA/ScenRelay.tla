----------------------------- MODULE ScenRelay -----------------------------
(***************************************************************************)
(* One-org scenarios. Configurations choose two or three connectors; all   *)
(* deliberately map to the same org, which is the deployment shape that    *)
(* exposed the production ownership livelock.                              *)
(***************************************************************************)
EXTENDS RelayTunnelOwnership

ScenConnectorOrg == [c \in CONNECTORS |-> "org1"]

ScenVersion ==
    [c \in CONNECTORS |->
       IF c = "new" THEN 3
       ELSE IF c = "mid" THEN 2
       ELSE 1]

===========================================================================
