"""Node acceptance wrapper for the recovery-code ceremony derivation (row 16).

Proves the dual-function derivation code -> {Ed25519 recovery keypair, KEK-
recovery seed} is deterministic, domain-separated, fail-closed, and that the
recovery signing key produces what auto-n9cy3's recovery co-signature verifies.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
NODE_TEST = (
    REPO_ROOT / "tools" / "dashboard" / "static" / "js"
    / "ceremony" / "tests" / "recovery.test.mjs"
)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_ceremony_recovery_derivation():
    subprocess.run(["node", str(NODE_TEST)], cwd=REPO_ROOT, check=True)
