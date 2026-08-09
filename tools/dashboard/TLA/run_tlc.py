#!/usr/bin/env python3
"""TLC runner for the rollout-ingestion model (pattern: Anchore ENTERPRISE-8916).

Green configurations must check clean; calibration configurations must FAIL
with a *real* violation.  The violation detection greps TLC output for
explicit violation markers, so a parse error or crash in a calibration spec
is reported as BROKEN — never mistaken for the required "it failed".

Usage:
    python3 tools/dashboard/TLA/run_tlc.py            # full suite
    python3 tools/dashboard/TLA/run_tlc.py CalFailOpen GreenCore   # subset

Environment:
    TLA_TOOLS_JAR   path to tla2tools.jar   (default ~/tools/tla2tools.jar)
    TLA_JAVA        path to the java binary (default: $JAVA_HOME/bin/java,
                    else newest ~/tools/jdk-*/bin/java, else `java`)
    TLC_WORKERS     TLC worker count        (default auto)
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

# (config path relative to HERE, root module) — greens must PASS.
GREEN = [
    ("GreenCore.cfg", "Scen2F"),
    ("GreenLive.cfg", "Scen2F"),
    ("GreenRollover.cfg", "Scen3M"),
    ("GreenRolloverLive.cfg", "Scen3M"),
]

# Calibrations must FAIL (each restores a known-bad design).
CALIBRATION = [
    ("calibration/CalStartupStall.cfg", "Scen2F"),
    ("calibration/CalFailOpen.cfg", "Scen2F"),
    ("calibration/CalOrdinalProvenance.cfg", "Scen2F"),
    ("calibration/CalNoDrainGate.cfg", "Scen2F"),
    ("calibration/CalSkipOnContention.cfg", "Scen2F"),
    ("calibration/CalEventDrivenDeadline.cfg", "Scen2F"),
    ("calibration/CalOffsetAckFirst.cfg", "Scen2F"),
    ("calibration/CalProcessFirstCrash.cfg", "Scen2F"),
    ("calibration/CalSyncEntryNoop.cfg", "Scen2F"),
    ("calibration/CalTerminalQuarantine.cfg", "Scen2F"),
    ("calibration/CalNaiveReobserve.cfg", "Scen2F"),
    ("calibration/CalCancelPinned.cfg", "Scen2F"),
    ("calibration/CalFinallyRelease.cfg", "Scen2F"),
    ("calibration/CalRMWHarness.cfg", "Scen2F"),
    ("calibration/CalNoEpochBarrier.cfg", "Scen2F"),
    ("calibration/CalPerPathGates.cfg", "Scen3M"),
    ("calibration/CalRolloverCAS.cfg", "Scen3M"),
    # Guard-branch reachability probe (must fail = branch is live):
    ("calibration/ProbeBirthCASFail.cfg", "Scen3M"),
]

# Real violation markers.  Anything else (parse error, crash, OOM) is
# BROKEN for greens and calibrations alike.
VIOLATION_MARKERS = (
    "is violated",
    "was violated",
    "Temporal properties were violated",
    "constitutes a counter-example",
)
CLEAN_MARKER = "Model checking completed. No error has been found"


def find_java() -> str:
    env = os.environ.get("TLA_JAVA")
    if env:
        return env
    jh = os.environ.get("JAVA_HOME")
    if jh and Path(jh, "bin", "java").exists():
        return str(Path(jh, "bin", "java"))
    local = sorted(glob.glob(str(Path.home() / "tools" / "jdk-*" / "bin" / "java")))
    if local:
        return local[-1]
    return "java"


def find_jar() -> str:
    return os.environ.get(
        "TLA_TOOLS_JAR", str(Path.home() / "tools" / "tla2tools.jar")
    )


def run_one(cfg: str, module: str, java: str, jar: str) -> tuple[str, str, float]:
    """Returns (verdict, detail, seconds): verdict in clean|violated|broken."""
    name = Path(cfg).stem
    is_live = "SPECIFICATION FairSpec" in (HERE / cfg).read_text()
    cmd = [
        java, "-XX:+UseParallelGC", "-Xmx6g", "-cp", jar, "tlc2.TLC",
        "-workers", os.environ.get("TLC_WORKERS", "auto"),
        "-deadlock",
        *( ["-lncheck", "final"] if is_live else [] ),
        "-metadir", f"/tmp/tlc/{name}",
        "-config", str(HERE / cfg),
        str(HERE / f"{module}.tla"),
    ]
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=HERE)
    dt = time.time() - t0
    out = proc.stdout + proc.stderr
    violated = any(m in out for m in VIOLATION_MARKERS)
    clean = CLEAN_MARKER in out
    if violated:
        prop = ""
        m = re.search(r"(?:Invariant|Temporal property|Property) (\w+) is violated", out)
        if m:
            prop = m.group(1)
        else:
            m = re.search(r"Error: (\w+)\s+is violated", out)
            prop = m.group(1) if m else "temporal"
        return "violated", prop, dt
    if clean:
        return "clean", "", dt
    tail = "\n".join(out.strip().splitlines()[-8:])
    return "broken", tail, dt


def main(argv: list[str]) -> int:
    java, jar = find_java(), find_jar()
    if not Path(jar).exists():
        print(f"FATAL: tla2tools.jar not found at {jar} (set TLA_TOOLS_JAR)")
        return 2
    only = set(argv)
    suite = [(c, m, "green") for c, m in GREEN] + [
        (c, m, "calibration") for c, m in CALIBRATION
    ]
    if only:
        suite = [row for row in suite if Path(row[0]).stem in only]
        missing = only - {Path(row[0]).stem for row in suite}
        if missing:
            print(f"FATAL: unknown config(s): {', '.join(sorted(missing))}")
            return 2
    failures = 0
    for cfg, module, kind in suite:
        name = Path(cfg).stem
        verdict, detail, dt = run_one(cfg, module, java, jar)
        if kind == "green":
            ok = verdict == "clean"
            expect = "must be clean"
        else:
            ok = verdict == "violated"
            expect = "must fail"
        status = "OK  " if ok else "FAIL"
        info = detail if verdict == "violated" else verdict
        print(f"{status} {kind:<11} {name:<24} {expect:<13} -> {info:<28} {dt:6.1f}s")
        if not ok:
            failures += 1
            if verdict == "broken":
                print("    --- TLC output tail ---")
                for line in detail.splitlines():
                    print("    " + line)
    print()
    if failures:
        print(f"SUITE FAILED: {failures} configuration(s) off-expectation")
        return 1
    print("SUITE PASSED: all greens clean, all calibrations violated as required")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
