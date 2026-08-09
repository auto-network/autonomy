------------------------------- MODULE Scen2F ---------------------------------
(***************************************************************************)
(* Shared scenario: one session ("s1"), one main rollout ("m1"), one       *)
(* sibling subagent rollout ("c1"), no rollover chain.  The root module    *)
(* for every configuration that needs only the startup/characterize/drain *)
(* surface.  Constant functions are injected from here via the .cfg's      *)
(* `CONSTANT X <- ScenX` substitutions.                                    *)
(***************************************************************************)
EXTENDS RolloutIngestion

ScenFKind    == [f \in FILES |-> IF f = "c1" THEN "sub" ELSE "main"]
ScenFSession == [f \in FILES |-> "s1"]
ScenFSucc    == [f \in FILES |-> "none"]
ScenFFirst   == [s \in SESSIONS |-> "m1"]

================================================================================
