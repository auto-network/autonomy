------------------------------- MODULE Scen3M ---------------------------------
(***************************************************************************)
(* Rollover scenario: one session ("s1") with a three-file main rollout    *)
(* succession chain m1 -> m2 -> m3 (no subagents).  Root module for the    *)
(* rollover CAS, per-path-gate, and restart-mid-rollover configurations.   *)
(***************************************************************************)
EXTENDS RolloutIngestion

ScenFKind    == [f \in FILES |-> "main"]
ScenFSession == [f \in FILES |-> "s1"]
ScenFSucc    == [f \in FILES |-> IF f = "m1" THEN "m2"
                                 ELSE IF f = "m2" THEN "m3"
                                 ELSE "none"]
ScenFFirst   == [s \in SESSIONS |-> "m1"]

================================================================================
