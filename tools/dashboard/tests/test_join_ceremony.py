"""Node acceptance wrapper for the org:join acceptance ceremony seam.

Proves the browser-local orchestration and its I1 guarantees against injected
fakes: the passphrase opens the armor and is never returned or sent, the root
and kem seeds are zeroed on every path (including a thrown mint), and only the
signed public claim plus the invitee's own kem key come back.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
NODE_TEST = (
    REPO_ROOT / "tools" / "dashboard" / "static" / "js"
    / "join" / "tests" / "ceremony.test.mjs"
)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_join_ceremony_seam():
    subprocess.run(
        ["node", str(NODE_TEST)],
        cwd=REPO_ROOT,
        check=True,
    )
