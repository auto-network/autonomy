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
]

CALIBRATION = [
    ("CurrentLivelock.cfg", "ScenRelay", "EventuallyAllTunnelsRegistered"),
    ("calibration/RandomAdmission.cfg", "ScenRelay", "AdmissionUsesLeastLoad"),
    ("calibration/TimestampPrune.cfg", "AckFloor", "DeltaAlwaysServable"),
    ("calibration/SoloPrune.cfg", "AckFloor", "DeltaAlwaysServable"),
    ("calibration/NoAckResetOnInstall.cfg", "AckFloor", "AckSoundness"),
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
            r"(?:Invariant|Temporal property|Property) (\w+) "
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
