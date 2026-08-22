#!/usr/bin/env python3
"""TLC runner for the factor / auth state-machine model (FactorAuth.tla).

Green configurations must check clean.  Calibration configurations must FAIL
with a *real* violation: four restore a known-bad factor design, three are
reachability probes whose "never reach it" invariant must be violated to
prove the target state is reachable.  Violation detection greps TLC output
for explicit markers, so a parse error or crash is reported as BROKEN — never
mistaken for the required "it failed".

Usage:
    python3 tools/network/idkit/TLA/run_tlc.py            # full suite
    python3 tools/network/idkit/TLA/run_tlc.py GreenCore  # subset

Environment:
    TLA_TOOLS_JAR   path to tla2tools.jar   (default ~/tools/tla2tools.jar)
    TLA_JAVA        path to the java binary  (default: $JAVA_HOME/bin/java,
                    else newest ~/tools/jdk-*/bin/java, else `java`)
    TLC_WORKERS     TLC worker count         (default auto)
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
MODULE = "FactorAuth"

# Green configurations — must PASS (clean).
GREEN = [
    "GreenCore.cfg",
]

# Calibrations / probes — must FAIL with a real violation.
#   Cal*  restore a broken design; the named invariant must be violated.
#   Probe* assert a target state is reachable (a "never reach it" invariant
#          that must be violated to prove reachability).
CALIBRATION = [
    "calibration/CalKeepIndividuals.cfg",   # -> MFAExclusive
    "calibration/CalNoRootGuard.cfg",       # -> RootReachable
    "calibration/CalTrustPayload.cfg",      # -> ProvenanceOK
    "calibration/CalSeedPersisted.cfg",     # -> SeedNeverPersisted
    "calibration/ProbePasskeyOnly.cfg",     # -> NoPasskeyOnly  (reachable)
    "calibration/ProbePasswordOnly.cfg",    # -> NoPasswordOnly (reachable)
    "calibration/ProbeFoundMFADirect.cfg",  # -> NoMFA (nothing->combined direct)
]

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


def run_one(cfg: str, java: str, jar: str) -> tuple[str, str, float]:
    """Returns (verdict, detail, seconds): verdict in clean|violated|broken."""
    name = Path(cfg).stem
    cmd = [
        java, "-XX:+UseParallelGC", "-Xmx4g", "-cp", jar, "tlc2.TLC",
        "-workers", os.environ.get("TLC_WORKERS", "auto"),
        "-metadir", f"/tmp/tlc-factor/{name}",
        "-config", str(HERE / cfg),
        str(HERE / f"{MODULE}.tla"),
    ]
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=HERE)
    dt = time.time() - t0
    out = proc.stdout + proc.stderr
    violated = any(m in out for m in VIOLATION_MARKERS)
    clean = CLEAN_MARKER in out
    if violated:
        m = re.search(r"(?:Invariant|Temporal property|Property) (\w+) is violated", out)
        prop = m.group(1) if m else "invariant"
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
    suite = [(c, "green") for c in GREEN] + [(c, "calibration") for c in CALIBRATION]
    if only:
        suite = [row for row in suite if Path(row[0]).stem in only]
        missing = only - {Path(row[0]).stem for row in suite}
        if missing:
            print(f"FATAL: unknown config(s): {', '.join(sorted(missing))}")
            return 2
    failures = 0
    for cfg, kind in suite:
        name = Path(cfg).stem
        verdict, detail, dt = run_one(cfg, java, jar)
        if kind == "green":
            ok = verdict == "clean"
            expect = "must be clean"
        else:
            ok = verdict == "violated"
            expect = "must fail"
        status = "OK  " if ok else "FAIL"
        info = detail if verdict == "violated" else verdict
        print(f"{status} {kind:<11} {name:<24} {expect:<13} -> {info:<20} {dt:6.1f}s")
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
    print("SUITE PASSED: green clean, all calibrations/probes violated as required")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
