"""Node acceptance wrapper for the org:join controller.

Drives the full join ladder (connect -> read org context -> accept -> submit ->
poll to a terminal) against a scripted stub SecureChannel and a stub ceremony
seam, asserting the ledger-truth / link-truth split at every step, the held-seam
refusal when no ceremony is supplied, and the remote-icon defence.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
NODE_TEST = (
    REPO_ROOT / "tools" / "dashboard" / "static" / "js"
    / "join" / "tests" / "accept-controller.test.mjs"
)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_join_accept_controller():
    subprocess.run(
        ["node", str(NODE_TEST)],
        cwd=REPO_ROOT,
        check=True,
    )
