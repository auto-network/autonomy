"""Node real-crypto regression guard for the personal-root-seed leak.

Runs the REAL derivePersona with a sentinel root seed and asserts no copy of
the sentinel survives the derivation (join security review, finding 1 / HIGH).
Unlike the injected-fake seam tests, this exercises the primitive under the
seam, where the leak actually lived.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
NODE_TEST = (
    REPO_ROOT / "tools" / "dashboard" / "static" / "js"
    / "join" / "tests" / "seed-zeroization.test.mjs"
)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_join_seed_zeroization():
    subprocess.run(["node", str(NODE_TEST)], cwd=REPO_ROOT, check=True)
