#!/usr/bin/env python3
"""TLC gate for the relay tunnel-pool model.

The cooperative pool must check clean. Calibrations restore singular
last-writer replacement and arbitrary admission; each must fail its exact
named property.
"""
from __future__ import annotations

import glob
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

GREEN = [
    ("PoolGreen.cfg", "ScenRelay"),
    ("AnycastPoolGreen.cfg", "ScenRelay"),
    ("AckFloorGreen.cfg", "AckFloor"),
    ("FleetSyncWriteFloorsCorrected.cfg", "FleetSyncWriteFloors"),
    ("FleetSyncWriteFloorsWide.cfg", "FleetSyncWriteFloors"),
    # auto-qrmlg.5: adopt-by-verification + checkpoint-at-admission (+ admit-
    # on-approval) give every admitted member a pull, safely.
    ("OrgAdmissionProposed.cfg", "OrgAdmission"),
    ("OrgAdmissionProposedApproval.cfg", "OrgAdmission"),
    ("OrgAdmissionProposedForge.cfg", "OrgAdmission"),
    ("OrgAdmissionCurrentForge.cfg", "OrgAdmission"),
    ("OrgAdmissionAdoptOnly.cfg", "OrgAdmission"),
    # auto-qrmlg.11: existing members across a checkpoint advance. RefA/RefB
    # are reference results only (they violate K1: the registry would hold
    # per-member data); C unbounded is live but not removal-safe (below).
    ("OrgAdmissionTransitionRefA.cfg", "OrgAdmission"),
    ("OrgAdmissionTransitionRefB.cfg", "OrgAdmission"),
    ("OrgAdmissionTransitionPredecessorC.cfg", "OrgAdmission"),
    ("OrgAdmissionTransitionVerifierEAny.cfg", "OrgAdmission"),
    ("OrgAdmissionTransitionD1.cfg", "OrgAdmission"),
    ("OrgAdmissionTransitionD2.cfg", "OrgAdmission"),
    ("OrgAdmissionTransitionD1Adversarial.cfg", "OrgAdmission"),
    ("OrgAdmissionTransitionD2Adversarial.cfg", "OrgAdmission"),
    ("OrgAdmissionTransitionVerifierEAnyAdm.cfg", "OrgAdmission"),
    # Three joiners, two consecutive removal re-keys; the founder's event
    # order is fixed (OrgAdmissionX3.tla), member actions are free.
    ("OrgAdmissionX3EAny.cfg", "OrgAdmissionX3"),
    ("OrgAdmissionX3EAnyAdm.cfg", "OrgAdmissionX3"),
    # Prover-downgrade with fold-only adoption (no P1; the registry serves
    # only its tuple) and the founder's record adopted from the join bundle.
    ("OrgAdmissionTransitionEAnyAdmDowngrade.cfg", "OrgAdmission"),
    ("OrgAdmissionX3EAnyAdmDowngrade.cfg", "OrgAdmissionX3"),
    # auto-qrmlg.12: the admission event (approver-authored, carrying the
    # invitee's unchanged signed claim plus approvals) and the final rules.
    ("OrgAdmissionEventT1.cfg", "OrgAdmissionEvent"),
    ("OrgAdmissionEventT2.cfg", "OrgAdmissionEvent"),
    ("OrgAdmissionEventNoRedeemedCheck.cfg", "OrgAdmissionEvent"),
    ("OrgAdmissionFinalApproval.cfg", "OrgAdmission"),
    ("OrgAdmissionFinalSelfAdmit.cfg", "OrgAdmission"),
    # E-any against rekeyed and re-admitted personas (safety).
    ("OrgAdmissionLeavesEAnyRekey.cfg", "OrgAdmissionLeaves"),
    ("OrgAdmissionLeavesEAnyAdmission.cfg", "OrgAdmissionLeaves"),
    ("OrgAdmissionLeavesNewest.cfg", "OrgAdmissionLeaves"),
]

CALIBRATION = [
    ("CurrentLivelock.cfg", "ScenRelay", "EventuallyAllTunnelsRegistered"),
    ("calibration/RandomAdmission.cfg", "ScenRelay", "AdmissionUsesLeastLoad"),
    ("calibration/TimestampPrune.cfg", "AckFloor", "DeltaAlwaysServable"),
    ("calibration/SoloPrune.cfg", "AckFloor", "DeltaAlwaysServable"),
    ("calibration/NoAckResetOnInstall.cfg", "AckFloor", "AckSoundness"),
    # auto-d8if0: with one candidate per dial a refused viewer cannot fail
    # over, so a viewer with a serving candidate left is never seated.
    ("calibration/FailoverCap1.cfg", "ScenRelay", "EventuallyEveryServableViewerAssigned"),
    # The record's write floor rules (bead auto-mmwgu, Propagation) break the
    # constitution's principle 1: a floor read after the transaction pages is
    # claimed as a cursor without the rows below it.
    ("FleetSyncWriteFloorsRecord.cfg", "FleetSyncWriteFloors", "CursorHoldsData"),
    # auto-qrmlg.5: the built rules deadlock a member admitted before a later
    # admission the checkpoint covers; each proposed rule is load-bearing.
    ("OrgAdmissionCurrent.cfg", "OrgAdmission", "EveryAdmittedMemberPulls"),
    ("calibration/CheckpointAtAdmissionOnly.cfg", "OrgAdmission", "EveryAdmittedMemberPulls"),
    ("calibration/AdoptOnlyNoSignOn.cfg", "OrgAdmission", "EveryAdmittedMemberPulls"),
    ("calibration/ApprovalWithoutAdmitOnApproval.cfg", "OrgAdmission", "ApprovedIsAdmitted"),
    ("calibration/NoSignerCheck.cfg", "OrgAdmission", "NoPullWithoutInclusion"),
    ("calibration/ProposedApprovalReachesSync.cfg", "OrgAdmission", "NotAllJoinedInSync"),
    # auto-qrmlg.11: production path construction locks an existing member
    # out; C bounded expires; C unbounded keeps a removed member; D1/D2 with
    # the post-rekey key, without the root check, or without monotone
    # adoption each fail; D1/D2 reach full sync.
    ("OrgAdmissionTransitionFold.cfg", "OrgAdmission", "EveryAdmittedMemberPulls"),
    ("OrgAdmissionTransitionFoldApproval.cfg", "OrgAdmission", "EveryAdmittedMemberPulls"),
    ("calibration/TransitionWindowC.cfg", "OrgAdmission", "EveryAdmittedMemberPulls"),
    ("calibration/TransitionPredecessorCRemoval.cfg", "OrgAdmission", "RemovedExcluded"),
    ("calibration/TransitionD1NoBundle.cfg", "OrgAdmission", "EveryAdmittedMemberPulls"),
    ("calibration/TransitionD2NoBundle.cfg", "OrgAdmission", "EveryAdmittedMemberPulls"),
    ("calibration/TransitionVerifierE.cfg", "OrgAdmission", "EveryAdmittedMemberPulls"),
    ("calibration/TransitionEAnyReachesSync.cfg", "OrgAdmission", "NotAllJoinedInSync"),
    ("calibration/TransitionD1NextKey.cfg", "OrgAdmission", "EveryAdmittedMemberPulls"),
    ("calibration/TransitionD2NextKey.cfg", "OrgAdmission", "EveryAdmittedMemberPulls"),
    ("calibration/TransitionD1NoRootCheck.cfg", "OrgAdmission", "ReconstructedIsCommitted"),
    ("calibration/TransitionD1NoMonotone.cfg", "OrgAdmission", "NoRegression"),
    ("calibration/TransitionD1ReachesSync.cfg", "OrgAdmission", "NotAllJoinedInSync"),
    ("calibration/TransitionD2ReachesSync.cfg", "OrgAdmission", "NotAllJoinedInSync"),
    # D1/D2 + bundle grant fail at the second consecutive re-key: the new
    # generation's grant reaches a member only by a pull (custody chain).
    ("calibration/X3D1Prev.cfg", "OrgAdmissionX3", "EveryAdmittedMemberPulls"),
    ("calibration/X3D2Prev.cfg", "OrgAdmissionX3", "EveryAdmittedMemberPulls"),
    # E-any without the admission bound admits a re-admitted persona under
    # a pre-removal record; C unbounded admits a rekeyed-away old key.
    ("calibration/LeavesEAnyReAdmit.cfg", "OrgAdmissionLeaves", "ReAdmitAfterRemoval"),
    ("calibration/LeavesPredecessorOldKey.cfg", "OrgAdmissionLeaves", "RekeyedOldKeyExcluded"),
    ("calibration/LeavesEAnyAdmitsStale.cfg", "OrgAdmissionLeaves", "NoStaleAdmission"),
    # E-any-adm with today's fold-only adoption (no P1): the lagging side
    # adopts nothing it could verify the up-to-date peer under.
    ("calibration/TransitionEAnyAdmFoldAdopt.cfg", "OrgAdmission", "EveryAdmittedMemberPulls"),
    # Downgrade needs both halves: without it the lagging side cannot adopt
    # the record the up-to-date side proves under; without the bundle adopt
    # (install/registry race) the lagging side has adopted nothing.
    ("calibration/TransitionDowngradeBundleOnly.cfg", "OrgAdmission", "EveryAdmittedMemberPulls"),
    ("calibration/TransitionDowngradeNoJoinAdopt.cfg", "OrgAdmission", "EveryAdmittedMemberPulls"),
    ("calibration/TransitionDowngradeReachesSync.cfg", "OrgAdmission", "NotAllJoinedInSync"),
    # auto-qrmlg.12: each admission-event check is load-bearing.
    ("calibration/EventNoSigCheck.cfg", "OrgAdmissionEvent", "ApproverCannotForgeClaim"),
    ("calibration/EventNoThreshold.cfg", "OrgAdmissionEvent", "AdmissionHasThreshold"),
    ("calibration/EventNoFloor.cfg", "OrgAdmissionEvent", "AdmissionRespectsRemoval"),
    ("calibration/EventNoRedeemedNoFloor.cfg", "OrgAdmissionEvent", "NoDoubleAdmission"),
    ("calibration/EventApprovalAdmits.cfg", "OrgAdmissionEvent", "NoApprovalAdmits"),
]

VIOLATION_MARKERS = (
    "is violated",
    "was violated",
    "Temporal properties were violated",
    "constitutes a counter-example",
)
CLEAN_MARKER = "Model checking completed. No error has been found"


def find_java() -> str:
    configured = os.environ.get("TLA_JAVA")
    if configured:
        return configured
    java_home = os.environ.get("JAVA_HOME")
    if java_home and Path(java_home, "bin", "java").exists():
        return str(Path(java_home, "bin", "java"))
    local = sorted(
        glob.glob(str(Path.home() / "tools" / "jdk-*" / "bin" / "java"))
    )
    return local[-1] if local else "java"


def find_jar() -> str:
    return os.environ.get(
        "TLA_TOOLS_JAR", str(Path.home() / "tools" / "tla2tools.jar")
    )


def run_one(
    config: str, module: str, java: str, jar: str
) -> tuple[str, str, float]:
    name = Path(config).stem
    command = [
        java,
        "-XX:+UseParallelGC",
        "-Xmx4g",
        "-cp",
        jar,
        "tlc2.TLC",
        "-workers",
        os.environ.get("TLC_WORKERS", "auto"),
        "-deadlock",
        "-noGenerateSpecTE",
        "-lncheck",
        "final",
        "-metadir",
        f"/tmp/tlc-relay/{name}",
        "-config",
        str(HERE / config),
        str(HERE / f"{module}.tla"),
    ]
    started = time.time()
    process = subprocess.run(command, capture_output=True, text=True, cwd=HERE)
    elapsed = time.time() - started
    output = process.stdout + process.stderr
    if any(marker in output for marker in VIOLATION_MARKERS):
        match = re.search(
            r"(?:Invariant|Temporal property|Action property|Property) (\w+) "
            r"(?:is|was) violated",
            output,
        )
        return "violated", match.group(1) if match else "temporal", elapsed
    if CLEAN_MARKER in output:
        return "clean", "", elapsed
    return "broken", "\n".join(output.strip().splitlines()[-10:]), elapsed


def main(arguments: list[str]) -> int:
    java, jar = find_java(), find_jar()
    if not Path(jar).exists():
        print(f"FATAL: tla2tools.jar not found at {jar} (set TLA_TOOLS_JAR)")
        return 2
    requested = set(arguments)
    suite = [(c, m, "green", "") for c, m in GREEN] + [
        (c, m, "calibration", expected) for c, m, expected in CALIBRATION
    ]
    if requested:
        suite = [row for row in suite if Path(row[0]).stem in requested]
        missing = requested - {Path(row[0]).stem for row in suite}
        if missing:
            print(f"FATAL: unknown config(s): {', '.join(sorted(missing))}")
            return 2

    failures = 0
    for config, module, kind, expected_violation in suite:
        verdict, detail, elapsed = run_one(config, module, java, jar)
        expected = (
            verdict == "clean"
            if kind == "green"
            else verdict == "violated" and detail == expected_violation
        )
        status = "OK" if expected else "FAIL"
        print(
            f"{status:4} {kind:11} {Path(config).stem:24} "
            f"-> {(detail or verdict):24} {elapsed:6.1f}s"
        )
        if not expected:
            failures += 1
            if verdict == "broken":
                print(detail)

    if failures:
        print(f"SUITE FAILED: {failures} configuration(s) off expectation")
        return 1
    print("SUITE PASSED: pool clean; all negative cases rediscovered")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
